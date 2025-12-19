"""
Embedded Signup API for Meta WhatsApp Business Account onboarding

This module handles the embedded signup flow that allows customers to:
1. Create/connect their Meta Business Account
2. Create/connect their WhatsApp Business Account (WABA)
3. Verify their phone number
4. Receive WABA credentials to store in their own system

IMPORTANT: We do NOT store WABA credentials - only pass them back to customer
"""

import frappe
from frappe import _
import secrets
import requests
from datetime import datetime

from walue_whatsapp_provider.constants import (
    META_API_BASE_URL,
    META_API_DEFAULT_VERSION,
    SIGNUP_STATUS_INITIATED,
    SIGNUP_STATUS_IN_PROGRESS,
    SIGNUP_STATUS_COMPLETED,
    SIGNUP_STATUS_FAILED,
    ERR_OAUTH_FAILED,
    MSG_SIGNUP_INITIATED,
    MSG_SIGNUP_COMPLETED,
)


def _log_debug(title: str, message: str):
    """Helper to log debug messages consistently"""
    frappe.log_error(
        title=f"[DEBUG] {title}"[:140],
        message=message[:2000] if message else "No message"
    )


@frappe.whitelist(allow_guest=True)
def initiate(customer_id: str) -> dict:
    """
    Initiate embedded signup flow for a customer

    Args:
        customer_id: The WhatsApp Customer document name

    Returns:
        dict: Contains signup_url and session_id
    """
    _log_debug("Initiate called", f"customer_id: {customer_id}")

    # Validate customer exists
    if not frappe.db.exists("WhatsApp Customer", customer_id):
        frappe.throw(_("Customer not found"), frappe.DoesNotExistError)

    # Get provider settings
    settings = frappe.get_single("WhatsApp Provider Settings")
    if not settings.enabled:
        frappe.throw(_("WhatsApp Provider is not enabled"))

    # Generate unique session ID
    session_id = secrets.token_urlsafe(32)

    # Build embedded signup URL
    # Reference: https://developers.facebook.com/docs/whatsapp/embedded-signup
    signup_url = _build_embedded_signup_url(settings, session_id, customer_id)

    _log_debug("Signup URL built", f"session_id: {session_id}, url: {signup_url[:200]}")

    # Create session record
    session = frappe.get_doc({
        "doctype": "Embedded Signup Session",
        "customer": customer_id,
        "session_id": session_id,
        "status": SIGNUP_STATUS_INITIATED,
        "signup_url": signup_url,
        "created_at": datetime.now(),
    })
    session.insert(ignore_permissions=True)

    _log_debug("Session created", f"session_name: {session.name}, session_id: {session_id}")

    return {
        "success": True,
        "message": MSG_SIGNUP_INITIATED,
        "signup_url": signup_url,
        "session_id": session_id,
    }


