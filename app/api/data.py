"""
GET /api/data — real Loyverse-sourced sales figures (daily sales, orders,
AOV, discounts, refunds, category mix, top products) merged with the
still-sample fields this dashboard section can't source for real yet (cost
%, labor %, waste, rating, complaints). See etl/data_builder.py for exactly
how that merge works and what's real vs. placeholder. Login-gated, same as
/api/pnl.
"""
from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.routes import get_current_user
from app.db.session import get_db
from app.db.models import LoyverseDaily
from app.etl.data_builder import REAL_BRANCH_NAMES, build_data_response, ex_vat
from app.etl.odoo_pnl import LOYVERSE_MAP

router = APIRouter()


@router.get("/api/data")
def get_data(db: Session = Depends(get_db), _user=Depends(get_current_user)):
    return build_data_response(db)


@router.get("/api/hourly")
def get_hourly(date_from: str, date_to: str, db: Session = Depends(get_db), _user=Depends(get_current_user)):
    """
    Sales and orders by Saudi clock hour (0-23), per branch, for the days
    date_from..date_to inclusive (YYYY-MM-DD). Sales are ex-VAT, like every
    other sales figure on the dashboard. Branches are keyed by the same
    display name /api/data uses. `coverage` says how many of the stored
    branch-days in the range already carry hourly data (older days are
    filled in by a re-pull; see LoyverseDaily.hours).
    """
    try:
        d0 = date.fromisoformat(date_from)
        d1 = date.fromisoformat(date_to)
    except ValueError:
        raise HTTPException(status_code=400, detail="date_from/date_to must be YYYY-MM-DD")
    if d1 < d0 or (d1 - d0).days > 400:
        raise HTTPException(status_code=400, detail="invalid date range")

    rows = db.execute(
        select(LoyverseDaily.branch, LoyverseDaily.hours).where(
            LoyverseDaily.branch.in_(REAL_BRANCH_NAMES),
            LoyverseDaily.date >= d0.isoformat(),
            LoyverseDaily.date <= d1.isoformat(),
        )
    ).all()
    out: dict[str, dict] = {}
    total = covered = 0
    for branch, hours in rows:
        total += 1
        if hours is None:
            continue
        covered += 1
        name = LOYVERSE_MAP.get(branch, branch)
        slot = out.setdefault(name, {"sales": [0.0] * 24, "orders": [0] * 24})
        for h, v in hours.items():
            i = int(h)
            if 0 <= i < 24:
                slot["sales"][i] += ex_vat(v.get("s", 0.0))
                slot["orders"][i] += int(v.get("o", 0))
    for slot in out.values():
        slot["sales"] = [round(x, 2) for x in slot["sales"]]
    return {
        "date_from": d0.isoformat(), "date_to": d1.isoformat(),
        "branches": out,
        "coverage": {"branch_days": total, "with_hours": covered},
    }
