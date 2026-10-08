"""Every Loyverse store with real receipts must be counted -- a store missing
from STORE_ID_TO_ODOO_NAME, or wrongly excluded as "test", silently drops its
sales (this understated September 2026 by ~SAR 195K before it was caught)."""
from app.etl.loyverse_pnl import LOYVERSE_TEST_BRANCHES, aggregate_receipts
from app.etl.loyverse_store_map import STORE_ID_TO_ODOO_NAME

ARBEEN_ID = "6a6485e1-80bc-4b33-8a41-b224c5d488f9"
NASEEM3_ID = "f8b28bbb-9de7-4f1c-9aa9-22a81c35211c"


def _receipt(store_id, total=100.0):
    return {"store_id": store_id, "receipt_date": "2026-09-10T12:00:00.000Z", "total_money": total,
            "receipt_type": "SALE", "line_items": []}


def test_arbeen_and_naseem3_are_counted():
    assert not LOYVERSE_TEST_BRANCHES
    assert STORE_ID_TO_ODOO_NAME[NASEEM3_ID] == "NASEEM 3"
    agg = aggregate_receipts([_receipt(ARBEEN_ID, 50), _receipt(NASEEM3_ID, 70)], {})
    assert agg["branches"]["ARBEEN"]["2026-09-10"]["sales"] == 50
    assert agg["branches"]["NASEEM 3"]["2026-09-10"]["sales"] == 70
    assert agg["skipped_unknown_store_receipts"] == 0


def test_unmapped_store_is_reported_by_id():
    agg = aggregate_receipts([_receipt("not-a-known-store"), _receipt("not-a-known-store")], {})
    assert agg["skipped_unknown_store_receipts"] == 2
    assert agg["skipped_unknown_store_ids"] == {"not-a-known-store": 2}


def test_receipts_are_bucketed_by_saudi_local_day():
    """Loyverse reports use the store's local (UTC+3) day. A sale at 21:30 UTC
    is 00:30 local the NEXT day; a sale at 20:59 UTC is still 23:59 local."""
    late = {"store_id": ARBEEN_ID, "receipt_date": "2026-09-30T21:30:00.000Z", "total_money": 10.0,
            "receipt_type": "SALE", "line_items": []}
    early = {"store_id": ARBEEN_ID, "receipt_date": "2026-09-30T20:59:59.000Z", "total_money": 5.0,
             "receipt_type": "SALE", "line_items": []}
    agg = aggregate_receipts([late, early], {})
    days = agg["branches"]["ARBEEN"]
    assert days["2026-10-01"]["sales"] == 10.0
    assert days["2026-09-30"]["sales"] == 5.0
