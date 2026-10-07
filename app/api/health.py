"""
GET /api/data-health — proactively surfaces data-sync anomalies directly on
the dashboard, so a silent collapse (or any future bug with the same
signature) gets caught automatically instead of relying on someone noticing
wrong numbers on the dashboard and reporting it. Login-gated, same as
/api/data -- this is a permanent product feature, not a _diag/ endpoint.

The actual scan lives in app/etl/data_health.py (compute_health()), shared
with the automatic after-sync alert and the nightly auto-heal job -- this
route is just that same check, wrapped for a logged-in browser request.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.auth.routes import get_current_user
from app.db.session import get_db
from app.etl.data_health import compute_health

router = APIRouter()


@router.get("/api/data-health")
def data_health(db: Session = Depends(get_db), _user=Depends(get_current_user)):
    return compute_health(db)
