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
from sqlalchemy import select

from app.config import get_settings
from app.db.session import SessionLocal
from app.db.models import LoyverseDaily, SyncLog
from app.etl import loyverse_client, odoo_client
from app.etl.loyverse_pnl import aggregate_receipts, build_item_category_lookup
from app.etl.odoo_client import _authenticate, _execute_kw
from app.etl.run_loyverse_sync import sync_loyverse
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


@router.get("/api/_diag/odoo-query")
def diag_odoo_query(secret: str, model: str, domain: str, fields: str, limit: int = 200, offset: int = 0, order: str = ""):
    """
    Read-only ad-hoc query: search_read(model, domain, fields, limit, offset,
    order). `domain` and `fields` are JSON-encoded strings (e.g.
    domain=[["account_id.code","=","51102000"],["date",">=","2026-08-01"]],
    fields=["id","date","name","quantity"]) so this can answer one-off
    reconnaissance questions (the GL-51102000-attachment-quantity
    investigation) without a new purpose-built endpoint + deploy each time.
    Read-only by construction (search_read only) and still DIAG_SECRET-gated
    like every other endpoint here -- same "temporary, delete once verified"
    status as the rest of this file.
    """
    expected = os.getenv("DIAG_SECRET")
    if not expected or secret != expected:
        raise HTTPException(status_code=404)

    import json as _json

    settings = get_settings()
    try:
        domain_parsed = _json.loads(domain)
        fields_parsed = _json.loads(fields)
        uid = _authenticate(settings)
        kwargs = {"fields": fields_parsed, "limit": limit, "offset": offset}
        if order:
            kwargs["order"] = order
        rows = _execute_kw(settings, uid, model, "search_read", [domain_parsed], kwargs)
        count = _execute_kw(settings, uid, model, "search_count", [domain_parsed])
        return {"ok": True, "total_count": count, "returned": len(rows), "rows": rows}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


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


@router.get("/api/_diag/loyverse-sync-now")
def diag_loyverse_sync_now(secret: str):
    """
    One-off manual trigger for the real Loyverse sync (incremental window +
    one backfill day), ahead of the hourly cron job existing. See
    run_loyverse_sync.sync_loyverse() for what each run actually does.
    """
    expected = os.getenv("DIAG_SECRET")
    if not expected or secret != expected:
        raise HTTPException(status_code=404)

    settings = get_settings()
    db = SessionLocal()
    try:
        result = sync_loyverse(db, settings)
        return {"ok": True, "result": result}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    finally:
        db.close()


@router.get("/api/_diag/loyverse-daily")
def diag_loyverse_daily(secret: str, branch: str | None = None):
    """Inspect what's actually stored in loyverse_daily right now, to
    confirm the sync + backfill are landing correctly. Optional `branch`
    filters to one branch (e.g. `&branch=ARBEEN` to double check exclusion)."""
    expected = os.getenv("DIAG_SECRET")
    if not expected or secret != expected:
        raise HTTPException(status_code=404)

    db = SessionLocal()
    try:
        q = select(LoyverseDaily).order_by(LoyverseDaily.branch, LoyverseDaily.date)
        if branch:
            q = q.where(LoyverseDaily.branch == branch)
        rows = db.scalars(q).all()
        by_branch: dict[str, dict] = {}
        for r in rows:
            b = by_branch.setdefault(r.branch, {"days": 0, "earliest": r.date, "latest": r.date, "total_sales": 0.0})
            b["days"] += 1
            b["earliest"] = min(b["earliest"], r.date)
            b["latest"] = max(b["latest"], r.date)
            b["total_sales"] = round(b["total_sales"] + r.sales, 2)
        return {"ok": True, "row_count": len(rows), "by_branch": by_branch}
    finally:
        db.close()


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
            "skipped_test_branch_receipts": agg["skipped_test_branch_receipts"],
            "branch_summaries": branch_summaries,
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


