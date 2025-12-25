"""
Bench commands for Walue WhatsApp Provider
"""

import click
import frappe
from frappe.commands import pass_context


@click.command("import-walue-pages")
@pass_context
def import_walue_pages(context):
    """
    Import Walue Web Pages from fixtures
    Usage: bench --site [sitename] import-walue-pages
    """
    site = context.sites[0] if context.sites else None
    if not site:
        click.echo("Please specify a site")
        return
    
    frappe.init(site=site)
    frappe.connect()
    
    try:
        from walue_whatsapp_provider.install import import_web_pages
        import_web_pages()
        click.echo("Web Pages imported successfully!")
    except Exception as e:
        click.echo(f"Error: {str(e)}")
    finally:
        frappe.destroy()


commands = [import_walue_pages]