@frappe.whitelist(allow_guest=True)
def callback():
    """
    Handle OAuth callback from Meta after embedded signup completion

    This endpoint receives the OAuth code from Meta and exchanges it
    for access tokens. The WABA credentials are returned to the customer
    app but NOT stored on our system.

    Query Parameters:
        code: OAuth authorization code from Meta
        state: Session ID for validation
    """
    _log_debug("Callback received", f"form_dict keys: {list(frappe.form_dict.keys())}")

    code = frappe.form_dict.get("code")
    state = frappe.form_dict.get("state")  # This is our session_id
    error = frappe.form_dict.get("error")
    error_description = frappe.form_dict.get("error_description")

    _log_debug("Callback params", f"code: {code[:50] if code else 'None'}..., state: {state}, error: {error}")

    # Handle errors from Meta
    if error:
        _log_debug("Callback error from Meta", f"error: {error}, desc: {error_description}")
        _handle_signup_error(state, error, error_description)
        return {"success": False, "error": error_description or error}

    if not code or not state:
        _log_debug("Missing params", f"code: {bool(code)}, state: {bool(state)}")
        frappe.throw(_("Missing required parameters"))

    # Validate session
    session = _get_valid_session(state)
    if not session:
        _log_debug("Session not found", f"state/session_id: {state}")
        frappe.throw(_("Invalid or expired signup session"))

    _log_debug("Session found", f"session_name: {session.name}, customer: {session.customer}, status: {session.status}")

    # Update session status
    session.status = SIGNUP_STATUS_IN_PROGRESS
    session.code = code
    session.save(ignore_permissions=True)
    _log_debug("Session updated to in_progress", f"session: {session.name}")

    try:
        # Exchange code for access token
        settings = frappe.get_single("WhatsApp Provider Settings")
        _log_debug("Exchanging code for token", f"meta_app_id: {settings.meta_app_id}")

        token_data = _exchange_code_for_token(settings, code)
        _log_debug("Token received", f"token_keys: {list(token_data.keys())}")

        # Get WABA details using the access token
        _log_debug("Getting WABA details", "Calling Meta API...")
        waba_details = _get_waba_details(token_data["access_token"])
        _log_debug("WABA details received", f"waba_id: {waba_details.get('waba_id')}, phone_id: {waba_details.get('phone_number_id')}, phone: {waba_details.get('phone_number')}")

        # CRITICAL: Subscribe our app to receive webhooks for this WABA
        # Without this, webhooks won't be delivered to our app
        if waba_details.get("waba_id"):
            _log_debug("Subscribing to WABA", f"waba_id: {waba_details['waba_id']}")
            _subscribe_app_to_waba(waba_details["waba_id"], token_data["access_token"])

        # Update customer record with WABA reference
        _log_debug("Updating customer", f"customer: {session.customer}")
        customer = frappe.get_doc("WhatsApp Customer", session.customer)
        customer.meta_business_id = waba_details.get("business_id")
        customer.waba_id = waba_details.get("waba_id")
        customer.phone_number_id = waba_details.get("phone_number_id")
        customer.phone_number = waba_details.get("phone_number")
        customer.embedded_signup_completed = 1
        customer.status = "Active"  # Activate customer after successful signup

        # Regenerate setup token for client app configuration
        customer._generate_setup_token()

        customer.save(ignore_permissions=True)
        _log_debug("Customer updated", f"customer: {customer.name}, waba_id: {customer.waba_id}, phone: {customer.phone_number}")

        # Store WABA credentials temporarily for setup token exchange
        # These will be cleared after the client app retrieves them
        waba_credentials = {
            "waba_id": waba_details.get("waba_id"),
            "phone_number_id": waba_details.get("phone_number_id"),
            "phone_number": waba_details.get("phone_number"),
            "business_id": waba_details.get("business_id"),
            "access_token": token_data["access_token"],
        }
        customer.store_waba_credentials_temp(waba_credentials)
        _log_debug("Temp credentials stored", f"customer: {customer.name}")

        # Update session as completed
        session.status = SIGNUP_STATUS_COMPLETED
        session.completed_at = datetime.now()
        session.save(ignore_permissions=True)
        _log_debug("Session completed", f"session: {session.name}")

        # Redirect to success page
        # If website is hosted separately, change this URL to point to your website
        # Default: use the /whatsapp-success page on the provider site
        base_url = frappe.utils.get_url()
        success_url = f"{base_url}/whatsapp-success?customer_id={customer.name}&session_id={session.session_id}"

        _log_debug("Redirecting to success", f"url: {success_url}")

        frappe.local.response["type"] = "redirect"
        frappe.local.response["location"] = success_url

        return {
            "success": True,
            "message": MSG_SIGNUP_COMPLETED,
            "redirect_url": success_url,
        }

    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        _log_debug("Callback exception", f"error: {str(e)}\n\nTraceback:\n{error_trace}")

        session.status = SIGNUP_STATUS_FAILED
        session.error_message = str(e)[:500]  # Truncate for DB field
        session.save(ignore_permissions=True)

        return {"success": False, "error": ERR_OAUTH_FAILED}