@router.get("/api/_diag/loyverse-items-by-branch")
def diag_loyverse_items_by_branch(secret: str, branch: str, date_min: str, date_max: str):
    """
    Item-level sales for ONE branch over a date range — built for the
    Est-010 (Forosia) sale-consumption request: qty sold (net of refunds)
    + gross sales per menu item, so it can be multiplied against the
    recipe/cost sheet's per-item cost to get theoretical ingredient
    consumption and compared against actual GL 51102000 purchases.

    `branch` matches an odoo_name value in STORE_ID_TO_ODOO_NAME (e.g.
    "FOROSIA"). `date_min`/`date_max` are ISO 8601 strings passed straight
    through as Loyverse's created_at_min/created_at_max (e.g.
    date_min=2026-08-01T00:00:00.000Z, date_max=2026-09-01T00:00:00.000Z).

    Same exclusion rules as loyverse_pnl.aggregate_receipts (cancelled
    receipts skipped, refunds netted out) but aggregated by item across the
    whole window rather than per-day, since this only needs a period total.
    """
    expected = os.getenv("DIAG_SECRET")
    if not expected or secret != expected:
        raise HTTPException(status_code=404)

    from collections import defaultdict

    from app.etl.loyverse_store_map import STORE_ID_TO_ODOO_NAME

    store_ids = [sid for sid, name in STORE_ID_TO_ODOO_NAME.items() if name == branch]
    if not store_ids:
        return {
            "ok": False,
            "error": f"Unknown branch {branch!r}. Known: {sorted(set(STORE_ID_TO_ODOO_NAME.values()))}",
        }
    store_id = store_ids[0]

    settings = get_settings()
    try:
        receipts = loyverse_client.list_all_receipts(
            settings, created_at_min=date_min, created_at_max=date_max, store_id=store_id,
        )
        items = loyverse_client.list_all_items(settings)
        item_category = build_item_category_lookup(items)

        item_totals: dict[str, dict] = defaultdict(lambda: {
            "cat": "Other", "qty": 0.0, "sales": 0.0, "refund_qty": 0.0, "refund_sales": 0.0,
        })
        sale_receipt_count = 0
        refund_receipt_count = 0
        cancelled_count = 0
        skipped_other_store = 0
        total_sales_net = 0.0
        total_discount = 0.0
        total_refund = 0.0

        for r in receipts:
            if r.get("cancelled_at"):
                cancelled_count += 1
                continue
            if r.get("store_id") != store_id:
                # Defensive — store_id is already server-side filtered, but
                # don't silently trust that if the API ever changes.
                skipped_other_store += 1
                continue

            is_refund = r.get("receipt_type") == "REFUND"
            amount = r.get("total_money") or 0.0

            if is_refund:
                refund_receipt_count += 1
                total_refund += amount
                total_sales_net -= amount
                for li in r.get("line_items", []):
                    name = li.get("item_name") or "(unnamed item)"
                    entry = item_totals[name]
                    entry["cat"] = item_category.get(li.get("item_id"), entry["cat"])
                    entry["refund_qty"] += li.get("quantity") or 0.0
                    entry["refund_sales"] += li.get("total_money") or 0.0
            else:
                sale_receipt_count += 1
                total_sales_net += amount
                total_discount += r.get("total_discount") or 0.0
                for li in r.get("line_items", []):
                    name = li.get("item_name") or "(unnamed item)"
                    entry = item_totals[name]
                    entry["cat"] = item_category.get(li.get("item_id"), entry["cat"])
                    entry["qty"] += li.get("quantity") or 0.0
                    entry["sales"] += li.get("total_money") or 0.0

        items_out = []
        for name, v in item_totals.items():
            items_out.append({
                "item_name": name,
                "category": v["cat"],
                "qty_sold": round(v["qty"], 3),
                "sales": round(v["sales"], 2),
                "qty_refunded": round(v["refund_qty"], 3),
                "sales_refunded": round(v["refund_sales"], 2),
                "net_qty": round(v["qty"] - v["refund_qty"], 3),
                "net_sales": round(v["sales"] - v["refund_sales"], 2),
            })
        items_out.sort(key=lambda x: -x["net_sales"])

        return {
            "ok": True,
            "branch": branch,
            "store_id": store_id,
            "window": {"from": date_min, "to": date_max},
            "raw_receipt_count": len(receipts),
            "sale_receipt_count": sale_receipt_count,
            "refund_receipt_count": refund_receipt_count,
            "cancelled_receipt_count": cancelled_count,
            "skipped_other_store_receipts": skipped_other_store,
            "total_sales_net": round(total_sales_net, 2),
            "total_discount": round(total_discount, 2),
            "total_refund": round(total_refund, 2),
            "item_count": len(items_out),
            "items": items_out,
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


@router.get("/api/_diag/reset-password")
def diag_reset_password(secret: str, email: str, new_password: str):
    """
    One-off admin-password reset, for when the original ADMIN_PASSWORD used
    on first boot has been lost -- changing the ADMIN_PASSWORD env var alone
    does nothing after first boot, since main.py's bootstrap only runs while
    the users table is empty. Sets the given user's password directly (or
    creates them as an active admin if they don't exist yet).
    """
    expected = os.getenv("DIAG_SECRET")
    if not expected or secret != expected:
        raise HTTPException(status_code=404)

    import bcrypt
    from app.db.models import User

    pw_hash = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()
    db = SessionLocal()
    try:
        user = db.scalar(select(User).where(User.email == email.lower().strip()))
        if user:
            user.password_hash = pw_hash
            user.is_active = True
            action = "updated"
        else:
            user = User(email=email.lower().strip(), password_hash=pw_hash, is_admin=True, is_active=True)
            db.add(user)
            action = "created"
        db.commit()
        return {"ok": True, "email": user.email, "action": action}
    finally:
        db.close()


@router.get("/api/_diag/data-preview")
def diag_data_preview(secret: str):
    """
    Diag-secret-protected mirror of GET /api/data's shape, so it can be
    inspected without a logged-in session -- trimmed to meta/sync_status
    plus one real branch's aggregate numbers, since the full 18-branch
    payload is large and most of it isn't needed to sanity-check the
    month/day-offset plumbing.
    """
    expected = os.getenv("DIAG_SECRET")
    if not expected or secret != expected:
        raise HTTPException(status_code=404)

    from app.etl.data_builder import build_data_response

    db = SessionLocal()
    try:
        resp = build_data_response(db)
    finally:
        db.close()

    sample_branch = next((b for b in resp["branches"] if b.get("is_real_sales")), None)
    branch_preview = None
    if sample_branch:
        daily = sample_branch["daily"]
        nonzero = [(i, v) for i, v in enumerate(daily) if v]
        branch_preview = {
            "name": sample_branch["name"],
            "id": sample_branch["id"],
            "daily_len": len(daily),
            "daily_nonzero_count": len(nonzero),
            "daily_nonzero_index_range": [nonzero[0][0], nonzero[-1][0]] if nonzero else None,
            "daily_nonzero_sample": nonzero[:3],
            "monthly": sample_branch["monthly"],
        }

    return {
        "ok": True,
        "meta": resp["meta"],
        "sync_status": resp["sync_status"],
        "sample_real_branch": branch_preview,
    }


@router.get("/api/_diag/loyverse-month-audit")
def diag_loyverse_month_audit(secret: str, month: str = "2026-08"):
    """
    Raw LoyverseDaily rows for one month, grouped two ways: company-wide
    total per DAY (to spot which specific days are anomalously low across
    every branch at once -- the signature of a sync/windowing bug) and
    total per BRANCH for the month (to spot a branch-specific issue
    instead). Bypasses data_builder.py entirely -- this is exactly what's
    stored, no aggregation-logic assumptions.
    """
    expected = os.getenv("DIAG_SECRET")
    if not expected or secret != expected:
        raise HTTPException(status_code=404)

    db = SessionLocal()
    try:
        rows = db.scalars(
            select(LoyverseDaily).where(LoyverseDaily.date.like(f"{month}-%")).order_by(LoyverseDaily.date)
        ).all()
    finally:
        db.close()

    by_day: dict[str, dict] = {}
    by_branch: dict[str, dict] = {}
    for r in rows:
        d = by_day.setdefault(r.date, {"sales": 0.0, "orders": 0, "branches": 0})
        d["sales"] += r.sales
        d["orders"] += r.orders
        d["branches"] += 1
        b = by_branch.setdefault(r.branch, {"sales": 0.0, "orders": 0, "days": 0})
        b["sales"] += r.sales
        b["orders"] += r.orders
        b["days"] += 1

    for d in by_day.values():
        d["sales"] = round(d["sales"], 2)
    for b in by_branch.values():
        b["sales"] = round(b["sales"], 2)

    return {
        "ok": True,
        "month": month,
        "row_count": len(rows),
        "company_total_sales": round(sum(r.sales for r in rows), 2),
        "company_total_orders": sum(r.orders for r in rows),
        "by_day": dict(sorted(by_day.items())),
        "by_branch": dict(sorted(by_branch.items(), key=lambda kv: -kv[1]["sales"])),
    }


@router.get("/api/_diag/loyverse-debug")
def diag_loyverse_debug(secret: str, dates: str = "2026-08-01,2026-08-02,2026-08-03"):
    """
    Two things needed to root-cause the every-other-day near-zero pattern
    found via loyverse-month-audit:

    1. SyncLog history for the loyverse source -- each run's message records
       exactly which day(s) it backfilled and how many receipts it pulled,
       so a repeat/skip/jump in the backfill's walk-backward-by-one-day
       sequence would show up directly here, run by run.
    2. Raw per-branch rows (with `updated_at`) for a few specific dates, so
       a near-zero day's row can be checked for WHEN it was last written --
       once, during initial backfill (meaning the pull itself was
       incomplete), vs. recently (meaning something is overwriting an
       already-correct row later).
    """
    expected = os.getenv("DIAG_SECRET")
    if not expected or secret != expected:
        raise HTTPException(status_code=404)

    date_list = [d.strip() for d in dates.split(",") if d.strip()]

    db = SessionLocal()
    try:
        logs = db.scalars(
            select(SyncLog)
            .where(SyncLog.source == "loyverse")
            .order_by(SyncLog.started_at.desc())
            .limit(40)
        ).all()
        rows = db.scalars(
            select(LoyverseDaily)
            .where(LoyverseDaily.date.in_(date_list))
            .order_by(LoyverseDaily.date, LoyverseDaily.branch)
        ).all()
    finally:
        db.close()

    return {
        "ok": True,
        "sync_log": [
            {
                "id": l.id,
                "success": l.success,
                "message": l.message,
                "started_at": l.started_at.isoformat() if l.started_at else None,
                "finished_at": l.finished_at.isoformat() if l.finished_at else None,
            }
            for l in logs
        ],
        "rows": [
            {
                "branch": r.branch,
                "date": r.date,
                "sales": r.sales,
                "orders": r.orders,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            }
            for r in rows
        ],
    }


@router.get("/api/_diag/loyverse-rewind-cursor")
def diag_loyverse_rewind_cursor(secret: str, to_date: str):
    """
    Manually rewinds the backfill cursor (see SyncCursor's docstring) so the
    next backfill run(s) redo everything from `to_date` backward -- for
    recovering a specific day that ended up with a bad value some way OTHER
    than the two known root causes already fixed (e.g. a transient partial
    receipts pull under load), since the normal cursor walk never revisits
    a day once it's moved past it. Safe: upserts are idempotent REPLACEs,
    so redoing already-good days along the way is harmless, just costs a
    bit of extra sync time. `to_date` becomes the new `backfilled_through`
    value, so the next backfill iteration's first target is `to_date` minus
    one day -- pass the day AFTER the first one you want redone.
    """
    expected = os.getenv("DIAG_SECRET")
    if not expected or secret != expected:
        raise HTTPException(status_code=404)

    from app.db.models import SyncCursor
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    db = SessionLocal()
    try:
        before = db.get(SyncCursor, "loyverse")
        before_value = before.backfilled_through if before else None

        stmt = pg_insert(SyncCursor).values(source="loyverse", backfilled_through=to_date)
        stmt = stmt.on_conflict_do_update(
            index_elements=["source"], set_={"backfilled_through": stmt.excluded.backfilled_through}
        )
        db.execute(stmt)
        db.commit()
        return {"ok": True, "cursor_before": before_value, "cursor_after": to_date}
    finally:
        db.close()


@router.get("/api/_diag/loyverse-resync-range")
def diag_loyverse_resync_range(secret: str, date_from: str, date_to: str):
    """
    Force a full, deliberate re-pull + upsert of EVERY branch's data for
    each calendar day in [date_from, date_to] (inclusive), no matter what's
    currently stored for those days.

    Built to fix a confirmed real-world gap: loyverse_daily rows for
    2026-08-24..2026-08-31 ended up scattered between correct values and
    near-zero/missing ones on a per-branch, per-day basis (e.g. FOROSIA
    near-zero every day that week, other branches only near-zero on SOME
    of those days, SHARKIA missing entirely on 2026-08-29 and 2026-08-31).
    Traced via loyverse-debug's row updated_at timestamps to a manual
    recovery attempt made around 2026-08-26/27 -- most likely repeated
    loyverse-rewind-cursor + loyverse-sync-now calls, landing on top of
    the original spillover-cursor bug (see 8ded1e3/b92040e) which was
    still live for part of that window before its fix actually deployed
    (the cron's redeploy itself lagged its commit by almost a full day).

    Unlike loyverse-rewind-cursor -- which only nudges a single GLOBAL
    cursor and relies on the hourly cron's bounded backfill loop to
    eventually walk back over the target range, with no record of what
    ran or why -- this pulls exactly the requested days directly and
    synchronously, and always writes a SyncLog entry, so a future "why did
    this day's numbers change" question has a real answer.

    Reuses the same per-day full-window pull (_pull_and_upsert_full_day)
    the hourly cron uses for today/yesterday, under the same advisory
    lock, so this can never race with it and can't reintroduce the
    original "partial window" class of bug. Upserts are idempotent
    REPLACEs, so re-running this over already-correct days is harmless --
    capped at 31 days per call so one bad call can't accidentally rewrite
    a huge swath of history.
    """
    expected = os.getenv("DIAG_SECRET")
    if not expected or secret != expected:
        raise HTTPException(status_code=404)

    from datetime import datetime, timedelta, timezone

    from app.etl.run_loyverse_sync import _pull_and_upsert_full_day, _release_sync_lock, _try_acquire_sync_lock

    try:
        start = datetime.strptime(date_from, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end = datetime.strptime(date_to, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        return {"ok": False, "error": f"date_from/date_to must be YYYY-MM-DD: {exc}"}
    if end < start:
        return {"ok": False, "error": "date_to must be on or after date_from"}
    if (end - start).days > 31:
        return {"ok": False, "error": "range too large for one call (max 31 days) -- call again in chunks"}

    settings = get_settings()
    db = SessionLocal()
    started = datetime.now(timezone.utc)
    if not _try_acquire_sync_lock(db):
        db.close()
        return {"ok": False, "error": "another loyverse sync is currently in progress -- try again shortly"}

    resynced: list[dict] = []
    error = None
    try:
        day = start
        while day <= end:
            try:
                count = _pull_and_upsert_full_day(db, settings, day)
                db.commit()
                resynced.append({"date": day.strftime("%Y-%m-%d"), "receipts": count})
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                error = f"{day.strftime('%Y-%m-%d')}: {exc}"
                break
            day += timedelta(days=1)

        success = error is None
        message = (
            f"manual resync-range {date_from}..{date_to}: "
            + (
                f"{len(resynced)} day(s) done, {sum(r['receipts'] for r in resynced)} receipts"
                if resynced else "0 days done"
            )
            + (f" (FAILED at {error})" if error else "")
        )
        db.add(SyncLog(
            source="loyverse", success=success, message=message[:2000],
            started_at=started, finished_at=datetime.now(timezone.utc),
        ))
        db.commit()
        return {"ok": success, "resynced": resynced, "error": error}
    finally:
        _release_sync_lock(db)
        db.close()
