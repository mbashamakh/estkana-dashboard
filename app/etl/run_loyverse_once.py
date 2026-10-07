"""
Entrypoint for the hourly Render Cron Job that syncs Loyverse sales data.
Separate cron job from the Odoo one (run_once.py) so the two sources fail
independently — see SyncLog, which already tracks success/failure per
source for exactly this reason.

Usage: python -m app.etl.run_loyverse_once
"""
from __future__ import annotations

import sys

from app.config import get_settings
from app.db.migrate import run_startup_migrations
from app.db.session import Base, SessionLocal, engine
from app.etl.alerting import check_and_alert
from app.etl.run_loyverse_sync import sync_loyverse


def main() -> int:
    # Belt-and-suspenders: the web service also does this on every startup,
    # but this cron job is a separately-deployed process and shouldn't
    # depend on deploy ordering to get a new table (e.g. sync_cursor) it
    # needs. Both are idempotent/cheap to call again here.
    run_startup_migrations(engine)
    Base.metadata.create_all(bind=engine)

    settings = get_settings()
    db = SessionLocal()
    try:
        result = sync_loyverse(db, settings)
        print(f"Loyverse sync OK: {result}")

        # Automatic data-health check + alert email -- separate from the
        # sync's own success/failure above, so a problem here (or a slow
        # Odoo mail send) can never turn a genuinely successful sync into
        # a failed cron run. See app/etl/alerting.py.
        try:
            check_and_alert(db, settings)
        except Exception as exc:  # noqa: BLE001
            print(f"Data-health alert check failed (non-fatal): {exc}", file=sys.stderr)

        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"Loyverse sync FAILED: {exc}", file=sys.stderr)
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