@frappe.whitelist(allow_guest=True)
def get_config() -> dict:
    """
    Get Facebook SDK configuration for embedded signup

    Returns:
        dict: Contains app_id and config_id
    """
    _log_debug("Get config called", "Fetching Meta app configuration")

    settings = frappe.get_single("WhatsApp Provider Settings")

    if not settings.enabled:
        return {"success": False, "error": "WhatsApp Provider is not enabled"}

    return {
        "success": True,
        "app_id": settings.meta_app_id,
        "config_id": settings.meta_configuration_id,
    }


@frappe.whitelist(allow_guest=True, methods=["POST"])
def complete() -> dict:
    """
    Complete embedded signup with WABA details from frontend

    This endpoint receives the waba_id and phone_number_id directly
    from the frontend (captured via sessionInfoListener) along with
    the OAuth code for token exchange.

    POST Body:
        customer_id: Customer document name
        session_id: Session ID from initiate()
        waba_id: WhatsApp Business Account ID from Meta
        phone_number_id: Phone number ID from Meta
        code: OAuth authorization code
    """
    customer_id = frappe.form_dict.get("customer_id")
    session_id = frappe.form_dict.get("session_id")
    waba_id = frappe.form_dict.get("waba_id")
    phone_number_id = frappe.form_dict.get("phone_number_id")
    code = frappe.form_dict.get("code")

    _log_debug("Complete called", f"customer: {customer_id}, session: {session_id}, waba: {waba_id}, phone_id: {phone_number_id}")

    if not customer_id or not waba_id:
        return {"success": False, "error": "Missing required parameters"}

    # Validate customer exists
    if not frappe.db.exists("WhatsApp Customer", customer_id):
        return {"success": False, "error": "Customer not found"}

    try:
        # Get or create session
        session = None
        if session_id:
            session = _get_valid_session(session_id)

        if session:
            session.status = SIGNUP_STATUS_IN_PROGRESS
            if code:
                session.code = code
            session.save(ignore_permissions=True)

        # Exchange code for access token if provided
        access_token = None
        if code:
            try:
                settings = frappe.get_single("WhatsApp Provider Settings")
                token_data = _exchange_code_for_token(settings, code)
                access_token = token_data.get("access_token")
                _log_debug("Token exchanged", f"Got access token: {bool(access_token)}")
            except Exception as e:
                _log_debug("Token exchange failed", str(e))
                # Continue without token - we still have the WABA details

        # Get phone number display if we have access token
        phone_number = None
        if access_token and phone_number_id:
            try:
                phone_url = f"{META_API_BASE_URL}/{META_API_DEFAULT_VERSION}/{phone_number_id}"
                phone_response = requests.get(phone_url, params={
                    "access_token": access_token,
                    "fields": "display_phone_number,verified_name"
                })
                if phone_response.ok:
                    phone_data = phone_response.json()
                    phone_number = phone_data.get("display_phone_number")
                    _log_debug("Phone number fetched", f"phone: {phone_number}")
            except Exception as e:
                _log_debug("Phone fetch failed", str(e))

        # Subscribe app to WABA for webhooks
        if access_token and waba_id:
            _subscribe_app_to_waba(waba_id, access_token)

        # Update customer record
        customer = frappe.get_doc("WhatsApp Customer", customer_id)
        customer.waba_id = waba_id
        customer.phone_number_id = phone_number_id
        customer.phone_number = phone_number
        customer.embedded_signup_completed = 1
        customer.status = "Active"

        # Generate setup token for client app configuration
        customer._generate_setup_token()

        customer.save(ignore_permissions=True)
        _log_debug("Customer updated", f"customer: {customer.name}, waba: {waba_id}, phone: {phone_number}")

        # Store temp credentials if we have access token
        if access_token:
            waba_credentials = {
                "waba_id": waba_id,
                "phone_number_id": phone_number_id,
                "phone_number": phone_number,
                "access_token": access_token,
            }
            customer.store_waba_credentials_temp(waba_credentials)

        # Update session as completed
        if session:
            session.status = SIGNUP_STATUS_COMPLETED
            session.completed_at = datetime.now()
            session.save(ignore_permissions=True)

        return {
            "success": True,
            "message": MSG_SIGNUP_COMPLETED,
            "phone_number": phone_number,
            "setup_token": customer.setup_token,
        }

    except Exception as e:
        import traceback
        _log_debug("Complete exception", f"error: {str(e)}\n\n{traceback.format_exc()}")

        if session:
            session.status = SIGNUP_STATUS_FAILED
            session.error_message = str(e)[:500]
            session.save(ignore_permissions=True)

        return {"success": False, "error": str(e)}


