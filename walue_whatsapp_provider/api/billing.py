"""
Billing Enforcement API

This module provides centralized billing and quota enforcement functions
that must be called BEFORE any Meta API requests to ensure:
1. Customer has sufficient balance
2. Customer hasn't exceeded quota limits
3. Rate limits are respected

CRITICAL: These checks must happen BEFORE the Meta API call,
not after, to prevent revenue loss.
"""

import frappe
from frappe import _
from datetime import date, datetime, timedelta
from typing import Optional

from walue_whatsapp_provider.constants import (
    ERR_INSUFFICIENT_BALANCE,
    MSG_QUOTA_EXCEEDED,
    USAGE_WARNING_THRESHOLD,
    USAGE_ALERT_THRESHOLD,
    DEFAULT_CALL_MARKUP_PERCENTAGE,
    DEFAULT_MESSAGE_MARKUP_PERCENTAGE,
)


class InsufficientBalanceError(frappe.ValidationError):
    """Raised when customer has insufficient balance"""
    http_status_code = 402  # Payment Required


class QuotaExceededError(frappe.ValidationError):
    """Raised when customer has exceeded their quota"""
    http_status_code = 402  # Payment Required


# =============================================================================
# COST ESTIMATION FUNCTIONS
# =============================================================================

def estimate_message_cost(customer_id: str, message_type: str = "template") -> float:
    """
    Estimate cost of a message before sending

    Args:
        customer_id: Customer identifier
        message_type: 'template', 'text', or 'media'

    Returns:
        float: Estimated cost in USD
    """
    customer = frappe.get_doc("WhatsApp Customer", customer_id)

    # Base rates (simplified - actual implementation needs rate cards)
    base_rates = {
        "template": 0.005,  # $0.005 per template message
        "text": 0.0,        # Free within conversation window
        "media": 0.0,       # Free within conversation window
    }

    base_cost = base_rates.get(message_type, 0.005)

    # Apply markup from subscription plan
    markup_percentage = DEFAULT_MESSAGE_MARKUP_PERCENTAGE / 100
    if customer.subscription_plan:
        plan = frappe.get_doc("Subscription Plan", customer.subscription_plan)
        markup_percentage = (plan.message_markup_percentage or DEFAULT_MESSAGE_MARKUP_PERCENTAGE) / 100

    total_cost = base_cost * (1 + markup_percentage)
    return round(total_cost, 4)


def estimate_call_cost(customer_id: str, estimated_minutes: float = 1.0) -> float:
    """
    Estimate cost of a call before initiating

    Args:
        customer_id: Customer identifier
        estimated_minutes: Expected call duration (default 1 minute for pre-check)

    Returns:
        float: Estimated cost in USD
    """
    customer = frappe.get_doc("WhatsApp Customer", customer_id)

    # Base rate (simplified - actual implementation needs rate cards by country)
    base_rate_per_minute = 0.03  # $0.03 USD per minute

    base_cost = estimated_minutes * base_rate_per_minute

    # Apply markup from subscription plan
    markup_percentage = DEFAULT_CALL_MARKUP_PERCENTAGE / 100
    if customer.subscription_plan:
        plan = frappe.get_doc("Subscription Plan", customer.subscription_plan)
        markup_percentage = (plan.call_markup_percentage or DEFAULT_CALL_MARKUP_PERCENTAGE) / 100

    total_cost = base_cost * (1 + markup_percentage)
    return round(total_cost, 4)


# =============================================================================
# BALANCE CHECK FUNCTIONS
# =============================================================================

