from app.etl.odoo_pnl import build_gl_company


def test_september_ties_to_odoo_report():
    groups = [
        {"t": "income", "m": "September 2026", "v": -3686228.49},
        {"t": "income_other", "m": "September 2026", "v": -31079.25},
        {"t": "expense_direct_cost", "m": "September 2026", "v": 1212731.51},
        {"t": "expense", "m": "September 2026", "v": 1601859.86},
        {"t": "asset_cash", "m": "September 2026", "v": 999.0},
        {"t": "income", "m": "August 2026", "v": -3775048.26},
    ]
    rows = build_gl_company(groups)
    assert [r["m"] for r in rows] == ["August 2026", "September 2026"]
    sep = rows[1]
    assert sep["revenue"] == 3686228.49
    assert sep["other_income"] == 31079.25
    net = sep["revenue"] - sep["cogs"] - sep["opex"] + sep["other_income"] - sep["other_expense"]
    assert round(net, 2) == 902716.37
