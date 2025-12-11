"""
Call Management API - Proxy for WhatsApp Business Calling

This module handles:
1. Call permission requests (proxy to Meta API)
2. Call initiation via Janus WebRTC gateway
3. Call termination and duration tracking

CRITICAL: We do NOT store call details - only aggregated metrics
- NO lead_id or user info stored
- NO phone numbers stored
- Only: customer_id, date, duration, cost
"""

import frappe
from frappe import _
import requests
from datetime import datetime, date
import secrets

from walue_whatsapp_provider.constants import (
    META_API_BASE_URL,
    META_API_DEFAULT_VERSION,
    CALLING_RESTRICTED_COUNTRIES,
    ERR_META_API,
    ERR_INVALID_TOKEN,
    ERR_JANUS_CONNECTION,
    MSG_CALLING_NOT_AVAILABLE,
    CUSTOMER_STATUS_ACTIVE,
)
from walue_whatsapp_provider.api.oauth import validate_token


def _authenticate_request() -> dict:
    """Authenticate the request using Bearer token"""
    auth_header = frappe.request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        frappe.throw(_(ERR_INVALID_TOKEN), frappe.AuthenticationError)

    token = auth_header.split(" ")[1]
    customer_info = validate_token(token)

    if not customer_info:
        frappe.throw(_(ERR_INVALID_TOKEN), frappe.AuthenticationError)

    customer = frappe.get_doc("WhatsApp Customer", customer_info["customer_id"])
    if customer.status != CUSTOMER_STATUS_ACTIVE:
        frappe.throw(_("Customer account is not active"), frappe.AuthenticationError)

    return customer_info


