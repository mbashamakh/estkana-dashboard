"""
Entrypoint for the hourly Render Cron Job that syncs Loyverse sales data.
Separate cron job from the Odoo one (run_once.py) so the two sources fail
independently — see SyncLog, which already tracks success/failure per
source for exactly this reason.

Usage: python -m app.etl.run_loyverse_once
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone

from app.config import get_settings
from app.db.migrate import run_startup_migrations
from app.db.session import Base, SessionLocal, engine
from app.etl.alerting import check_and_alert
from app.etl.run_loyverse_autoheal import run_autoheal
from app.etl.run_loyverse_sync import sync_loyverse

# Runs the nightly auto-heal pass (app/etl/run_loyverse_autoheal.run_autoheal)
# once a day, from inside this hourly job rather than as its own separate
# Render Cron Job -- see run_loyverse_autoheal.py's module docstring for why
# (in short: this job already has every credential auto-heal needs, and the
# Render API has no way to copy those secret values into a second job).
# 0 UTC = ~3am Riyadh, picked to land in Estkana's own off-hours.
NIGHTLY_AUTOHEAL_HOUR_UTC = 0


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

        # Nightly auto-heal -- only once a day, at this one hourly run
        # closest to Estkana's off-hours. Also fully isolated from the
        # sync's own success/failure, for the same reason as the alert
        # check above.
        if datetime.now(timezone.utc).hour == NIGHTLY_AUTOHEAL_HOUR_UTC:
            try:
                run_autoheal(db, settings)
            except Exception as exc:  # noqa: BLE001
                print(f"Nightly auto-heal failed (non-fatal): {exc}", file=sys.stderr)

        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"Loyverse sync FAILED: {exc}", file=sys.stderr)
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
