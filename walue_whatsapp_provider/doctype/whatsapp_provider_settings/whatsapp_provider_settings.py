"""
WhatsApp Provider Settings - Single DocType

Central configuration for the WhatsApp Provider platform.
Contains Meta API credentials, Janus configuration, and OAuth settings.
"""

import frappe
from frappe.model.document import Document


class WhatsAppProviderSettings(Document):
    def validate(self):
        """Validate settings before save"""
        if self.enabled:
            if not self.meta_app_id:
                frappe.throw("Meta App ID is required when provider is enabled")
            if not self.meta_app_secret:
                frappe.throw("Meta App Secret is required when provider is enabled")
            if not self.meta_webhook_verify_token:
                frappe.throw("Webhook Verify Token is required when provider is enabled")

    def on_update(self):
        """Clear cache on settings update"""
        frappe.cache().delete_value("whatsapp_provider_settings")
