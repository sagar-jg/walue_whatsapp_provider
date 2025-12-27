"""
WhatsApp Consent Log DocType

Maintains an immutable audit trail of consent actions for GDPR/compliance:
- When consent was granted (during embedded signup)
- When consent was revoked (via webhook or user action)
- What permissions were included
- IP address and user agent for verification
"""

import frappe
from frappe.model.document import Document
from datetime import datetime
import json


class WhatsAppConsentLog(Document):
    def before_insert(self):
        """Set timestamp if not provided"""
        if not self.timestamp:
            self.timestamp = datetime.now()

    def after_insert(self):
        """Update customer consent status based on this log entry"""
        if self.customer:
            self._update_customer_consent_status()

    def _update_customer_consent_status(self):
        """Update the WhatsApp Customer's consent fields based on this log"""
        try:
            customer = frappe.get_doc("WhatsApp Customer", self.customer)

            if self.action == "Consent Granted":
                customer.consent_status = "Granted"
                customer.consent_granted_at = self.timestamp
                customer.consent_permissions = self.permissions
                customer.consent_policy_version = self.policy_version
                customer.consent_ip_address = self.ip_address
                customer.consent_revoked_at = None
            elif self.action == "Consent Revoked":
                customer.consent_status = "Revoked"
                customer.consent_revoked_at = self.timestamp
                # Optionally suspend the customer
                customer.status = "Suspended"

            customer.save(ignore_permissions=True)

        except Exception as e:
            frappe.log_error(
                title="Consent Log: Failed to update customer",
                message=f"Customer: {self.customer}\nAction: {self.action}\nError: {str(e)}"
            )


def log_consent_granted(
    customer: str,
    waba_id: str = None,
    phone_number_id: str = None,
    permissions: str = None,
    policy_version: str = None,
    session_id: str = None,
    ip_address: str = None,
    user_agent: str = None,
    meta_user_id: str = None,
    raw_event_data: dict = None
):
    """
    Helper function to log a consent granted event.

    Call this after successful embedded signup completion.

    Args:
        customer: WhatsApp Customer document name
        waba_id: WhatsApp Business Account ID
        phone_number_id: Phone number ID from Meta
        permissions: Comma-separated OAuth scopes granted
        policy_version: Version of TOS/privacy policy
        session_id: Embedded Signup Session ID
        ip_address: Client IP address
        user_agent: Browser user agent
        meta_user_id: Facebook/Meta user ID
        raw_event_data: Raw event data for debugging
    """
    doc = frappe.get_doc({
        "doctype": "WhatsApp Consent Log",
        "customer": customer,
        "action": "Consent Granted",
        "timestamp": datetime.now(),
        "waba_id": waba_id,
        "phone_number_id": phone_number_id,
        "permissions": permissions,
        "policy_version": policy_version,
        "ip_address": ip_address,
        "user_agent": user_agent,
        "source": "Embedded Signup",
        "meta_user_id": meta_user_id,
        "session_id": session_id,
        "raw_event_data": json.dumps(raw_event_data) if raw_event_data else None,
    })
    doc.insert(ignore_permissions=True)
    return doc.name


def log_consent_revoked(
    customer: str,
    waba_id: str = None,
    meta_user_id: str = None,
    raw_event_data: dict = None,
    source: str = "Webhook"
):
    """
    Helper function to log a consent revoked event.

    Call this when receiving a consent revocation webhook from Meta.

    Args:
        customer: WhatsApp Customer document name
        waba_id: WhatsApp Business Account ID
        meta_user_id: Facebook/Meta user ID who revoked
        raw_event_data: Raw webhook data
        source: Source of revocation (Webhook, Manual)
    """
    doc = frappe.get_doc({
        "doctype": "WhatsApp Consent Log",
        "customer": customer,
        "action": "Consent Revoked",
        "timestamp": datetime.now(),
        "waba_id": waba_id,
        "source": source,
        "meta_user_id": meta_user_id,
        "raw_event_data": json.dumps(raw_event_data) if raw_event_data else None,
    })
    doc.insert(ignore_permissions=True)
    return doc.name
