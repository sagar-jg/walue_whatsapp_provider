"""
Message Management API - Proxy to Meta WhatsApp Business API

This module proxies message requests to the Meta API.
CRITICAL: We do NOT store message content - only count and cost metrics.

The customer app:
1. Sends message request with their WABA credentials
2. We proxy to Meta API
3. Return message_id to customer
4. Record only: customer_id, date, count, cost (NO content)
"""

import frappe
from frappe import _
import requests
from datetime import datetime, date

from walue_whatsapp_provider.constants import (
    META_API_BASE_URL,
    META_API_DEFAULT_VERSION,
    ERR_META_API,
    ERR_INVALID_TOKEN,
    ERR_CUSTOMER_NOT_FOUND,
    CUSTOMER_STATUS_ACTIVE,
)
from walue_whatsapp_provider.api.oauth import validate_token


def _authenticate_request() -> dict:
    """
    Authenticate the request using Bearer token
    Returns customer info if valid, throws error if not
    """
    auth_header = frappe.request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        frappe.throw(_(ERR_INVALID_TOKEN), frappe.AuthenticationError)

    token = auth_header.split(" ")[1]
    customer_info = validate_token(token)

    if not customer_info:
        frappe.throw(_(ERR_INVALID_TOKEN), frappe.AuthenticationError)

    # Verify customer is active
    customer = frappe.get_doc("WhatsApp Customer", customer_info["customer_id"])
    if customer.status != CUSTOMER_STATUS_ACTIVE:
        frappe.throw(_("Customer account is not active"), frappe.AuthenticationError)

    return customer_info


@frappe.whitelist(allow_guest=True, methods=["POST"])
def send_template():
    """
    Send a template message via Meta WhatsApp Business API

    POST Body (JSON):
        phone_number_id: Customer's WhatsApp phone number ID
        access_token: Customer's Meta access token
        to: Recipient phone number (E.164 format)
        template_name: Name of the approved template
        template_language: Language code (e.g., 'en_US')
        template_components: Optional template variable values

    Returns:
        dict: Contains message_id and cost info

    Note: Message content is NOT stored - only metrics
    """
    customer_info = _authenticate_request()

    # Parse request body
    data = frappe.parse_json(frappe.request.data)

    phone_number_id = data.get("phone_number_id")
    access_token = data.get("access_token")
    to_number = data.get("to")
    template_name = data.get("template_name")
    template_language = data.get("template_language", "en_US")
    template_components = data.get("template_components", [])

    if not all([phone_number_id, access_token, to_number, template_name]):
        frappe.throw(_("Missing required parameters"))

    # Build Meta API request
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
            "language": {
                "code": template_language
            }
        }
    }

    if template_components:
        payload["template"]["components"] = template_components

    try:
        response = requests.post(url, json=payload, headers=headers)
        response_data = response.json()

        if response.status_code != 200:
            error_msg = response_data.get("error", {}).get("message", ERR_META_API)
            return {
                "success": False,
                "error": error_msg,
            }

        message_id = response_data.get("messages", [{}])[0].get("id")

        # Record usage metrics (NO content stored)
        _record_message_metric(
            customer_id=customer_info["customer_id"],
            message_type="template",
        )

        # Calculate cost (simplified - actual implementation needs rate cards)
        cost = _calculate_message_cost(template_name)

        return {
            "success": True,
            "message_id": message_id,
            "cost": cost,
        }

    except requests.RequestException as e:
        frappe.log_error(f"Meta API request failed: {str(e)}")
        return {
            "success": False,
            "error": ERR_META_API,
        }


