from frappe import _


def get_data():
    return [
        {
            "module_name": "Walue Whatsapp Provider",
            "color": "green",
            "icon": "octicon octicon-broadcast",
            "type": "module",
            "label": _("WhatsApp Provider"),
            "description": _("Manage WhatsApp customers, billing, and API proxy"),
        }
    ]