@frappe.whitelist(allow_guest=True, methods=["POST"])
def request_permission():
    """
    Send call permission request via Meta WhatsApp Business API

    The permission request is sent as either:
    - Interactive message (within 24hr window)
    - Template message (outside window)

    POST Body (JSON):
        phone_number_id: Customer's WhatsApp phone number ID
        access_token: Customer's Meta access token
        to: Recipient phone number (E.164 format)
        use_template: Boolean - use template or interactive message

    Returns:
        dict: Contains success status and message_id

    Note: Permission status is tracked in CUSTOMER's app, not here
    """
    customer_info = _authenticate_request()

    data = frappe.parse_json(frappe.request.data)

    phone_number_id = data.get("phone_number_id")
    access_token = data.get("access_token")
    to_number = data.get("to")
    use_template = data.get("use_template", False)

    if not all([phone_number_id, access_token, to_number]):
        frappe.throw(_("Missing required parameters"))

    # Check if calling is available for this region
    country_code = _extract_country_code(to_number)
    if country_code in CALLING_RESTRICTED_COUNTRIES:
        return {
            "success": False,
            "error": MSG_CALLING_NOT_AVAILABLE,
            "restricted": True,
        }

    url = f"{META_API_BASE_URL}/{META_API_DEFAULT_VERSION}/{phone_number_id}/messages"

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    if use_template:
        # Use template message for permission request
        # Template: VOICE_CALL_REQUEST (pre-approved by Meta)
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to_number,
            "type": "template",
            "template": {
                "name": "voice_call_request",
                "language": {"code": "en"},
                "components": [
                    {
                        "type": "button",
                        "sub_type": "voice_call",
                        "index": 0,
                        "parameters": []
                    }
                ]
            }
        }
    else:
        # Use interactive call permission request (within 24hr window)
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to_number,
            "type": "interactive",
            "interactive": {
                "type": "call_permission_request",
                "body": {
                    "text": "We'd like to call you. Please approve to receive our call."
                },
                "action": {
                    "name": "voice_call",
                    "parameters": {}
                }
            }
        }

    try:
        response = requests.post(url, json=payload, headers=headers)
        response_data = response.json()

        if response.status_code != 200:
            error_msg = response_data.get("error", {}).get("message", ERR_META_API)
            return {"success": False, "error": error_msg}

        message_id = response_data.get("messages", [{}])[0].get("id")

        return {
            "success": True,
            "message_id": message_id,
            "message": "Permission request sent successfully",
        }

    except requests.RequestException as e:
        frappe.log_error(f"Meta API call permission request failed: {str(e)}")
        return {"success": False, "error": ERR_META_API}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def initiate():
    """
    Initiate a WhatsApp call via Janus WebRTC gateway

    This creates a Janus session and returns WebRTC connection details.
    The actual call routing depends on Janus SIP plugin configuration.

    POST Body (JSON):
        phone_number_id: Customer's WhatsApp phone number ID
        access_token: Customer's Meta access token
        to: Recipient phone number (E.164 format)
        from_number: Caller's WhatsApp number

    Returns:
        dict: Contains Janus session details for WebRTC connection
            - call_session_id: Unique session identifier
            - janus_session_id: Janus session ID
            - janus_handle_id: Janus plugin handle ID
            - ice_servers: STUN/TURN server configuration

    Note: We do NOT receive/store lead_id or user info
    """
    customer_info = _authenticate_request()

    data = frappe.parse_json(frappe.request.data)

    phone_number_id = data.get("phone_number_id")
    access_token = data.get("access_token")
    to_number = data.get("to")
    from_number = data.get("from_number")

    if not all([phone_number_id, access_token, to_number, from_number]):
        frappe.throw(_("Missing required parameters"))

    # Check if calling is available for this region
    country_code = _extract_country_code(to_number)
    if country_code in CALLING_RESTRICTED_COUNTRIES:
        return {
            "success": False,
            "error": MSG_CALLING_NOT_AVAILABLE,
            "restricted": True,
        }

    # Generate unique call session ID
    call_session_id = secrets.token_urlsafe(24)

    try:
        # Create Janus session
        janus_session = _create_janus_session(customer_info["customer_id"])

        if not janus_session:
            return {"success": False, "error": ERR_JANUS_CONNECTION}

        # Store session metadata in cache (temporary, not in DB)
        frappe.cache().set_value(
            f"call_session:{call_session_id}",
            {
                "customer_id": customer_info["customer_id"],
                "janus_session_id": janus_session["session_id"],
                "janus_handle_id": janus_session["handle_id"],
                "started_at": datetime.now().isoformat(),
                "status": "initiating",
            },
            expires_in_sec=3600  # 1 hour max
        )

        # Get STUN/TURN servers
        settings = frappe.get_single("WhatsApp Provider Settings")
        ice_servers = _get_ice_servers(settings)

        return {
            "success": True,
            "call_session_id": call_session_id,
            "janus_session_id": janus_session["session_id"],
            "janus_handle_id": janus_session["handle_id"],
            "janus_ws_url": settings.janus_ws_url,
            "ice_servers": ice_servers,
        }

    except Exception as e:
        frappe.log_error(f"Call initiation failed: {str(e)}")
        return {"success": False, "error": ERR_JANUS_CONNECTION}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def end():
    """
    End a call and record usage metrics

    POST Body (JSON):
        call_session_id: The session ID from initiate()
        duration_seconds: Actual call duration

    Returns:
        dict: Contains cost information for customer to store

    Note: We only store customer_id, date, duration, cost
    NO call details, phone numbers, or lead info
    """
    customer_info = _authenticate_request()

    data = frappe.parse_json(frappe.request.data)

    call_session_id = data.get("call_session_id")
    duration_seconds = data.get("duration_seconds", 0)

    if not call_session_id:
        frappe.throw(_("Missing call_session_id"))

    # Get session from cache
    session_data = frappe.cache().get_value(f"call_session:{call_session_id}")

    if not session_data:
        return {"success": False, "error": "Session not found or expired"}

    if session_data["customer_id"] != customer_info["customer_id"]:
        return {"success": False, "error": "Session does not belong to this customer"}

    # Clean up Janus session
    _cleanup_janus_session(
        session_data["janus_session_id"],
        session_data["janus_handle_id"]
    )

    # Calculate cost
    customer = frappe.get_doc("WhatsApp Customer", customer_info["customer_id"])
    cost = _calculate_call_cost(duration_seconds, customer)

    # Record usage metrics (ONLY aggregated data)
    _record_call_metric(
        customer_id=customer_info["customer_id"],
        duration_seconds=duration_seconds,
        cost=cost,
    )

    # Remove session from cache
    frappe.cache().delete_value(f"call_session:{call_session_id}")

    return {
        "success": True,
        "duration_seconds": duration_seconds,
        "cost": cost["total_cost"],
        "breakdown": {
            "base_cost": cost["base_cost"],
            "markup": cost["markup"],
        }
    }


@frappe.whitelist(allow_guest=True, methods=["GET"])
def status():
    """
    Get call session status

    Query Parameters:
        call_session_id: The session ID to check

    Returns:
        dict: Current session status
    """
    customer_info = _authenticate_request()

    call_session_id = frappe.form_dict.get("call_session_id")

    if not call_session_id:
        frappe.throw(_("Missing call_session_id"))

    session_data = frappe.cache().get_value(f"call_session:{call_session_id}")

    if not session_data:
        return {"status": "not_found"}

    if session_data["customer_id"] != customer_info["customer_id"]:
        return {"status": "not_found"}

    return {
        "status": session_data.get("status", "unknown"),
        "started_at": session_data.get("started_at"),
    }


