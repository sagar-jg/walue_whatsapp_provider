"""
Webhook Handlers for Meta WhatsApp Business API

This module handles incoming webhooks from Meta for:
1. Message status updates (sent, delivered, read)
2. Inbound messages
3. Call permission responses
4. Call status updates

These webhooks are then forwarded to the appropriate customer app
based on the WABA ID in the webhook payload.

IMPORTANT: We do NOT store webhook data - only route it to customers
"""

import frappe
from frappe import _
import hmac
import hashlib
import json

from walue_whatsapp_provider.constants import (
    WEBHOOK_MESSAGE_STATUS,
    WEBHOOK_INBOUND_MESSAGE,
    WEBHOOK_CALL_PERMISSION_REPLY,
    WEBHOOK_CALL_STATUS,
)


@frappe.whitelist(allow_guest=True, methods=["GET"])
def verify():
    """
    Webhook verification endpoint for Meta

    Meta sends a GET request with:
    - hub.mode: 'subscribe'
    - hub.verify_token: Our configured token
    - hub.challenge: Challenge to return

    Returns the challenge if token matches
    """
    mode = frappe.form_dict.get("hub.mode")
    token = frappe.form_dict.get("hub.verify_token")
    challenge = frappe.form_dict.get("hub.challenge")

    settings = frappe.get_single("WhatsApp Provider Settings")
    verify_token = settings.get_password("meta_webhook_verify_token")

    if mode == "subscribe" and token == verify_token:
        frappe.response["type"] = "text"
        frappe.response["status_code"] = 200
        return challenge

    frappe.throw(_("Verification failed"), frappe.AuthenticationError)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def receive():
    """
    Receive webhooks from Meta WhatsApp Business API

    Validates the signature, extracts the WABA ID,
    and forwards to the appropriate customer's webhook endpoint.

    We do NOT store webhook data - only route it.
    """
    # Verify webhook signature
    if not _verify_signature():
        frappe.log_error("Invalid webhook signature")
        frappe.throw(_("Invalid signature"), frappe.AuthenticationError)

    try:
        payload = frappe.parse_json(frappe.request.data)
    except Exception:
        frappe.log_error("Invalid webhook payload")
        return {"status": "error", "message": "Invalid payload"}

    # Process webhook entries
    entries = payload.get("entry", [])

    for entry in entries:
        waba_id = entry.get("id")

        if not waba_id:
            continue

        # Find customer by WABA ID
        customer = _find_customer_by_waba(waba_id)

        if not customer:
            frappe.log_error(f"No customer found for WABA: {waba_id}")
            continue

        # Process changes in the entry
        changes = entry.get("changes", [])

        for change in changes:
            field = change.get("field")
            value = change.get("value", {})

            # Route based on webhook type
            if field == "messages":
                _route_message_webhook(customer, value)

    # Return 200 OK quickly to avoid Meta retries
    return {"status": "ok"}


def _verify_signature() -> bool:
    """
    Verify the webhook signature from Meta

    Meta signs webhooks with HMAC-SHA256 using the app secret
    """
    signature_header = frappe.request.headers.get("X-Hub-Signature-256", "")

    if not signature_header:
        return False

    if not signature_header.startswith("sha256="):
        return False

    expected_signature = signature_header[7:]

    settings = frappe.get_single("WhatsApp Provider Settings")
    app_secret = settings.get_password("meta_app_secret")

    if not app_secret:
        frappe.log_error("Meta app secret not configured")
        return False

    # Calculate HMAC
    payload = frappe.request.data
    calculated = hmac.new(
        app_secret.encode(),
        payload,
        hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(calculated, expected_signature)


def _find_customer_by_waba(waba_id: str):
    """Find customer document by WABA ID"""
    customer_name = frappe.db.get_value(
        "WhatsApp Customer",
        {"waba_id": waba_id, "status": "Active"},
        "name"
    )

    if customer_name:
        return frappe.get_doc("WhatsApp Customer", customer_name)

    return None


def _route_message_webhook(customer, value: dict):
    """
    Route message webhooks to customer's app

    Webhook types:
    - statuses: Message status updates (sent, delivered, read, failed)
    - messages: Inbound messages
    """
    metadata = value.get("metadata", {})
    phone_number_id = metadata.get("phone_number_id")
    display_phone = metadata.get("display_phone_number")

    # Process message statuses
    statuses = value.get("statuses", [])
    for status in statuses:
        _forward_to_customer(customer, {
            "type": WEBHOOK_MESSAGE_STATUS,
            "message_id": status.get("id"),
            "status": status.get("status"),
            "timestamp": status.get("timestamp"),
            "recipient_id": status.get("recipient_id"),
            "errors": status.get("errors", []),
        })

    # Process inbound messages
    messages = value.get("messages", [])
    for message in messages:
        _forward_to_customer(customer, {
            "type": WEBHOOK_INBOUND_MESSAGE,
            "message_id": message.get("id"),
            "from": message.get("from"),
            "timestamp": message.get("timestamp"),
            "message_type": message.get("type"),
            "text": message.get("text", {}).get("body") if message.get("type") == "text" else None,
            # Include other message types as needed
        })

    # Process call permission replies
    # These come through interactive message responses
    if value.get("messages"):
        for msg in value["messages"]:
            if msg.get("type") == "interactive":
                interactive = msg.get("interactive", {})
                if interactive.get("type") == "call_permission_reply":
                    _forward_to_customer(customer, {
                        "type": WEBHOOK_CALL_PERMISSION_REPLY,
                        "from": msg.get("from"),
                        "timestamp": msg.get("timestamp"),
                        "response": interactive.get("call_permission_reply", {}).get("response"),
                        "expiration": interactive.get("call_permission_reply", {}).get("expiration_timestamp"),
                    })


def _forward_to_customer(customer, webhook_data: dict):
    """
    Forward webhook data to customer's Frappe site

    The customer app has a webhook endpoint that receives these updates
    and stores them locally.
    """
    import requests

    if not customer.frappe_site_url:
        frappe.log_error(f"Customer {customer.name} has no site URL configured")
        return

    # Build webhook URL for customer app
    webhook_url = f"{customer.frappe_site_url}/api/method/walue_whatsapp_client.api.webhooks.receive"

    try:
        # Generate a signature for the customer to verify
        settings = frappe.get_single("WhatsApp Provider Settings")
        secret = settings.get_password("meta_webhook_verify_token")

        payload_bytes = json.dumps(webhook_data).encode()
        signature = hmac.new(
            secret.encode(),
            payload_bytes,
            hashlib.sha256
        ).hexdigest()

        headers = {
            "Content-Type": "application/json",
            "X-Walue-Signature": f"sha256={signature}",
        }

        response = requests.post(
            webhook_url,
            json=webhook_data,
            headers=headers,
            timeout=5  # Quick timeout to not block Meta
        )

        if response.status_code != 200:
            frappe.log_error(
                f"Failed to forward webhook to {customer.name}: {response.status_code}"
            )

    except requests.RequestException as e:
        frappe.log_error(f"Webhook forwarding failed for {customer.name}: {str(e)}")

    # We intentionally don't store the webhook data
    # Customer app handles storage


@frappe.whitelist(allow_guest=True, methods=["POST"])
def call_status():
    """
    Handle call status webhooks

    These may come from Janus or from Meta depending on implementation.
    Routes to customer for local storage.
    """
    if not _verify_signature():
        frappe.throw(_("Invalid signature"), frappe.AuthenticationError)

    payload = frappe.parse_json(frappe.request.data)

    # Extract customer info and forward
    # Implementation depends on how call status is reported

    return {"status": "ok"}
