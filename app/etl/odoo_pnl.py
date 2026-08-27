"""
Pure aggregation/allocation logic for the Odoo-sourced P&L data.

This is a port of /home/claude/estkana_dash3/build_pnl.py, restructured as
testable functions that take raw pulled records in and return the exact JSON
shapes the dashboard frontend already expects (PNL.branches, .branch_rollup,
.company_total, .overhead, .ho, .overhead_accounts).

Deliberately has ZERO network/database/FastAPI dependency — it's exercised in
tests against the same fixture files (mrev.json/mcogs.json/mopex.json) the
original script ran against, so "does this reproduce known-good output" is a
fast, offline check before any of it touches a live Odoo connection.

Input record shape (unchanged from the existing Odoo pull): a flat list of
    {"a": <analytic_account_id>, "n": "[code] Display Name", "m": "January 2026", "v": <amount>}
grouped by analytic account name + month.

One correction versus the original script: MONTH_ORDER was hardcoded to
Jan-Aug 2026 there, which only worked for a one-off snapshot. Here it's
derived from whatever months are actually present in the data, so this module
keeps working as real months roll forward.
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime
from typing import Iterable

# Cost centers that are NOT outlet branches. "Outlet" is a duplicate/rollup
# account (cross-checked against the sum of branch revenue), not a real cost
# center, and is excluded everywhere except the cross-check itself.
OVERHEAD = {"CPU", "FIN", "GM", "HO", "HR", "Kaftrea", "MAINTINANACE", "Operation", "Outlet"}

# Branches with no meaningful activity in the current dataset — excluded from
# the branch rollup so they don't drag down averages with all-zero rows.
DORMANT_NAMES = {"Alsamer 2", "NASEEM 3"}

# The subset of OVERHEAD the user asked to see broken out individually in the
# "HQ / overhead cost centers" chart. Excludes Kaftrea (not requested) and
# Outlet (the duplicate rollup account, never a real cost center).
OVERHEAD_ACCOUNTS_OF_INTEREST = ["CPU", "FIN", "GM", "HO", "HR", "MAINTINANACE", "Operation"]


# Odoo branch display name -> Loyverse store name. Ambiguous mapping flagged:
# Khomra -1/-2 were confirmed by the user (2026-08-16) as Al-Qurainiah /
# Al-Ta'awun 2 respectively (matched by store-creation order — both outlets
# opened the same day).
LOYVERSE_MAP = {
    "NOZHA": "Al-Nuzhah",
    "HAMDANEYA": "Al-Hamdaniyah",
    "HERAA": "Hira",
    "FOROSIA": "Al-Furusiyah",
    "FALSTEEN": "Falasteen",
    "SHARKIA": "Ash-Sharqiyah Al-Rayyan",
    "SASCO": "Sasco",
    "SAFA": "Al-Safa",
    "ARBEEN": "ARBEEN",
    "Alsamer": "Al-Samer",
    "SAFWA": "Al-Safwa",
    "Madian": "Al-Madinah",
    "NASEEM 1": "Al-Naseem",
    "NASEEM 2": "Al-Naseem 2",
    "slumaniah": "Al-Sulaimaniyah",
    "Zahra": "Al-Zahra",
    "Khomra -1": "Al-Qurainiah",
    "Khomra -2": "Al-Ta'awun 2",
}


def parse_name(n: str) -> tuple[str | None, str]:
    """'[Estk-008] ARBEEN' -> ('Estk-008', 'ARBEEN')"""
    m = re.match(r"\[([^\]]+)\]\s*(.+)", n)
    if m:
        return m.group(1), m.group(2).strip()
    return None, n.strip()


def _build_index(records: Iterable[dict], sign: int = 1) -> tuple[dict, dict]:
    idx: dict[str, dict[str, float]] = defaultdict(dict)
    codes: dict[str, str | None] = {}
    for r in records:
        code, name = parse_name(r["n"])
        codes[name] = code
        idx[name][r["m"]] = idx[name].get(r["m"], 0) + sign * r["v"]
    return idx, codes


def derive_month_order(*record_lists: Iterable[dict]) -> list[str]:
    """
    Every month label seen across the given record lists, sorted
    chronologically. Replaces the hardcoded Jan-Aug 2026 list in the
    original script so this keeps working as real months roll forward.
    """
    months: set[str] = set()
    for records in record_lists:
        for r in records:
            months.add(r["m"])

    def _key(label: str) -> datetime:
        return datetime.strptime(label, "%B %Y")

    return sorted(months, key=_key)


def _round(v: float) -> float:
    return round(v, 2)


def _month_row(m: str, r: float, c: float, o: float) -> dict:
    r, c, o = _round(r), _round(c), _round(o)
    gp = _round(r - c)
    np_ = _round(gp - o)
    return {
        "m": m,
        "revenue": r,
        "cogs": c,
        "gross_profit": gp,
        "gross_margin_pct": _round(gp / r * 100) if r else None,
        "opex": o,
        "net_profit": np_,
        "net_margin_pct": _round(np_ / r * 100) if r else None,
    }


def valid_month_labels(branch_rollup_months: list[dict]) -> list[str]:
    """
    Odoo posts revenue, COGS, and opex on different lags (the current
    snapshot: revenue through July 2026, COGS/opex through Aug 16 2026) — a
    month can have real opex/COGS rows before its revenue lands anywhere,
    including on cost-center-only accounts like HO that have little or no
    revenue of their own. Matches the frontend's PNL_VALID_MONTHS exactly:
    "is this month complete enough to report" is decided ONCE, from
    outlet (branch_rollup) revenue only, and the same label set is then
    applied to every other series (HO, overhead, company total) — it is NOT
    recomputed per-series, since HO's own revenue is near-zero and unrelated
    to whether the month is postable.
    """
    return [m["m"] for m in branch_rollup_months if m["revenue"] > 0]


def filter_to_labels(months: list[dict], labels: list[str]) -> list[dict]:
    label_set = set(labels)
    return [m for m in months if m["m"] in label_set]


def ytd_from_months(months: list[dict]) -> dict:
    r = _round(sum(x["revenue"] for x in months))
    c = _round(sum(x["cogs"] for x in months))
    o = _round(sum(x["opex"] for x in months))
    gp = _round(r - c)
    np_ = _round(gp - o)
    return {
        "revenue": r,
        "cogs": c,
        "gross_profit": gp,
        "gross_margin_pct": _round(gp / r * 100) if r else None,
        "opex": o,
        "net_profit": np_,
        "net_margin_pct": _round(np_ / r * 100) if r else None,
    }


def build_pnl(rev: list[dict], cogs: list[dict], opex: list[dict]) -> dict:
    """
    Main entry point. rev/cogs/opex are the raw pulled record lists (same
    shape as mrev.json/mcogs.json/mopex.json). Returns the exact dict shape
    the dashboard's `const PNL = {...}` currently holds.
    """
    month_order = derive_month_order(rev, cogs, opex)

    rev_idx, rev_codes = _build_index(rev)
    cogs_idx, cogs_codes = _build_index(cogs, sign=-1)  # Odoo stores these negative
    opex_idx, opex_codes = _build_index(opex, sign=-1)

    all_names = set(rev_idx) | set(cogs_idx) | set(opex_idx)
    branch_names = sorted(n for n in all_names if n not in OVERHEAD and n not in DORMANT_NAMES)

    def monthly_for(name: str) -> list[dict]:
        rows = []
        for m in month_order:
            r = rev_idx.get(name, {}).get(m, 0)
            c = cogs_idx.get(name, {}).get(m, 0)
            o = opex_idx.get(name, {}).get(m, 0)
            if r == 0 and c == 0 and o == 0:
                continue
            rows.append(_month_row(m, r, c, o))
        return rows

    branches = []
    for name in branch_names:
        code = rev_codes.get(name) or cogs_codes.get(name) or opex_codes.get(name)
        months = monthly_for(name)
        branches.append({
            "code": code,
            "odoo_name": name,
            "loyverse_name": LOYVERSE_MAP.get(name, "NO MATCH"),
            "months": months,
            "ytd": ytd_from_months(months),
        })

    def summed_monthly(names: list[str]) -> list[dict]:
        rows = []
        for m in month_order:
            r = sum(rev_idx.get(n, {}).get(m, 0) for n in names)
            c = sum(cogs_idx.get(n, {}).get(m, 0) for n in names)
            o = sum(opex_idx.get(n, {}).get(m, 0) for n in names)
            if r == 0 and c == 0 and o == 0:
                continue
            rows.append(_month_row(m, r, c, o))
        return rows

    branch_rollup_months = summed_monthly(branch_names)

    # Company total: every real analytic account except the "Outlet" umbrella
    # (a running duplicate of the branch totals — see cross_check below).
    all_real_names = sorted(n for n in all_names if n != "Outlet")
    company_months = summed_monthly(all_real_names)

    overhead_names = sorted(n for n in all_names if n in OVERHEAD and n != "Outlet")
    overhead_months = []
    for m in month_order:
        r = sum(rev_idx.get(n, {}).get(m, 0) for n in overhead_names)
        c = sum(cogs_idx.get(n, {}).get(m, 0) for n in overhead_names)
        o = sum(opex_idx.get(n, {}).get(m, 0) for n in overhead_names)
        if r == 0 and c == 0 and o == 0:
            continue
        overhead_months.append({
            "m": m, "revenue": _round(r), "cogs": _round(c), "opex": _round(o),
            "net": _round(r - c - o),
        })

    # HO and CPU analytic accounts, each on their own — HO is the ONE overhead
    # cost center that gets allocated into the P&L statement's "HO share"
    # line; CPU gets its own separate "CPU share" line right below it (the
    # user asked, 2026-08-16 -> 2026-08-27, for CPU broken out as its own
    # line under HO, not merged into HO's total). Both are computed with the
    # exact same shape so renderPnL in the frontend can allocate/display them
    # identically. The other overhead accounts (FIN, GM, HR, Kaftrea,
    # Maintenance, Operation) are informational only.
    def _single_account_months(name: str) -> list[dict]:
        rows = []
        for m in month_order:
            r = rev_idx.get(name, {}).get(m, 0)
            c = cogs_idx.get(name, {}).get(m, 0)
            o = opex_idx.get(name, {}).get(m, 0)
            if r == 0 and c == 0 and o == 0:
                continue
            rows.append({"m": m, "revenue": _round(r), "cogs": _round(c), "opex": _round(o)})
        return rows

    ho_months = _single_account_months("HO")
    cpu_months = _single_account_months("CPU")

    overhead_accounts = []
    for name in OVERHEAD_ACCOUNTS_OF_INTEREST:
        code = rev_codes.get(name) or cogs_codes.get(name) or opex_codes.get(name)
        months = []
        for m in month_order:
            r = rev_idx.get(name, {}).get(m, 0)
            c = cogs_idx.get(name, {}).get(m, 0)
            o = opex_idx.get(name, {}).get(m, 0)
            if r == 0 and c == 0 and o == 0:
                continue
            months.append({"m": m, "revenue": _round(r), "cogs": _round(c), "opex": _round(o)})
        overhead_accounts.append({"name": name, "code": code, "months": months})

    outlet_umbrella_rev = rev_idx.get("Outlet", {})
    cross_check = {
        m: {
            "sum_of_branches": next((x["revenue"] for x in branch_rollup_months if x["m"] == m), None),
            "outlet_umbrella_account": outlet_umbrella_rev.get(m),
        }
        for m in month_order if m in outlet_umbrella_rev
    }

    return {
        "branches": branches,
        "branch_rollup": {"months": branch_rollup_months, "ytd": ytd_from_months(branch_rollup_months)},
        "company_total": {"months": company_months, "ytd": ytd_from_months(company_months)},
        "overhead": {"months": overhead_months, "cost_centers": overhead_names},
        "ho": {"months": ho_months, "cost_center": "[HO] HO"},
        "cpu": {"months": cpu_months, "cost_center": "[CPU] CPU"},
        "overhead_accounts": overhead_accounts,
        "cross_check_vs_outlet_umbrella_account": cross_check,
        "overhead_excluded_from_branch_rollup": sorted(OVERHEAD),
        "dormant_excluded_from_branch_rollup": sorted(DORMANT_NAMES),
        "ambiguous_mapping": ["Khomra -1", "Khomra -2"],
        "month_order": month_order,
    }
