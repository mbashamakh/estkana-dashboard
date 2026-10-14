from app.etl.odoo_pnl import build_labor


def test_build_labor_outlets_only_and_positive():
    recs = [
        {"a": 1, "n": "[Estk-001] NOZHA", "m": "September 2026", "v": -60000.0},
        {"a": 1, "n": "[Estk-001] NOZHA", "m": "August 2026", "v": -55000.5},
        {"a": 2, "n": "[HO] HO", "m": "September 2026", "v": -99999.0},
        {"a": 3, "n": "[Estk-099] Alsamer 2", "m": "September 2026", "v": -10.0},
    ]
    rows = build_labor(recs)
    assert rows == [
        {"name": "NOZHA", "m": "August 2026", "labor": 55000.5},
        {"name": "NOZHA", "m": "September 2026", "labor": 60000.0},
    ]
