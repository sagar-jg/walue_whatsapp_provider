"""
OAuth Provider API for Customer App Authentication

This module implements OAuth 2.0 authorization server functionality
to authenticate customer Frappe instances connecting to our platform.

The customer app uses these endpoints to:
1. Authorize and get access code
2. Exchange code for access/refresh tokens
3. Refresh expired tokens
"""

import frappe
from frappe import _
import secrets
import jwt
from datetime import datetime, timedelta

from walue_whatsapp_provider.constants import (
    DEFAULT_OAUTH_TOKEN_EXPIRY,
    DEFAULT_OAUTH_REFRESH_EXPIRY,
    ERR_OAUTH_FAILED,
    ERR_INVALID_TOKEN,
    ERR_CUSTOMER_NOT_FOUND,
)


@frappe.whitelist(allow_guest=True)
def authorize():
    """
    OAuth 2.0 Authorization Endpoint

    Validates client credentials and returns authorization code

    Query Parameters:
        client_id: Customer's OAuth client ID
        redirect_uri: Callback URL on customer's site
        response_type: Must be 'code'
        state: CSRF protection token from client

    Returns:
        Redirects to redirect_uri with code and state
    """
    client_id = frappe.form_dict.get("client_id")
    redirect_uri = frappe.form_dict.get("redirect_uri")
    response_type = frappe.form_dict.get("response_type")
    state = frappe.form_dict.get("state")

    # Validate required parameters
    if not all([client_id, redirect_uri, response_type]):
        frappe.throw(_("Missing required OAuth parameters"))

    if response_type != "code":
        frappe.throw(_("Invalid response_type. Must be 'code'"))

    # Validate client
    customer = _validate_client(client_id)
    if not customer:
        frappe.throw(_(ERR_CUSTOMER_NOT_FOUND))

    # Validate redirect URI matches registered URI
    if not _validate_redirect_uri(customer, redirect_uri):
        frappe.throw(_("Invalid redirect_uri"))

    # Generate authorization code
    auth_code = secrets.token_urlsafe(32)

    # Store auth code temporarily (expires in 10 minutes)
    frappe.cache().set_value(
        f"oauth_code:{auth_code}",
        {
            "customer_id": customer.name,
            "redirect_uri": redirect_uri,
            "created_at": datetime.now().isoformat(),
        },
        expires_in_sec=600  # 10 minutes
    )

    # Build redirect URL
    separator = "&" if "?" in redirect_uri else "?"
    redirect_url = f"{redirect_uri}{separator}code={auth_code}"
    if state:
        redirect_url += f"&state={state}"

    frappe.local.response["type"] = "redirect"
    frappe.local.response["location"] = redirect_url


@frappe.whitelist(allow_guest=True)
def token():
    """
    OAuth 2.0 Token Endpoint

    Exchanges authorization code for access and refresh tokens

    POST Parameters:
        grant_type: 'authorization_code' or 'refresh_token'
        code: Authorization code (for authorization_code grant)
        refresh_token: Refresh token (for refresh_token grant)
        client_id: Customer's OAuth client ID
        client_secret: Customer's OAuth client secret
        redirect_uri: Must match the one used in authorize

    Returns:
        dict: Contains access_token, refresh_token, expires_in, token_type
    """
    grant_type = frappe.form_dict.get("grant_type")
    client_id = frappe.form_dict.get("client_id")
    client_secret = frappe.form_dict.get("client_secret")

    # Validate client credentials
    customer = _validate_client_credentials(client_id, client_secret)
    if not customer:
        return {"error": "invalid_client", "error_description": ERR_OAUTH_FAILED}

    if grant_type == "authorization_code":
        return _handle_authorization_code_grant(customer)
    elif grant_type == "refresh_token":
        return _handle_refresh_token_grant(customer)
    else:
        return {"error": "unsupported_grant_type"}


@frappe.whitelist(allow_guest=True)
def refresh():
    """
    Refresh an expired access token

    POST Parameters:
        refresh_token: Valid refresh token
        client_id: Customer's OAuth client ID
        client_secret: Customer's OAuth client secret

    Returns:
        dict: New access_token and optionally new refresh_token
    """
    refresh_token = frappe.form_dict.get("refresh_token")
    client_id = frappe.form_dict.get("client_id")
    client_secret = frappe.form_dict.get("client_secret")

    # Validate client credentials
    customer = _validate_client_credentials(client_id, client_secret)
    if not customer:
        return {"error": "invalid_client", "error_description": ERR_OAUTH_FAILED}

    # Validate refresh token
    try:
        settings = frappe.get_single("WhatsApp Provider Settings")
        secret = settings.get_password("meta_webhook_verify_token")  # Using as JWT secret

        payload = jwt.decode(refresh_token, secret, algorithms=["HS256"])

        if payload.get("customer_id") != customer.name:
            return {"error": "invalid_grant", "error_description": ERR_INVALID_TOKEN}

        if payload.get("type") != "refresh":
            return {"error": "invalid_grant", "error_description": ERR_INVALID_TOKEN}

    except jwt.ExpiredSignatureError:
        return {"error": "invalid_grant", "error_description": "Refresh token expired"}
    except jwt.InvalidTokenError:
        return {"error": "invalid_grant", "error_description": ERR_INVALID_TOKEN}

    # Generate new tokens
    tokens = _generate_tokens(customer.name, settings)

    return tokens


