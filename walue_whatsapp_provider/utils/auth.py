"""
Authentication utilities for API requests

Supports two authentication methods:
1. Bearer token (OAuth access token)
2. Client credentials (X-Client-ID + X-Client-Secret headers)
"""

import frappe
from frappe import _

from walue_whatsapp_provider.constants import (
    ERR_INVALID_TOKEN,
    CUSTOMER_STATUS_ACTIVE,
)


def authenticate_request() -> dict:
    """
    Authenticate API request using either Bearer token or client credentials

    Returns:
        dict: Customer info with customer_id

    Raises:
        frappe.AuthenticationError: If authentication fails
    """
    # Try Bearer token first
    auth_header = frappe.request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return _authenticate_bearer_token(auth_header)

    # Try client credentials
    client_id = frappe.request.headers.get("X-Client-ID")
    client_secret = frappe.request.headers.get("X-Client-Secret")
    if client_id and client_secret:
        return _authenticate_client_credentials(client_id, client_secret)

    frappe.throw(_(ERR_INVALID_TOKEN), frappe.AuthenticationError)


def _authenticate_bearer_token(auth_header: str) -> dict:
    """Authenticate using OAuth Bearer token"""
    from walue_whatsapp_provider.api.oauth import validate_token

    token = auth_header.split(" ")[1]
    customer_info = validate_token(token)

    if not customer_info:
        frappe.throw(_(ERR_INVALID_TOKEN), frappe.AuthenticationError)

    _verify_customer_active(customer_info["customer_id"])
    return customer_info


def _authenticate_client_credentials(client_id: str, client_secret: str) -> dict:
    """Authenticate using OAuth client credentials directly"""
    # Find the OAuth client
    client = frappe.db.get_value(
        "OAuth Client",
        {"client_id": client_id},
        ["name", "client_secret", "customer"],
        as_dict=True
    )

    if not client:
        frappe.throw(_(ERR_INVALID_TOKEN), frappe.AuthenticationError)

    # Verify client secret
    stored_secret = frappe.utils.password.get_decrypted_password(
        "OAuth Client", client.name, "client_secret"
    )

    if stored_secret != client_secret:
        frappe.throw(_(ERR_INVALID_TOKEN), frappe.AuthenticationError)

    customer_id = client.customer
    if not customer_id:
        frappe.throw(_("OAuth client not linked to a customer"), frappe.AuthenticationError)

    _verify_customer_active(customer_id)

    return {"customer_id": customer_id}


def _verify_customer_active(customer_id: str):
    """Verify the customer account is active"""
    customer = frappe.get_doc("WhatsApp Customer", customer_id)
    if customer.status != CUSTOMER_STATUS_ACTIVE:
        frappe.throw(_("Customer account is not active"), frappe.AuthenticationError)
