"""
compute_health() must exclude any issue a human has acknowledged via
DataHealthAck (app/api/health.py's POST /api/data-health/acknowledge) from
its "issues"/"ok"/"issue_count" result, since that's what drives the
dashboard banner, the nightly auto-heal retry loop
(app/etl/run_loyverse_autoheal.py), and the alert email
(app/etl/alerting.py) -- an acknowledged issue should stop showing, stop
being retried, and stop being emailed about, while still coming back
separately as "acknowledged_issues" so the explanation isn't lost.

In-memory SQLite, since this project doesn't have a shared DB test fixture
yet (see tests/test_odoo_pnl.py and test_data_builder.py, both deliberately
DB-free) -- compute_health() needs a real Session, so this is the smallest
thing that can stand in for one.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import DataHealthAck, LoyverseDaily
from app.db.session import Base
from app.etl.data_health import compute_health


def _make_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _seed_collapse_and_missing(db):
    """15 healthy trailing days at 100 orders/day, then one collapsed day
    (2 orders), then one fully-missing day, then one more healthy day so
    the branch's active date range still spans across the gap -- matching
    exactly what compute_health's collapse/missing checks look for."""
    today = datetime.now(timezone.utc).date()
    d = today - timedelta(days=20)
    stop = today - timedelta(days=5)
    while d < stop:
        db.add(LoyverseDaily(branch="SHARKIA", date=d.isoformat(), orders=100, sales=5000.0))
        d += timedelta(days=1)

    collapse_date = d.isoformat()
    db.add(LoyverseDaily(branch="SHARKIA", date=collapse_date, orders=2, sales=14.0))

    missing_date = (d + timedelta(days=1)).isoformat()
    resume_date = (d + timedelta(days=2)).isoformat()
    db.add(LoyverseDaily(branch="SHARKIA", date=resume_date, orders=95, sales=4800.0))

    db.commit()
    return collapse_date, missing_date


def test_unacknowledged_issues_show_up_as_active():
    db = _make_session()
    collapse_date, missing_date = _seed_collapse_and_missing(db)

    health = compute_health(db)

    assert health["ok"] is False
    found = {(i["branch"], i["date"]): i["type"] for i in health["issues"]}
    assert found.get(("SHARKIA", collapse_date)) == "collapse"
    assert found.get(("SHARKIA", missing_date)) == "missing"
    assert health["acknowledged_issues"] == []


def test_acknowledged_issue_is_excluded_from_active_list_only():
    db = _make_session()
    collapse_date, missing_date = _seed_collapse_and_missing(db)

    db.add(DataHealthAck(
        branch="SHARKIA",
        date=collapse_date,
        issue_type="collapse",
        note="Confirmed against Loyverse's own report -- branch was closed that day.",
        acknowledged_by="m.bashamakh@wuthuq.com",
        acknowledged_at=datetime.now(timezone.utc),
    ))
    db.commit()

    health = compute_health(db)

    active_dates = {(i["branch"], i["date"]) for i in health["issues"]}
    assert ("SHARKIA", collapse_date) not in active_dates  # acknowledged -- no longer active
    assert ("SHARKIA", missing_date) in active_dates        # untouched -- still active
    assert health["issue_count"] == 1
    assert health["ok"] is False  # one real issue still outstanding

    acked = {(i["branch"], i["date"]): i for i in health["acknowledged_issues"]}
    assert ("SHARKIA", collapse_date) in acked
    assert acked[("SHARKIA", collapse_date)]["acknowledged_by"] == "m.bashamakh@wuthuq.com"
    assert acked[("SHARKIA", collapse_date)]["note"].startswith("Confirmed")


def test_fully_acknowledged_branch_reports_ok():
    db = _make_session()
    collapse_date, missing_date = _seed_collapse_and_missing(db)

    for date, issue_type in [(collapse_date, "collapse"), (missing_date, "missing")]:
        db.add(DataHealthAck(
            branch="SHARKIA", date=date, issue_type=issue_type, note=None,
            acknowledged_by="m.bashamakh@wuthuq.com", acknowledged_at=datetime.now(timezone.utc),
        ))
    db.commit()

    health = compute_health(db)

    assert health["ok"] is True
    assert health["issue_count"] == 0
    assert health["issues"] == []
    assert len(health["acknowledged_issues"]) == 2
