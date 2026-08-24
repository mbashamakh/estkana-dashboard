"""
Orchestrates Loyverse syncing: pull raw receipts for a window, aggregate
with loyverse_pnl.aggregate_receipts(), upsert into LoyverseDaily.

Two things happen on every run, both bounded so a single run stays fast
enough for an hourly cron job:

1. INCREMENTAL: re-pull the last few hours (with overlap, so a receipt that
   posts a bit late doesn't get missed) and upsert those days. This is what
   keeps "today" current.

2. BACKFILL, one chunk at a time: real receipt volume is large (~12k/day
   company-wide), so pulling the whole year in one request would be far too
   slow/expensive for one HTTP call. Instead, each run backfills
   BACKFILL_CHUNK_DAYS more days older than whatever's already in the DB
   (each day pulled — and committed — separately within the chunk, so a
   mid-chunk failure doesn't lose the days that already succeeded), until
   the target (Jan 1 of the current year — the user confirmed 2026-only
   history is enough, 2026-08-24) is reached. At 5 days/run, hourly, the
   full year backfills in under two days — slower than a one-shot backfill,
   but each run stays a bounded, safe size, and the whole thing is
   resumable (idempotent upserts — a run that dies partway just gets picked
   up again next hour from wherever it left off).
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

BACKFILL_CHUNK_DAYS = 5
INCREMENTAL_LOOKBACK_HOURS = 26  # >24h so a skipped/failed hourly run doesn't leave a gap


def _backfill_target_date(now: datetime) -> str:
    """Jan 1 of `now`'s year — the user confirmed (2026-08-24) that real
    history only needs to cover the current year, not further back."""
    return now.replace(month=1, day=1).strftime("%Y-%m-%d")


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _upsert_day(db: Session, branch: str, date: str, day_data: dict) -> None:
    stmt = pg_insert(LoyverseDaily).values(
        branch=branch, date=date,
        sales=day_data["sales"], orders=day_data["orders"],
        discount_amt=day_data["discount_amt"], refund_amt=day_data["refund_amt"],
        line_items=day_data["items"],
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["branch", "date"],
        set_={
            "sales": stmt.excluded.sales, "orders": stmt.excluded.orders,
            "discount_amt": stmt.excluded.discount_amt, "refund_amt": stmt.excluded.refund_amt,
            "line_items": stmt.excluded.line_items, "updated_at": datetime.now(timezone.utc),
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
    now = datetime.now(timezone.utc)

    # 1. Incremental — always runs, keeps "today" current. Its own
    # try/except so a backfill-chunk failure below still lets this succeed
    # and be logged, and vice versa.
    try:
        incr_min = _iso(now - timedelta(hours=INCREMENTAL_LOOKBACK_HOURS))
        incr_max = _iso(now)
        incremental_count = _pull_and_upsert_window(db, settings, incr_min, incr_max)
        db.commit()
        incremental_error = None
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        incremental_count = 0
        incremental_error = str(exc)[:1000]

    # 2. Backfill — up to BACKFILL_CHUNK_DAYS more days older than whatever's
    # already stored, each pulled and committed one day at a time so a
    # failure partway through the chunk keeps whatever already succeeded.
    target_date = _backfill_target_date(now)
    backfilled_dates: list[str] = []
    backfill_receipts_total = 0
    backfill_error = None
    try:
        for _ in range(BACKFILL_CHUNK_DAYS):
            earliest = _earliest_synced_date(db)
            if earliest is not None and earliest <= target_date:
                break  # target reached
            anchor = datetime.strptime(earliest, "%Y-%m-%d") if earliest else now
            backfill_day = anchor - timedelta(days=1)
            day_label = backfill_day.strftime("%Y-%m-%d")
            day_min = _iso(backfill_day.replace(hour=0, minute=0, second=0, microsecond=0))
            day_max = _iso((backfill_day + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0))
            count = _pull_and_upsert_window(db, settings, day_min, day_max)
            db.commit()
            backfilled_dates.append(day_label)
            backfill_receipts_total += count
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        backfill_error = str(exc)[:1000]

    success = incremental_error is None and backfill_error is None
    message = (
        f"incremental={incremental_count} receipts"
        + (f" (FAILED: {incremental_error})" if incremental_error else "")
        + f"; backfilled {len(backfilled_dates)} day(s) "
        + (f"[{backfilled_dates[0]}..{backfilled_dates[-1]}]" if backfilled_dates else "(none — target reached or nothing to do)")
        + f", {backfill_receipts_total} receipts"
        + (f" (FAILED: {backfill_error})" if backfill_error else "")
    )
    db.add(SyncLog(
        source="loyverse", success=success, message=message[:2000],
        started_at=started, finished_at=datetime.now(timezone.utc),
    ))
    db.commit()

    result = {
        "success": success,
        "incremental_receipts": incremental_count,
        "incremental_error": incremental_error,
        "backfilled_dates": backfilled_dates,
        "backfill_receipts": backfill_receipts_total,
        "backfill_error": backfill_error,
    }
    if not success:
        # run_loyverse_once.py (the cron entrypoint) expects an exception on
        # failure so it exits non-zero and Render's cron shows a failed run.
        raise RuntimeError(message)
    return result
