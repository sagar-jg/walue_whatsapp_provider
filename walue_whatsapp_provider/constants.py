"""
Constants for Walue WhatsApp Provider App
All business rules, rate limits, and configuration constants
"""

# =============================================================================
# META API CONFIGURATION
# =============================================================================

# Meta API Version
META_API_DEFAULT_VERSION = "v21.0"
META_API_BASE_URL = "https://graph.facebook.com"

# =============================================================================
# RATE LIMITS (FROM META DOCUMENTATION)
# =============================================================================

# Call Permission Request Limits
CALL_PERMISSION_DAILY_LIMIT = 1  # Max permission requests per 24 hours
CALL_PERMISSION_WEEKLY_LIMIT = 2  # Max permission requests per 7 days
MAX_CALLS_AFTER_PERMISSION = 5  # Max calls per 24 hours after permission granted
PERMISSION_VALIDITY_DAYS = 7  # Permission expires after 7 days
MAX_UNANSWERED_CALLS_BEFORE_REVOKE = 4  # Permission revoked after 4 unanswered calls

# Conversation Windows
CONVERSATION_WINDOW_HOURS = 24  # Free messaging window after user message
FREE_ADS_WINDOW_HOURS = 72  # Free window after ad click

# =============================================================================
# OAUTH CONFIGURATION
# =============================================================================

DEFAULT_OAUTH_TOKEN_EXPIRY = 3600  # 1 hour in seconds
DEFAULT_OAUTH_REFRESH_EXPIRY = 2592000  # 30 days in seconds

# =============================================================================
# BILLING DEFAULTS
# =============================================================================

DEFAULT_CALL_MARKUP_PERCENTAGE = 35.0  # 35%
DEFAULT_MESSAGE_MARKUP_PERCENTAGE = 30.0  # 30%
DEFAULT_BASE_FEE = 29.00  # USD per month

# Pricing Tiers
PLAN_STARTER = {
    "name": "Starter",
    "base_fee": 29.00,
    "call_markup": 0.35,
    "message_markup": 0.30,
    "features": ["calling", "messaging", "basic_analytics"]
}

PLAN_PROFESSIONAL = {
    "name": "Professional",
    "base_fee": 99.00,
    "call_markup": 0.30,
    "message_markup": 0.25,
    "features": ["calling", "messaging", "advanced_analytics", "call_recording"]
}

PLAN_ENTERPRISE = {
    "name": "Enterprise",
    "base_fee": 299.00,
    "call_markup": 0.25,
    "message_markup": 0.20,
    "features": ["calling", "messaging", "advanced_analytics", "call_recording", "priority_support"]
}

# =============================================================================
# DATA RETENTION POLICIES
# =============================================================================

USAGE_METRICS_RETENTION_DAYS = 90  # Keep aggregated metrics for 90 days
BILLING_RECORDS_RETENTION_YEARS = 7  # Tax/legal requirement
SYSTEM_LOG_RETENTION_DAYS = 7  # Debug logs
SESSION_CLEANUP_HOURS = 24  # Cleanup expired sessions after 24 hours

# =============================================================================
# QUEUE NAMES
# =============================================================================

QUEUE_METRICS_AGGREGATION = "long"
QUEUE_BILLING = "long"
QUEUE_CLEANUP = "short"
QUEUE_WEBHOOK_PROCESSING = "default"

# =============================================================================
# JANUS WEBRTC CONFIGURATION
# =============================================================================

JANUS_DEFAULT_PORT = 8188
JANUS_ADMIN_PORT = 7088
JANUS_SESSION_TIMEOUT = 60  # seconds
JANUS_ROOM_EXPIRY = 3600  # 1 hour

# =============================================================================
# CALLING AVAILABILITY BY COUNTRY
# =============================================================================

# Countries where WhatsApp Business Calling is NOT available
CALLING_RESTRICTED_COUNTRIES = [
    "US",  # United States
    "CA",  # Canada
    "NG",  # Nigeria
    "EG",  # Egypt
    "VN",  # Vietnam
    "TR",  # Turkey
]

# =============================================================================
# SYSTEM MESSAGES
# =============================================================================

MSG_SIGNUP_INITIATED = "Embedded signup initiated. Please complete the process."
MSG_SIGNUP_COMPLETED = "WhatsApp Business Account connected successfully!"
MSG_CALLING_NOT_AVAILABLE = "WhatsApp calling is not available in your region."
MSG_QUOTA_EXCEEDED = "Usage quota exceeded. Please upgrade your plan."
MSG_CUSTOMER_SUSPENDED = "Your account has been suspended. Please contact support."
MSG_FEATURE_NOT_ENABLED = "This feature is not enabled for your subscription plan."

# =============================================================================
# ERROR MESSAGES
# =============================================================================

ERR_OAUTH_FAILED = "Authentication failed. Please try again."
ERR_WABA_NOT_FOUND = "WhatsApp Business Account not found."
ERR_INVALID_CREDENTIALS = "Invalid credentials provided."
ERR_RATE_LIMIT = "Rate limit exceeded. Please try again later."
ERR_META_API = "Meta API error. Please contact support."
ERR_JANUS_CONNECTION = "WebRTC gateway connection failed. Please try again."
ERR_CUSTOMER_NOT_FOUND = "Customer account not found."
ERR_INVALID_TOKEN = "Invalid or expired token."
ERR_INSUFFICIENT_BALANCE = "Insufficient account balance."
ERR_SESSION_EXPIRED = "Session expired. Please reconnect."

# =============================================================================
# HTTP STATUS CODES
# =============================================================================

HTTP_OK = 200
HTTP_CREATED = 201
HTTP_BAD_REQUEST = 400
HTTP_UNAUTHORIZED = 401
HTTP_FORBIDDEN = 403
HTTP_NOT_FOUND = 404
HTTP_RATE_LIMITED = 429
HTTP_SERVER_ERROR = 500

# =============================================================================
# WEBHOOK EVENT TYPES
# =============================================================================

WEBHOOK_MESSAGE_STATUS = "message_status"
WEBHOOK_INBOUND_MESSAGE = "inbound_message"
WEBHOOK_CALL_PERMISSION_REPLY = "call_permission_reply"
WEBHOOK_CALL_STATUS = "call_status"

# =============================================================================
# CUSTOMER STATUS
# =============================================================================

CUSTOMER_STATUS_ACTIVE = "Active"
CUSTOMER_STATUS_SUSPENDED = "Suspended"
CUSTOMER_STATUS_CANCELLED = "Cancelled"
CUSTOMER_STATUS_PENDING = "Pending"

# =============================================================================
# EMBEDDED SIGNUP STATUS
# =============================================================================

SIGNUP_STATUS_INITIATED = "initiated"
SIGNUP_STATUS_IN_PROGRESS = "in_progress"
SIGNUP_STATUS_COMPLETED = "completed"
SIGNUP_STATUS_FAILED = "failed"

# =============================================================================
# INVOICE STATUS
# =============================================================================

INVOICE_STATUS_DRAFT = "Draft"
INVOICE_STATUS_SENT = "Sent"
INVOICE_STATUS_PAID = "Paid"
INVOICE_STATUS_OVERDUE = "Overdue"

# =============================================================================
# USAGE ALERT THRESHOLDS
# =============================================================================

USAGE_WARNING_THRESHOLD = 0.75  # 75% of quota
USAGE_ALERT_THRESHOLD = 0.90  # 90% of quota
USAGE_EXCEEDED_THRESHOLD = 1.0  # 100% of quota
