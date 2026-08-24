"""
GET /api/data — real Loyverse-sourced sales figures (daily sales, orders,
AOV, discounts, refunds, category mix, top products) merged with the
still-sample fields this dashboard section can't source for real yet (cost
%, labor %, waste, rating, complaints). See etl/data_builder.py for exactly
how that merge works and what's real vs. placeholder. Login-gated, same as
/api/pnl.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.auth.routes import get_current_user
from app.db.session import get_db
from app.etl.data_builder import build_data_response

router = APIRouter()


@router.get("/api/data")
def get_data(db: Session = Depends(get_db), _user=Depends(get_current_user)):
    return build_data_response(db)
