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

from app.etl.loyverse_category_map import display_category_for
from app.etl.loyverse_store_map import STORE_ID_TO_ODOO_NAME

# ARBEEN's Loyverse account is a test setup for a different POS app
# ("Looped") the user is evaluating — confirmed by the user 2026-08-24, not
# real sales activity. Excluded from aggregation entirely until that
# integration is ready and the user says to connect it. (ARBEEN still gets
# real financials from Odoo — this exclusion is Loyverse/sales-side only.)
LOYVERSE_TEST_BRANCHES = {"ARBEEN"}


def build_item_category_lookup(items: list[dict]) -> dict[str, str]:
    """item_id -> dashboard display category (Other/Shabati/Bakery & Snacks/
    Hot drinks/Cold Drink), via each item's Loyverse category_id."""
    return {it["id"]: display_category_for(it.get("category_id")) for it in items if it.get("id")}


def _day(receipt: dict) -> str:
    """'2026-08-16' from receipt_date's ISO timestamp — receipt_date (the
    actual transaction time) is used over created_at (sync time) since
    those can differ, e.g. for a receipt synced late."""
    return receipt["receipt_date"][:10]


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
    skipped_test_branch = 0
    for r in receipts:
        if r.get("cancelled_at"):
            continue
        branch = STORE_ID_TO_ODOO_NAME.get(r.get("store_id"))
        if branch is None:
            skipped_unknown_store += 1
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
        "skipped_test_branch_receipts": skipped_test_branch,
    }
