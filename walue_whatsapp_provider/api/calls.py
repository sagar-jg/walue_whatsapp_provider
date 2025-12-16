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
from walue_whatsapp_provider.utils.auth import authenticate_request as _authenticate_request
from walue_whatsapp_provider.api.rate_limit import enforce_rate_limit


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
        template_name: Name of the template to use (optional, defaults to voice_call_request)
        template_language: Language code (optional, defaults to en)
        template_components: Template components JSON (optional)

    Returns:
        dict: Contains success status and message_id

    Note: Permission status is tracked in CUSTOMER's app, not here
    """
    customer_info = _authenticate_request()

    # Enforce rate limit for permission requests
    enforce_rate_limit(customer_info["customer_id"], "request_permission")

    data = frappe.parse_json(frappe.request.data)

    phone_number_id = data.get("phone_number_id")
    access_token = data.get("access_token")
    to_number = data.get("to")
    use_template = data.get("use_template", False)
    template_name = data.get("template_name", "voice_call_request")
    template_language = data.get("template_language", "en")
    template_components = data.get("template_components")

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
        # Use customer's selected template or default voice_call_request
        template_config = {
            "name": template_name,
            "language": {"code": template_language},
            "components": [
                {
                    "type": "button",
                    "sub_type": "voice_call",
                    "index": 0,
                    "parameters": []
                }
            ]
        }

        # If customer provided custom components, use those
        if template_components:
            if isinstance(template_components, str):
                import json
                template_components = json.loads(template_components)
            template_config["components"] = template_components

        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to_number,
            "type": "template",
            "template": template_config
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

    # Enforce rate limit for call initiation
    enforce_rate_limit(customer_info["customer_id"], "initiate")

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
                "janus_room_id": janus_session.get("room_id"),
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
            "janus_room_id": janus_session.get("room_id"),
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
        session_data["janus_handle_id"],
        session_data.get("janus_room_id")
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


@frappe.whitelist(allow_guest=True, methods=["POST"])
def pre_accept():
    """
    Send pre-accept signal to Meta WhatsApp Business Calling API

    This should be called after generating SDP answer from WebRTC.
    Pre-accept establishes media connection before final acceptance.

    POST Body (JSON):
        phone_number_id: Customer's WhatsApp phone number ID
        access_token: Customer's Meta access token
        call_id: The call ID from call_connect webhook
        sdp: SDP answer from WebRTC

    Returns:
        dict: Success status
    """
    customer_info = _authenticate_request()

    data = frappe.parse_json(frappe.request.data)

    phone_number_id = data.get("phone_number_id")
    access_token = data.get("access_token")
    call_id = data.get("call_id")
    sdp = data.get("sdp")

    if not all([phone_number_id, access_token, call_id, sdp]):
        frappe.throw(_("Missing required parameters"))

    url = f"{META_API_BASE_URL}/{META_API_DEFAULT_VERSION}/{phone_number_id}/calls"

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    payload = {
        "messaging_product": "whatsapp",
        "call_id": call_id,
        "action": "pre_accept",
        "session": {
            "sdp_type": "answer",
            "sdp": sdp
        }
    }

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        response_data = response.json()

        if response.status_code != 200:
            error_msg = response_data.get("error", {}).get("message", ERR_META_API)
            frappe.log_error(f"Meta pre_accept failed: {error_msg}")
            return {"success": False, "error": error_msg}

        return {
            "success": True,
            "message": "Pre-accept sent successfully",
        }

    except requests.RequestException as e:
        frappe.log_error(f"Meta API pre_accept failed: {str(e)}")
        return {"success": False, "error": ERR_META_API}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def accept():
    """
    Accept a WhatsApp call via Meta API

    This should be called after pre_accept and once WebRTC connection is established.

    POST Body (JSON):
        phone_number_id: Customer's WhatsApp phone number ID
        access_token: Customer's Meta access token
        call_id: The call ID from call_connect webhook
        sdp: SDP answer (if not sent in pre_accept)

    Returns:
        dict: Success status
    """
    customer_info = _authenticate_request()

    data = frappe.parse_json(frappe.request.data)

    phone_number_id = data.get("phone_number_id")
    access_token = data.get("access_token")
    call_id = data.get("call_id")
    sdp = data.get("sdp")

    if not all([phone_number_id, access_token, call_id]):
        frappe.throw(_("Missing required parameters"))

    url = f"{META_API_BASE_URL}/{META_API_DEFAULT_VERSION}/{phone_number_id}/calls"

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    payload = {
        "messaging_product": "whatsapp",
        "call_id": call_id,
        "action": "accept",
    }

    # Include SDP if provided
    if sdp:
        payload["session"] = {
            "sdp_type": "answer",
            "sdp": sdp
        }

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        response_data = response.json()

        if response.status_code != 200:
            error_msg = response_data.get("error", {}).get("message", ERR_META_API)
            frappe.log_error(f"Meta accept failed: {error_msg}")
            return {"success": False, "error": error_msg}

        return {
            "success": True,
            "message": "Call accepted successfully",
        }

    except requests.RequestException as e:
        frappe.log_error(f"Meta API accept failed: {str(e)}")
        return {"success": False, "error": ERR_META_API}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def reject():
    """
    Reject an incoming WhatsApp call

    POST Body (JSON):
        phone_number_id: Customer's WhatsApp phone number ID
        access_token: Customer's Meta access token
        call_id: The call ID from call_connect webhook

    Returns:
        dict: Success status
    """
    customer_info = _authenticate_request()

    data = frappe.parse_json(frappe.request.data)

    phone_number_id = data.get("phone_number_id")
    access_token = data.get("access_token")
    call_id = data.get("call_id")

    if not all([phone_number_id, access_token, call_id]):
        frappe.throw(_("Missing required parameters"))

    url = f"{META_API_BASE_URL}/{META_API_DEFAULT_VERSION}/{phone_number_id}/calls"

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    payload = {
        "messaging_product": "whatsapp",
        "call_id": call_id,
        "action": "reject",
    }

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        response_data = response.json()

        if response.status_code != 200:
            error_msg = response_data.get("error", {}).get("message", ERR_META_API)
            return {"success": False, "error": error_msg}

        return {
            "success": True,
            "message": "Call rejected",
        }

    except requests.RequestException as e:
        frappe.log_error(f"Meta API reject failed: {str(e)}")
        return {"success": False, "error": ERR_META_API}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def terminate():
    """
    Terminate an active WhatsApp call

    POST Body (JSON):
        phone_number_id: Customer's WhatsApp phone number ID
        access_token: Customer's Meta access token
        call_id: The call ID

    Returns:
        dict: Success status
    """
    customer_info = _authenticate_request()

    data = frappe.parse_json(frappe.request.data)

    phone_number_id = data.get("phone_number_id")
    access_token = data.get("access_token")
    call_id = data.get("call_id")

    if not all([phone_number_id, access_token, call_id]):
        frappe.throw(_("Missing required parameters"))

    url = f"{META_API_BASE_URL}/{META_API_DEFAULT_VERSION}/{phone_number_id}/calls"

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    payload = {
        "messaging_product": "whatsapp",
        "call_id": call_id,
        "action": "terminate",
    }

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        response_data = response.json()

        if response.status_code != 200:
            error_msg = response_data.get("error", {}).get("message", ERR_META_API)
            return {"success": False, "error": error_msg}

        return {
            "success": True,
            "message": "Call terminated",
        }

    except requests.RequestException as e:
        frappe.log_error(f"Meta API terminate failed: {str(e)}")
        return {"success": False, "error": ERR_META_API}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def connect():
    """
    Initiate an outbound WhatsApp call to a user

    This sends a call connect request to Meta API with SDP offer.
    The user must have granted call permission first.

    POST Body (JSON):
        phone_number_id: Customer's WhatsApp phone number ID
        access_token: Customer's Meta access token
        to: Recipient phone number (E.164 format)
        sdp: SDP offer from WebRTC

    Returns:
        dict: Contains call_id if successful
    """
    print(f"[CONNECT] Starting connect request")

    try:
        customer_info = _authenticate_request()
        print(f"[CONNECT] Authenticated customer: {customer_info.get('customer_id')}")
    except Exception as auth_error:
        print(f"[CONNECT] Authentication failed: {auth_error}")
        return {"success": False, "error": f"Authentication failed: {str(auth_error)}"}

    try:
        # Enforce rate limit for call connections
        enforce_rate_limit(customer_info["customer_id"], "connect")
        print(f"[CONNECT] Rate limit check passed")
    except Exception as rate_error:
        print(f"[CONNECT] Rate limit error: {rate_error}")
        return {"success": False, "error": f"Rate limit exceeded: {str(rate_error)}"}

    try:
        raw_data = frappe.request.data
        if isinstance(raw_data, bytes):
            raw_data = raw_data.decode('utf-8')
        data = frappe.parse_json(raw_data) if raw_data else {}
        print(f"[CONNECT] Parsed data keys: {list(data.keys()) if isinstance(data, dict) else 'invalid'}")
    except Exception as parse_error:
        print(f"[CONNECT] JSON parse error: {parse_error}")
        return {"success": False, "error": f"Invalid JSON payload: {str(parse_error)}"}

    phone_number_id = data.get("phone_number_id")
    access_token = data.get("access_token")
    to_number = data.get("to")
    sdp = data.get("sdp")

    print(f"[CONNECT] phone_number_id: {bool(phone_number_id)}, access_token: {bool(access_token)}, to: {to_number}, sdp: {bool(sdp)}")

    # Check for missing parameters and provide specific error
    missing = []
    if not phone_number_id:
        missing.append("phone_number_id")
    if not access_token:
        missing.append("access_token")
    if not to_number:
        missing.append("to")
    if not sdp:
        missing.append("sdp")

    if missing:
        error_msg = f"Missing required parameters: {', '.join(missing)}"
        print(f"[CONNECT] {error_msg}")
        return {"success": False, "error": error_msg}

    # Check if calling is available for this region
    country_code = _extract_country_code(to_number)
    if country_code in CALLING_RESTRICTED_COUNTRIES:
        return {
            "success": False,
            "error": MSG_CALLING_NOT_AVAILABLE,
            "restricted": True,
        }

    url = f"{META_API_BASE_URL}/{META_API_DEFAULT_VERSION}/{phone_number_id}/calls"

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    payload = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "action": "connect",
        "session": {
            "sdp_type": "offer",
            "sdp": sdp
        }
    }

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        response_data = response.json()

        if response.status_code != 200:
            error_msg = response_data.get("error", {}).get("message", ERR_META_API)
            frappe.log_error(f"Meta connect call failed: {error_msg}")
            return {"success": False, "error": error_msg}

        call_id = response_data.get("call_id")

        return {
            "success": True,
            "call_id": call_id,
            "message": "Call initiated, waiting for user to accept",
        }

    except requests.RequestException as e:
        frappe.log_error(f"Meta API connect call failed: {str(e)}")
        return {"success": False, "error": ERR_META_API}


def _create_janus_session(customer_id: str) -> dict:
    """
    Create a Janus WebRTC session for WhatsApp calling

    Creates session, attaches AudioBridge plugin, and creates a room.

    Args:
        customer_id: Customer identifier for logging

    Returns:
        dict: Session credentials including session_id, handle_id, room_id
    """
    settings = frappe.get_single("WhatsApp Provider Settings")

    if not settings.janus_ws_url:
        frappe.log_error("Janus WebSocket URL not configured")
        return None

    try:
        from walue_whatsapp_provider.utils.janus_client import create_call_session

        janus_session = create_call_session(customer_id)

        return {
            "session_id": janus_session.session_id,
            "handle_id": janus_session.handle_id,
            "room_id": janus_session.room_id,
        }

    except Exception as e:
        frappe.log_error(f"Janus session creation failed: {str(e)}", "Janus Call Setup")
        return None


def _cleanup_janus_session(session_id: str, handle_id: str, room_id: int = None):
    """
    Clean up Janus session after call ends

    Destroys the room, detaches handles, and destroys session.

    Args:
        session_id: Janus session ID
        handle_id: Plugin handle ID
        room_id: AudioBridge room ID (optional)
    """
    try:
        from walue_whatsapp_provider.utils.janus_client import cleanup_call_session

        cleanup_call_session(session_id, handle_id, room_id)

    except Exception as e:
        # Log but don't fail - session might already be cleaned up
        frappe.log_error(f"Janus cleanup warning: {str(e)}", "Janus Cleanup")


def _get_ice_servers(settings) -> list:
    """
    Get STUN/TURN server configuration for WebRTC

    Returns ICE servers list for RTCPeerConnection configuration.

    Args:
        settings: WhatsApp Provider Settings document

    Returns:
        list: ICE servers configuration
    """
    ice_servers = []

    # Add STUN servers from settings
    if settings.stun_servers:
        for stun_url in settings.stun_servers.strip().split("\n"):
            stun_url = stun_url.strip()
            if stun_url:
                ice_servers.append({"urls": stun_url})
    else:
        # Default to Google's STUN servers
        ice_servers = [
            {"urls": "stun:stun.l.google.com:19302"},
            {"urls": "stun:stun1.l.google.com:19302"},
        ]

    # Add TURN server with credentials if configured
    if settings.turn_server_url:
        turn_config = {"urls": settings.turn_server_url}

        if settings.turn_username:
            turn_config["username"] = settings.turn_username

        if settings.turn_credential:
            turn_config["credential"] = settings.get_password("turn_credential")

        ice_servers.append(turn_config)

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
