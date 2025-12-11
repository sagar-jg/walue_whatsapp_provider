"""
Monthly Usage Summary DocType

Aggregated monthly totals for billing purposes.
"""

import frappe
from frappe.model.document import Document


class MonthlyUsageSummary(Document):
    def validate(self):
        """Validate month format"""
        import re
        if not re.match(r"^\d{4}-\d{2}$", self.month):
            frappe.throw("Month must be in YYYY-MM format (e.g., 2025-01)")

    def before_save(self):
        """Calculate total amount"""
        self.total_amount = (self.base_fee or 0) + (self.usage_charges or 0)
