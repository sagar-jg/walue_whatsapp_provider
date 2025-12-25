"""
Installation scripts for Walue WhatsApp Provider
"""

import os
import json
import frappe


def after_install():
    """
    Import Web Page fixtures after app installation
    """
    import_web_pages()


def import_web_pages():
    """
    Import Web Pages from fixture JSON files
    """
    fixtures_path = frappe.get_app_path("walue_whatsapp_provider", "fixtures")

    fixture_files = [
        "web_page_index.json",
        "web_page_dashboard.json",
        "web_page_success.json"
    ]

    for fixture_file in fixture_files:
        file_path = os.path.join(fixtures_path, fixture_file)

        if not os.path.exists(file_path):
            print(f"Fixture file not found: {file_path}")
            continue

        try:
            with open(file_path, "r") as f:
                records = json.load(f)

            for record in records:
                doctype = record.get("doctype")
                name = record.get("name")

                # Delete existing record if exists (to ensure clean import)
                if frappe.db.exists(doctype, name):
                    frappe.delete_doc(doctype, name, ignore_permissions=True, force=True)
                    print(f"Deleted existing {doctype}: {name}")

                # Create new record
                doc = frappe.get_doc(record)
                doc.insert(ignore_permissions=True)
                print(f"Created {doctype}: {name}")

            frappe.db.commit()

        except Exception as e:
            print(f"Error importing {fixture_file}: {str(e)}")
            import traceback
            traceback.print_exc()
            frappe.db.rollback()

    print("Web Page fixtures imported successfully!")