def check_sufficient_balance(customer_id: str, estimated_cost: float) -> dict:
    """
    Check if customer has sufficient balance for the operation

    Args:
        customer_id: Customer identifier
        estimated_cost: Estimated cost of the operation

    Returns:
        dict: {
            'sufficient': bool,
            'current_balance': float,
            'estimated_cost': float,
            'remaining_after': float,
            'quota_status': str ('ok', 'warning', 'critical')
        }
    """
    customer = frappe.get_doc("WhatsApp Customer", customer_id)
    current_balance = customer.current_balance or 0

    # Get current month's usage
    today = date.today()
    month_start = today.replace(day=1)

    monthly_usage = frappe.db.sql("""
        SELECT COALESCE(SUM(total_revenue), 0) as total
        FROM `tabDaily Usage Metrics`
        WHERE customer = %s AND date >= %s
    """, (customer_id, month_start), as_dict=True)[0]

    total_used = monthly_usage.get("total") or 0
    remaining_balance = current_balance - total_used
    remaining_after = remaining_balance - estimated_cost

    # Calculate quota status
    quota_status = "ok"
    if current_balance > 0:
        projected_usage_ratio = (total_used + estimated_cost) / current_balance
        if projected_usage_ratio >= 1.0:
            quota_status = "exceeded"
        elif projected_usage_ratio >= USAGE_ALERT_THRESHOLD:
            quota_status = "critical"
        elif projected_usage_ratio >= USAGE_WARNING_THRESHOLD:
            quota_status = "warning"

    return {
        "sufficient": remaining_after >= 0,
        "current_balance": current_balance,
        "total_used": total_used,
        "estimated_cost": estimated_cost,
        "remaining_balance": remaining_balance,
        "remaining_after": remaining_after,
        "quota_status": quota_status,
    }


def get_customer_limits(customer_id: str) -> dict:
    """
    Get customer's quota limits from subscription plan

    Args:
        customer_id: Customer identifier

    Returns:
        dict: {
            'message_limit': int (0 = unlimited),
            'call_minute_limit': int (0 = unlimited),
            'daily_message_limit': int (0 = unlimited),
            'daily_call_limit': int (0 = unlimited),
            'current_month_messages': int,
            'current_month_calls': int,
            'current_month_call_minutes': float,
            'today_messages': int,
            'today_calls': int,
        }
    """
    customer = frappe.get_doc("WhatsApp Customer", customer_id)
    today = date.today()
    month_start = today.replace(day=1)

    # Get plan limits (default to 0 = unlimited if fields don't exist)
    limits = {
        "message_limit": 0,
        "call_minute_limit": 0,
        "daily_message_limit": 0,
        "daily_call_limit": 0,
    }

    if customer.subscription_plan:
        plan = frappe.get_doc("Subscription Plan", customer.subscription_plan)
        # These fields may not exist yet - will be added in Phase 2
        limits["message_limit"] = getattr(plan, "message_limit", 0) or 0
        limits["call_minute_limit"] = getattr(plan, "call_minute_limit", 0) or 0
        limits["daily_message_limit"] = getattr(plan, "daily_message_limit", 0) or 0
        limits["daily_call_limit"] = getattr(plan, "daily_call_limit", 0) or 0

    # Get current month usage
    monthly_usage = frappe.db.sql("""
        SELECT
            COALESCE(SUM(total_messages), 0) as messages,
            COALESCE(SUM(total_calls), 0) as calls,
            COALESCE(SUM(total_call_minutes), 0) as call_minutes
        FROM `tabDaily Usage Metrics`
        WHERE customer = %s AND date >= %s
    """, (customer_id, month_start), as_dict=True)[0]

    # Get today's usage
    daily_usage = frappe.db.sql("""
        SELECT
            COALESCE(SUM(total_messages), 0) as messages,
            COALESCE(SUM(total_calls), 0) as calls
        FROM `tabDaily Usage Metrics`
        WHERE customer = %s AND date = %s
    """, (customer_id, today), as_dict=True)[0]

    limits["current_month_messages"] = monthly_usage.get("messages") or 0
    limits["current_month_calls"] = monthly_usage.get("calls") or 0
    limits["current_month_call_minutes"] = monthly_usage.get("call_minutes") or 0
    limits["today_messages"] = daily_usage.get("messages") or 0
    limits["today_calls"] = daily_usage.get("calls") or 0

    return limits


# =============================================================================
# MAIN ENFORCEMENT FUNCTION
# =============================================================================

