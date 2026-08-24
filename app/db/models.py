"""
SQLAlchemy models.

Two different data-maturity levels are modeled deliberately differently:

- PNL data (AnalyticMonthly) is REAL, already-validated Odoo data with a
  known shape (see etl/odoo_pnl.py, ported from build_pnl.py) — it gets
  proper relational columns with a uniqueness constraint so hourly re-syncs
  upsert cleanly instead of double-counting.

- The broader outlet DATA blob (daily sales, items, categories, and the
  still-partially-sample per-branch fields like standard_cost_pct) is stored
  as a single versioned JSON snapshot (DataSnapshot) for now. Most of its
  fields don't have a live source wired up yet (Loyverse pull is still
  blocked on an API token, Standard Cost still needs the user's Excel
  sheet). Once the Loyverse ETL lands, this should be normalized into real
  tables the same way PNL was — don't mistake the JSONB shortcut for the
  final design.
"""
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Branch(Base):
    """The 18 outlet cost centers. Static/reference data, not re-synced hourly."""
    __tablename__ = "branches"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # slug, e.g. "hamdaniyah"
    odoo_name: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    odoo_code: Mapped[str | None] = mapped_column(String, nullable=True)
    loyverse_name: Mapped[str | None] = mapped_column(String, nullable=True)
    region: Mapped[str | None] = mapped_column(String, nullable=True)
    display_name: Mapped[str] = mapped_column(String, nullable=False)

    rating: Mapped["CustomerRating"] = relationship(back_populates="branch", uselist=False)


class AnalyticMonthly(Base):
    """
    One row per (kind, name, month) — revenue/cogs/opex for either a branch
    or an HQ/overhead cost center, exactly matching what build_pnl.py already
    groups Odoo's raw analytic-account records into. Upserted on
    (kind, name, month), not appended, so hourly re-syncs are idempotent.
    """
    __tablename__ = "analytic_monthly"
    __table_args__ = (UniqueConstraint("kind", "name", "month", name="uq_analytic_monthly"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String, nullable=False)  # "branch" | "overhead" | "dormant"
    name: Mapped[str] = mapped_column(String, nullable=False)  # odoo_name, e.g. "ARBEEN" or "CPU"
    month: Mapped[str] = mapped_column(String, nullable=False)  # "January 2026"
    revenue: Mapped[float] = mapped_column(Float, default=0.0)
    cogs: Mapped[float] = mapped_column(Float, default=0.0)
    opex: Mapped[float] = mapped_column(Float, default=0.0)
    # Whether this month is "complete enough" to show as final (Odoo posts
    # revenue/COGS/opex on different lags — see etl/odoo_pnl.py). Until the
    # completeness rule is applied, ETL writes rows but the API layer should
    # treat incomplete months conservatively (matches the existing
    # PNL_VALID_MONTHS filter in the current dashboard).
    is_complete: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class LoyverseDaily(Base):
    """
    One row per (branch, date) — aggregated from raw Loyverse receipts by
    etl/loyverse_pnl.aggregate_receipts(). Deliberately stores the
    aggregate, not raw receipts: a single branch does thousands of receipts
    a day, so keeping the daily summary (this table) instead of the raw
    transactions is what keeps this cheap to store and fast to query,
    matching how the dashboard actually consumes it (daily trend, top
    products, category mix — never a single receipt).

    `line_items` is a JSON blob ({item_name: {"cat", "qty", "sales"}})
    rather than its own table for the same reason AnalyticMonthly didn't
    get one for its month rows — this is fine to query/aggregate in Python
    at read time and isn't worth a join for what's currently a single
    dashboard. Named `line_items`, not `items` — SQLAlchemy's upsert
    `excluded` proxy resolves `.items` to the dict-like `.items()` method
    before it resolves to a column of that name, so a column literally
    named "items" silently breaks `on_conflict_do_update`. Learned that the
    hard way; renaming was simpler than fighting the attribute lookup.
    """
    __tablename__ = "loyverse_daily"
    __table_args__ = (UniqueConstraint("branch", "date", name="uq_loyverse_daily"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    branch: Mapped[str] = mapped_column(String, nullable=False)  # odoo_name, e.g. "ARBEEN"
    date: Mapped[str] = mapped_column(String, nullable=False)    # "2026-08-16"
    sales: Mapped[float] = mapped_column(Float, default=0.0)
    orders: Mapped[int] = mapped_column(Integer, default=0)
    discount_amt: Mapped[float] = mapped_column(Float, default=0.0)
    refund_amt: Mapped[float] = mapped_column(Float, default=0.0)
    line_items: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class CustomerRating(Base):
    """
    Manually maintained — Customer Rating is sourced from Google Maps via a
    browser lookup, which was explicitly decided NOT to be automated (see
    project notes). An admin edits this periodically; it is never written by
    the hourly ETL job.
    """
    __tablename__ = "customer_ratings"

    branch_id: Mapped[str] = mapped_column(ForeignKey("branches.id"), primary_key=True)
    rating: Mapped[float] = mapped_column(Float, nullable=False)
    source_note: Mapped[str] = mapped_column(String, default="Google Maps (manual lookup)")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)
    updated_by: Mapped[str | None] = mapped_column(String, nullable=True)

    branch: Mapped["Branch"] = relationship(back_populates="rating")


class DataSnapshot(Base):
    """
    Versioned JSON snapshot backing /api/data (the sample-data-heavy outlet
    dashboard payload). See module docstring — this is a deliberate shortcut
    until the Loyverse ETL replaces the sample fields with real tables.
    """
    __tablename__ = "data_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    schema_version: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class SyncLog(Base):
    """
    One row per ETL run per source, so the frontend can show "Financials as
    of <time>; Sales as of <time>" instead of silently going stale if one
    source's pull fails while the other keeps succeeding.
    """
    __tablename__ = "sync_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String, nullable=False)  # "odoo" | "loyverse"
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    message: Mapped[str | None] = mapped_column(String, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class User(Base):
    """
    Login-gated access, admin-managed — no self-signup. `password_hash` is
    nullable because the final auth mechanism (password vs. Google Sign-In
    restricted to a Workspace domain / explicit allowlist) is still pending
    confirmation of whether the user's domain is Workspace-managed.
    """
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    password_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
