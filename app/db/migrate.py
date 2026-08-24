"""
Tiny, hand-written startup migrations — this project doesn't have Alembic
actually wired up yet (see main.py's docstring: create_all() only, meant to
be swapped for real migrations once the schema stabilizes). create_all()
handles new tables fine but never touches an existing table's columns, so a
rename/drop needs a one-off manual fixup here instead.

Each function must be safe to run on every startup (checks before acting)
since this runs unconditionally, every deploy, forever — not a one-shot
"migration N of M" system.
"""
from __future__ import annotations

from sqlalchemy import inspect
from sqlalchemy.engine import Engine


def run_startup_migrations(engine: Engine) -> None:
    _rename_loyverse_daily_items_column(engine)


def _rename_loyverse_daily_items_column(engine: Engine) -> None:
    """
    loyverse_daily was first deployed with a column named "items", which
    turned out to collide with SQLAlchemy's `excluded.items` upsert proxy
    resolving to the dict-like `.items()` method instead of the column
    (see LoyverseDaily's docstring in db/models.py). Renamed to
    "line_items" in code; this brings any already-created table in line
    with that, without losing whatever rows synced before the rename.
    """
    inspector = inspect(engine)
    if "loyverse_daily" not in inspector.get_table_names():
        return  # fresh DB — create_all() will make the table with the right name already

    columns = {c["name"] for c in inspector.get_columns("loyverse_daily")}
    if "line_items" in columns:
        return  # already migrated
    if "items" not in columns:
        return  # unexpected, but nothing this function knows how to fix

    with engine.begin() as conn:
        conn.exec_driver_sql("ALTER TABLE loyverse_daily RENAME COLUMN items TO line_items")
