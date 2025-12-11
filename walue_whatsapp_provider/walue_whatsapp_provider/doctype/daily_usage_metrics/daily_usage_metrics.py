"""
Daily Usage Metrics DocType

Stores AGGREGATED usage metrics per customer per day.

CRITICAL: This stores ONLY counts and costs - NO individual call/message details.
- NO phone numbers
- NO message content
- NO lead/contact information
- NO call recordings

This is for billing and analytics only.
"""

import frappe
from frappe.model.document import Document


class DailyUsageMetrics(Document):
    def validate(self):
        """Validate metrics are non-negative"""
        if self.total_calls < 0:
            self.total_calls = 0
        if self.total_messages < 0:
            self.total_messages = 0
        if self.total_call_minutes < 0:
            self.total_call_minutes = 0

    def before_save(self):
        """Calculate total revenue"""
        base_cost = (self.total_call_cost or 0) + (self.total_message_cost or 0)
        self.total_revenue = base_cost + (self.total_markup or 0)