@frappe.whitelist(allow_guest=False)
def validate_token(access_token: str) -> dict:
    """
    Validate an access token and return customer info

    Used internally by other API methods to authenticate requests

    Args:
        access_token: JWT access token

    Returns:
        dict: Customer info if valid, None if invalid
    """
    try:
        settings = frappe.get_single("WhatsApp Provider Settings")
        secret = settings.get_password("meta_webhook_verify_token")

        payload = jwt.decode(access_token, secret, algorithms=["HS256"])

        if payload.get("type") != "access":
            return None

        customer_id = payload.get("customer_id")
        if not frappe.db.exists("WhatsApp Customer", customer_id):
            return None

        return {
            "customer_id": customer_id,
            "exp": payload.get("exp"),
        }

    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None


def _validate_client(client_id: str):
    """Validate OAuth client ID and return customer doc"""
    customer = frappe.db.get_value(
        "WhatsApp Customer",
        {"oauth_client_id": client_id, "status": "Active"},
        "*",
        as_dict=True
    )
    return customer


def _validate_client_credentials(client_id: str, client_secret: str):
    """Validate OAuth client ID and secret"""
    if not client_id or not client_secret:
        return None

    customer = frappe.get_doc("WhatsApp Customer", {"oauth_client_id": client_id})
    if not customer:
        return None

    stored_secret = customer.get_password("oauth_client_secret")
    if stored_secret != client_secret:
        return None

    if customer.status != "Active":
        return None

    return customer


def _validate_redirect_uri(customer, redirect_uri: str) -> bool:
    """Validate that redirect URI matches customer's registered site"""
    # Check if redirect URI starts with customer's registered site URL
    if not customer.frappe_site_url:
        return False

    return redirect_uri.startswith(customer.frappe_site_url)


def _handle_authorization_code_grant(customer):
    """Handle authorization_code grant type"""
    code = frappe.form_dict.get("code")
    redirect_uri = frappe.form_dict.get("redirect_uri")

    if not code:
        return {"error": "invalid_request", "error_description": "Missing code"}

    # Retrieve and validate code
    code_data = frappe.cache().get_value(f"oauth_code:{code}")
    if not code_data:
        return {"error": "invalid_grant", "error_description": "Invalid or expired code"}

    if code_data.get("customer_id") != customer.name:
        return {"error": "invalid_grant", "error_description": "Code not issued to this client"}

    if code_data.get("redirect_uri") != redirect_uri:
        return {"error": "invalid_grant", "error_description": "redirect_uri mismatch"}

    # Invalidate the code (single use)
    frappe.cache().delete_value(f"oauth_code:{code}")

    # Generate tokens
    settings = frappe.get_single("WhatsApp Provider Settings")
    tokens = _generate_tokens(customer.name, settings)

    return tokens


def _handle_refresh_token_grant(customer):
    """Handle refresh_token grant type"""
    refresh_token = frappe.form_dict.get("refresh_token")

    if not refresh_token:
        return {"error": "invalid_request", "error_description": "Missing refresh_token"}

    try:
        settings = frappe.get_single("WhatsApp Provider Settings")
        secret = settings.get_password("meta_webhook_verify_token")

        payload = jwt.decode(refresh_token, secret, algorithms=["HS256"])

        if payload.get("customer_id") != customer.name:
            return {"error": "invalid_grant", "error_description": ERR_INVALID_TOKEN}

        if payload.get("type") != "refresh":
            return {"error": "invalid_grant", "error_description": ERR_INVALID_TOKEN}

    except jwt.ExpiredSignatureError:
        return {"error": "invalid_grant", "error_description": "Refresh token expired"}
    except jwt.InvalidTokenError:
        return {"error": "invalid_grant", "error_description": ERR_INVALID_TOKEN}

    # Generate new tokens
    tokens = _generate_tokens(customer.name, settings)

    return tokens


def _generate_tokens(customer_id: str, settings) -> dict:
    """Generate access and refresh tokens"""
    secret = settings.get_password("meta_webhook_verify_token")

    access_expiry = settings.oauth_token_expiry_seconds or DEFAULT_OAUTH_TOKEN_EXPIRY
    refresh_expiry = settings.oauth_refresh_expiry_seconds or DEFAULT_OAUTH_REFRESH_EXPIRY

    now = datetime.utcnow()

    # Access token
    access_payload = {
        "customer_id": customer_id,
        "type": "access",
        "iat": now,
        "exp": now + timedelta(seconds=access_expiry),
    }
    access_token = jwt.encode(access_payload, secret, algorithm="HS256")

    # Refresh token
    refresh_payload = {
        "customer_id": customer_id,
        "type": "refresh",
        "iat": now,
        "exp": now + timedelta(seconds=refresh_expiry),
    }
    refresh_token = jwt.encode(refresh_payload, secret, algorithm="HS256")

    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "Bearer",
        "expires_in": access_expiry,
    }