@frappe.whitelist(allow_guest=False)
def status(session_id: str) -> dict:
    """
    Get the status of an embedded signup session

    Args:
        session_id: The session ID returned from initiate()

    Returns:
        dict: Session status and details
    """
    session = frappe.db.get_value(
        "Embedded Signup Session",
        {"session_id": session_id},
        ["status", "error_message", "created_at", "completed_at"],
        as_dict=True
    )

    if not session:
        frappe.throw(_("Session not found"))

    return {
        "status": session.status,
        "error_message": session.error_message,
        "created_at": session.created_at,
        "completed_at": session.completed_at,
    }


def _build_embedded_signup_url(settings, session_id: str, customer_id: str) -> str:
    """Build the Meta embedded signup URL"""

    # Get base URL without port for ngrok
    site_url = frappe.utils.get_url()

    # Remove port if present (ngrok doesn't need it)
    if ':8000' in site_url or ':443' in site_url:
        site_url = site_url.replace(':8000', '').replace(':443', '')

    # Build callback URL
    callback_url = f"{site_url}/api/method/walue_whatsapp_provider.api.embedded_signup.callback"

    # Build OAuth URL for embedded signup
    # Reference: https://developers.facebook.com/docs/whatsapp/embedded-signup/oauth-flow
    params = {
        "client_id": settings.meta_app_id,
        "config_id": settings.meta_configuration_id,
        "response_type": "code",
        "override_default_response_type": "true",
        "redirect_uri": callback_url,
        "state": session_id,
        "scope": "whatsapp_business_management,whatsapp_business_messaging",
    }

    base_url = "https://www.facebook.com/v21.0/dialog/oauth"
    query_string = "&".join([f"{k}={v}" for k, v in params.items()])

    return f"{base_url}?{query_string}"


def _exchange_code_for_token(settings, code: str) -> dict:
    """Exchange OAuth code for access token"""
    # Get base URL without port for ngrok
    site_url = frappe.utils.get_url()
    if ':8000' in site_url or ':443' in site_url:
        site_url = site_url.replace(':8000', '').replace(':443', '')
    callback_url = f"{site_url}/api/method/walue_whatsapp_provider.api.embedded_signup.callback"

    url = f"{META_API_BASE_URL}/{META_API_DEFAULT_VERSION}/oauth/access_token"
    params = {
        "client_id": settings.meta_app_id,
        "client_secret": settings.get_password("meta_app_secret"),
        "code": code,
        "redirect_uri": callback_url,
    }

    _log_debug("Token exchange request", f"url: {url}, redirect_uri: {callback_url}")

    response = requests.get(url, params=params)

    _log_debug("Token exchange response", f"status: {response.status_code}, body: {response.text[:500]}")

    response.raise_for_status()

    return response.json()


