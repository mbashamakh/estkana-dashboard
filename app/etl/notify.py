"""
Sends alert emails through Odoo's own outbound mail (the `mail.mail`
model), reusing the Odoo XML-RPC connection already configured for the
P&L sync (see odoo_client.py for the same _authenticate/_execute_kw
pattern) instead of adding a brand-new third-party email service/account
just for alerting.

Best-effort by design: a failure here (Odoo unreachable, mail not
configured, etc.) must never break the sync job that called it -- the
sync's own success/failure is already tracked via SyncLog regardless of
whether the alert email went out.
"""
from __future__ import annotations

import xmlrpc.client

from app.config import Settings


def send_alert_email(settings: Settings, subject: str, body_html: str) -> bool:
    """Returns True if the email was handed to Odoo's mail queue, False if
    alerting isn't configured (no ALERT_EMAIL / Odoo not configured) or the
    send itself failed."""
    if not settings.odoo_configured or not settings.alert_email:
        return False
    try:
        common = xmlrpc.client.ServerProxy(f"{settings.odoo_url}/xmlrpc/2/common")
        uid = common.authenticate(settings.odoo_db, settings.odoo_username, settings.odoo_api_key, {})
        if not uid:
            return False

        models = xmlrpc.client.ServerProxy(f"{settings.odoo_url}/xmlrpc/2/object")
        mail_id = models.execute_kw(
            settings.odoo_db, uid, settings.odoo_api_key,
            "mail.mail", "create",
            [{
                "subject": subject,
                "body_html": body_html,
                "email_to": settings.alert_email,
                "auto_delete": True,
            }],
        )
        models.execute_kw(
            settings.odoo_db, uid, settings.odoo_api_key,
            "mail.mail", "send", [[mail_id]],
        )
        return True
    except Exception:  # noqa: BLE001
        return False
