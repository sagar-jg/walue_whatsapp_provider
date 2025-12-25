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
    WEBHOOK_CALL_CONNECT,
    WEBHOOK_CALL_TERMINATE,
)


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
def meta_webhook():
    """
    Main webhook endpoint for Meta WhatsApp Business API

    This single endpoint handles:
    - GET: Webhook verification from Meta
    - POST: Receive webhook events

    Configure this URL in Meta Developer Portal:
    https://your-provider.com/api/method/walue_whatsapp_provider.api.webhooks.meta_webhook
    """
    # Debug logging
    print(f"[WEBHOOK] Received {frappe.request.method} request")
    print(f"[WEBHOOK] Site: {frappe.local.site}")
    print(f"[WEBHOOK] Args: {frappe.form_dict}")

    if frappe.request.method == "GET":
        return _verify_meta_webhook()
    else:
        return _receive_meta_webhook()


def _verify_meta_webhook():
    """
    Handle webhook verification from Meta (GET request)

    Meta sends:
    - hub.mode: 'subscribe'
    - hub.verify_token: Our configured token
    - hub.challenge: Challenge to return
    """
    mode = frappe.form_dict.get("hub.mode")
    token = frappe.form_dict.get("hub.verify_token")
    challenge = frappe.form_dict.get("hub.challenge")

    print(f"[WEBHOOK VERIFY] mode={mode}, token={token}, challenge={challenge}")

    try:
        settings = frappe.get_single("WhatsApp Provider Settings")
        verify_token = settings.get_password("meta_webhook_verify_token")
        print(f"[WEBHOOK VERIFY] Expected token from settings: {verify_token}")
    except Exception as e:
        print(f"[WEBHOOK VERIFY] Error getting settings: {e}")
        frappe.throw(_(f"Settings error: {e}"), frappe.AuthenticationError)

    if mode == "subscribe" and token == verify_token:
        print(f"[WEBHOOK VERIFY] SUCCESS - returning challenge: {challenge}")
        # Return plain text challenge for Meta verification
        frappe.response.update({
            "http_status_code": 200
        })
        # Use Response object to return plain text
        from werkzeug.wrappers import Response
        return Response(challenge, status=200, mimetype="text/plain")

    print(f"[WEBHOOK VERIFY] FAILED - token mismatch or wrong mode")
    frappe.throw(_("Verification failed"), frappe.AuthenticationError)


def _receive_meta_webhook():
    """
    Receive webhooks from Meta WhatsApp Business API

    Validates the signature, extracts the WABA ID,
    and forwards to the appropriate customer's webhook endpoint.

    We do NOT store webhook data - only route it.
    """
    print(f"[WEBHOOK POST] Starting to process webhook")

    # Verify webhook signature
    signature_valid = _verify_signature()
    print(f"[WEBHOOK POST] Signature valid: {signature_valid}")

    if not signature_valid:
        print(f"[WEBHOOK POST] Signature verification FAILED")
        frappe.log_error("Invalid webhook signature")
        frappe.throw(_("Invalid signature"), frappe.AuthenticationError)

    try:
        # Try to get payload from request.data first, fall back to form_dict
        if frappe.request.data:
            import json
            raw_data = frappe.request.data
            if isinstance(raw_data, bytes):
                raw_data = raw_data.decode('utf-8')
            payload = json.loads(raw_data)
            print(f"[WEBHOOK POST] Payload parsed from request.data")
        else:
            # Frappe might have already parsed JSON into form_dict
            payload = dict(frappe.form_dict)
            # Remove cmd key added by Frappe
            payload.pop('cmd', None)
            print(f"[WEBHOOK POST] Payload taken from form_dict")

        print(f"[WEBHOOK POST] Payload type: {type(payload)}, keys: {list(payload.keys()) if isinstance(payload, dict) else 'N/A'}")
    except Exception as e:
        print(f"[WEBHOOK POST] Payload parse error: {e}")
        import traceback
        traceback.print_exc()
        frappe.log_error("Invalid webhook payload")
        return {"status": "error", "message": "Invalid payload"}

    try:
        # Process webhook entries
        entries = payload.get("entry", [])
        print(f"[WEBHOOK POST] Processing {len(entries)} entries")

        for entry in entries:
            waba_id = entry.get("id")
            print(f"[WEBHOOK POST] Entry WABA ID: {waba_id}")

            if not waba_id:
                continue

            # Find customer by WABA ID
            customer = _find_customer_by_waba(waba_id)
            print(f"[WEBHOOK POST] Customer found: {customer.name if customer else 'None'}")

            if not customer:
                print(f"[WEBHOOK POST] No customer found for WABA: {waba_id}, skipping")
                continue

            # Process changes in the entry
            changes = entry.get("changes", [])
            print(f"[WEBHOOK POST] Processing {len(changes)} changes")

            for change in changes:
                field = change.get("field")
                value = change.get("value", {})
                print(f"[WEBHOOK POST] Change field: {field}")

                # Route based on webhook type
                if field == "messages":
                    print(f"[WEBHOOK POST] Routing message webhook to customer")
                    _route_message_webhook(customer, value)
                elif field == "calls":
                    print(f"[WEBHOOK POST] Routing call webhook to customer")
                    _route_call_webhook(customer, value, entry)

        print(f"[WEBHOOK POST] Done processing, returning OK")
        # Return 200 OK quickly to avoid Meta retries
        return {"status": "ok"}

    except Exception as e:
        print(f"[WEBHOOK POST] Error processing webhook: {e}")
        import traceback
        traceback.print_exc()
        frappe.log_error(f"Webhook processing error: {e}")
        return {"status": "error", "message": str(e)}


