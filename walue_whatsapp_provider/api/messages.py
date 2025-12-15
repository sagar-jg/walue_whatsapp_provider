"""
Message Management API - Proxy to Meta WhatsApp Business API

This module proxies ALL message requests through the provider for billing.

Flow:
1. Customer authenticates with OAuth credentials
2. Customer passes their Meta token in X-Meta-Token header
3. Provider checks quota/balance BEFORE calling Meta
4. Provider proxies request to Meta API
5. Provider records usage and deducts balance SYNCHRONOUSLY
6. Provider returns response to customer

CRITICAL: We do NOT store message content - only count and cost metrics.
"""

import frappe
from frappe import _
import requests
from datetime import datetime, date

from walue_whatsapp_provider.constants import (
    META_API_BASE_URL,
    META_API_DEFAULT_VERSION,
    ERR_META_API,
)
from walue_whatsapp_provider.utils.auth import (
    authenticate_request,
    get_meta_token,
    enforce_quota,
    deduct_balance,
)
from walue_whatsapp_provider.api.rate_limit import enforce_rate_limit


# =============================================================================
# PRICING CONFIGURATION (move to settings/rate cards later)
# =============================================================================

# Base costs (Meta's approximate pricing - varies by country)
TEMPLATE_MESSAGE_COST = 0.005  # $0.005 per template message
TEXT_MESSAGE_COST = 0.0       # Free within 24h window
MEDIA_MESSAGE_COST = 0.0      # Free within 24h window

# Markup (provider's margin)
DEFAULT_MARKUP_PERCENTAGE = 0.35  # 35% markup


def _get_customer_markup(customer_id: str) -> float:
    """Get customer's markup percentage from subscription plan"""
    plan_name = frappe.db.get_value("WhatsApp Customer", customer_id, "subscription_plan")
    if plan_name:
        markup = frappe.db.get_value("Subscription Plan", plan_name, "message_markup_percentage")
        if markup:
            return markup / 100
    return DEFAULT_MARKUP_PERCENTAGE


def _calculate_total_cost(base_cost: float, customer_id: str) -> dict:
    """Calculate total cost including markup"""
    markup_rate = _get_customer_markup(customer_id)
    markup = base_cost * markup_rate
    total = base_cost + markup

    return {
        "base_cost": round(base_cost, 6),
        "markup": round(markup, 6),
        "total_cost": round(total, 6),
        "markup_percentage": markup_rate * 100,
    }


# =============================================================================
# MESSAGE PROXY ENDPOINTS
# =============================================================================