@frappe.whitelist(allow_guest=True, methods=["POST"])
def send_text():
    """
    Send a free-form text message via Meta WhatsApp Business API

    Only works within 24-hour conversation window.

    POST Body (JSON):
        phone_number_id: Customer's WhatsApp phone number ID
        access_token: Customer's Meta access token
        to: Recipient phone number (E.164 format)
        text: Message text content

    Returns:
        dict: Contains message_id and cost info

    Note: Message content is NOT stored - only metrics
    """
    customer_info = _authenticate_request()

    # Parse request body
    data = frappe.parse_json(frappe.request.data)

    phone_number_id = data.get("phone_number_id")
    access_token = data.get("access_token")
    to_number = data.get("to")
    text = data.get("text")

    if not all([phone_number_id, access_token, to_number, text]):
        frappe.throw(_("Missing required parameters"))

    # Build Meta API request
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
        "text": {
            "preview_url": False,
            "body": text  # We send but do NOT store this
        }
    }

    try:
        response = requests.post(url, json=payload, headers=headers)
        response_data = response.json()

        if response.status_code != 200:
            error_msg = response_data.get("error", {}).get("message", ERR_META_API)
            return {
                "success": False,
                "error": error_msg,
            }

        message_id = response_data.get("messages", [{}])[0].get("id")

        # Record usage metrics (NO content stored)
        _record_message_metric(
            customer_id=customer_info["customer_id"],
            message_type="text",
        )

        # Free-form messages are typically free within conversation window
        cost = 0.0

        return {
            "success": True,
            "message_id": message_id,
            "cost": cost,
        }

    except requests.RequestException as e:
        frappe.log_error(f"Meta API request failed: {str(e)}")
        return {
            "success": False,
            "error": ERR_META_API,
        }


@frappe.whitelist(allow_guest=True, methods=["POST"])
def send_media():
    """
    Send a media message (image, video, document) via Meta API

    POST Body (JSON):
        phone_number_id: Customer's WhatsApp phone number ID
        access_token: Customer's Meta access token
        to: Recipient phone number
        media_type: 'image', 'video', 'document', or 'audio'
        media_url: URL of the media file
        caption: Optional caption for images/videos
        filename: Required for documents

    Returns:
        dict: Contains message_id and cost info
    """
    customer_info = _authenticate_request()

    data = frappe.parse_json(frappe.request.data)

    phone_number_id = data.get("phone_number_id")
    access_token = data.get("access_token")
    to_number = data.get("to")
    media_type = data.get("media_type")
    media_url = data.get("media_url")
    caption = data.get("caption")
    filename = data.get("filename")

    if not all([phone_number_id, access_token, to_number, media_type, media_url]):
        frappe.throw(_("Missing required parameters"))

    if media_type not in ["image", "video", "document", "audio"]:
        frappe.throw(_("Invalid media_type"))

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
        response = requests.post(url, json=payload, headers=headers)
        response_data = response.json()

        if response.status_code != 200:
            error_msg = response_data.get("error", {}).get("message", ERR_META_API)
            return {"success": False, "error": error_msg}

        message_id = response_data.get("messages", [{}])[0].get("id")

        _record_message_metric(
            customer_id=customer_info["customer_id"],
            message_type="media",
        )

        return {
            "success": True,
            "message_id": message_id,
            "cost": 0.0,  # Media within conversation window is free
        }

    except requests.RequestException as e:
        frappe.log_error(f"Meta API request failed: {str(e)}")
        return {"success": False, "error": ERR_META_API}


def _record_message_metric(customer_id: str, message_type: str):
    """
    Record message usage metric

    IMPORTANT: We only record counts, NOT content
    """
    today = date.today()

    # Get or create daily metrics record
    existing = frappe.db.get_value(
        "Daily Usage Metrics",
        {"customer": customer_id, "date": today},
        "name"
    )

    if existing:
        # Increment message count
        frappe.db.sql("""
            UPDATE `tabDaily Usage Metrics`
            SET total_messages = total_messages + 1
            WHERE name = %s
        """, (existing,))
    else:
        # Create new daily record
        frappe.get_doc({
            "doctype": "Daily Usage Metrics",
            "customer": customer_id,
            "date": today,
            "total_calls": 0,
            "total_call_minutes": 0,
            "total_messages": 1,
            "total_call_cost": 0,
            "total_message_cost": 0,
            "total_markup": 0,
            "total_revenue": 0,
        }).insert(ignore_permissions=True)

    frappe.db.commit()


def _calculate_message_cost(template_name: str) -> float:
    """
    Calculate message cost based on template category

    Actual implementation needs:
    - Meta rate cards by country
    - Template category (marketing, utility, etc.)
    - Customer's subscription markup
    """
    # Simplified cost calculation
    # TODO: Implement actual rate card lookup
    base_cost = 0.005  # $0.005 USD per message (example)

    return base_cost
