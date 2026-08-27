"""
GET /api/pnl — reconstructs the exact JSON shape the dashboard's
`const PNL = {...}` used to hold, but read from AnalyticMonthly rows instead
of a hardcoded blob. Login-gated (see auth.routes.get_current_user).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.routes import get_current_user
from app.config import get_settings
from app.db.models import AnalyticMonthly, SyncLog
from app.db.session import get_db
from app.etl.odoo_pnl import (
    DORMANT_NAMES,
    LOYVERSE_MAP,
    OVERHEAD,
    OVERHEAD_ACCOUNTS_OF_INTEREST,
    ytd_from_months,
)

router = APIRouter()
settings = get_settings()


def _month_row_from_db(row: AnalyticMonthly) -> dict:
    r, c, o = row.revenue, row.cogs, row.opex
    gp = round(r - c, 2)
    np_ = round(gp - o, 2)
    return {
        "m": row.month,
        "revenue": r, "cogs": c,
        "gross_profit": gp, "gross_margin_pct": round(gp / r * 100, 2) if r else None,
        "opex": o, "net_profit": np_, "net_margin_pct": round(np_ / r * 100, 2) if r else None,
    }


@router.get("/api/pnl")
def get_pnl(db: Session = Depends(get_db), _user=Depends(get_current_user)):
    branch_rows = db.scalars(
        select(AnalyticMonthly).where(AnalyticMonthly.kind == "branch").order_by(AnalyticMonthly.name, AnalyticMonthly.month)
    ).all()
    overhead_rows = db.scalars(
        select(AnalyticMonthly).where(AnalyticMonthly.kind == "overhead").order_by(AnalyticMonthly.name, AnalyticMonthly.month)
    ).all()
    # Combined all-overhead-cost-centers total (includes Kaftrea, unlike
    # overhead_rows above which is only the 7 named-breakdown accounts) — the
    # correct source for PNL.overhead.months and for company_total's overhead
    # component. See run_odoo_sync.py for how this is written.
    overhead_total_rows = db.scalars(
        select(AnalyticMonthly).where(AnalyticMonthly.kind == "overhead_total", AnalyticMonthly.name == "ALL")
        .order_by(AnalyticMonthly.month)
    ).all()

    branches_by_name: dict[str, list[AnalyticMonthly]] = {}
    for row in branch_rows:
        branches_by_name.setdefault(row.name, []).append(row)

    branches = []
    branch_rollup_months: dict[str, dict] = {}
    for name, rows in sorted(branches_by_name.items()):
        months = [_month_row_from_db(r) for r in rows]
        branches.append({
            "code": "—",  # not modeled in AnalyticMonthly — see note in db/models.py
            "odoo_name": name,
            "loyverse_name": LOYVERSE_MAP.get(name, "NO MATCH"),
            "months": months,
            "ytd": ytd_from_months(months),
        })
        for row in rows:
            agg = branch_rollup_months.setdefault(row.month, {"m": row.month, "revenue": 0.0, "cogs": 0.0, "opex": 0.0})
            agg["revenue"] += row.revenue
            agg["cogs"] += row.cogs
            agg["opex"] += row.opex

    rollup_months_sorted = [branch_rollup_months[m] for m in sorted(branch_rollup_months, key=lambda x: branches_by_name and 0)]
    # re-derive gross_profit/net_profit for the summed rollup rows
    rollup_months = []
    for m in branch_rollup_months.values():
        gp = round(m["revenue"] - m["cogs"], 2)
        np_ = round(gp - m["opex"], 2)
        rollup_months.append({
            "m": m["m"], "revenue": round(m["revenue"], 2), "cogs": round(m["cogs"], 2),
            "gross_profit": gp, "gross_margin_pct": round(gp / m["revenue"] * 100, 2) if m["revenue"] else None,
            "opex": round(m["opex"], 2), "net_profit": np_,
            "net_margin_pct": round(np_ / m["revenue"] * 100, 2) if m["revenue"] else None,
        })

    overhead_by_name: dict[str, list[AnalyticMonthly]] = {}
    for row in overhead_rows:
        overhead_by_name.setdefault(row.name, []).append(row)

    overhead_accounts = [
        {"name": name, "code": "—", "months": [_month_row_from_db(r) for r in overhead_by_name.get(name, [])]}
        for name in OVERHEAD_ACCOUNTS_OF_INTEREST
    ]
    ho_months = next((a["months"] for a in overhead_accounts if a["name"] == "HO"), [])
    cpu_months = next((a["months"] for a in overhead_accounts if a["name"] == "CPU"), [])

    overhead_months = [_month_row_from_db(r) for r in overhead_total_rows]

    company_months_by_month: dict[str, dict] = {}
    for row in list(branch_rows) + list(overhead_total_rows):
        agg = company_months_by_month.setdefault(row.month, {"m": row.month, "revenue": 0.0, "cogs": 0.0, "opex": 0.0})
        agg["revenue"] += row.revenue
        agg["cogs"] += row.cogs
        agg["opex"] += row.opex
    company_months = []
    for m in company_months_by_month.values():
        gp = round(m["revenue"] - m["cogs"], 2)
        np_ = round(gp - m["opex"], 2)
        company_months.append({
            "m": m["m"], "revenue": round(m["revenue"], 2), "cogs": round(m["cogs"], 2),
            "gross_profit": gp, "gross_margin_pct": round(gp / m["revenue"] * 100, 2) if m["revenue"] else None,
            "opex": round(m["opex"], 2), "net_profit": np_,
            "net_margin_pct": round(np_ / m["revenue"] * 100, 2) if m["revenue"] else None,
        })

    last_sync = db.scalars(
        select(SyncLog).where(SyncLog.source == "odoo", SyncLog.success == True).order_by(SyncLog.finished_at.desc())  # noqa: E712
    ).first()

    return {
        "schema_version": settings.schema_version,
        "last_synced_at": last_sync.finished_at.isoformat() if last_sync else None,
        "branches": branches,
        "branch_rollup": {"months": rollup_months, "ytd": ytd_from_months(rollup_months)},
        "company_total": {"months": company_months, "ytd": ytd_from_months(company_months)},
        "overhead": {"months": overhead_months},
        "ho": {"months": ho_months, "cost_center": "[HO] HO"},
        "cpu": {"months": cpu_months, "cost_center": "[CPU] CPU"},
        "overhead_accounts": overhead_accounts,
        "overhead_excluded_from_branch_rollup": sorted(OVERHEAD),
        "dormant_excluded_from_branch_rollup": sorted(DORMANT_NAMES),
    }
