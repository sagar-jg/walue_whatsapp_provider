"""
Embedded Signup Session DocType

Tracks Meta embedded signup sessions for customer onboarding.
Sessions are temporary and cleaned up after 24 hours.
"""

import frappe
from frappe.model.document import Document
from datetime import datetime


class EmbeddedSignupSession(Document):
    def before_insert(self):
        """Set created_at timestamp"""
        if not self.created_at:
            self.created_at = datetime.now()

    def mark_completed(self):
        """Mark session as completed"""
        self.status = "completed"
        self.completed_at = datetime.now()
        self.save(ignore_permissions=True)

    def mark_failed(self, error_message):
        """Mark session as failed"""
        self.status = "failed"
        self.error_message = error_message
        self.save(ignore_permissions=True)
