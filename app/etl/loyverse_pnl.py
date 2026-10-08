"""
Pure aggregation logic for Loyverse receipts — turns raw receipt/item
records into the per-branch daily sales figures the dashboard needs.
Deliberately has zero network/DB dependency (same reasoning as
odoo_pnl.py): easy to unit test, easy to run against a small real sample
before trusting it against the full receipt history.

STATUS: first pass, not yet verified against real numbers — receipt shape
was seen for exactly 3 sample receipts (via /api/_diag/loyverse), which
didn't include a REFUND or a cancelled receipt, so those code paths are a
reasoned guess:
  - `cancelled_at` set (non-null)              -> excluded entirely.
  - `receipt_type == "REFUND"`                  -> subtracted from sales
    (Loyverse's own docs describe refund receipts as carrying a positive
    total_money representing money returned, not a negative sale).
Both need confirming against a real example before this is trusted for
money the user acts on.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

from app.etl.loyverse_category_map import display_category_for
from app.etl.loyverse_store_map import STORE_ID_TO_ODOO_NAME

# Branches whose Loyverse receipts are skipped entirely. EMPTY on purpose:
# ARBEEN was once excluded here as a "test" setup, but the user confirmed
# 2026-10-08 that nothing in Loyverse is test data -- every store's sales are
# real (excluding it understated September 2026 by SAR 136,586.69). The
# mechanism is kept so a genuine test store can be excluded again in one line.
LOYVERSE_TEST_BRANCHES: set[str] = set()


def build_item_category_lookup(items: list[dict]) -> dict[str, str]:
    """item_id -> dashboard display category (Other/Shabati/Bakery & Snacks/
    Hot drinks/Cold Drink), via each item's Loyverse category_id."""
    return {it["id"]: display_category_for(it.get("category_id")) for it in items if it.get("id")}


# Saudi Arabia is UTC+3 year-round (no DST). Loyverse's own reports bucket each
# sale by the STORE's local calendar day, but receipt_date arrives in UTC, so
# bucketing by the raw UTC date puts each local day's first 3 hours (00:00-03:00
# local) into the previous UTC day, so every day total is shifted. Verified 2026-10-08: ARBEEN and Naseem 3 September totals match Loyverse
# to the cent with local bucketing, and are ~0.3-0.6% off with UTC bucketing.
LOCAL_UTC_OFFSET_HOURS = 3


def _day(receipt: dict) -> str:
    """'2026-08-16' -- the Saudi (UTC+3) calendar day of receipt_date.
    receipt_date (the actual transaction time) is used over created_at (sync
    time) since those can differ, e.g. for a receipt synced late."""
    raw = receipt["receipt_date"]
    try:
        ts = datetime.strptime(raw[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return raw[:10]
    return (ts + timedelta(hours=LOCAL_UTC_OFFSET_HOURS)).strftime("%Y-%m-%d")


def aggregate_receipts(receipts: list[dict], item_category: dict[str, str]) -> dict:
    """
    Returns {"branches": {odoo_branch_name: {date: {...}}},
    "skipped_unknown_store_receipts": int} — one branch/date entry per
    calendar day that had any activity. Receipts for stores not in
    STORE_ID_TO_ODOO_NAME are skipped and counted (shouldn't happen with
    all 18 known, but this is defensive against Loyverse adding a 19th
    store later, and surfaces it instead of silently dropping data).

    Per-day shape:
      {
        "sales": float,           # net of refunds, gross of (inclusive) VAT
        "orders": int,            # SALE receipt count (refunds don't count
                                   # as a new order, they reduce an existing one)
        "discount_amt": float,
        "refund_amt": float,
        "items": {item_name: {"cat": str, "qty": float, "sales": float}},
      }
    """
    out: dict[str, dict[str, dict]] = defaultdict(lambda: defaultdict(lambda: {
        "sales": 0.0, "orders": 0, "discount_amt": 0.0, "refund_amt": 0.0,
        "items": defaultdict(lambda: {"cat": "Other", "qty": 0.0, "sales": 0.0}),
    }))

    skipped_unknown_store = 0
    skipped_unknown_store_ids: dict[str, int] = defaultdict(int)
    skipped_test_branch = 0
    for r in receipts:
        if r.get("cancelled_at"):
            continue
        branch = STORE_ID_TO_ODOO_NAME.get(r.get("store_id"))
        if branch is None:
            skipped_unknown_store += 1
            skipped_unknown_store_ids[str(r.get("store_id"))] += 1
            continue
        if branch in LOYVERSE_TEST_BRANCHES:
            skipped_test_branch += 1
            continue

        day_bucket = out[branch][_day(r)]
        is_refund = r.get("receipt_type") == "REFUND"
        amount = r.get("total_money") or 0.0

        if is_refund:
            day_bucket["sales"] -= amount
            day_bucket["refund_amt"] += amount
        else:
            day_bucket["sales"] += amount
            day_bucket["orders"] += 1
            day_bucket["discount_amt"] += r.get("total_discount") or 0.0
            for li in r.get("line_items", []):
                name = li.get("item_name") or "(unnamed item)"
                cat = item_category.get(li.get("item_id"), "Other")
                entry = day_bucket["items"][name]
                entry["cat"] = cat
                entry["qty"] += li.get("quantity") or 0.0
                entry["sales"] += li.get("total_money") or 0.0

    # Convert nested defaultdicts to plain dicts so this is safely JSON-serializable.
    branches = {
        branch: {
            day: {**vals, "items": dict(vals["items"])}
            for day, vals in days.items()
        }
        for branch, days in out.items()
    }
    return {
        "branches": branches,
        "skipped_unknown_store_receipts": skipped_unknown_store,
        "skipped_unknown_store_ids": dict(skipped_unknown_store_ids),
        "skipped_test_branch_receipts": skipped_test_branch,
    }
