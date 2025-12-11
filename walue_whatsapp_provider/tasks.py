"""
Scheduled Tasks for Walue WhatsApp Provider

These tasks run on schedule as defined in hooks.py:
- Hourly: Usage aggregation
- Daily: Cleanup old data
- Monthly: Generate invoices
"""

import frappe
from frappe import _
from datetime import date, datetime, timedelta

from walue_whatsapp_provider.constants import (
    USAGE_METRICS_RETENTION_DAYS,
    BILLING_RECORDS_RETENTION_YEARS,
    SESSION_CLEANUP_HOURS,
)


def aggregate_usage_metrics():
    """
    Hourly task: Aggregate usage metrics

    - Consolidates daily metrics
    - Calculates running costs
    - Triggers quota alerts if needed
    """
    frappe.logger().info("Starting usage metrics aggregation")

    try:
        today = date.today()
        current_month = today.strftime("%Y-%m")

        # Get all active customers
        customers = frappe.get_all(
            "WhatsApp Customer",
            filters={"status": "Active"},
            pluck="name"
        )

        for customer_id in customers:
            _aggregate_customer_metrics(customer_id, current_month)

        frappe.logger().info(f"Aggregated metrics for {len(customers)} customers")

    except Exception as e:
        frappe.log_error(f"Usage aggregation failed: {str(e)}")


def _aggregate_customer_metrics(customer_id: str, month: str):
    """Aggregate metrics for a single customer"""
    # Get month start and end dates
    year, month_num = map(int, month.split("-"))
    month_start = date(year, month_num, 1)

    if month_num == 12:
        month_end = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        month_end = date(year, month_num + 1, 1) - timedelta(days=1)

    # Aggregate daily metrics
    aggregated = frappe.db.sql("""
        SELECT
            SUM(total_calls) as total_calls,
            SUM(total_call_minutes) as total_call_minutes,
            SUM(total_messages) as total_messages,
            SUM(total_call_cost) as base_call_cost,
            SUM(total_message_cost) as base_message_cost,
            SUM(total_markup) as total_markup,
            SUM(total_revenue) as total_revenue
        FROM `tabDaily Usage Metrics`
        WHERE customer = %s
        AND date BETWEEN %s AND %s
    """, (customer_id, month_start, month_end), as_dict=True)[0]

    # Get or create monthly summary
    existing = frappe.db.get_value(
        "Monthly Usage Summary",
        {"customer": customer_id, "month": month},
        "name"
    )

    # Get customer's base fee
    customer = frappe.get_doc("WhatsApp Customer", customer_id)
    base_fee = 0

    if customer.subscription_plan:
        plan = frappe.get_doc("Subscription Plan", customer.subscription_plan)
        base_fee = plan.base_monthly_fee or 0

    usage_charges = (aggregated.get("total_revenue") or 0)
    total_amount = base_fee + usage_charges

    if existing:
        frappe.db.set_value("Monthly Usage Summary", existing, {
            "total_calls": aggregated.get("total_calls") or 0,
            "total_call_minutes": aggregated.get("total_call_minutes") or 0,
            "total_messages": aggregated.get("total_messages") or 0,
            "base_fee": base_fee,
            "usage_charges": usage_charges,
            "total_amount": total_amount,
        })
    else:
        frappe.get_doc({
            "doctype": "Monthly Usage Summary",
            "customer": customer_id,
            "month": month,
            "total_calls": aggregated.get("total_calls") or 0,
            "total_call_minutes": aggregated.get("total_call_minutes") or 0,
            "total_messages": aggregated.get("total_messages") or 0,
            "base_fee": base_fee,
            "usage_charges": usage_charges,
            "total_amount": total_amount,
            "invoice_generated": 0,
        }).insert(ignore_permissions=True)

    frappe.db.commit()


def cleanup_old_data():
    """
    Daily task: Clean up old data

    - Archive metrics older than retention period
    - Remove expired sessions
    - Clean up temporary data
    """
    frappe.logger().info("Starting daily cleanup")

    try:
        # Clean up old usage metrics
        _cleanup_old_metrics()

        # Clean up expired sessions
        _cleanup_expired_sessions()

        # Clean up old error logs
        _cleanup_old_logs()

        frappe.logger().info("Daily cleanup completed")

    except Exception as e:
        frappe.log_error(f"Daily cleanup failed: {str(e)}")


