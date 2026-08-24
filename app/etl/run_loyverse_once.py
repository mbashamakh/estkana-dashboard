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
from app.db.session import SessionLocal
from app.etl.run_loyverse_sync import sync_loyverse


def main() -> int:
    settings = get_settings()
    db = SessionLocal()
    try:
        result = sync_loyverse(db, settings)
        print(f"Loyverse sync OK: {result}")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"Loyverse sync FAILED: {exc}", file=sys.stderr)
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