def _get_waba_details(access_token: str) -> dict:
    """Get WABA details using the access token"""
    waba_id = None
    phone_number_id = None
    phone_number = None
    business_id = None

    # Get WABA from shared accounts
    waba_url = f"{META_API_BASE_URL}/{META_API_DEFAULT_VERSION}/me/whatsapp_business_accounts"
    _log_debug("Getting WABA list", f"url: {waba_url}")

    waba_response = requests.get(waba_url, params={"access_token": access_token})

    _log_debug("WABA list response", f"status: {waba_response.status_code}, body: {waba_response.text[:500]}")

    if not waba_response.ok:
        _log_debug("WABA list failed", f"Status: {waba_response.status_code}, Response: {waba_response.text[:500]}")
        return {"waba_id": None, "phone_number_id": None, "phone_number": None, "business_id": None}

    waba_data = waba_response.json()
    _log_debug("WABA data parsed", f"data count: {len(waba_data.get('data', []))}")

    if waba_data.get("data"):
        waba_info = waba_data["data"][0]
        waba_id = waba_info.get("id")
        _log_debug("WABA info", f"waba_id: {waba_id}, full_info: {waba_info}")

        # Get phone numbers for this WABA
        phone_url = f"{META_API_BASE_URL}/{META_API_DEFAULT_VERSION}/{waba_id}/phone_numbers"
        _log_debug("Getting phone numbers", f"url: {phone_url}")

        phone_response = requests.get(phone_url, params={"access_token": access_token})
        _log_debug("Phone response", f"status: {phone_response.status_code}, body: {phone_response.text[:500]}")

        if phone_response.ok:
            phone_data = phone_response.json()
            _log_debug("Phone data", f"data count: {len(phone_data.get('data', []))}")

            if phone_data.get("data"):
                phone_info = phone_data["data"][0]
                phone_number_id = phone_info.get("id")
                phone_number = phone_info.get("display_phone_number")
                _log_debug("Phone info", f"phone_id: {phone_number_id}, phone: {phone_number}, full: {phone_info}")

        # Get business ID from WABA info if available
        business_id = waba_info.get("business_id") or waba_info.get("owner_business_info", {}).get("id")

    result = {
        "waba_id": waba_id,
        "phone_number_id": phone_number_id,
        "phone_number": phone_number,
        "business_id": business_id,
    }
    _log_debug("WABA details result", f"{result}")

    return result


def _get_valid_session(session_id: str):
    """Get and validate a signup session"""
    _log_debug("Looking up session", f"session_id: {session_id}")

    # Use frappe.db.get_value to find the document name first
    session_name = frappe.db.get_value(
        "Embedded Signup Session",
        {"session_id": session_id},
        "name"
    )

    _log_debug("Session lookup result", f"session_name: {session_name}")

    if session_name:
        return frappe.get_doc("Embedded Signup Session", session_name)
    return None


def _subscribe_app_to_waba(waba_id: str, access_token: str) -> bool:
    """
    Subscribe our app to receive webhooks for the customer's WABA

    CRITICAL: This must be called after embedded signup to enable webhooks.
    Without this subscription, Meta will not send webhooks to our app
    for this customer's WhatsApp Business Account.

    Args:
        waba_id: The customer's WhatsApp Business Account ID
        access_token: The customer's access token (from embedded signup)

    Returns:
        bool: True if subscription succeeded

    Reference: https://developers.facebook.com/docs/whatsapp/embedded-signup/webhooks
    """
    url = f"{META_API_BASE_URL}/{META_API_DEFAULT_VERSION}/{waba_id}/subscribed_apps"

    try:
        _log_debug("Subscribing to WABA", f"url: {url}")

        response = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json"
            }
        )

        result = response.json()
        _log_debug("Subscribe response", f"status: {response.status_code}, result: {result}")

        if result.get("success"):
            _log_debug("Subscribe success", f"WABA {waba_id} subscribed")
            return True
        else:
            error_msg = result.get("error", {}).get("message", "Unknown error")
            _log_debug("Subscribe failed", f"WABA {waba_id}: {error_msg}")
            return False

    except Exception as e:
        _log_debug("Subscribe exception", f"WABA {waba_id}: {str(e)}")
        return False


def _handle_signup_error(session_id: str, error: str, error_description: str):
    """Handle and log signup errors"""
    _log_debug("Handling signup error", f"session_id: {session_id}, error: {error}, desc: {error_description}")

    if session_id:
        session = _get_valid_session(session_id)
        if session:
            session.status = SIGNUP_STATUS_FAILED
            session.error_message = (error_description or error)[:500]
            session.save(ignore_permissions=True)
            _log_debug("Session marked failed", f"session: {session.name}")
