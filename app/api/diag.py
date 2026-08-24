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
from app.etl import loyverse_client, odoo_client
from app.etl.loyverse_pnl import aggregate_receipts, build_item_category_lookup
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


@router.get("/api/_diag/loyverse")
def diag_loyverse(secret: str):
    """
    Verifies the Loyverse token works and shows the real store list + a
    tiny receipts sample, so I can (a) confirm the token is valid, (b)
    check the real store names against odoo_pnl.LOYVERSE_MAP, and (c) see
    what a real receipt object looks like before building the full ETL.
    """
    expected = os.getenv("DIAG_SECRET")
    if not expected or secret != expected:
        raise HTTPException(status_code=404)

    settings = get_settings()
    result: dict = {}

    # Two independent try/excepts so a hang/failure on one call doesn't hide
    # whether the other one actually works.
    try:
        stores = loyverse_client.list_stores(settings)
        result["stores"] = {
            "ok": True,
            "store_count": len(stores),
            "stores": [{"id": s.get("id"), "name": s.get("name")} for s in stores],
        }
    except Exception as exc:  # noqa: BLE001
        result["stores"] = {"ok": False, "error": str(exc)}

    try:
        receipts_page = loyverse_client.list_receipts_page(
            settings,
            created_at_min="2026-08-15T00:00:00.000Z",
            created_at_max="2026-08-17T00:00:00.000Z",
            limit=3,
        )
        result["receipts"] = {
            "ok": True,
            "sample_receipt_count": len(receipts_page.get("receipts", [])),
            "sample_receipts": receipts_page.get("receipts", []),
            "cursor": receipts_page.get("cursor"),
        }
    except Exception as exc:  # noqa: BLE001
        result["receipts"] = {"ok": False, "error": str(exc)}

    result["ok"] = result["stores"]["ok"] and result["receipts"]["ok"]
    return result


@router.get("/api/_diag/loyverse-catalog")
def diag_loyverse_catalog(secret: str):
    """Category list + one page of items, to see how to attach a category
    label to each receipt line_item (which only carries item_id/item_name)."""
    expected = os.getenv("DIAG_SECRET")
    if not expected or secret != expected:
        raise HTTPException(status_code=404)

    settings = get_settings()
    try:
        categories = loyverse_client.list_categories(settings)
        items_page = loyverse_client.list_items_page(settings, limit=5)
        return {
            "ok": True,
            "category_count": len(categories),
            "categories": categories,
            "sample_items": items_page.get("items", []),
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


@router.get("/api/_diag/loyverse-agg")
def diag_loyverse_agg(secret: str, days: int = 2):
    """
    Pulls the last `days` days of real receipts + the item catalog, runs
    them through loyverse_pnl.aggregate_receipts(), and returns a compact
    per-branch summary — small enough to eyeball, before this gets scaled
    up to a full historical backfill. Kept to a short window deliberately:
    this is for verifying the aggregation logic is right, not for pulling
    real data into the database yet.
    """
    expected = os.getenv("DIAG_SECRET")
    if not expected or secret != expected:
        raise HTTPException(status_code=404)

    settings = get_settings()
    try:
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        created_at_min = (now - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00.000Z")
        created_at_max = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")

        receipts = loyverse_client.list_all_receipts(settings, created_at_min, created_at_max)
        items = loyverse_client.list_all_items(settings)
        item_category = build_item_category_lookup(items)
        agg = aggregate_receipts(receipts, item_category)

        branch_summaries = {}
        for branch, days_data in agg["branches"].items():
            total_sales = sum(d["sales"] for d in days_data.values())
            total_orders = sum(d["orders"] for d in days_data.values())
            total_discount = sum(d["discount_amt"] for d in days_data.values())
            total_refund = sum(d["refund_amt"] for d in days_data.values())
            branch_summaries[branch] = {
                "days_with_activity": len(days_data),
                "total_sales": round(total_sales, 2),
                "total_orders": total_orders,
                "total_discount": round(total_discount, 2),
                "total_refund": round(total_refund, 2),
            }

        return {
            "ok": True,
            "window": {"from": created_at_min, "to": created_at_max},
            "raw_receipt_count": len(receipts),
            "item_catalog_count": len(items),
            "skipped_unknown_store_receipts": agg["skipped_unknown_store_receipts"],
            "branch_summaries": branch_summaries,
        }
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