def _create_janus_session(customer_id: str) -> dict:
    """
    Create a Janus WebRTC session

    Returns session_id and handle_id for VideoRoom plugin
    """
    settings = frappe.get_single("WhatsApp Provider Settings")

    if not settings.janus_ws_url:
        frappe.log_error("Janus WebSocket URL not configured")
        return None

    # TODO: Implement actual Janus API calls
    # This is a placeholder - actual implementation requires:
    # 1. Create Janus session via HTTP/WebSocket
    # 2. Attach to VideoRoom or SIP plugin
    # 3. Create/join room
    # 4. Return session credentials

    # For now, return mock data
    return {
        "session_id": secrets.token_hex(16),
        "handle_id": secrets.token_hex(16),
    }


def _cleanup_janus_session(session_id: str, handle_id: str):
    """
    Clean up Janus session after call ends

    Destroys the room and detaches handles
    """
    # TODO: Implement actual Janus cleanup
    # 1. Send destroy room request
    # 2. Detach handle
    # 3. Destroy session
    pass


def _get_ice_servers(settings) -> list:
    """Get STUN/TURN server configuration"""
    # Default to Google's STUN servers
    ice_servers = [
        {"urls": "stun:stun.l.google.com:19302"},
        {"urls": "stun:stun1.l.google.com:19302"},
    ]

    # TODO: Add configured TURN servers with credentials
    # if settings.turn_server_url:
    #     ice_servers.append({
    #         "urls": settings.turn_server_url,
    #         "username": settings.turn_username,
    #         "credential": settings.get_password("turn_credential"),
    #     })

    return ice_servers


def _extract_country_code(phone_number: str) -> str:
    """Extract country code from E.164 phone number"""
    # Simple extraction - actual implementation needs libphonenumber
    if phone_number.startswith("+1"):
        # Could be US or Canada
        return "US"  # Simplified
    elif phone_number.startswith("+91"):
        return "IN"
    elif phone_number.startswith("+55"):
        return "BR"
    elif phone_number.startswith("+52"):
        return "MX"
    elif phone_number.startswith("+62"):
        return "ID"
    # Add more as needed
    return "UNKNOWN"


def _calculate_call_cost(duration_seconds: int, customer) -> dict:
    """
    Calculate call cost based on duration and customer's plan

    Returns breakdown of base cost and markup
    """
    # Get customer's subscription plan
    plan = None
    if customer.subscription_plan:
        plan = frappe.get_doc("Subscription Plan", customer.subscription_plan)

    # Base rate (Meta's rate) - simplified
    # Actual implementation needs rate cards by country
    base_rate_per_minute = 0.03  # $0.03 USD per minute (example)
    duration_minutes = duration_seconds / 60

    base_cost = duration_minutes * base_rate_per_minute

    # Apply markup from plan
    markup_percentage = 0.35  # Default 35%
    if plan:
        markup_percentage = plan.call_markup_percentage / 100

    markup = base_cost * markup_percentage
    total_cost = base_cost + markup

    return {
        "base_cost": round(base_cost, 4),
        "markup": round(markup, 4),
        "total_cost": round(total_cost, 4),
    }


def _record_call_metric(customer_id: str, duration_seconds: int, cost: dict):
    """
    Record call usage metric

    IMPORTANT: We only record aggregated data
    - customer_id
    - date
    - call count
    - total duration
    - total cost

    NO phone numbers, NO lead info, NO call details
    """
    today = date.today()
    duration_minutes = duration_seconds / 60

    existing = frappe.db.get_value(
        "Daily Usage Metrics",
        {"customer": customer_id, "date": today},
        "name"
    )

    if existing:
        frappe.db.sql("""
            UPDATE `tabDaily Usage Metrics`
            SET
                total_calls = total_calls + 1,
                total_call_minutes = total_call_minutes + %s,
                total_call_cost = total_call_cost + %s,
                total_markup = total_markup + %s,
                total_revenue = total_revenue + %s
            WHERE name = %s
        """, (
            duration_minutes,
            cost["base_cost"],
            cost["markup"],
            cost["total_cost"],
            existing
        ))
    else:
        frappe.get_doc({
            "doctype": "Daily Usage Metrics",
            "customer": customer_id,
            "date": today,
            "total_calls": 1,
            "total_call_minutes": duration_minutes,
            "total_messages": 0,
            "total_call_cost": cost["base_cost"],
            "total_message_cost": 0,
            "total_markup": cost["markup"],
            "total_revenue": cost["total_cost"],
        }).insert(ignore_permissions=True)

    frappe.db.commit()
