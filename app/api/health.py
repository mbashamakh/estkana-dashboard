"""
GET /api/data-health — proactively surfaces data-sync anomalies directly on
the dashboard, so a silent collapse (or any future bug with the same
signature) gets caught automatically instead of relying on someone noticing
wrong numbers on the dashboard and reporting it. Login-gated, same as
/api/data -- this is a permanent product feature, not a _diag/ endpoint.

The actual scan lives in app/etl/data_health.py (compute_health()), shared
with the automatic after-sync alert and the nightly auto-heal job -- this
route is just that same check, wrapped for a logged-in browser request.

POST /api/data-health/acknowledge and /unacknowledge let a logged-in user
mark a specific flagged (branch, date) as reviewed/explained -- e.g.
confirmed against Loyverse's own reporting that a branch was genuinely
closed, not a sync bug. See DataHealthAck's docstring in db/models.py for
why this is a table and not just a frontend-side "dismiss": it also stops
the nightly auto-heal job from burning retries on something a human already
explained, and stops the alert email from re-reporting it.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.routes import get_current_user
from app.db.models import DataHealthAck, User
from app.db.session import get_db
from app.etl.data_health import compute_health

router = APIRouter()


@router.get("/api/data-health")
def data_health(db: Session = Depends(get_db), _user=Depends(get_current_user)):
    return compute_health(db)


class AcknowledgeRequest(BaseModel):
    branch: str
    date: str
    issue_type: str
    note: str | None = None


@router.post("/api/data-health/acknowledge")
def acknowledge_data_health_issue(
    body: AcknowledgeRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """Idempotent: acknowledging an already-acknowledged (branch, date)
    just updates the note and who/when, rather than erroring. Returns the
    fresh compute_health() result so the caller can re-render the banner
    from this one response instead of a separate GET."""
    existing = db.scalar(
        select(DataHealthAck).where(DataHealthAck.branch == body.branch, DataHealthAck.date == body.date)
    )
    if existing:
        existing.issue_type = body.issue_type
        existing.note = body.note
        existing.acknowledged_by = user.email
        existing.acknowledged_at = datetime.now(timezone.utc)
    else:
        db.add(DataHealthAck(
            branch=body.branch,
            date=body.date,
            issue_type=body.issue_type,
            note=body.note,
            acknowledged_by=user.email,
            acknowledged_at=datetime.now(timezone.utc),
        ))
    db.commit()
    return compute_health(db)


class UnacknowledgeRequest(BaseModel):
    branch: str
    date: str


@router.post("/api/data-health/unacknowledge")
def unacknowledge_data_health_issue(
    body: UnacknowledgeRequest, db: Session = Depends(get_db), _user: User = Depends(get_current_user)
):
    """Undoes an acknowledge -- if a live scan still finds the issue, it
    goes back to showing on the banner and resumes nightly auto-heal/alert
    treatment, same as if it had never been reviewed."""
    existing = db.scalar(
        select(DataHealthAck).where(DataHealthAck.branch == body.branch, DataHealthAck.date == body.date)
    )
    if not existing:
        raise HTTPException(status_code=404, detail="No acknowledgement found for that branch/date")
    db.delete(existing)
    db.commit()
    return compute_health(db)
