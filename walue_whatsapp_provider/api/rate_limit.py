"""
Rate Limiting Module

Provides Redis-backed rate limiting for API endpoints.
Uses Frappe's cache (which is Redis-backed) for storage.

Rate limits are applied per-customer per-endpoint to prevent
abuse while allowing legitimate high-volume usage.
"""

import frappe
from frappe import _
from functools import wraps
from datetime import datetime

from walue_whatsapp_provider.constants import (
    ERR_RATE_LIMIT,
    RATE_LIMIT_WINDOW,
    RATE_LIMIT_MESSAGES,
    RATE_LIMIT_MEDIA,
    RATE_LIMIT_CALLS_INITIATE,
    RATE_LIMIT_CALLS_CONNECT,
    RATE_LIMIT_PERMISSION,
)


class RateLimitExceededError(frappe.ValidationError):
    """Raised when rate limit is exceeded"""
    http_status_code = 429  # Too Many Requests


def get_rate_limit_key(customer_id: str, endpoint: str) -> str:
    """
    Generate a unique cache key for rate limiting

    Args:
        customer_id: Customer identifier
        endpoint: API endpoint name (e.g., 'send_template', 'initiate')

    Returns:
        str: Cache key for rate limiting
    """
    # Include current minute to create sliding window
    current_minute = datetime.now().strftime("%Y%m%d%H%M")
    return f"rate_limit:{customer_id}:{endpoint}:{current_minute}"


def check_rate_limit(customer_id: str, endpoint: str, limit: int) -> dict:
    """
    Check if customer has exceeded rate limit for an endpoint

    Args:
        customer_id: Customer identifier
        endpoint: API endpoint name
        limit: Maximum requests allowed per window

    Returns:
        dict: {
            'allowed': bool,
            'current_count': int,
            'limit': int,
            'remaining': int,
            'reset_seconds': int,
        }
    """
    key = get_rate_limit_key(customer_id, endpoint)
    current_count = frappe.cache().get_value(key) or 0

    remaining = max(0, limit - current_count)
    # Reset at the end of the current minute
    reset_seconds = 60 - datetime.now().second

    return {
        "allowed": current_count < limit,
        "current_count": current_count,
        "limit": limit,
        "remaining": remaining,
        "reset_seconds": reset_seconds,
    }


def increment_rate_limit(customer_id: str, endpoint: str) -> int:
    """
    Increment the rate limit counter for a customer/endpoint

    Args:
        customer_id: Customer identifier
        endpoint: API endpoint name

    Returns:
        int: New count after incrementing
    """
    key = get_rate_limit_key(customer_id, endpoint)
    current_count = frappe.cache().get_value(key) or 0
    new_count = current_count + 1

    # Set with expiry at end of window (60 seconds from start of minute)
    frappe.cache().set_value(key, new_count, expires_in_sec=RATE_LIMIT_WINDOW)

    return new_count


def enforce_rate_limit(customer_id: str, endpoint: str, limit: int = None) -> dict:
    """
    Check and enforce rate limit, throwing error if exceeded

    This function should be called at the start of each API endpoint.

    Args:
        customer_id: Customer identifier
        endpoint: API endpoint name
        limit: Optional custom limit (defaults based on endpoint type)

    Returns:
        dict: Rate limit status if allowed

    Raises:
        RateLimitExceededError: If rate limit is exceeded
    """
    # Get default limit based on endpoint if not provided
    if limit is None:
        limit = get_default_limit(endpoint)

    # Check rate limit
    status = check_rate_limit(customer_id, endpoint, limit)

    if not status["allowed"]:
        frappe.throw(
            _("{0} Please wait {1} seconds before retrying.").format(
                ERR_RATE_LIMIT,
                status["reset_seconds"]
            ),
            RateLimitExceededError,
        )

    # Increment counter
    increment_rate_limit(customer_id, endpoint)

    return status


def get_default_limit(endpoint: str) -> int:
    """
    Get default rate limit for an endpoint

    Args:
        endpoint: API endpoint name

    Returns:
        int: Default rate limit for the endpoint
    """
    # Map endpoints to their limits
    endpoint_limits = {
        # Message endpoints
        "send_template": RATE_LIMIT_MESSAGES,
        "send_text": RATE_LIMIT_MESSAGES,
        "send_media": RATE_LIMIT_MEDIA,

        # Call endpoints
        "initiate": RATE_LIMIT_CALLS_INITIATE,
        "connect": RATE_LIMIT_CALLS_CONNECT,
        "request_permission": RATE_LIMIT_PERMISSION,

        # Other endpoints
        "pre_accept": RATE_LIMIT_CALLS_CONNECT,
        "accept": RATE_LIMIT_CALLS_CONNECT,
        "reject": RATE_LIMIT_CALLS_CONNECT,
        "terminate": RATE_LIMIT_CALLS_CONNECT,
        "end": RATE_LIMIT_CALLS_CONNECT,
    }

    return endpoint_limits.get(endpoint, RATE_LIMIT_MESSAGES)


def rate_limit(endpoint_name: str = None, limit: int = None):
    """
    Decorator to apply rate limiting to API endpoints

    Usage:
        @frappe.whitelist()
        @rate_limit("send_template")
        def send_template():
            ...

    Args:
        endpoint_name: Name of the endpoint (defaults to function name)
        limit: Custom rate limit (optional)

    Returns:
        Decorator function
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            # Get customer_id from authentication
            # This assumes the endpoint uses Bearer token auth
            auth_header = frappe.request.headers.get("Authorization", "")
            if auth_header.startswith("Bearer "):
                from walue_whatsapp_provider.api.oauth import validate_token
                token = auth_header.split(" ")[1]
                customer_info = validate_token(token)
                if customer_info:
                    customer_id = customer_info["customer_id"]
                    name = endpoint_name or func.__name__
                    enforce_rate_limit(customer_id, name, limit)

            return func(*args, **kwargs)
        return wrapper
    return decorator


def get_rate_limit_headers(customer_id: str, endpoint: str) -> dict:
    """
    Get HTTP headers for rate limit information

    Args:
        customer_id: Customer identifier
        endpoint: API endpoint name

    Returns:
        dict: Headers to add to response
    """
    limit = get_default_limit(endpoint)
    status = check_rate_limit(customer_id, endpoint, limit)

    return {
        "X-RateLimit-Limit": str(status["limit"]),
        "X-RateLimit-Remaining": str(status["remaining"]),
        "X-RateLimit-Reset": str(status["reset_seconds"]),
    }


def clear_rate_limit(customer_id: str, endpoint: str = None):
    """
    Clear rate limit counters for a customer (admin function)

    Args:
        customer_id: Customer identifier
        endpoint: Optional specific endpoint to clear (clears all if None)
    """
    if endpoint:
        key = get_rate_limit_key(customer_id, endpoint)
        frappe.cache().delete_value(key)
    else:
        # Clear all rate limit keys for this customer
        # Note: This is a pattern-based clear which may not work with all cache backends
        pattern = f"rate_limit:{customer_id}:*"
        # For now, just log that we can't do pattern-based clear
        frappe.log_error(
            f"Pattern-based rate limit clear requested for {customer_id}",
            "Rate Limit Clear"
        )
