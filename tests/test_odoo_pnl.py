"""
Fixture-based regression test: build_pnl() must reproduce the exact PNL
shape currently embedded in outlet_dashboard.html (pnl_known_good.json),
which was manually verified against Odoo (branch mapping confirmed by the
user, HO-share and overhead-account math checked by hand in the dashboard
review). This is the fast, offline check that the ported/refactored logic
still agrees with known-good output before it's wired to a live Odoo
connection.
"""
import json
from pathlib import Path

import pytest

from app.etl.odoo_pnl import build_pnl

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def result():
    rev = json.loads((FIXTURES / "mrev.json").read_text())
    cogs = json.loads((FIXTURES / "mcogs.json").read_text())
    opex = json.loads((FIXTURES / "mopex.json").read_text())
    return build_pnl(rev, cogs, opex)


@pytest.fixture(scope="module")
def known_good():
    return json.loads((FIXTURES / "pnl_known_good.json").read_text())


def test_branch_count_and_names(result, known_good):
    got = {b["odoo_name"] for b in result["branches"]}
    want = {b["odoo_name"] for b in known_good["branches"]}
    assert got == want
    assert len(result["branches"]) == 18


def test_loyverse_mapping_matches(result, known_good):
    got = {b["odoo_name"]: b["loyverse_name"] for b in result["branches"]}
    want = {b["odoo_name"]: b["loyverse_name"] for b in known_good["branches"]}
    assert got == want
    # explicitly pin the two branches that needed manual disambiguation
    assert got["Khomra -1"] == "Al-Qurainiah"
    assert got["Khomra -2"] == "Al-Ta'awun 2"


def test_branch_rollup_ytd_matches(result, known_good):
    assert result["branch_rollup"]["ytd"] == known_good["branch_rollup"]["ytd"]


def test_company_total_ytd_matches(result, known_good):
    assert result["company_total"]["ytd"] == known_good["company_total"]["ytd"]


def test_overhead_cost_centers_match(result, known_good):
    assert result["overhead"]["cost_centers"] == known_good["overhead"]["cost_centers"]


def test_ho_share_source_matches(result, known_good):
    """
    HO share in the dashboard is computed client-side as
    -(pnlAggMonths(PNL.ho.months).net_profit). The user asked (2026-08-27) for
    CPU to be folded into the same "HO share" line alongside HO, so
    PNL.ho.months is now HO + CPU combined rather than HO alone —
    known_good.json predates that change and still holds HO-only figures, so
    reconstruct the expected combined series from known_good's HO and CPU
    entries in overhead_accounts (which the port doesn't touch) instead of
    comparing against known_good["ho"] directly.
    """
    oh_by_name = {a["name"]: {m["m"]: m for m in a["months"]} for a in known_good["overhead_accounts"]}
    ho_by_month = oh_by_name["HO"]
    cpu_by_month = oh_by_name["CPU"]
    all_months = sorted(set(ho_by_month) | set(cpu_by_month),
                         key=lambda m: result["month_order"].index(m))
    expected = []
    for m in all_months:
        h = ho_by_month.get(m, {"revenue": 0, "cogs": 0, "opex": 0})
        c = cpu_by_month.get(m, {"revenue": 0, "cogs": 0, "opex": 0})
        r, cg, o = h["revenue"] + c["revenue"], h["cogs"] + c["cogs"], h["opex"] + c["opex"]
        if r == 0 and cg == 0 and o == 0:
            continue
        expected.append({"m": m, "revenue": round(r, 2), "cogs": round(cg, 2), "opex": round(o, 2)})
    assert result["ho"]["months"] == expected


def test_overhead_accounts_breakdown_matches(result, known_good):
    got = {a["name"]: a["months"] for a in result["overhead_accounts"]}
    want = {a["name"]: a["months"] for a in known_good["overhead_accounts"]}
    assert got == want
    assert list(got.keys()) == ["CPU", "FIN", "GM", "HO", "HR", "MAINTINANACE", "Operation"]


def test_cross_check_reconciles(result):
    """
    Sum of branch revenue should equal the 'Outlet' umbrella account for
    every month present — this is the sanity check that catches a branch
    being miscategorized as overhead/dormant or vice versa.

    Six of the seven months match to within a few SAR (float summation-order
    drift). February 2026 is a real, pre-existing SAR 240.13 gap that
    already exists in the known-good fixture too (i.e. it's in Odoo's own
    data, not introduced by this port) — worth flagging to Estkana's finance
    team as a genuine reconciliation item, but not something this ETL logic
    can or should silently correct. Bound generously enough to not fail on
    that known gap, tight enough to still catch a real miscategorization bug
    on any other month.
    """
    KNOWN_GAPS = {"February 2026": 250.0}
    for month, check in result["cross_check_vs_outlet_umbrella_account"].items():
        tolerance = KNOWN_GAPS.get(month, 5.0)
        assert check["sum_of_branches"] == pytest.approx(check["outlet_umbrella_account"], abs=tolerance), month


def test_month_order_is_chronological(result):
    from datetime import datetime
    months = result["month_order"]
    parsed = [datetime.strptime(m, "%B %Y") for m in months]
    assert parsed == sorted(parsed)


def test_statement_reconciles_for_all_outlets(result):
    """
    Gross profit - opex - HO share must equal net profit, for the 'All
    Outlets' view — this is the exact invariant renderPnL() in the frontend
    relies on. Originally pinned against SAR 1,740,783 YTD (verified in the
    dashboard UI screenshot during the P&L statement review), back when "HO
    share" meant the HO analytic account alone. The user asked (2026-08-27)
    for CPU to be folded into the same "HO share" line, which pulls CPU's own
    (substantial) opex into total_ho too — re-pinned to SAR 1,058,633, the
    figure this fixture now produces with HO+CPU combined; if this drifts,
    either the source data changed or the allocation logic broke.
    Uses valid_month_labels() derived from branch_rollup ONLY, then applies
    that same label set to both series — exactly like the frontend's
    PNL_VALID_MONTHS. Filtering HO by its own revenue instead (which is
    near-zero every month except a stray June/July amount) would drop most
    of HO's real opex and overstate profit by roughly the same margin this
    was originally understated by when the completeness filter was skipped
    entirely.
    """
    from app.etl.odoo_pnl import filter_to_labels, valid_month_labels, ytd_from_months
    labels = valid_month_labels(result["branch_rollup"]["months"])
    rollup_ytd = ytd_from_months(filter_to_labels(result["branch_rollup"]["months"], labels))
    ho_ytd = ytd_from_months(filter_to_labels(result["ho"]["months"], labels))
    total_ho = -ho_ytd["net_profit"]
    net_profit = rollup_ytd["gross_profit"] - rollup_ytd["opex"] - total_ho
    assert net_profit == pytest.approx(1_058_633, abs=1)
