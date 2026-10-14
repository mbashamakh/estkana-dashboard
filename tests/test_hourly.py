"""Per-hour sales/orders aggregation (feeds Peak Sales Hours)."""
from app.etl.loyverse_pnl import aggregate_receipts
from app.etl.loyverse_store_map import STORE_ID_TO_ODOO_NAME

STORE = next(iter(STORE_ID_TO_ODOO_NAME))
BRANCH = STORE_ID_TO_ODOO_NAME[STORE]


def _r(ts, total, kind="SALE"):
    return {"store_id": STORE, "receipt_date": ts, "total_money": total, "receipt_type": kind,
            "total_discount": 0, "line_items": []}


def test_hours_use_saudi_clock_and_match_daily_totals():
    receipts = [
        _r("2026-09-10T10:05:00.000Z", 100.0),   # 13:05 Saudi
        _r("2026-09-10T10:50:00.000Z", 50.0),    # 13:50 Saudi
        _r("2026-09-10T21:30:00.000Z", 20.0),    # 00:30 Saudi, NEXT day (Sep 11)
        _r("2026-09-10T11:10:00.000Z", 30.0, "REFUND"),  # 14:10 Saudi, refund
    ]
    out = aggregate_receipts(receipts, {})["branches"][BRANCH]
    d10 = out["2026-09-10"]
    assert d10["hours"]["13"] == {"s": 150.0, "o": 2}
    assert d10["hours"]["14"] == {"s": -30.0, "o": 0}
    assert round(sum(v["s"] for v in d10["hours"].values()), 2) == round(d10["sales"], 2) == 120.0
    d11 = out["2026-09-11"]
    assert d11["hours"] == {"0": {"s": 20.0, "o": 1}}


def test_hours_json_serialisable():
    import json
    out = aggregate_receipts([_r("2026-09-10T10:05:00.000Z", 10.0)], {})
    json.dumps(out)