def enforce_quota(customer_id: str, operation_type: str, estimated_cost: float = None) -> dict:
    """
    Main function to enforce quota and balance checks BEFORE an operation

    This function MUST be called before any Meta API request to ensure
    the customer has sufficient balance and hasn't exceeded quotas.

    Args:
        customer_id: Customer identifier
        operation_type: 'message' or 'call'
        estimated_cost: Pre-calculated cost (optional, will be estimated if not provided)

    Returns:
        dict: {
            'allowed': bool,
            'reason': str (if not allowed),
            'balance_info': dict,
            'limits': dict,
        }

    Raises:
        InsufficientBalanceError: If balance is insufficient
        QuotaExceededError: If quota is exceeded
    """
    # Estimate cost if not provided
    if estimated_cost is None:
        if operation_type == "message":
            estimated_cost = estimate_message_cost(customer_id)
        else:
            estimated_cost = estimate_call_cost(customer_id)

    # Check balance
    balance_info = check_sufficient_balance(customer_id, estimated_cost)

    if not balance_info["sufficient"]:
        frappe.throw(
            _(ERR_INSUFFICIENT_BALANCE),
            InsufficientBalanceError,
        )

    # Check quota limits
    limits = get_customer_limits(customer_id)

    # Check monthly message limit
    if operation_type == "message" and limits["message_limit"] > 0:
        if limits["current_month_messages"] >= limits["message_limit"]:
            frappe.throw(
                _("Monthly message limit of {0} exceeded").format(limits["message_limit"]),
                QuotaExceededError,
            )

    # Check daily message limit
    if operation_type == "message" and limits["daily_message_limit"] > 0:
        if limits["today_messages"] >= limits["daily_message_limit"]:
            frappe.throw(
                _("Daily message limit of {0} exceeded. Please try again tomorrow.").format(
                    limits["daily_message_limit"]
                ),
                QuotaExceededError,
            )

    # Check monthly call minute limit
    if operation_type == "call" and limits["call_minute_limit"] > 0:
        if limits["current_month_call_minutes"] >= limits["call_minute_limit"]:
            frappe.throw(
                _("Monthly call minute limit of {0} exceeded").format(limits["call_minute_limit"]),
                QuotaExceededError,
            )

    # Check daily call limit
    if operation_type == "call" and limits["daily_call_limit"] > 0:
        if limits["today_calls"] >= limits["daily_call_limit"]:
            frappe.throw(
                _("Daily call limit of {0} exceeded. Please try again tomorrow.").format(
                    limits["daily_call_limit"]
                ),
                QuotaExceededError,
            )

    return {
        "allowed": True,
        "balance_info": balance_info,
        "limits": limits,
        "estimated_cost": estimated_cost,
    }


# =============================================================================
# BALANCE DEDUCTION FUNCTIONS
# =============================================================================

def deduct_balance(customer_id: str, amount: float, description: str = None) -> dict:
    """
    Deduct amount from customer's current balance

    This should be called AFTER a successful Meta API call to record
    the actual cost.

    Args:
        customer_id: Customer identifier
        amount: Amount to deduct
        description: Optional description for the transaction

    Returns:
        dict: {
            'success': bool,
            'previous_balance': float,
            'deducted': float,
            'new_balance': float,
        }
    """
    customer = frappe.get_doc("WhatsApp Customer", customer_id)
    previous_balance = customer.current_balance or 0

    # Update balance
    new_balance = previous_balance - amount
    frappe.db.set_value(
        "WhatsApp Customer",
        customer_id,
        "current_balance",
        new_balance,
        update_modified=True
    )

    frappe.db.commit()

    return {
        "success": True,
        "previous_balance": previous_balance,
        "deducted": amount,
        "new_balance": new_balance,
    }


def add_balance(customer_id: str, amount: float, description: str = None) -> dict:
    """
    Add amount to customer's current balance (for top-ups/payments)

    Args:
        customer_id: Customer identifier
        amount: Amount to add
        description: Optional description for the transaction

    Returns:
        dict: {
            'success': bool,
            'previous_balance': float,
            'added': float,
            'new_balance': float,
        }
    """
    customer = frappe.get_doc("WhatsApp Customer", customer_id)
    previous_balance = customer.current_balance or 0

    new_balance = previous_balance + amount
    frappe.db.set_value(
        "WhatsApp Customer",
        customer_id,
        "current_balance",
        new_balance,
        update_modified=True
    )

    frappe.db.commit()

    return {
        "success": True,
        "previous_balance": previous_balance,
        "added": amount,
        "new_balance": new_balance,
    }