def _verify_signature() -> bool:
    """
    Verify the webhook signature from Meta

    Meta signs webhooks with HMAC-SHA256 using the app secret
    """
    signature_header = frappe.request.headers.get("X-Hub-Signature-256", "")
    print(f"[SIGNATURE] Header: {signature_header[:50] if signature_header else 'MISSING'}...")

    if not signature_header:
        print("[SIGNATURE] FAILED: No signature header")
        return False

    if not signature_header.startswith("sha256="):
        print("[SIGNATURE] FAILED: Header doesn't start with sha256=")
        return False

    expected_signature = signature_header[7:]

    settings = frappe.get_single("WhatsApp Provider Settings")
    app_secret = settings.get_password("meta_app_secret")

    if not app_secret:
        print("[SIGNATURE] FAILED: meta_app_secret not configured in settings")
        frappe.log_error("Meta app secret not configured")
        return False

    print(f"[SIGNATURE] App secret configured: Yes (length={len(app_secret)})")

    # Calculate HMAC
    payload = frappe.request.data
    calculated = hmac.new(
        app_secret.encode(),
        payload,
        hashlib.sha256
    ).hexdigest()

    match = hmac.compare_digest(calculated, expected_signature)
    print(f"[SIGNATURE] Match: {match}")
    if not match:
        print(f"[SIGNATURE] Expected: {expected_signature[:20]}...")
        print(f"[SIGNATURE] Calculated: {calculated[:20]}...")

    return match


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


