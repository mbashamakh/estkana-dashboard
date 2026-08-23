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

from fastapi import APIRouter, HTTPException

from app.config import get_settings
from app.etl import odoo_client

router = APIRouter()


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
