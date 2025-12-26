"""
Request hooks for WhatsApp Provider

Handles CSRF bypass for cross-origin API calls from customer sites.
"""

import frappe


# API endpoints that should bypass CSRF validation (cross-origin calls)
CSRF_EXEMPT_PATHS = [
    "/api/method/walue_whatsapp_provider.api.customers.register",
    "/api/method/walue_whatsapp_provider.api.customers.get_info",
    "/api/method/walue_whatsapp_provider.api.messages.send_template",
    "/api/method/walue_whatsapp_provider.api.messages.send_text",
    "/api/method/walue_whatsapp_provider.api.messages.send_image",
    "/api/method/walue_whatsapp_provider.api.embedded_signup.initiate",
    "/api/method/walue_whatsapp_provider.api.embedded_signup.callback",
    "/api/method/walue_whatsapp_provider.api.webhooks.meta_webhook",
]


def before_request():
    """
    Hook called before each request.
    Bypasses CSRF validation for specific API endpoints that handle
    cross-origin requests from customer Frappe sites.
    """
    if frappe.request and frappe.request.path:
        path = frappe.request.path

        # Check if this path should bypass CSRF
        for exempt_path in CSRF_EXEMPT_PATHS:
            if path.startswith(exempt_path):
                frappe.flags.ignore_csrf = True
                return

        # Also bypass for any walue_whatsapp_provider API call with OAuth headers
        if "walue_whatsapp_provider.api" in path:
            # If request has X-Client-ID header, it's an authenticated API call
            if frappe.request.headers.get("X-Client-ID"):
                frappe.flags.ignore_csrf = True
                return
