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


@frappe.whitelist(allow_guest=False)
def initiate(customer_id: str) -> dict:
    """
    Initiate embedded signup flow for a customer

    Args:
        customer_id: The WhatsApp Customer document name

    Returns:
        dict: Contains signup_url and session_id
    """
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
    code = frappe.form_dict.get("code")
    state = frappe.form_dict.get("state")  # This is our session_id
    error = frappe.form_dict.get("error")
    error_description = frappe.form_dict.get("error_description")

    # Handle errors from Meta
    if error:
        _handle_signup_error(state, error, error_description)
        return {"success": False, "error": error_description or error}

    if not code or not state:
        frappe.throw(_("Missing required parameters"))

    # Validate session
    session = _get_valid_session(state)
    if not session:
        frappe.throw(_("Invalid or expired signup session"))

    # Update session status
    session.status = SIGNUP_STATUS_IN_PROGRESS
    session.code = code
    session.save(ignore_permissions=True)

    try:
        # Exchange code for access token
        settings = frappe.get_single("WhatsApp Provider Settings")
        token_data = _exchange_code_for_token(settings, code)

        # Get WABA details using the access token
        waba_details = _get_waba_details(token_data["access_token"])

        # Update customer record with WABA reference (ID only, not credentials)
        customer = frappe.get_doc("WhatsApp Customer", session.customer)
        customer.meta_business_id = waba_details.get("business_id")
        customer.waba_id = waba_details.get("waba_id")
        customer.phone_number_id = waba_details.get("phone_number_id")
        customer.embedded_signup_completed = 1
        customer.save(ignore_permissions=True)

        # Update session as completed
        session.status = SIGNUP_STATUS_COMPLETED
        session.completed_at = datetime.now()
        session.save(ignore_permissions=True)

        # Return WABA credentials to customer app (they store it, not us)
        return {
            "success": True,
            "message": MSG_SIGNUP_COMPLETED,
            "waba_credentials": {
                "waba_id": waba_details.get("waba_id"),
                "phone_number_id": waba_details.get("phone_number_id"),
                "phone_number": waba_details.get("phone_number"),
                "business_id": waba_details.get("business_id"),
                "access_token": token_data["access_token"],  # Customer stores this
            }
        }

    except Exception as e:
        session.status = SIGNUP_STATUS_FAILED
        session.error_message = str(e)
        session.save(ignore_permissions=True)
        frappe.log_error(f"Embedded signup callback failed: {str(e)}")
        return {"success": False, "error": ERR_OAUTH_FAILED}


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
    # Get callback URL
    callback_url = frappe.utils.get_url(
        "/api/method/walue_whatsapp_provider.api.embedded_signup.callback"
    )

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
    callback_url = frappe.utils.get_url(
        "/api/method/walue_whatsapp_provider.api.embedded_signup.callback"
    )

    url = f"{META_API_BASE_URL}/{META_API_DEFAULT_VERSION}/oauth/access_token"
    params = {
        "client_id": settings.meta_app_id,
        "client_secret": settings.get_password("meta_app_secret"),
        "code": code,
        "redirect_uri": callback_url,
    }

    response = requests.get(url, params=params)
    response.raise_for_status()

    return response.json()


def _get_waba_details(access_token: str) -> dict:
    """Get WABA details using the access token"""
    # Get shared WABAs
    url = f"{META_API_BASE_URL}/{META_API_DEFAULT_VERSION}/debug_token"
    params = {
        "input_token": access_token,
        "access_token": access_token,
    }

    response = requests.get(url, params=params)
    response.raise_for_status()
    data = response.json()

    # Extract WABA info from granular scopes
    # This is a simplified version - actual implementation needs more parsing
    waba_id = None
    phone_number_id = None
    phone_number = None
    business_id = data.get("data", {}).get("app_id")

    # Get WABA from shared accounts
    waba_url = f"{META_API_BASE_URL}/{META_API_DEFAULT_VERSION}/me/whatsapp_business_accounts"
    waba_response = requests.get(waba_url, params={"access_token": access_token})
    if waba_response.ok:
        waba_data = waba_response.json()
        if waba_data.get("data"):
            waba_id = waba_data["data"][0].get("id")

            # Get phone numbers for this WABA
            phone_url = f"{META_API_BASE_URL}/{META_API_DEFAULT_VERSION}/{waba_id}/phone_numbers"
            phone_response = requests.get(phone_url, params={"access_token": access_token})
            if phone_response.ok:
                phone_data = phone_response.json()
                if phone_data.get("data"):
                    phone_number_id = phone_data["data"][0].get("id")
                    phone_number = phone_data["data"][0].get("display_phone_number")

    return {
        "waba_id": waba_id,
        "phone_number_id": phone_number_id,
        "phone_number": phone_number,
        "business_id": business_id,
    }


def _get_valid_session(session_id: str):
    """Get and validate a signup session"""
    if frappe.db.exists("Embedded Signup Session", {"session_id": session_id}):
        return frappe.get_doc("Embedded Signup Session", {"session_id": session_id})
    return None


def _handle_signup_error(session_id: str, error: str, error_description: str):
    """Handle and log signup errors"""
    if session_id:
        session = _get_valid_session(session_id)
        if session:
            session.status = SIGNUP_STATUS_FAILED
            session.error_message = error_description or error
            session.save(ignore_permissions=True)

    frappe.log_error(f"Embedded signup error: {error} - {error_description}")
