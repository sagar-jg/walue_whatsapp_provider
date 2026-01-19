"""
Setup Token Exchange API

This module handles the setup token exchange flow that allows customer's
Frappe app to auto-configure WhatsApp Settings with all necessary credentials.

Flow:
1. Customer completes embedded signup on provider website
2. Provider stores WABA credentials temporarily
3. Customer installs Frappe app and enters setup token
4. App calls this API to exchange token for full configuration
5. Provider returns all credentials and marks token as used
6. Temporary credentials are cleared for security
"""

import frappe
from frappe import _
from datetime import datetime
import json

from walue_whatsapp_provider.constants import (
    ERR_INVALID_TOKEN,
    ERR_CUSTOMER_NOT_FOUND,
    CUSTOMER_STATUS_ACTIVE,
)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def exchange_token():
    """
    Exchange setup token for full WhatsApp configuration.

    This is a ONE-TIME operation. After successful exchange:
    - Token is marked as used
    - Temporary WABA credentials are cleared

    POST Body (JSON):
        setup_token: The setup token provided during registration

    Returns:
        dict: Complete configuration for client app
            - provider_url
            - oauth_client_id
            - oauth_client_secret
            - waba_id
            - phone_number_id
            - phone_number
            - meta_business_id
            - meta_access_token
    """
    # Parse JSON from request body
    data = {}
    if frappe.request.data:
        raw_data = frappe.request.data
        if isinstance(raw_data, bytes):
            raw_data = raw_data.decode('utf-8')
        data = json.loads(raw_data) if raw_data else {}

    setup_token = data.get("setup_token") or frappe.form_dict.get("setup_token")

    if not setup_token:
        frappe.throw(_("Setup token is required"), frappe.ValidationError)

    # Find customer by setup token
    customer_name = frappe.db.get_value(
        "WhatsApp Customer",
        {"setup_token": setup_token},
        "name"
    )

    if not customer_name:
        frappe.throw(_(ERR_INVALID_TOKEN), frappe.ValidationError)

    customer = frappe.get_doc("WhatsApp Customer", customer_name)

    # Validate token
    if not customer.is_setup_token_valid():
        if customer.setup_token_used:
            frappe.throw(_("Setup token has already been used. Contact support if you need to reconfigure."), frappe.ValidationError)
        else:
            frappe.throw(_("Setup token has expired. Please request a new one from your dashboard."), frappe.ValidationError)

    # Check if embedded signup was completed
    if not customer.embedded_signup_completed:
        frappe.throw(_("WhatsApp Business Account setup is not complete. Please complete the embedded signup first."), frappe.ValidationError)

    # Get temporary WABA credentials
    waba_credentials = customer.get_waba_credentials_temp()

    if not waba_credentials or not waba_credentials.get("access_token"):
        frappe.throw(_("WABA credentials not found. Please complete embedded signup or contact support."), frappe.ValidationError)

    # Build response with full configuration
    # Remove port numbers from URL (ngrok/production don't need them)
    provider_url = frappe.utils.get_url()
    if ':8000' in provider_url:
        provider_url = provider_url.replace(':8000', '')
    if ':443' in provider_url:
        provider_url = provider_url.replace(':443', '')

    response_data = {
        "success": True,
        "message": "Configuration retrieved successfully. Your WhatsApp integration is ready!",
        "configuration": {
            # Provider connection
            "provider_url": provider_url,
            "oauth_client_id": customer.oauth_client_id,
            "oauth_client_secret": customer.get_password("oauth_client_secret"),

            # WABA details
            "waba_id": waba_credentials.get("waba_id") or customer.waba_id,
            "phone_number_id": waba_credentials.get("phone_number_id") or customer.phone_number_id,
            "phone_number": waba_credentials.get("phone_number") or customer.phone_number,
            "meta_business_id": waba_credentials.get("business_id") or customer.meta_business_id,
            "meta_app_id": waba_credentials.get("app_id"),
            "meta_access_token": waba_credentials.get("access_token"),

            # Features (based on subscription plan)
            "calling_enabled": True,
            "messaging_enabled": True,
            "call_recording_enabled": _is_recording_enabled(customer),
        },
        "customer_info": {
            "customer_id": customer.name,
            "customer_name": customer.customer_name,
            "subscription_plan": customer.subscription_plan,
        }
    }

    # Mark setup as complete (this also clears temp credentials)
    customer.mark_setup_complete()

    # Log successful setup
    frappe.logger().info(f"Setup token exchanged successfully for customer {customer.name}")

    return response_data


@frappe.whitelist(allow_guest=True, methods=["GET"])
def validate_token():
    """
    Validate a setup token without consuming it.

    Useful for UI to check if token is valid before attempting exchange.

    Query Parameters:
        setup_token: The setup token to validate

    Returns:
        dict: Token validation status
    """
    setup_token = frappe.form_dict.get("setup_token")

    if not setup_token:
        return {"valid": False, "reason": "No token provided"}

    customer_name = frappe.db.get_value(
        "WhatsApp Customer",
        {"setup_token": setup_token},
        "name"
    )

    if not customer_name:
        return {"valid": False, "reason": "Token not found"}

    customer = frappe.get_doc("WhatsApp Customer", customer_name)

    if customer.setup_token_used:
        return {
            "valid": False,
            "reason": "Token already used",
            "setup_completed_at": str(customer.setup_completed_at) if customer.setup_completed_at else None
        }

    if not customer.is_setup_token_valid():
        return {
            "valid": False,
            "reason": "Token expired",
            "expired_at": str(customer.setup_token_expiry) if customer.setup_token_expiry else None
        }

    if not customer.embedded_signup_completed:
        return {
            "valid": False,
            "reason": "Embedded signup not completed",
            "message": "Please complete WhatsApp Business Account setup first"
        }

    return {
        "valid": True,
        "customer_name": customer.customer_name,
        "waba_connected": bool(customer.waba_id),
        "phone_number": customer.phone_number,
        "expires_at": str(customer.setup_token_expiry) if customer.setup_token_expiry else None
    }


