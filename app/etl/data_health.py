"""
Shared data-health scan, used by two callers:
  1. app/api/health.py's GET /api/data-health -- the interactive dashboard
     banner, read on demand by whoever has the page open.
  2. app/etl/alerting.py and app/etl/run_loyverse_autoheal.py -- the
     automatic, after-every-sync version that emails an alert and the
     nightly version that tries to fix what it finds, neither of which
     have a logged-in request to hang a route off of.
Keeping the scan itself in one place means the banner and the automated
checks can never silently drift apart on what counts as "wrong".

Two independent checks, combined:
  1. Recent SyncLog "WARNING:" entries -- _upsert_day() in run_loyverse_sync.py
     already writes one of these any time a day's orders would collapse
     >50% on a write (see its docstring). This is the richest signal since
     it's tied to the actual write event, not a guess made after the fact.
  2. A live scan of what's actually stored in loyverse_daily: for every
     branch, flag any day (excluding today, which is still accumulating)
     whose orders are <= _COLLAPSE_ORDERS_MAX while that branch's trailing
     _TRAILING_DAYS-day median is >= _MEDIAN_FLOOR -- the exact signature
     of every spillover/collapse incident found and fixed in this
     dashboard's history (confirmed by scanning March-September 2026: 0
     false positives in two full months of known-healthy data, 36/36 known
     incidents caught) -- and flag any day with NO row at all between a
     branch's own first and last active date (a full day silently
     skipped, not just collapsed).
This catches both a bug that writes a near-empty day on top of a good one
and a bug that skips a day's write entirely, without needing to know in
advance what caused either one.

A third input, DataHealthAck (db/models.py), lets a human mark a specific
(branch, date) issue as reviewed/explained -- e.g. the branch was actually
closed, confirmed against Loyverse's own reporting directly, not a sync
bug. Anything acknowledged is excluded from "issues"/"ok"/"issue_count" (so
the banner, the nightly auto-heal retry loop, and the alert email all stop
treating it as live), but still comes back as "acknowledged_issues" so the
explanation isn't lost, just quieted.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import DataHealthAck, LoyverseDaily, SyncLog

_SCAN_WINDOW_DAYS = 120
_COLLAPSE_ORDERS_MAX = 10
_MEDIAN_FLOOR = 50
_TRAILING_DAYS = 14
_WARNING_FRESH_HOURS = 48


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2


def compute_health(db: Session) -> dict:
    """Returns {"ok", "issue_count", "issues", "acknowledged_issues",
    "recent_sync_warnings"} -- see module docstring for what counts as an
    issue and how acknowledgement works. Pure read, no writes, safe to call
    from any context (route, cron script, etc.)."""
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    cutoff = (now - timedelta(days=_SCAN_WINDOW_DAYS)).strftime("%Y-%m-%d")

    rows = db.scalars(
        select(LoyverseDaily)
        .where(LoyverseDaily.date >= cutoff)
        .order_by(LoyverseDaily.branch, LoyverseDaily.date)
    ).all()

    by_branch: dict[str, list] = {}
    for r in rows:
        by_branch.setdefault(r.branch, []).append(r)

    issues: list[dict] = []
    for branch, branch_rows in by_branch.items():
        dates_present = {r.date for r in branch_rows}
        first_date = branch_rows[0].date
        last_date = branch_rows[-1].date

        for i, r in enumerate(branch_rows):
            if r.date >= today:
                continue  # still accumulating, not a real signal yet
            trailing = [
                br.orders for br in branch_rows[max(0, i - _TRAILING_DAYS):i] if br.orders > 0
            ]
            med = _median(trailing)
            if med >= _MEDIAN_FLOOR and r.orders <= _COLLAPSE_ORDERS_MAX:
                issues.append({
                    "type": "collapse",
                    "branch": branch,
                    "date": r.date,
                    "orders": r.orders,
                    "expected_approx": round(med),
                })

        # Missing-day check: any gap inside the branch's own active range.
        d = datetime.strptime(first_date, "%Y-%m-%d")
        end = datetime.strptime(last_date, "%Y-%m-%d")
        while d < end:
            d += timedelta(days=1)
            label = d.strftime("%Y-%m-%d")
            if label >= today:
                break
            if label not in dates_present:
                issues.append({"type": "missing", "branch": branch, "date": label})

    recent_warnings = db.scalars(
        select(SyncLog)
        .where(SyncLog.source == "loyverse", SyncLog.message.like("WARNING:%"))
        .order_by(SyncLog.started_at.desc())
        .limit(20)
    ).all()

    issues.sort(key=lambda x: x["date"], reverse=True)

    acks = db.scalars(select(DataHealthAck)).all()
    ack_by_key = {(a.branch, a.date): a for a in acks}

    active_issues: list[dict] = []
    acknowledged_issues: list[dict] = []
    for issue in issues:
        ack = ack_by_key.get((issue["branch"], issue["date"]))
        if ack:
            acknowledged_issues.append({
                **issue,
                "note": ack.note,
                "acknowledged_by": ack.acknowledged_by,
                "acknowledged_at": ack.acknowledged_at.isoformat() if ack.acknowledged_at else None,
            })
        else:
            active_issues.append(issue)

    # Only RECENT warnings count against "ok". A WARNING row is a historical
    # event log (e.g. a collapse that was later reviewed and acknowledged); if
    # old ones kept ok False forever, the banner could never clear. The full
    # list is still returned for information.
    fresh_cutoff = now - timedelta(hours=_WARNING_FRESH_HOURS)
    fresh_warnings = [
        w for w in recent_warnings
        if w.started_at is not None and _aware(w.started_at) >= fresh_cutoff
    ]

    return {
        "ok": len(active_issues) == 0 and len(fresh_warnings) == 0,
        "issue_count": len(active_issues),
        "issues": active_issues,
        "acknowledged_issues": acknowledged_issues,
        "fresh_sync_warnings": [
            {"message": w.message, "started_at": w.started_at.isoformat() if w.started_at else None}
            for w in fresh_warnings
        ],
        "recent_sync_warnings": [
            {"message": w.message, "started_at": w.started_at.isoformat() if w.started_at else None}
            for w in recent_warnings
        ],
    }
