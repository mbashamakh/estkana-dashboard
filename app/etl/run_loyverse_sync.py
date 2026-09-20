"""
Orchestrates Loyverse syncing: pull raw receipts for a window, aggregate
with loyverse_pnl.aggregate_receipts(), upsert into LoyverseDaily.

Two things happen on every run, both bounded so a single run stays fast
enough for an hourly cron job:

1. INCREMENTAL: re-pull today's and yesterday's FULL calendar-day windows
   (not a rolling created_at slice -- see sync_loyverse()'s docstring for
   why that broke) and upsert those two days. This is what keeps "today"
   and "yesterday" current.

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

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import LoyverseDaily, SyncCursor, SyncLog
from app.etl import loyverse_client
from app.etl.loyverse_pnl import aggregate_receipts, build_item_category_lookup

BACKFILL_CHUNK_DAYS = 5

# Arbitrary fixed key for a Postgres session-level advisory lock -- see
# _try_acquire_sync_lock()'s docstring for why this exists.
_SYNC_LOCK_KEY = 847362910123


def _backfill_target_date(now: datetime) -> str:
    """Jan 1 of `now`'s year — the user confirmed (2026-08-24) that real
    history only needs to cover the current year, not further back."""
    return now.replace(month=1, day=1).strftime("%Y-%m-%d")


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _upsert_day(db: Session, branch: str, date: str, day_data: dict) -> None:
    # Regression guard: this REPLACE is exactly the mechanical property
    # that let the original spillover-cursor bug (8ded1e3), the shrinking
    # incremental window (b92040e), the hourly/manual sync race (ef530af),
    # and an untracked manual recovery attempt all silently overwrite an
    # already-correct day with a near-zero one, with no record left behind
    # afterward. This doesn't block the write (a day CAN legitimately go
    # from a normal volume to near-zero, and a blocked write would leave
    # the backfill cursor permanently unable to move past a genuinely
    # quiet day) -- it just makes sure a large, silent collapse always
    # leaves a SyncLog trace, which is the exact audit trail that was
    # missing every previous time this happened.
    existing_orders = db.scalar(
        select(LoyverseDaily.orders).where(LoyverseDaily.branch == branch, LoyverseDaily.date == date)
    )
    new_orders = day_data["orders"]
    if existing_orders is not None and existing_orders > 20 and new_orders < existing_orders * 0.5:
        db.add(SyncLog(
            source="loyverse", success=True,
            message=(
                f"WARNING: {branch}/{date} orders would drop from {existing_orders} to {new_orders} "
                "(>50% collapse) -- writing the new value anyway, but flagging for review since this "
                "is exactly the signature of a partial/bad pull overwriting a previously-good day."
            )[:2000],
            started_at=datetime.now(timezone.utc), finished_at=datetime.now(timezone.utc),
        ))

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


def _pull_and_upsert_window(
    db: Session, settings: Settings, created_at_min: str, created_at_max: str, only_date: str | None = None,
) -> int:
    """Returns the number of raw receipts processed (for logging).

    If `only_date` (YYYY-MM-DD) is given, ONLY that date's aggregated bucket
    is upserted -- any other date found in the aggregation is dropped, not
    written. This matters because this window is a `created_at` (sync time)
    filter, but aggregate_receipts buckets each receipt by its `receipt_date`
    (actual transaction time) -- see loyverse_pnl.py's _day() docstring. A
    receipt's created_at can fall inside this window while its receipt_date
    lands on the adjacent calendar day, so the aggregation can legitimately
    produce a second, unrelated date bucket containing only that handful of
    stray receipts -- a tiny, incomplete slice of that other day, not a real
    total for it. _upsert_day() REPLACES a day's row outright, so writing
    that stray bucket would silently overwrite the adjacent day's true,
    complete total with a near-empty one. Restricting to `only_date` makes
    every call site safe on its own, instead of relying on caller-side
    ordering (e.g. "pull yesterday last", see _sync_loyverse_locked's
    docstring) to self-heal it after the fact -- the loyverse-resync-range
    diag endpoint had no such ordering and let this exact spillover silently
    corrupt the day before whatever range was requested."""
    receipts = loyverse_client.list_all_receipts(settings, created_at_min, created_at_max)
    items = loyverse_client.list_all_items(settings)
    item_category = build_item_category_lookup(items)
    agg = aggregate_receipts(receipts, item_category)
    for branch, days in agg["branches"].items():
        for date, day_data in days.items():
            if only_date is not None and date != only_date:
                continue
            _upsert_day(db, branch, date, day_data)
    return len(receipts)


def _pull_and_upsert_full_day(db: Session, settings: Settings, day: datetime) -> int:
    """Like _pull_and_upsert_window, but for one whole calendar day
    (day 00:00 -> day+1 00:00), regardless of what time `day` itself is.
    Restricted to `day` itself -- see _pull_and_upsert_window's `only_date`
    docstring for why."""
    day_min = _iso(day.replace(hour=0, minute=0, second=0, microsecond=0))
    day_max = _iso((day + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0))
    return _pull_and_upsert_window(db, settings, day_min, day_max, only_date=day.strftime("%Y-%m-%d"))


def _earliest_synced_date(db: Session) -> str | None:
    """Oldest date with ANY row in loyverse_daily -- informational only
    (used e.g. for the frontend's sync-status display). NOT used to drive
    the backfill loop itself anymore -- see SyncCursor's docstring for why
    that was the actual root cause of the every-other-day undercount."""
    row = db.scalars(select(LoyverseDaily.date).order_by(LoyverseDaily.date.asc()).limit(1)).first()
    return row


def _get_backfill_cursor(db: Session) -> str | None:
    """Oldest date CONFIRMED fully, deliberately backfilled -- None means
    backfill hasn't started yet (or this is a pre-cursor DB that needs one
    initialized), in which case the walk bootstraps from "today" exactly
    like before."""
    cursor = db.get(SyncCursor, "loyverse")
    return cursor.backfilled_through if cursor else None


def _advance_backfill_cursor(db: Session, date_label: str) -> None:
    stmt = pg_insert(SyncCursor).values(source="loyverse", backfilled_through=date_label)
    stmt = stmt.on_conflict_do_update(
        index_elements=["source"],
        set_={"backfilled_through": stmt.excluded.backfilled_through, "updated_at": datetime.now(timezone.utc)},
    )
    db.execute(stmt)


def _try_acquire_sync_lock(db: Session) -> bool:
    """
    Postgres session-level advisory lock so two sync_loyverse() runs can
    never execute concurrently -- e.g. a manually-triggered diagnostic sync
    overlapping the hourly cron firing at the top of the hour. Nothing
    previously stopped that: two concurrent runs would each independently
    read the same backfill cursor, each do their own separate Loyverse API
    pull for the same day, and race on which one's commit landed last --
    silently overwriting an already-correct full-day total with whichever
    run happened to finish second. Caught this live corrupting Aug 8, then
    Aug 4, then Aug 21 on three separate occasions during manual recovery
    syncs on 2026-08-25/26.
    Non-blocking (pg_try_advisory_lock, not pg_advisory_lock): if another
    run already holds the lock, this run skips entirely rather than
    queuing and running later anyway, which would just move the same race
    to a different pair of runs.
    Session-level (not pg_advisory_xact_lock), because this function calls
    db.commit() multiple times over its life -- a transaction-level lock
    would release itself after the very first commit, defeating the point.
    Released explicitly in sync_loyverse()'s `finally` block; also
    auto-released by Postgres if the connection is ever dropped uncleanly.
    """
    return bool(db.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": _SYNC_LOCK_KEY}).scalar())


def _release_sync_lock(db: Session) -> None:
    db.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": _SYNC_LOCK_KEY})


def sync_loyverse(db: Session, settings: Settings) -> dict:
    if not _try_acquire_sync_lock(db):
        # Another sync is already running (most likely the hourly cron
        # overlapping a manual diagnostic trigger). Skip entirely rather
        # than race it -- see _try_acquire_sync_lock()'s docstring. Not
        # logged as a SyncLog failure since this is an expected, benign
        # outcome, not something that needs the user's attention.
        return {
            "success": True,
            "skipped": True,
            "reason": "another loyverse sync was already in progress",
            "incremental_receipts": 0,
            "incremental_error": None,
            "backfilled_dates": [],
            "backfill_receipts": 0,
            "backfill_error": None,
        }
    try:
        return _sync_loyverse_locked(db, settings)
    finally:
        _release_sync_lock(db)


def _sync_loyverse_locked(db: Session, settings: Settings) -> dict:
    started = datetime.now(timezone.utc)
    now = datetime.now(timezone.utc)

    # 1. Incremental — always runs, keeps "today" and "yesterday" current.
    # Its own try/except so a backfill-chunk failure below still lets this
    # succeed and be logged, and vice versa.
    #
    # Pulls two full, calendar-aligned day windows rather than one rolling
    # created_at_min/max window (the old INCREMENTAL_LOOKBACK_HOURS=26h
    # approach). That rolling window silently corrupted "yesterday" once
    # its remaining hours aged out of the lookback: _upsert_day() REPLACES
    # a day's total outright, so a run whose 26h window only partially
    # overlapped yesterday would overwrite its correct, complete total with
    # a partial one -- and since backfill only ever walks further into the
    # past (never back to yesterday once it's moved on), nothing would ever
    # restore it. Pulling each day as its own full [00:00, 24:00) window
    # every run makes that impossible: yesterday is always re-derived from
    # its ENTIRE day, never a partial slice.
    #
    # Order matters here: today is pulled first, then yesterday. A day's
    # window can legitimately net a couple of stray receipts dated the
    # day before (see loyverse_pnl.py's _day() docstring, and SyncCursor's
    # docstring for how this exact spillover broke the backfill cursor) --
    # pulling yesterday LAST means its own authoritative full-day pull is
    # always what's left standing, overwriting any such spillover from
    # today's pull rather than the other way around.
    try:
        today_count = _pull_and_upsert_full_day(db, settings, now)
        yesterday_count = _pull_and_upsert_full_day(db, settings, now - timedelta(days=1))
        incremental_count = today_count + yesterday_count
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
            cursor = _get_backfill_cursor(db)
            if cursor is not None and cursor <= target_date:
                break  # target reached
            anchor = datetime.strptime(cursor, "%Y-%m-%d") if cursor else now
            backfill_day = anchor - timedelta(days=1)
            day_label = backfill_day.strftime("%Y-%m-%d")
            day_min = _iso(backfill_day.replace(hour=0, minute=0, second=0, microsecond=0))
            day_max = _iso((backfill_day + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0))
            count = _pull_and_upsert_window(db, settings, day_min, day_max, only_date=day_label)
            # Cursor only ever advances on a deliberate, completed pull of
            # exactly this day -- never inferred from what a stray
            # cross-midnight receipt happened to already leave in the
            # table (see SyncCursor's docstring). This is what makes a day
            # that received only incidental spillover data still get its
            # own real backfill turn, instead of being skipped forever.
            _advance_backfill_cursor(db, day_label)
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
