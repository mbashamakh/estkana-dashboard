"""
Orchestrates one Odoo sync: pull raw records, run them through odoo_pnl.py's
build_pnl(), upsert into AnalyticMonthly, record a SyncLog row either way.

This is the entrypoint the Cloud Run Job calls hourly (see the deployment
runbook). It is deliberately separate from odoo_pnl.py's pure aggregation
functions so those stay unit-testable with zero network/DB dependency.

NOTE: odoo_client.fetch_records() is not implemented yet — it's blocked on
the user providing Odoo API credentials (URL, database, username, API key).
Everything downstream of that call (aggregation, upsert, completeness
labeling) is real and tested against fixtures; only the live fetch is a stub.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db.models import AnalyticMonthly, SyncLog
from app.etl.odoo_pnl import build_gl_company, build_labor, build_pnl, valid_month_labels


def _upsert_analytic_monthly(db: Session, kind: str, name: str, month_row: dict, is_complete: bool) -> None:
    """
    Upsert keyed on (kind, name, month) — see AnalyticMonthly's unique
    constraint. Idempotent by design: rerunning the same hour's data (or
    replaying a backfill) overwrites in place instead of duplicating rows,
    which matters because this runs on an hourly schedule against
    overlapping date ranges.
    """
    stmt = pg_insert(AnalyticMonthly).values(
        kind=kind,
        name=name,
        month=month_row["m"],
        revenue=month_row["revenue"],
        cogs=month_row["cogs"],
        opex=month_row["opex"],
        is_complete=is_complete,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["kind", "name", "month"],
        set_={
            "revenue": stmt.excluded.revenue,
            "cogs": stmt.excluded.cogs,
            "opex": stmt.excluded.opex,
            "is_complete": stmt.excluded.is_complete,
            "updated_at": datetime.now(timezone.utc),
        },
    )
    db.execute(stmt)


def _try_fetch_gl_company() -> list[dict] | None:
    """Best-effort ledger pull; a failure here must never break the main sync."""
    try:
        from app.config import get_settings
        from app.etl import odoo_client
        return odoo_client.fetch_gl_company(get_settings())
    except Exception:  # noqa: BLE001
        return None


def _try_fetch_labor() -> list[dict] | None:
    """Best-effort staff-cost pull; a failure here must never break the main sync."""
    try:
        from app.config import get_settings
        from app.etl import odoo_client
        return odoo_client.fetch_labor(get_settings())
    except Exception:  # noqa: BLE001
        return None


def sync_odoo(db: Session, rev: list[dict], cogs: list[dict], opex: list[dict],
              gl: list[dict] | None = None, labor: list[dict] | None = None) -> dict:
    """
    Given already-fetched raw Odoo records, aggregate and persist them.
    Split out from the live fetch so this half can be exercised in tests
    against the same fixtures odoo_pnl.py already uses.
    """
    started = datetime.now(timezone.utc)
    try:
        result = build_pnl(rev, cogs, opex)
        complete_labels = set(valid_month_labels(result["branch_rollup"]["months"]))

        for branch in result["branches"]:
            for row in branch["months"]:
                _upsert_analytic_monthly(
                    db, kind="branch", name=branch["odoo_name"], month_row=row,
                    is_complete=row["m"] in complete_labels,
                )
        for account in result["overhead_accounts"]:
            for row in account["months"]:
                _upsert_analytic_monthly(
                    db, kind="overhead", name=account["name"], month_row=row,
                    is_complete=row["m"] in complete_labels,
                )
        # Combined "all overhead cost centers" total (includes Kaftrea, unlike
        # overhead_accounts above which is only the 7 named-breakdown accounts
        # the user asked to see individually) — needed so /api/pnl's
        # company_total and PNL.overhead.months are complete, not undercounted.
        for row in result["overhead"]["months"]:
            _upsert_analytic_monthly(
                db, kind="overhead_total", name="ALL", month_row=row,
                is_complete=row["m"] in complete_labels,
            )

        # Company totals from Odoo's general ledger (ties to its own P&L report).
        if gl is None:
            gl = _try_fetch_gl_company()
        gl_months = build_gl_company(gl) if gl else []
        for row in gl_months:
            _upsert_analytic_monthly(
                db, kind="gl", name="ALL",
                month_row={"m": row["m"], "revenue": row["revenue"], "cogs": row["cogs"], "opex": row["opex"]},
                is_complete=True,
            )
            _upsert_analytic_monthly(
                db, kind="gl_other", name="ALL",
                month_row={"m": row["m"], "revenue": row["other_income"], "cogs": row["other_expense"], "opex": 0.0},
                is_complete=True,
            )

        # Staff cost per outlet (GL 611xxxxx + 612xxxxx) -> Labor Cost %.
        if labor is None:
            labor = _try_fetch_labor()
        labor_rows = build_labor(labor) if labor else []
        for row in labor_rows:
            _upsert_analytic_monthly(
                db, kind="labor", name=row["name"],
                month_row={"m": row["m"], "revenue": 0.0, "cogs": 0.0, "opex": row["labor"]},
                is_complete=True,
            )

        db.add(SyncLog(
            source="odoo", success=True,
            message=f"{len(result['branches'])} branches synced" + (f", {len(gl_months)} ledger months" if gl_months else ", ledger totals unavailable"),
            started_at=started, finished_at=datetime.now(timezone.utc),
        ))
        db.commit()
        return {"success": True, "branches": len(result["branches"])}
    except Exception as exc:  # noqa: BLE001 — deliberately broad: this must never crash the job silently
        db.rollback()
        db.add(SyncLog(
            source="odoo", success=False, message=str(exc)[:2000],
            started_at=started, finished_at=datetime.now(timezone.utc),
        ))
        db.commit()
        raise
