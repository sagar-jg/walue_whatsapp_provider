"""
Customer Management API

This module handles customer account operations:
- Customer registration and setup
- Feature enable/disable
- Usage summary (aggregated only)
- Account status management
"""

import frappe
from frappe import _
import secrets

from walue_whatsapp_provider.constants import (
    ERR_INVALID_TOKEN,
    ERR_CUSTOMER_NOT_FOUND,
    CUSTOMER_STATUS_ACTIVE,
    CUSTOMER_STATUS_SUSPENDED,
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

    return customer_info


@frappe.whitelist(allow_guest=False, methods=["POST"])
def register():
    """
    Register a new customer

    POST Body (JSON):
        customer_name: Company name
        company_email: Primary contact email
        frappe_site_url: Customer's Frappe site URL
        subscription_plan: Optional plan name

    Returns:
        dict: Customer ID and OAuth credentials
    """
    data = frappe.parse_json(frappe.request.data)

    customer_name = data.get("customer_name")
    company_email = data.get("company_email")
    frappe_site_url = data.get("frappe_site_url")
    subscription_plan = data.get("subscription_plan")

    if not all([customer_name, company_email, frappe_site_url]):
        frappe.throw(_("Missing required fields"))

    # Check if customer already exists
    if frappe.db.exists("WhatsApp Customer", {"company_email": company_email}):
        frappe.throw(_("Customer with this email already exists"))

    # Generate OAuth credentials
    oauth_client_id = secrets.token_urlsafe(24)
    oauth_client_secret = secrets.token_urlsafe(32)

    # Create customer record
    customer = frappe.get_doc({
        "doctype": "WhatsApp Customer",
        "customer_name": customer_name,
        "company_email": company_email,
        "frappe_site_url": frappe_site_url,
        "status": "Pending",
        "oauth_client_id": oauth_client_id,
        "oauth_client_secret": oauth_client_secret,
        "subscription_plan": subscription_plan,
        "billing_cycle": "Monthly",
        "current_balance": 0,
    })
    customer.insert(ignore_permissions=True)

    return {
        "success": True,
        "customer_id": customer.name,
        "oauth_client_id": oauth_client_id,
        "oauth_client_secret": oauth_client_secret,
        "message": "Customer registered. Complete embedded signup to activate.",
    }


@frappe.whitelist(allow_guest=True, methods=["GET"])
def get_info():
    """
    Get customer account information

    Returns:
        dict: Customer info (no sensitive data)
    """
    customer_info = _authenticate_request()
    customer_id = customer_info["customer_id"]

    customer = frappe.get_doc("WhatsApp Customer", customer_id)

    plan_details = None
    if customer.subscription_plan:
        plan = frappe.get_doc("Subscription Plan", customer.subscription_plan)
        plan_details = {
            "name": plan.plan_name,
            "base_fee": plan.base_monthly_fee,
            "features": plan.features_json,
        }

    return {
        "customer_id": customer.name,
        "customer_name": customer.customer_name,
        "status": customer.status,
        "waba_connected": bool(customer.waba_id),
        "waba_id": customer.waba_id,  # Reference only
        "subscription_plan": plan_details,
        "current_balance": customer.current_balance,
        "billing_cycle": customer.billing_cycle,
        "last_sync": customer.last_sync,
    }


@frappe.whitelist(allow_guest=True, methods=["GET"])
def usage_summary():
    """
    Get aggregated usage summary

    Query Parameters:
        period: 'today', 'week', 'month'

    Returns:
        dict: Aggregated counts and costs only
    """
    customer_info = _authenticate_request()
    customer_id = customer_info["customer_id"]

    # Redirect to metrics API
    from walue_whatsapp_provider.api.metrics import get_usage_summary
    return get_usage_summary()


@frappe.whitelist(allow_guest=True, methods=["POST"])
def update_features():
    """
    Update feature flags for customer

    This is an admin-only endpoint for Walue Biz staff

    POST Body (JSON):
        customer_id: Target customer
        calling_enabled: Boolean
        messaging_enabled: Boolean
        recording_enabled: Boolean
    """
    # Check if user has admin role
    if "System Manager" not in frappe.get_roles():
        frappe.throw(_("Not authorized"), frappe.PermissionError)

    data = frappe.parse_json(frappe.request.data)
    customer_id = data.get("customer_id")

    if not customer_id:
        frappe.throw(_("Missing customer_id"))

    if not frappe.db.exists("WhatsApp Customer", customer_id):
        frappe.throw(_(ERR_CUSTOMER_NOT_FOUND))

    customer = frappe.get_doc("WhatsApp Customer", customer_id)

    # Update features (these would be stored in customer doc)
    # For now, features are derived from subscription plan
    # This endpoint would update plan-specific overrides

    return {
        "success": True,
        "message": "Features updated",
    }


@frappe.whitelist(allow_guest=False, methods=["POST"])
def suspend():
    """
    Suspend a customer account (admin only)

    POST Body (JSON):
        customer_id: Customer to suspend
        reason: Suspension reason
    """
    if "System Manager" not in frappe.get_roles():
        frappe.throw(_("Not authorized"), frappe.PermissionError)

    data = frappe.parse_json(frappe.request.data)
    customer_id = data.get("customer_id")
    reason = data.get("reason", "")

    if not customer_id:
        frappe.throw(_("Missing customer_id"))

    customer = frappe.get_doc("WhatsApp Customer", customer_id)
    customer.status = CUSTOMER_STATUS_SUSPENDED
    customer.add_comment("Comment", f"Account suspended: {reason}")
    customer.save(ignore_permissions=True)

    return {
        "success": True,
        "message": f"Customer {customer_id} suspended",
    }


@frappe.whitelist(allow_guest=False, methods=["POST"])
def activate():
    """
    Activate a customer account (admin only)

    POST Body (JSON):
        customer_id: Customer to activate
    """
    if "System Manager" not in frappe.get_roles():
        frappe.throw(_("Not authorized"), frappe.PermissionError)

    data = frappe.parse_json(frappe.request.data)
    customer_id = data.get("customer_id")

    if not customer_id:
        frappe.throw(_("Missing customer_id"))

    customer = frappe.get_doc("WhatsApp Customer", customer_id)
    customer.status = CUSTOMER_STATUS_ACTIVE
    customer.add_comment("Comment", "Account activated")
    customer.save(ignore_permissions=True)

    return {
        "success": True,
        "message": f"Customer {customer_id} activated",
    }
