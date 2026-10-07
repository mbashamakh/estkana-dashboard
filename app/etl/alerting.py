"""
After every hourly Loyverse sync, runs the same check behind the
dashboard's data-health banner (app/etl/data_health.compute_health) and
emails an alert if it finds anything wrong -- so a problem is caught the
moment it happens instead of waiting for someone to open the dashboard and
notice. See app/etl/notify.py for how the email itself is sent.

Throttled to at most one alert per ALERT_THROTTLE_HOURS: as long as an
issue stays unresolved, the hourly cron would otherwise re-email every
single hour. Throttle state is a plain SyncLog row ("ALERT: ...") rather
than a new table -- consistent with how this codebase already uses
SyncLog as its one audit trail (see run_loyverse_sync.py's _upsert_day
docstring).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import SyncLog
from app.etl.data_health import compute_health
from app.etl.notify import send_alert_email

ALERT_THROTTLE_HOURS = 6


def _recently_alerted(db: Session) -> bool:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=ALERT_THROTTLE_HOURS)
    row = db.scalars(
        select(SyncLog.id)
        .where(SyncLog.source == "loyverse", SyncLog.message.like("ALERT:%"), SyncLog.started_at >= cutoff)
        .limit(1)
    ).first()
    return row is not None


def check_and_alert(db: Session, settings: Settings) -> None:
    health = compute_health(db)
    if health["ok"]:
        return
    if _recently_alerted(db):
        return

    items = []
    for issue in health["issues"][:15]:
        if issue["type"] == "collapse":
            items.append(
                f"<li>{issue['branch']} {issue['date']}: {issue['orders']} orders "
                f"(usually ~{issue['expected_approx']})</li>"
            )
        else:
            items.append(f"<li>{issue['branch']} {issue['date']}: no sales data recorded</li>")
    extra = health["issue_count"] - len(items)
    if extra > 0:
        items.append(f"<li>...and {extra} more</li>")

    body_html = (
        f"<p>The automatic data check found <b>{health['issue_count']}</b> day(s) that look wrong "
        f"in the Loyverse sales sync.</p>"
        f"<ul>{''.join(items)}</ul>"
        f"<p>These are retried automatically overnight. Check the dashboard for the latest status: "
        f"<a href='https://estkana-dashboard.onrender.com'>estkana-dashboard.onrender.com</a></p>"
    )

    sent = send_alert_email(
        settings,
        subject=f"Estkana dashboard: {health['issue_count']} data issue(s) found",
        body_html=body_html,
    )
    db.add(SyncLog(
        source="loyverse", success=True,
        message=f"ALERT: {'sent' if sent else 'FAILED to send'} for {health['issue_count']} issue(s)"[:2000],
        started_at=datetime.now(timezone.utc), finished_at=datetime.now(timezone.utc),
    ))
    db.commit()
