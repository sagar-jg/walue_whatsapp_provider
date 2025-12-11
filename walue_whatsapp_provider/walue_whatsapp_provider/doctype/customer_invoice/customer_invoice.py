"""
Customer Invoice DocType

Monthly invoice for WhatsApp service usage.
"""

import frappe
from frappe.model.document import Document


class CustomerInvoice(Document):
    def validate(self):
        """Validate invoice data"""
        if self.invoice_period_end < self.invoice_period_start:
            frappe.throw("Period end date must be after start date")

        if self.invoice_status == "Paid" and not self.payment_date:
            frappe.throw("Payment date is required for paid invoices")

    def before_save(self):
        """Calculate total amount"""
        self.total_amount = (
            (self.base_fee or 0) +
            (self.call_charges or 0) +
            (self.message_charges or 0)
        )

    def on_update(self):
        """Update customer balance when invoice is paid"""
        if self.has_value_changed("invoice_status") and self.invoice_status == "Paid":
            # Could update customer balance here
            pass
