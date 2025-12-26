"""
Request hooks for WhatsApp Provider

Handles CSRF bypass for cross-origin API calls from customer sites.
"""

import frappe


def before_request():
    """
    Hook called before each request.
    Bypasses CSRF validation for all walue_whatsapp_provider API endpoints.
    These are cross-origin calls from customer Frappe sites that use
    OAuth authentication instead of CSRF tokens.
    """
    if not frappe.request:
        return

    path = frappe.request.path or ""

    # Bypass CSRF for all walue_whatsapp_provider API calls
    if "walue_whatsapp_provider" in path:
        frappe.flags.ignore_csrf = True
        print(f"[CSRF] Bypassing CSRF for: {path}")
        return

    # Also bypass for /api/method calls that might be our endpoints
    if path.startswith("/api/method/"):
        cmd = frappe.form_dict.get("cmd", "")
        if "walue_whatsapp_provider" in cmd:
            frappe.flags.ignore_csrf = True
            print(f"[CSRF] Bypassing CSRF for cmd: {cmd}")
            return
