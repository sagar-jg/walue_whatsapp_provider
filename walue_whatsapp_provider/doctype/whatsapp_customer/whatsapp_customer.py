"""
WhatsApp Customer DocType

Represents a customer account on the WhatsApp Provider platform.
Stores customer info, OAuth credentials, and billing details.

IMPORTANT: We only store WABA ID as reference. Actual credentials
are stored in the customer's own Frappe instance.
"""

import frappe
from frappe.model.document import Document
import secrets


class WhatsAppCustomer(Document):
    def before_insert(self):
        """Generate OAuth credentials before insert"""
        if not self.oauth_client_id:
            self.oauth_client_id = secrets.token_urlsafe(24)
        if not self.oauth_client_secret:
            self.oauth_client_secret = secrets.token_urlsafe(32)
        if not self.created_date:
            self.created_date = frappe.utils.today()

    def validate(self):
        """Validate customer data"""
        # Validate email format
        if self.company_email and not frappe.utils.validate_email_address(self.company_email):
            frappe.throw("Invalid email address")

        # Validate URL format
        if self.frappe_site_url:
            if not self.frappe_site_url.startswith(("http://", "https://")):
                frappe.throw("Frappe Site URL must start with http:// or https://")
            # Remove trailing slash
            self.frappe_site_url = self.frappe_site_url.rstrip("/")

    def on_update(self):
        """Actions after customer update"""
        # If status changed to Active, update last_sync
        if self.has_value_changed("status") and self.status == "Active":
            self.db_set("last_sync", frappe.utils.now())

    def regenerate_oauth_secret(self):
        """Regenerate OAuth client secret"""
        self.oauth_client_secret = secrets.token_urlsafe(32)
        self.save(ignore_permissions=True)
        return True

    def get_current_month_usage(self):
        """Get usage metrics for current month"""
        from datetime import date

        today = date.today()
        month_start = today.replace(day=1)

        usage = frappe.db.sql("""
            SELECT
                SUM(total_calls) as total_calls,
                SUM(total_call_minutes) as total_minutes,
                SUM(total_messages) as total_messages,
                SUM(total_revenue) as total_cost
            FROM `tabDaily Usage Metrics`
            WHERE customer = %s AND date >= %s
        """, (self.name, month_start), as_dict=True)[0]

        return {
            "calls": usage.get("total_calls") or 0,
            "minutes": usage.get("total_minutes") or 0,
            "messages": usage.get("total_messages") or 0,
            "cost": usage.get("total_cost") or 0,
        }
