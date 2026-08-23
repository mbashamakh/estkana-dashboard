"""
Temporary diagnostic endpoint — lets me verify the live Odoo connection
works from Render (which has real internet access, unlike the dev sandbox
this was built in) without needing DB/session plumbing. Protected by a
shared secret (DIAG_SECRET env var) rather than login, since this checks
infrastructure before any users necessarily exist yet.

Delete this file (and its include_router call in app/main.py) once the
Odoo client is verified and the real hourly sync is confirmed working —
it deliberately bypasses normal auth and shouldn't stay in a production
app long-term.
"""
from __future__ import annotations

import os
import xmlrpc.client

from fastapi import APIRouter, HTTPException

from app.config import get_settings
from app.db.session import SessionLocal
from app.etl import odoo_client
from app.etl.odoo_client import _authenticate, _execute_kw
from app.etl.run_odoo_sync import sync_odoo

router = APIRouter()


@router.get("/api/_diag/sync-now")
def diag_sync_now(secret: str):
    """One-off manual trigger for the real sync, ahead of the hourly cron job existing."""
    expected = os.getenv("DIAG_SECRET")
    if not expected or secret != expected:
        raise HTTPException(status_code=404)

    settings = get_settings()
    db = SessionLocal()
    try:
        rev, cogs, opex = odoo_client.fetch_records(settings)
        result = sync_odoo(db, rev, cogs, opex)
        return {"ok": True, "result": result}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    finally:
        db.close()


@router.get("/api/_diag/odoo-fields")
def diag_odoo_fields(secret: str, model: str = "account.analytic.line", search: str = ""):
    """
    Lists field names (and types) on the given model, so I can stop guessing
    field names one deploy at a time. Optional `search` filters to field
    names containing that substring (case-insensitive).
    """
    expected = os.getenv("DIAG_SECRET")
    if not expected or secret != expected:
        raise HTTPException(status_code=404)

    settings = get_settings()
    try:
        uid = _authenticate(settings)
        fields = _execute_kw(
            settings, uid, model, "fields_get", [], {"attributes": ["string", "type", "relation"]}
        )
        if search:
            fields = {k: v for k, v in fields.items() if search.lower() in k.lower()}
        return {"ok": True, "field_count": len(fields), "fields": fields}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


@router.get("/api/_diag/odoo-dblist")
def diag_odoo_dblist(secret: str):
    """
    Asks the Odoo server what databases it actually has, via the unauthenticated
    db.list() XML-RPC method — no login needed, just a way to stop guessing the
    database name. Some Odoo SaaS instances disable this for security; if so,
    this will return the RPC error explaining that instead.
    """
    expected = os.getenv("DIAG_SECRET")
    if not expected or secret != expected:
        raise HTTPException(status_code=404)

    settings = get_settings()
    try:
        db_service = xmlrpc.client.ServerProxy(f"{settings.odoo_url}/xmlrpc/2/db")
        databases = db_service.list()
        return {"ok": True, "databases": databases}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


@router.get("/api/_diag/odoo")
def diag_odoo(secret: str):
    expected = os.getenv("DIAG_SECRET")
    if not expected or secret != expected:
        raise HTTPException(status_code=404)

    settings = get_settings()
    print(f"DIAG_ODOO_HIT odoo_db={settings.odoo_db!r} odoo_url={settings.odoo_url!r}", flush=True)
    config_seen = {
        "odoo_url": settings.odoo_url,
        "odoo_db": settings.odoo_db,
        "odoo_username": settings.odoo_username,
    }
    try:
        rev, cogs, opex = odoo_client.fetch_records(settings)
    except Exception as exc:  # noqa: BLE001 -- surfacing the raw error is the point of this endpoint
        return {"ok": False, "error": str(exc), "config_seen": config_seen}

    arbeen_jan = next(
        (r["v"] for r in rev if "ARBEEN" in r["n"] and r["m"] == "January 2026"), None
    )
    return {
        "ok": True,
        "revenue_records": len(rev),
        "cogs_records": len(cogs),
        "opex_records": len(opex),
        "arbeen_jan_2026_revenue": arbeen_jan,
        "expected": 148293.4,
        "matches_known_good": arbeen_jan == 148293.4,
    }