def _cleanup_old_metrics():
    """Delete metrics older than retention period"""
    cutoff_date = date.today() - timedelta(days=USAGE_METRICS_RETENTION_DAYS)

    deleted = frappe.db.sql("""
        DELETE FROM `tabDaily Usage Metrics`
        WHERE date < %s
    """, (cutoff_date,))

    frappe.db.commit()
    frappe.logger().info(f"Cleaned up metrics older than {cutoff_date}")


def _cleanup_expired_sessions():
    """Remove expired embedded signup sessions"""
    cutoff_time = datetime.now() - timedelta(hours=SESSION_CLEANUP_HOURS)

    frappe.db.sql("""
        DELETE FROM `tabEmbedded Signup Session`
        WHERE created_at < %s
        AND status IN ('initiated', 'in_progress', 'failed')
    """, (cutoff_time,))

    frappe.db.commit()


def _cleanup_old_logs():
    """Clean up old error logs"""
    from frappe.core.doctype.error_log.error_log import clear_error_logs

    # Clear error logs older than 7 days
    clear_error_logs()


def generate_monthly_invoices():
    """
    Monthly task: Generate invoices for all active customers

    Runs on the 1st of each month for the previous month
    """
    frappe.logger().info("Starting monthly invoice generation")

    try:
        # Get previous month
        today = date.today()
        if today.month == 1:
            prev_month = date(today.year - 1, 12, 1)
        else:
            prev_month = date(today.year, today.month - 1, 1)

        month_str = prev_month.strftime("%Y-%m")

        # Get all monthly summaries that need invoicing
        summaries = frappe.get_all(
            "Monthly Usage Summary",
            filters={
                "month": month_str,
                "invoice_generated": 0
            },
            fields=["name", "customer", "total_amount", "base_fee", "usage_charges",
                    "total_calls", "total_messages"]
        )

        for summary in summaries:
            _generate_customer_invoice(summary, prev_month)

        frappe.logger().info(f"Generated {len(summaries)} invoices for {month_str}")

    except Exception as e:
        frappe.log_error(f"Invoice generation failed: {str(e)}")


def _generate_customer_invoice(summary: dict, month_date: date):
    """Generate invoice for a single customer"""
    # Calculate invoice period
    if month_date.month == 12:
        period_end = date(month_date.year + 1, 1, 1) - timedelta(days=1)
    else:
        period_end = date(month_date.year, month_date.month + 1, 1) - timedelta(days=1)

    # Create invoice
    invoice = frappe.get_doc({
        "doctype": "Customer Invoice",
        "customer": summary["customer"],
        "invoice_period_start": month_date,
        "invoice_period_end": period_end,
        "base_fee": summary["base_fee"],
        "call_charges": 0,  # Calculated from usage
        "message_charges": 0,  # Calculated from usage
        "total_calls": summary["total_calls"],
        "total_messages": summary["total_messages"],
        "total_amount": summary["total_amount"],
        "invoice_status": "Draft",
    })
    invoice.insert(ignore_permissions=True)

    # Mark summary as invoiced
    frappe.db.set_value(
        "Monthly Usage Summary",
        summary["name"],
        "invoice_generated",
        1
    )

    frappe.db.commit()

    # Send invoice notification
    _send_invoice_notification(invoice)


def _send_invoice_notification(invoice):
    """Send invoice notification email to customer"""
    customer = frappe.get_doc("WhatsApp Customer", invoice.customer)

    if not customer.company_email:
        return

    try:
        frappe.sendmail(
            recipients=[customer.company_email],
            subject=f"Walue WhatsApp - Invoice for {invoice.invoice_period_start.strftime('%B %Y')}",
            message=f"""
Dear {customer.customer_name},

Your invoice for {invoice.invoice_period_start.strftime('%B %Y')} is ready.

Invoice Summary:
- Base Fee: ${invoice.base_fee:.2f}
- Usage Charges: ${(invoice.total_amount - invoice.base_fee):.2f}
- Total Calls: {invoice.total_calls}
- Total Messages: {invoice.total_messages}
- Total Amount: ${invoice.total_amount:.2f}

Please log in to your dashboard to view the full invoice and make payment.

Best regards,
Walue Biz Team
            """,
            delayed=False
        )
    except Exception as e:
        frappe.log_error(f"Failed to send invoice email to {customer.company_email}: {str(e)}")
