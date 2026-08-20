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
from app.etl.odoo_pnl import build_pnl, valid_month_labels


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


def sync_odoo(db: Session, rev: list[dict], cogs: list[dict], opex: list[dict]) -> dict:
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

        db.add(SyncLog(
            source="odoo", success=True, message=f"{len(result['branches'])} branches synced",
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
