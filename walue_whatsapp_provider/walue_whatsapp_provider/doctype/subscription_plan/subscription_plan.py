"""
Subscription Plan DocType

Defines pricing tiers for WhatsApp service customers.
"""

import frappe
from frappe.model.document import Document
import json


class SubscriptionPlan(Document):
    def validate(self):
        """Validate plan configuration"""
        if self.base_monthly_fee < 0:
            frappe.throw("Base monthly fee cannot be negative")

        if self.call_markup_percentage < 0 or self.call_markup_percentage > 100:
            frappe.throw("Call markup percentage must be between 0 and 100")

        if self.message_markup_percentage < 0 or self.message_markup_percentage > 100:
            frappe.throw("Message markup percentage must be between 0 and 100")

        # Validate features JSON
        if self.features_json:
            try:
                features = json.loads(self.features_json)
                if not isinstance(features, list):
                    frappe.throw("Features must be a JSON array")
            except json.JSONDecodeError:
                frappe.throw("Invalid JSON in features field")

    def get_features(self):
        """Return features as a list"""
        if self.features_json:
            return json.loads(self.features_json)
        return []

    def has_feature(self, feature_name):
        """Check if plan includes a specific feature"""
        return feature_name in self.get_features()