# =============================================================================
# BALANCE ALERT FUNCTIONS
# =============================================================================

def check_and_send_balance_alerts(customer_id: str) -> Optional[dict]:
    """
    Check balance status and send alerts if thresholds are crossed

    Sends email alerts at:
    - 75% usage (warning)
    - 90% usage (critical)
    - 100% usage (suspended)

    Args:
        customer_id: Customer identifier

    Returns:
        dict: Alert info if sent, None otherwise
    """
    customer = frappe.get_doc("WhatsApp Customer", customer_id)
    current_balance = customer.current_balance or 0

    if current_balance <= 0:
        return None

    # Get current month's usage
    today = date.today()
    month_start = today.replace(day=1)

    monthly_usage = frappe.db.sql("""
        SELECT COALESCE(SUM(total_revenue), 0) as total
        FROM `tabDaily Usage Metrics`
        WHERE customer = %s AND date >= %s
    """, (customer_id, month_start), as_dict=True)[0]

    total_used = monthly_usage.get("total") or 0
    usage_ratio = total_used / current_balance

    alert_type = None
    if usage_ratio >= 1.0:
        alert_type = "suspended"
    elif usage_ratio >= USAGE_ALERT_THRESHOLD:
        alert_type = "critical"
    elif usage_ratio >= USAGE_WARNING_THRESHOLD:
        alert_type = "warning"

    if not alert_type:
        return None

    # Check if we already sent this alert today
    cache_key = f"balance_alert:{customer_id}:{alert_type}:{today.isoformat()}"
    if frappe.cache().get_value(cache_key):
        return None  # Already sent today

    # Send alert email
    _send_balance_alert_email(customer, alert_type, usage_ratio, total_used, current_balance)

    # Mark as sent today
    frappe.cache().set_value(cache_key, True, expires_in_sec=86400)

    return {
        "alert_type": alert_type,
        "usage_ratio": usage_ratio,
        "total_used": total_used,
        "current_balance": current_balance,
    }


def _send_balance_alert_email(customer, alert_type: str, usage_ratio: float,
                               total_used: float, current_balance: float):
    """Send balance alert email to customer"""
    try:
        subject_map = {
            "warning": "Balance Warning - 75% Usage Reached",
            "critical": "Critical Balance Alert - 90% Usage Reached",
            "suspended": "Account Suspended - Balance Exhausted",
        }

        message_map = {
            "warning": f"""
                <p>Your WhatsApp Business account has used {usage_ratio*100:.0f}% of your balance.</p>
                <p>Current Balance: ${current_balance:.2f}</p>
                <p>Used This Month: ${total_used:.2f}</p>
                <p>Please top up your account to avoid service interruption.</p>
            """,
            "critical": f"""
                <p><strong>CRITICAL:</strong> Your account has used {usage_ratio*100:.0f}% of your balance.</p>
                <p>Current Balance: ${current_balance:.2f}</p>
                <p>Used This Month: ${total_used:.2f}</p>
                <p>Your service will be suspended soon if you don't add funds.</p>
            """,
            "suspended": f"""
                <p><strong>Your account has been suspended due to insufficient balance.</strong></p>
                <p>Current Balance: ${current_balance:.2f}</p>
                <p>Used This Month: ${total_used:.2f}</p>
                <p>Please add funds immediately to restore service.</p>
            """,
        }

        frappe.sendmail(
            recipients=[customer.company_email],
            subject=subject_map.get(alert_type, "Balance Alert"),
            message=message_map.get(alert_type, "Please check your account balance."),
        )

    except Exception as e:
        frappe.log_error(f"Failed to send balance alert email: {str(e)}", "Balance Alert Email")


# =============================================================================
# RESPONSE HEADERS HELPER
# =============================================================================

def get_billing_response_headers(customer_id: str) -> dict:
    """
    Get billing info to include in response headers

    Args:
        customer_id: Customer identifier

    Returns:
        dict: Headers to add to response
    """
    balance_info = check_sufficient_balance(customer_id, 0)

    return {
        "X-Walue-Balance": str(round(balance_info["remaining_balance"], 2)),
        "X-Walue-Quota-Status": balance_info["quota_status"],
    }
