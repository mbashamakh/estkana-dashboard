"""
Entrypoint for the nightly Render Cron Job that automatically repairs any
day app/etl/data_health.compute_health() flags as collapsed or missing,
instead of requiring someone to notice and manually trigger a resync (the
whole reason this project kept needing a human in the loop every time).

Bounded to AUTOHEAL_MAX_ATTEMPTS consecutive nightly attempts per date --
after that it stops retrying that specific date and reports it in the
summary email instead of hammering the Loyverse API indefinitely for a day
that may genuinely need a human look (e.g. the branch really had no
sales, or the receipts are gone from Loyverse itself).

Re-pulls the WHOLE day (all branches), same as the manual resync-range
diag endpoint and for the same reason: a "collapse" issue is reported
per-branch, but _pull_and_upsert_full_day's created_at window naturally
covers every branch at once, and re-pulling a few extra already-good
branches for that date is harmless (idempotent) -- see
_pull_and_upsert_window's only_date docstring for why a single day is
always safe to re-pull on its own.

Usage: python -m app.etl.run_loyverse_autoheal
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone

from sqlalchemy import select

from app.config import get_settings
from app.db.migrate import run_startup_migrations
from app.db.models import SyncLog
from app.db.session import Base, SessionLocal, engine
from app.etl.data_health import compute_health
from app.etl.notify import send_alert_email
from app.etl.run_loyverse_sync import (
    _pull_and_upsert_full_day,
    _release_sync_lock,
    _try_acquire_sync_lock,
)

AUTOHEAL_MAX_ATTEMPTS = 3


def _attempt_count_for_date(db, date_label: str) -> int:
    rows = db.scalars(
        select(SyncLog.id).where(
            SyncLog.source == "loyverse",
            SyncLog.message.like(f"AUTOHEAL attempt%for {date_label}%"),
        )
    ).all()
    return len(rows)


def _log(db, success: bool, message: str) -> None:
    db.add(SyncLog(
        source="loyverse", success=success, message=message[:2000],
        started_at=datetime.now(timezone.utc), finished_at=datetime.now(timezone.utc),
    ))
    db.commit()


def main() -> int:
    run_startup_migrations(engine)
    Base.metadata.create_all(bind=engine)

    settings = get_settings()
    db = SessionLocal()
    try:
        health = compute_health(db)
        if health["ok"]:
            print("Auto-heal: no issues found, nothing to do.")
            return 0

        # Collapse and missing-day issues both just need their date
        # re-pulled company-wide -- dedupe by date since several branches
        # can flag the same date.
        dates_to_fix = sorted({issue["date"] for issue in health["issues"]})

        if not _try_acquire_sync_lock(db):
            print("Auto-heal: another sync is in progress, skipping this run (will retry next night).")
            return 0

        fixed, maxed_out, failed = [], [], []
        try:
            for date_label in dates_to_fix:
                attempts = _attempt_count_for_date(db, date_label)
                if attempts >= AUTOHEAL_MAX_ATTEMPTS:
                    maxed_out.append(date_label)
                    continue
                try:
                    day = datetime.strptime(date_label, "%Y-%m-%d")
                    count = _pull_and_upsert_full_day(db, settings, day)
                    db.commit()
                    _log(db, True, f"AUTOHEAL attempt {attempts + 1}/{AUTOHEAL_MAX_ATTEMPTS} for {date_label}: {count} receipts")
                    fixed.append(date_label)
                except Exception as exc:  # noqa: BLE001
                    db.rollback()
                    _log(db, False, f"AUTOHEAL attempt {attempts + 1}/{AUTOHEAL_MAX_ATTEMPTS} for {date_label} FAILED: {exc}")
                    failed.append(date_label)
        finally:
            _release_sync_lock(db)

        # Re-check after fixing, then only alert about what's still wrong
        # -- a quiet, fully-successful night shouldn't email anyone.
        health_after = compute_health(db)
        print(f"Auto-heal: attempted {len(dates_to_fix)} date(s), fixed {len(fixed)}, "
              f"{len(maxed_out)} maxed out, {len(failed)} failed this run.")

        if not health_after["ok"] or maxed_out:
            lines = [f"Auto-heal fixed {len(fixed)} day(s) automatically tonight."]
            if maxed_out:
                lines.append(
                    f"{len(maxed_out)} day(s) still unresolved after {AUTOHEAL_MAX_ATTEMPTS} attempts "
                    f"and need a manual look: {', '.join(maxed_out)}"
                )
            if not health_after["ok"]:
                lines.append(f"{health_after['issue_count']} day(s) are still flagged after tonight's run.")
            send_alert_email(
                settings,
                subject="Estkana dashboard: nightly auto-heal summary",
                body_html="".join(f"<p>{line}</p>" for line in lines),
            )
        else:
            print("Auto-heal: all clear after tonight's run, no alert needed.")

        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"Auto-heal FAILED: {exc}", file=sys.stderr)
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
