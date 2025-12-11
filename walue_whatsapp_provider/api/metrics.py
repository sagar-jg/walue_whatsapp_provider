"""
Metrics Collection API

This module receives aggregated usage metrics from customer apps.
CRITICAL: We only accept and store aggregated counts and costs.

Customer app sends:
- Total call count for period
- Total message count for period
- Total duration
- Calculated costs

We do NOT accept or store:
- Individual call/message IDs
- Phone numbers
- Lead information
- Message content
"""

import frappe
from frappe import _
from datetime import date, datetime, timedelta

from walue_whatsapp_provider.constants import (
    ERR_INVALID_TOKEN,
    CUSTOMER_STATUS_ACTIVE,
    USAGE_WARNING_THRESHOLD,
    USAGE_ALERT_THRESHOLD,
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


@frappe.whitelist(allow_guest=True, methods=["POST"])
def report_usage():
    """
    Receive usage metrics from customer app

    POST Body (JSON):
        usage_type: 'call' or 'message'
        count: Number of calls/messages
        duration_minutes: Total duration (for calls)
        cost: Total cost

    Returns:
        dict: Updated balance and quota status
    """
    customer_info = _authenticate_request()

    data = frappe.parse_json(frappe.request.data)

    usage_type = data.get("usage_type")
    count = data.get("count", 0)
    duration_minutes = data.get("duration_minutes", 0)
    cost = data.get("cost", 0)

    if usage_type not in ["call", "message"]:
        frappe.throw(_("Invalid usage_type"))

    customer_id = customer_info["customer_id"]
    today = date.today()

    # Get or create daily metrics
    existing = frappe.db.get_value(
        "Daily Usage Metrics",
        {"customer": customer_id, "date": today},
        "name"
    )

    if usage_type == "call":
        if existing:
            frappe.db.sql("""
                UPDATE `tabDaily Usage Metrics`
                SET
                    total_calls = total_calls + %s,
                    total_call_minutes = total_call_minutes + %s,
                    total_call_cost = total_call_cost + %s
                WHERE name = %s
            """, (count, duration_minutes, cost, existing))
        else:
            frappe.get_doc({
                "doctype": "Daily Usage Metrics",
                "customer": customer_id,
                "date": today,
                "total_calls": count,
                "total_call_minutes": duration_minutes,
                "total_messages": 0,
                "total_call_cost": cost,
                "total_message_cost": 0,
            }).insert(ignore_permissions=True)

    elif usage_type == "message":
        if existing:
            frappe.db.sql("""
                UPDATE `tabDaily Usage Metrics`
                SET
                    total_messages = total_messages + %s,
                    total_message_cost = total_message_cost + %s
                WHERE name = %s
            """, (count, cost, existing))
        else:
            frappe.get_doc({
                "doctype": "Daily Usage Metrics",
                "customer": customer_id,
                "date": today,
                "total_calls": 0,
                "total_call_minutes": 0,
                "total_messages": count,
                "total_call_cost": 0,
                "total_message_cost": cost,
            }).insert(ignore_permissions=True)

    frappe.db.commit()

    # Get updated balance and quota info
    balance_info = _get_balance_info(customer_id)

    return {
        "success": True,
        "balance": balance_info["balance"],
        "quota_status": balance_info["quota_status"],
        "alerts": balance_info.get("alerts", []),
    }


@frappe.whitelist(allow_guest=True, methods=["GET"])
def get_usage_summary():
    """
    Get aggregated usage summary for customer

    Query Parameters:
        period: 'today', 'week', 'month', 'custom'
        start_date: For custom period (YYYY-MM-DD)
        end_date: For custom period (YYYY-MM-DD)

    Returns:
        dict: Aggregated usage metrics
    """
    customer_info = _authenticate_request()
    customer_id = customer_info["customer_id"]

    period = frappe.form_dict.get("period", "month")
    start_date = frappe.form_dict.get("start_date")
    end_date = frappe.form_dict.get("end_date")

    # Calculate date range
    today = date.today()

    if period == "today":
        start = end = today
    elif period == "week":
        start = today - timedelta(days=7)
        end = today
    elif period == "month":
        start = today.replace(day=1)
        end = today
    elif period == "custom" and start_date and end_date:
        start = datetime.strptime(start_date, "%Y-%m-%d").date()
        end = datetime.strptime(end_date, "%Y-%m-%d").date()
    else:
        start = today.replace(day=1)
        end = today

    # Get aggregated metrics
    metrics = frappe.db.sql("""
        SELECT
            SUM(total_calls) as total_calls,
            SUM(total_call_minutes) as total_call_minutes,
            SUM(total_messages) as total_messages,
            SUM(total_call_cost) as total_call_cost,
            SUM(total_message_cost) as total_message_cost,
            SUM(total_markup) as total_markup,
            SUM(total_revenue) as total_revenue
        FROM `tabDaily Usage Metrics`
        WHERE customer = %s
        AND date BETWEEN %s AND %s
    """, (customer_id, start, end), as_dict=True)[0]

    # Get daily breakdown for charts
    daily_data = frappe.db.sql("""
        SELECT
            date,
            total_calls,
            total_call_minutes,
            total_messages,
            total_revenue
        FROM `tabDaily Usage Metrics`
        WHERE customer = %s
        AND date BETWEEN %s AND %s
        ORDER BY date
    """, (customer_id, start, end), as_dict=True)

    return {
        "period": {
            "start": start.isoformat(),
            "end": end.isoformat(),
        },
        "summary": {
            "total_calls": metrics.get("total_calls") or 0,
            "total_call_minutes": round(metrics.get("total_call_minutes") or 0, 2),
            "total_messages": metrics.get("total_messages") or 0,
            "total_cost": round(
                (metrics.get("total_call_cost") or 0) +
                (metrics.get("total_message_cost") or 0), 2
            ),
            "total_revenue": round(metrics.get("total_revenue") or 0, 2),
        },
        "daily": daily_data,
    }


@frappe.whitelist(allow_guest=True, methods=["GET"])
def get_billing_info():
    """
    Get current billing information for customer

    Returns:
        dict: Current balance, subscription info, upcoming charges
    """
    customer_info = _authenticate_request()
    customer_id = customer_info["customer_id"]

    customer = frappe.get_doc("WhatsApp Customer", customer_id)

    # Get current month usage
    today = date.today()
    month_start = today.replace(day=1)

    monthly_usage = frappe.db.sql("""
        SELECT
            SUM(total_revenue) as total_charges,
            SUM(total_calls) as total_calls,
            SUM(total_messages) as total_messages
        FROM `tabDaily Usage Metrics`
        WHERE customer = %s
        AND date >= %s
    """, (customer_id, month_start), as_dict=True)[0]

    # Get subscription plan details
    plan_details = None
    if customer.subscription_plan:
        plan = frappe.get_doc("Subscription Plan", customer.subscription_plan)
        plan_details = {
            "name": plan.plan_name,
            "base_fee": plan.base_monthly_fee,
            "call_markup": plan.call_markup_percentage,
            "message_markup": plan.message_markup_percentage,
        }

    return {
        "customer_id": customer_id,
        "status": customer.status,
        "current_balance": customer.current_balance or 0,
        "subscription_plan": plan_details,
        "current_month_charges": round(monthly_usage.get("total_charges") or 0, 2),
        "current_month_calls": monthly_usage.get("total_calls") or 0,
        "current_month_messages": monthly_usage.get("total_messages") or 0,
        "billing_cycle": customer.billing_cycle,
    }


def _get_balance_info(customer_id: str) -> dict:
    """Get customer balance and quota status"""
    customer = frappe.get_doc("WhatsApp Customer", customer_id)

    # Get current month usage
    today = date.today()
    month_start = today.replace(day=1)

    monthly_usage = frappe.db.sql("""
        SELECT SUM(total_revenue) as total
        FROM `tabDaily Usage Metrics`
        WHERE customer = %s AND date >= %s
    """, (customer_id, month_start), as_dict=True)[0]

    total_usage = monthly_usage.get("total") or 0
    balance = customer.current_balance or 0

    # Calculate quota status
    alerts = []
    quota_status = "ok"

    if balance > 0:
        usage_ratio = total_usage / balance

        if usage_ratio >= USAGE_ALERT_THRESHOLD:
            quota_status = "critical"
            alerts.append({
                "type": "quota_critical",
                "message": f"You've used {usage_ratio*100:.0f}% of your balance",
            })
        elif usage_ratio >= USAGE_WARNING_THRESHOLD:
            quota_status = "warning"
            alerts.append({
                "type": "quota_warning",
                "message": f"You've used {usage_ratio*100:.0f}% of your balance",
            })

    return {
        "balance": balance,
        "total_usage": total_usage,
        "quota_status": quota_status,
        "alerts": alerts,
    }