@frappe.whitelist(allow_guest=False, methods=["POST"])
def regenerate_token():
    """
    Regenerate setup token for a customer (admin only).

    Use this when customer needs to reconfigure their app.

    POST Body (JSON):
        customer_id: The customer document name

    Returns:
        dict: New setup token and expiry
    """
    if "System Manager" not in frappe.get_roles():
        frappe.throw(_("Not authorized"), frappe.PermissionError)

    # Parse JSON from request body
    data = {}
    if frappe.request.data:
        raw_data = frappe.request.data
        if isinstance(raw_data, bytes):
            raw_data = raw_data.decode('utf-8')
        data = json.loads(raw_data) if raw_data else {}

    customer_id = data.get("customer_id")

    if not customer_id:
        frappe.throw(_("Customer ID is required"))

    if not frappe.db.exists("WhatsApp Customer", customer_id):
        frappe.throw(_(ERR_CUSTOMER_NOT_FOUND))

    customer = frappe.get_doc("WhatsApp Customer", customer_id)
    result = customer.regenerate_setup_token()

    return {
        "success": True,
        "message": "Setup token regenerated",
        "setup_token": result["setup_token"],
        "expiry": str(result["expiry"]),
    }


@frappe.whitelist(allow_guest=True, methods=["GET"])
def get_setup_info():
    """
    Get setup information for a customer by session ID.

    Called from the success page after embedded signup.

    Query Parameters:
        session_id: The embedded signup session ID
        customer_id: The customer document name

    Returns:
        dict: Setup information including token and instructions
    """
    session_id = frappe.form_dict.get("session_id")
    customer_id = frappe.form_dict.get("customer_id")

    if not session_id and not customer_id:
        frappe.throw(_("Session ID or Customer ID is required"))

    # Validate session if provided
    if session_id:
        session = frappe.db.get_value(
            "Embedded Signup Session",
            {"session_id": session_id},
            ["customer", "status"],
            as_dict=True
        )

        if not session:
            frappe.throw(_("Invalid session"))

        # Get customer from session
        customer_id = session.customer

    if not frappe.db.exists("WhatsApp Customer", customer_id):
        frappe.throw(_(ERR_CUSTOMER_NOT_FOUND))

    customer = frappe.get_doc("WhatsApp Customer", customer_id)

    # Check if signup is complete - either via session status or customer flag
    # This handles race conditions where session may not be committed yet
    if session_id:
        session = frappe.db.get_value(
            "Embedded Signup Session",
            {"session_id": session_id},
            ["status"],
            as_dict=True
        )
        session_completed = session and session.status == "completed"
    else:
        session_completed = False

    # Allow if either session is completed OR customer has completed embedded signup
    if not session_completed and not customer.embedded_signup_completed:
        frappe.throw(_("WhatsApp Business Account setup not completed"))

    provider_url = frappe.utils.get_url()

    # Build setup link for client app
    setup_link = f"{customer.frappe_site_url}/api/method/walue_whatsapp_client.api.setup.auto_configure?token={customer.setup_token}"

    return {
        "success": True,
        "customer_name": customer.customer_name,
        "phone_number": customer.phone_number,
        "waba_id": customer.waba_id,
        "setup_token": customer.setup_token if not customer.setup_token_used else None,
        "setup_token_used": customer.setup_token_used,
        "setup_token_expiry": str(customer.setup_token_expiry) if customer.setup_token_expiry else None,
        "setup_link": setup_link if not customer.setup_token_used else None,
        "frappe_site_url": customer.frappe_site_url,
        "provider_url": provider_url,
        "instructions": _get_setup_instructions(customer, setup_link),
    }


def _is_recording_enabled(customer) -> bool:
    """Check if call recording is enabled for customer's plan"""
    if not customer.subscription_plan:
        return False

    try:
        plan = frappe.get_doc("Subscription Plan", customer.subscription_plan)
        features = plan.features_json if hasattr(plan, "features_json") else ""
        return "call_recording" in features.lower() if features else False
    except Exception:
        return False


def _get_setup_instructions(customer, setup_link: str) -> list:
    """Generate setup instructions for the customer"""
    return [
        {
            "step": 1,
            "title": "Install the WhatsApp Client App",
            "description": "Run these commands on your Frappe/ERPNext server:",
            "commands": [
                "bench get-app https://github.com/walue-biz/walue-whatsapp-client",
                f"bench --site {customer.frappe_site_url.replace('https://', '').replace('http://', '')} install-app walue_whatsapp_client",
                "bench restart"
            ]
        },
        {
            "step": 2,
            "title": "Auto-Configure (Option A - Recommended)",
            "description": "Click the setup link or copy and paste it in your browser while logged into your Frappe site:",
            "setup_link": setup_link,
        },
        {
            "step": 3,
            "title": "Manual Setup (Option B)",
            "description": "If auto-configure doesn't work, go to WhatsApp Settings in your Frappe site and enter the setup token:",
            "setup_token": customer.setup_token,
        },
        {
            "step": 4,
            "title": "Verify Connection",
            "description": "After setup, go to WhatsApp Settings and verify the connection status shows 'Ready'.",
        }
    ]
