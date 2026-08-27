"""
Unit tests for data_builder._branch_month_cost_fields() — the pure function
behind wiring the "Cost & profitability" tiles' Actual Cost % / Gross Profit
/ Gross Margin % / Contribution to real Odoo COGS/revenue where it's been
posted, instead of the flat per-branch sample percentage that used to apply
to every month regardless of period.

Deliberately zero DB/session dependency (see the function's own docstring):
`odoo_row` is duck-typed via SimpleNamespace here rather than a real
AnalyticMonthly instance, so these run fast and offline, same as
test_odoo_pnl.py's fixture-based checks.
"""
from types import SimpleNamespace

from app.etl.data_builder import _branch_month_cost_fields


def _odoo_row(revenue, cogs, is_complete=True):
    return SimpleNamespace(revenue=revenue, cogs=cogs, is_complete=is_complete)


def test_real_odoo_row_used_when_complete():
    """A complete, real Odoo month should drive Actual Cost % (and
    everything derived from it) instead of the sample percentage."""
    fields = _branch_month_cost_fields(
        is_real=True, odoo_row=_odoo_row(revenue=100_000, cogs=32_000),
        sample_food_cost_pct=28.0, labor_cost_pct=18.0,
        sales=95_000, discount_amt=1_000,
    )
    assert fields["food_cost_pct"] == 32.0
    assert fields["food_cost_pct_is_real"] is True
    assert fields["gross_margin_pct"] == 68.0
    # gross_profit is derived from REAL Loyverse sales (95,000), not Odoo's
    # own revenue (100,000) — the two systems' revenue figures can differ
    # slightly (see the ARBEEN-exclusion gap discussed with the user), and
    # gross_profit needs to stay consistent with the Total Sales KPI card,
    # which is 100% Loyverse-sourced.
    assert fields["gross_profit"] == round(95_000 * 0.68, 2)
    expected_contribution = round(fields["gross_profit"] - 95_000 * 0.18 - 1_000 * 0.5, 2)
    assert fields["contribution"] == expected_contribution


def test_falls_back_to_sample_when_odoo_row_missing():
    """No Odoo row at all for this (branch, month) -- e.g. not synced
    yet -- falls back to the sample percentage, not a crash or a 0%."""
    fields = _branch_month_cost_fields(
        is_real=True, odoo_row=None,
        sample_food_cost_pct=29.5, labor_cost_pct=18.0,
        sales=50_000, discount_amt=500,
    )
    assert fields["food_cost_pct"] == 29.5
    assert fields["food_cost_pct_is_real"] is False
    # gross_profit is still computed from real sales even on the sample
    # cost % fallback -- this is what fixes the tile being disconnected
    # from the branch's actual sales scale entirely, independent of
    # whether Odoo has posted the month yet.
    assert fields["gross_profit"] == round(50_000 * (100 - 29.5) / 100, 2)


def test_falls_back_to_sample_when_odoo_row_incomplete():
    """Odoo posts revenue/COGS/opex on different lags -- an incomplete
    month (e.g. COGS landed before revenue did) must not be trusted even
    though a row exists, per AnalyticMonthly.is_complete's whole purpose."""
    fields = _branch_month_cost_fields(
        is_real=True, odoo_row=_odoo_row(revenue=80_000, cogs=20_000, is_complete=False),
        sample_food_cost_pct=27.0, labor_cost_pct=18.0,
        sales=40_000, discount_amt=0,
    )
    assert fields["food_cost_pct"] == 27.0
    assert fields["food_cost_pct_is_real"] is False


def test_falls_back_to_sample_on_zero_revenue():
    """A zero-revenue Odoo row (e.g. HO/overhead-only account, or a month
    that genuinely posted nothing) must not divide by zero -- falls back to
    sample instead of raising or producing inf/NaN."""
    fields = _branch_month_cost_fields(
        is_real=True, odoo_row=_odoo_row(revenue=0, cogs=0),
        sample_food_cost_pct=30.0, labor_cost_pct=18.0,
        sales=10_000, discount_amt=0,
    )
    assert fields["food_cost_pct"] == 30.0
    assert fields["food_cost_pct_is_real"] is False


def test_non_real_branch_gets_no_gross_profit_or_contribution():
    """Branches with no real Loyverse sales (the 10 never-hand-built-sample
    branches, ARBEEN) have no real `sales` figure to multiply against, so
    gross_profit/contribution are left for the caller's existing sample
    values entirely -- only food_cost_pct/gross_margin_pct are computed
    here (both purely informational display fields for these branches)."""
    fields = _branch_month_cost_fields(
        is_real=False, odoo_row=None,
        sample_food_cost_pct=29.0, labor_cost_pct=18.0,
        sales=0, discount_amt=0,
    )
    assert fields["food_cost_pct"] == 29.0
    assert fields["food_cost_pct_is_real"] is False
    assert "gross_profit" not in fields
    assert "contribution" not in fields
