"""
Authentication and billing utilities for API requests

Supports:
1. Bearer token (OAuth access token)
2. Client credentials (X-Client-ID + X-Client-Secret headers)
3. Meta token extraction from X-Meta-Token header
4. Quota/balance checking and enforcement
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
        dict: Customer info with customer_id and customer doc

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


def get_meta_token() -> str:
    """
    Extract Meta access token from X-Meta-Token header

    Returns:
        str: Meta access token

    Raises:
        frappe.ValidationError: If token not provided
    """
    meta_token = frappe.request.headers.get("X-Meta-Token")
    if not meta_token:
        frappe.throw(_("X-Meta-Token header is required"), frappe.ValidationError)
    return meta_token


def check_quota(customer_id: str, estimated_cost: float = 0.01) -> dict:
    """
    Check if customer has sufficient balance/quota

    Args:
        customer_id: Customer document name
        estimated_cost: Estimated cost of the operation

    Returns:
        dict: {allowed: bool, balance: float, reason: str}

    Note: Does NOT deduct - use deduct_balance() after successful operation
    """
    customer = frappe.get_doc("WhatsApp Customer", customer_id)

    # Get current balance
    balance = customer.current_balance or 0

    # Check if balance is sufficient
    if balance < estimated_cost:
        return {
            "allowed": False,
            "balance": balance,
            "reason": f"Insufficient balance. Current: ${balance:.4f}, Required: ${estimated_cost:.4f}"
        }

    return {
        "allowed": True,
        "balance": balance,
        "reason": None
    }


def enforce_quota(customer_id: str, estimated_cost: float = 0.01):
    """
    Enforce quota - raises exception if insufficient balance

    Args:
        customer_id: Customer document name
        estimated_cost: Estimated cost of the operation

    Raises:
        frappe.ValidationError: If insufficient balance
    """
    result = check_quota(customer_id, estimated_cost)
    if not result["allowed"]:
        frappe.throw(
            _(f"Quota exceeded: {result['reason']}"),
            frappe.ValidationError
        )


def deduct_balance(customer_id: str, amount: float, description: str = None):
    """
    Deduct amount from customer balance (synchronous)

    Args:
        customer_id: Customer document name
        amount: Amount to deduct
        description: Optional description for audit

    Returns:
        float: New balance after deduction
    """
    if amount <= 0:
        return

    # Use SQL for atomic update
    frappe.db.sql("""
        UPDATE `tabWhatsApp Customer`
        SET current_balance = current_balance - %s
        WHERE name = %s
    """, (amount, customer_id))

    # Get new balance
    new_balance = frappe.db.get_value("WhatsApp Customer", customer_id, "current_balance")

    frappe.logger().info(
        f"[Billing] Deducted ${amount:.4f} from {customer_id}. "
        f"New balance: ${new_balance:.4f}. {description or ''}"
    )

    return new_balance


def add_balance(customer_id: str, amount: float, description: str = None):
    """
    Add amount to customer balance (for payments/credits)

    Args:
        customer_id: Customer document name
        amount: Amount to add
        description: Optional description for audit

    Returns:
        float: New balance after addition
    """
    if amount <= 0:
        return

    frappe.db.sql("""
        UPDATE `tabWhatsApp Customer`
        SET current_balance = current_balance + %s
        WHERE name = %s
    """, (amount, customer_id))

    new_balance = frappe.db.get_value("WhatsApp Customer", customer_id, "current_balance")

    frappe.logger().info(
        f"[Billing] Added ${amount:.4f} to {customer_id}. "
        f"New balance: ${new_balance:.4f}. {description or ''}"
    )

    return new_balance


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
    """Authenticate using OAuth client credentials from WhatsApp Customer"""
    # Find customer by oauth_client_id
    customer = frappe.db.get_value(
        "WhatsApp Customer",
        {"oauth_client_id": client_id},
        ["name", "oauth_client_secret", "status"],
        as_dict=True
    )

    if not customer:
        frappe.throw(_(ERR_INVALID_TOKEN), frappe.AuthenticationError)

    # Verify client secret
    stored_secret = frappe.utils.password.get_decrypted_password(
        "WhatsApp Customer", customer.name, "oauth_client_secret"
    )

    if stored_secret != client_secret:
        frappe.throw(_(ERR_INVALID_TOKEN), frappe.AuthenticationError)

    _verify_customer_active(customer.name)

    return {"customer_id": customer.name}


def _verify_customer_active(customer_id: str):
    """Verify the customer account is active"""
    status = frappe.db.get_value("WhatsApp Customer", customer_id, "status")
    if status != CUSTOMER_STATUS_ACTIVE:
        frappe.throw(_("Customer account is not active"), frappe.AuthenticationError)
