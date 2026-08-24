"""
Orchestrates Loyverse syncing: pull raw receipts for a window, aggregate
with loyverse_pnl.aggregate_receipts(), upsert into LoyverseDaily.

Two things happen on every run, both bounded so a single run stays fast
enough for an hourly cron job:

1. INCREMENTAL: re-pull the last few hours (with overlap, so a receipt that
   posts a bit late doesn't get missed) and upsert those days. This is what
   keeps "today" current.

2. BACKFILL, one day at a time: real receipt volume is large (~12k/day
   company-wide), so pulling a month of history in one request would be far
   too slow/expensive for one HTTP call or even one cron run. Instead, each
   run backfills exactly one additional day older than whatever's already
   in the DB, until BACKFILL_TARGET_DAYS of history is reached. Running
   hourly, a 30-day target fills in within about 30 hours of the first
   deploy — slower than a one-shot backfill, but each run stays cheap and
   the whole thing is safely resumable (upserts are idempotent, so a run
   that dies partway just gets picked up again next hour).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import LoyverseDaily, SyncLog
from app.etl import loyverse_client
from app.etl.loyverse_pnl import aggregate_receipts, build_item_category_lookup

BACKFILL_TARGET_DAYS = 30
INCREMENTAL_LOOKBACK_HOURS = 26  # >24h so a skipped/failed hourly run doesn't leave a gap


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _upsert_day(db: Session, branch: str, date: str, day_data: dict) -> None:
    stmt = pg_insert(LoyverseDaily).values(
        branch=branch, date=date,
        sales=day_data["sales"], orders=day_data["orders"],
        discount_amt=day_data["discount_amt"], refund_amt=day_data["refund_amt"],
        items=day_data["items"],
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["branch", "date"],
        set_={
            "sales": stmt.excluded.sales, "orders": stmt.excluded.orders,
            "discount_amt": stmt.excluded.discount_amt, "refund_amt": stmt.excluded.refund_amt,
            "items": stmt.excluded.items, "updated_at": datetime.now(timezone.utc),
        },
    )
    db.execute(stmt)


def _pull_and_upsert_window(db: Session, settings: Settings, created_at_min: str, created_at_max: str) -> int:
    """Returns the number of raw receipts processed (for logging)."""
    receipts = loyverse_client.list_all_receipts(settings, created_at_min, created_at_max)
    items = loyverse_client.list_all_items(settings)
    item_category = build_item_category_lookup(items)
    agg = aggregate_receipts(receipts, item_category)
    for branch, days in agg["branches"].items():
        for date, day_data in days.items():
            _upsert_day(db, branch, date, day_data)
    return len(receipts)


def _earliest_synced_date(db: Session) -> str | None:
    row = db.scalars(select(LoyverseDaily.date).order_by(LoyverseDaily.date.asc()).limit(1)).first()
    return row


def sync_loyverse(db: Session, settings: Settings) -> dict:
    started = datetime.now(timezone.utc)
    try:
        now = datetime.now(timezone.utc)

        # 1. Incremental — always runs, keeps "today" current.
        incr_min = _iso(now - timedelta(hours=INCREMENTAL_LOOKBACK_HOURS))
        incr_max = _iso(now)
        incremental_count = _pull_and_upsert_window(db, settings, incr_min, incr_max)

        # 2. Backfill — one additional day older than what's already stored,
        # up to BACKFILL_TARGET_DAYS back. No-ops once the target is reached.
        target_date = (now - timedelta(days=BACKFILL_TARGET_DAYS)).strftime("%Y-%m-%d")
        earliest = _earliest_synced_date(db)
        backfill_count = 0
        backfilled_date = None
        if earliest is None or earliest > target_date:
            # earliest is a "YYYY-MM-DD" string; if no rows yet, start from yesterday.
            anchor = datetime.strptime(earliest, "%Y-%m-%d") if earliest else now
            backfill_day = (anchor - timedelta(days=1))
            backfilled_date = backfill_day.strftime("%Y-%m-%d")
            day_min = _iso(backfill_day.replace(hour=0, minute=0, second=0, microsecond=0))
            day_max = _iso((backfill_day + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0))
            backfill_count = _pull_and_upsert_window(db, settings, day_min, day_max)

        db.add(SyncLog(
            source="loyverse", success=True,
            message=f"incremental={incremental_count} receipts; backfilled {backfilled_date or 'nothing (target reached)'} ({backfill_count} receipts)",
            started_at=started, finished_at=datetime.now(timezone.utc),
        ))
        db.commit()
        return {
            "success": True,
            "incremental_receipts": incremental_count,
            "backfilled_date": backfilled_date,
            "backfill_receipts": backfill_count,
        }
    except Exception as exc:  # noqa: BLE001 — must never crash the hourly job silently
        db.rollback()
        db.add(SyncLog(
            source="loyverse", success=False, message=str(exc)[:2000],
            started_at=started, finished_at=datetime.now(timezone.utc),
        ))
        db.commit()
        raise
