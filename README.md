# Walue WhatsApp Provider

WhatsApp Calling & Messaging Provider Platform for Frappe/ERPNext.

## Overview

This is the **provider-side** Frappe app for Walue Biz's WhatsApp SaaS platform. It manages:

- Customer accounts and subscriptions
- Meta WhatsApp Business API proxy
- Janus WebRTC gateway integration
- OAuth authentication for customer apps
- Aggregated usage metrics and billing

## Installation

```bash
bench get-app https://github.com/sagar-jg/walue_whatsapp_provider.git
bench --site your-site install-app walue_whatsapp_provider
```

## Configuration

1. Go to **WhatsApp Provider Settings**
2. Enter your Meta App ID and App Secret
3. Configure Janus WebRTC gateway URL
4. Set webhook verify token

## DocTypes

| DocType | Purpose |
|---------|---------|
| WhatsApp Provider Settings | Central configuration |
| WhatsApp Customer | Customer accounts |
| Subscription Plan | Pricing tiers |
| Daily Usage Metrics | Aggregated daily usage |
| Monthly Usage Summary | Monthly billing totals |
| Customer Invoice | Invoices |
| Embedded Signup Session | Meta signup tracking |

## Data Privacy

This app follows strict data isolation principles:
- **NO** message content stored
- **NO** phone numbers stored
- **NO** call recordings stored
- Only aggregated counts and costs for billing

## License

MIT