@frappe.whitelist(allow_guest=True, methods=["POST"])
def send_template():
    """
    Proxy: Send template message via Meta WhatsApp Business API

    Headers Required:
        X-Client-ID: Customer's OAuth client ID
        X-Client-Secret: Customer's OAuth client secret
        X-Meta-Token: Customer's Meta access token

    POST Body (JSON):
        phone_number_id: Customer's WhatsApp phone number ID
        to: Recipient phone number (E.164 format)
        template_name: Name of the approved template
        template_language: Language code (e.g., 'en_US')
        template_components: Optional template variable values

    Returns:
        dict: Contains message_id, cost info, and new balance
    """
    # 1. Authenticate customer
    customer_info = authenticate_request()
    customer_id = customer_info["customer_id"]

    # 2. Enforce rate limit
    enforce_rate_limit(customer_id, "send_template")

    # 3. Get Meta token from header
    access_token = get_meta_token()

    # 3. Parse request body
    data = frappe.parse_json(frappe.request.data)
    phone_number_id = data.get("phone_number_id")
    to_number = data.get("to")
    template_name = data.get("template_name")
    template_language = data.get("template_language", "en_US")
    template_components = data.get("template_components", [])

    if not all([phone_number_id, to_number, template_name]):
        frappe.throw(_("Missing required parameters: phone_number_id, to, template_name"))

    # 4. Calculate cost and check quota BEFORE calling Meta
    cost_info = _calculate_total_cost(TEMPLATE_MESSAGE_COST, customer_id)
    enforce_quota(customer_id, cost_info["total_cost"])

    # 5. Build and send Meta API request
    url = f"{META_API_BASE_URL}/{META_API_DEFAULT_VERSION}/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_number,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": template_language}
        }
    }
    if template_components:
        payload["template"]["components"] = template_components

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=30)
        response_data = response.json()

        if response.status_code != 200:
            error_msg = response_data.get("error", {}).get("message", ERR_META_API)
            frappe.logger().error(f"[WhatsApp] Meta API error for {customer_id}: {error_msg}")
            return {"success": False, "error": error_msg}

        message_id = response_data.get("messages", [{}])[0].get("id")

        # 6. SUCCESS - Deduct balance and record usage SYNCHRONOUSLY
        new_balance = deduct_balance(
            customer_id,
            cost_info["total_cost"],
            f"Template message: {template_name}"
        )

        _record_message_metric(customer_id, cost_info)

        frappe.db.commit()  # Ensure billing is committed

        return {
            "success": True,
            "message_id": message_id,
            "cost": cost_info,
            "new_balance": new_balance,
        }

    except requests.RequestException as e:
        frappe.log_error(f"Meta API request failed: {str(e)}")
        return {"success": False, "error": ERR_META_API}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def send_text():
    """
    Proxy: Send free-form text message via Meta WhatsApp Business API

    Only works within 24-hour conversation window (free).

    Headers Required:
        X-Client-ID, X-Client-Secret, X-Meta-Token

    POST Body (JSON):
        phone_number_id: Customer's WhatsApp phone number ID
        to: Recipient phone number (E.164 format)
        text: Message text content

    Returns:
        dict: Contains message_id and balance info
    """
    # 1. Authenticate
    customer_info = authenticate_request()
    customer_id = customer_info["customer_id"]

    # 2. Enforce rate limit
    enforce_rate_limit(customer_id, "send_text")

    # 3. Get Meta token
    access_token = get_meta_token()

    # 4. Parse request
    data = frappe.parse_json(frappe.request.data)
    phone_number_id = data.get("phone_number_id")
    to_number = data.get("to")
    text = data.get("text")

    if not all([phone_number_id, to_number, text]):
        frappe.throw(_("Missing required parameters: phone_number_id, to, text"))

    # 4. Text messages within window are free, but still check quota
    cost_info = _calculate_total_cost(TEXT_MESSAGE_COST, customer_id)
    # Don't enforce quota for free messages, but still track

    # 5. Send to Meta
    url = f"{META_API_BASE_URL}/{META_API_DEFAULT_VERSION}/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_number,
        "type": "text",
        "text": {"preview_url": False, "body": text}
    }

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=30)
        response_data = response.json()

        if response.status_code != 200:
            error_msg = response_data.get("error", {}).get("message", ERR_META_API)
            return {"success": False, "error": error_msg}

        message_id = response_data.get("messages", [{}])[0].get("id")

        # Record usage (free, so no balance deduction)
        _record_message_metric(customer_id, cost_info)
        frappe.db.commit()

        balance = frappe.db.get_value("WhatsApp Customer", customer_id, "current_balance")

        return {
            "success": True,
            "message_id": message_id,
            "cost": cost_info,
            "balance": balance,
        }

    except requests.RequestException as e:
        frappe.log_error(f"Meta API request failed: {str(e)}")
        return {"success": False, "error": ERR_META_API}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def send_media():
    """
    Proxy: Send media message (image, video, document) via Meta API

    Headers Required:
        X-Client-ID, X-Client-Secret, X-Meta-Token

    POST Body (JSON):
        phone_number_id: Customer's WhatsApp phone number ID
        to: Recipient phone number
        media_type: 'image', 'video', 'document', or 'audio'
        media_url: URL of the media file
        caption: Optional caption for images/videos
        filename: Required for documents

    Returns:
        dict: Contains message_id and balance info
    """
    customer_info = authenticate_request()
    customer_id = customer_info["customer_id"]

    # Enforce rate limit (media has lower limit)
    enforce_rate_limit(customer_id, "send_media")

    access_token = get_meta_token()

    data = frappe.parse_json(frappe.request.data)
    phone_number_id = data.get("phone_number_id")
    to_number = data.get("to")
    media_type = data.get("media_type")
    media_url = data.get("media_url")
    caption = data.get("caption")
    filename = data.get("filename")

    if not all([phone_number_id, to_number, media_type, media_url]):
        frappe.throw(_("Missing required parameters"))

    if media_type not in ["image", "video", "document", "audio"]:
        frappe.throw(_("Invalid media_type"))

    # Media within conversation window is free
    cost_info = _calculate_total_cost(MEDIA_MESSAGE_COST, customer_id)

    url = f"{META_API_BASE_URL}/{META_API_DEFAULT_VERSION}/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    media_object = {"link": media_url}
    if caption and media_type in ["image", "video"]:
        media_object["caption"] = caption
    if filename and media_type == "document":
        media_object["filename"] = filename

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_number,
        "type": media_type,
        media_type: media_object
    }

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=30)
        response_data = response.json()

        if response.status_code != 200:
            error_msg = response_data.get("error", {}).get("message", ERR_META_API)
            return {"success": False, "error": error_msg}

        message_id = response_data.get("messages", [{}])[0].get("id")

        _record_message_metric(customer_id, cost_info)
        frappe.db.commit()

        balance = frappe.db.get_value("WhatsApp Customer", customer_id, "current_balance")

        return {
            "success": True,
            "message_id": message_id,
            "cost": cost_info,
            "balance": balance,
        }

    except requests.RequestException as e:
        frappe.log_error(f"Meta API request failed: {str(e)}")
        return {"success": False, "error": ERR_META_API}


# =============================================================================
# INTERNAL FUNCTIONS
# =============================================================================


def _record_message_metric(customer_id: str, cost_info: dict):
    """
    Record message usage metric SYNCHRONOUSLY

    IMPORTANT: We only record counts and costs, NOT content
    """
    today = date.today()

    existing = frappe.db.get_value(
        "Daily Usage Metrics",
        {"customer": customer_id, "date": today},
        "name"
    )

    if existing:
        frappe.db.sql("""
            UPDATE `tabDaily Usage Metrics`
            SET
                total_messages = total_messages + 1,
                total_message_cost = total_message_cost + %s,
                total_markup = total_markup + %s,
                total_revenue = total_revenue + %s
            WHERE name = %s
        """, (
            cost_info["base_cost"],
            cost_info["markup"],
            cost_info["total_cost"],
            existing
        ))
    else:
        frappe.get_doc({
            "doctype": "Daily Usage Metrics",
            "customer": customer_id,
            "date": today,
            "total_calls": 0,
            "total_call_minutes": 0,
            "total_messages": 1,
            "total_call_cost": 0,
            "total_message_cost": cost_info["base_cost"],
            "total_markup": cost_info["markup"],
            "total_revenue": cost_info["total_cost"],
        }).insert(ignore_permissions=True)
