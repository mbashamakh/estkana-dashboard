"""
Entrypoint for the hourly Render Cron Job: fetch live Odoo records, run the
existing tested sync_odoo() to aggregate + upsert them. Exits non-zero on
failure so Render's cron job shows a failed run (and can alert) rather than
silently succeeding with stale data.

Usage: python -m app.etl.run_once
"""
from __future__ import annotations

import sys

from app.config import get_settings
from app.db.session import SessionLocal
from app.etl import odoo_client
from app.etl.run_odoo_sync import sync_odoo


def main() -> int:
    settings = get_settings()
    db = SessionLocal()
    try:
        rev, cogs, opex = odoo_client.fetch_records(settings)
        result = sync_odoo(db, rev, cogs, opex)
        print(f"Odoo sync OK: {result}")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"Odoo sync FAILED: {exc}", file=sys.stderr)
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