def _route_call_webhook(customer, value: dict, entry: dict):
    """
    Route WhatsApp Business Calling API webhooks to customer's app

    Webhook types:
    - CONNECT: Call initiated, contains SDP offer
    - TERMINATE: Call ended

    Args:
        customer: Customer document
        value: Change value from webhook (contains 'calls' array)
        entry: Full entry containing call data
    """
    # WhatsApp Business Calling API webhooks structure:
    # entry.changes[].value.calls[] contains the call events
    # Each call has: id, from, to, event, direction, session (with sdp)

    # First try value.calls (correct structure per Meta docs)
    calls = value.get("calls", [])

    if not calls:
        # Fallback: try entry.calls
        calls = entry.get("calls", [])

    if not calls:
        print(f"[CALL WEBHOOK] No calls found in value or entry. value keys: {list(value.keys())}")
        return

    print(f"[CALL WEBHOOK] Found {len(calls)} call(s) to process")

    for call in calls:
        call_id = call.get("id")
        event = call.get("event", "").lower()
        direction = call.get("direction", "USER_INITIATED")
        from_number = call.get("from")
        to_number = call.get("to")
        session = call.get("session", {})

        print(f"[CALL WEBHOOK] Processing call event: {event}, call_id: {call_id}")

        if event == "connect":
            # Call connect event with SDP offer
            _forward_to_customer(customer, {
                "type": WEBHOOK_CALL_CONNECT,
                "call_id": call_id,
                "direction": direction,
                "from": from_number,
                "to": to_number,
                "sdp_type": session.get("sdp_type", "offer"),
                "sdp": session.get("sdp"),
                "timestamp": call.get("timestamp"),
            })

        elif event == "terminate":
            # Call terminated
            # Debug: Log what Meta actually sends
            print(f"[CALL WEBHOOK] Terminate event raw data: {call}")
            print(f"[CALL WEBHOOK] duration={call.get('duration')}, start_time={call.get('start_time')}, end_time={call.get('end_time')}")

            _forward_to_customer(customer, {
                "type": WEBHOOK_CALL_TERMINATE,
                "call_id": call_id,
                "status": call.get("status", "COMPLETED"),
                "duration": call.get("duration"),
                "start_time": call.get("start_time"),
                "end_time": call.get("end_time"),
                "timestamp": call.get("timestamp"),
            })

        else:
            # Generic call status update
            _forward_to_customer(customer, {
                "type": WEBHOOK_CALL_STATUS,
                "call_id": call_id,
                "event": event,
                "status": call.get("status"),
                "timestamp": call.get("timestamp"),
            })


def _forward_to_customer(customer, webhook_data: dict):
    """
    Forward webhook data to customer's Frappe site

    The customer app has a webhook endpoint that receives these updates
    and stores them locally.

    Uses the customer's OAuth client secret to sign the webhook payload.
    The customer verifies using the same secret (stored as provider_oauth_client_secret).
    """
    import requests

    print(f"[FORWARD] Starting forward to customer {customer.name}")

    if not customer.frappe_site_url:
        print(f"[FORWARD] ERROR: No site URL for customer {customer.name}")
        frappe.log_error(f"Customer {customer.name} has no site URL configured")
        return

    # Build webhook URL for customer app
    webhook_url = f"{customer.frappe_site_url}/api/method/walue_whatsapp_client.api.webhooks.receive"
    print(f"[FORWARD] URL: {webhook_url}")

    try:
        # Use customer's OAuth client secret to sign the payload
        # This same secret is stored on customer's side as provider_oauth_client_secret
        secret = customer.get_password("oauth_client_secret")

        if not secret:
            print(f"[FORWARD] ERROR: No oauth_client_secret for customer {customer.name}")
            frappe.log_error(f"Customer {customer.name} has no OAuth client secret configured")
            return

        payload_bytes = json.dumps(webhook_data).encode()
        signature = hmac.new(
            secret.encode(),
            payload_bytes,
            hashlib.sha256
        ).hexdigest()

        print(f"[FORWARD] Payload: {webhook_data}")
        print(f"[FORWARD] Signature: sha256={signature[:20]}...")

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

        print(f"[FORWARD] Response: {response.status_code}")

        if response.status_code != 200:
            print(f"[FORWARD] ERROR: Customer returned {response.status_code}: {response.text[:200]}")
            frappe.log_error(
                message=f"Status: {response.status_code}\nResponse: {response.text[:500]}",
                title=f"Webhook forward failed: {customer.name}"
            )

    except requests.RequestException as e:
        frappe.log_error(
            message=str(e),
            title=f"Webhook forward error: {customer.name}"
        )

    # We intentionally don't store the webhook data
    # Customer app handles storage


@frappe.whitelist(allow_guest=True, methods=["POST"])
def call_status():
    """
    Handle call status webhooks from Meta.
    Routes to customer for local storage.
    """
    if not _verify_signature():
        frappe.throw(_("Invalid signature"), frappe.AuthenticationError)

    payload = frappe.parse_json(frappe.request.data)

    # Extract customer info and forward
    # Implementation depends on how call status is reported

    return {"status": "ok"}
