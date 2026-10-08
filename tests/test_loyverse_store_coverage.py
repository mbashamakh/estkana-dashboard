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
