"""
Builds the /api/data response: real Loyverse sales figures overlaid onto a
sample "template" for whatever this dashboard section still can't source
for real (cost %, labor %, waste, customer rating, complaints — none of
which Loyverse or Odoo currently supply per-branch; see project notes).

Only 8 of the 18 branches ever got hand-built sample values for those
still-sample fields (this app's original mockup phase never finished the
other 10). Rather than inventing branch-specific numbers with no basis,
the other 10 (and ARBEEN, excluded from real Loyverse data — see
loyverse_pnl.LOYVERSE_TEST_BRANCHES) get the AVERAGE of the 8 real sample
branches for those fields. This is a clearly-labeled placeholder (the
frontend already tags every such field "(sample)"), not a claim of
accuracy — averaging just keeps the dashboard visually coherent instead of
showing 0% food cost, which would look like a bug rather than missing data.

Sales-side fields (daily sales, orders, AOV, discounts, refunds, category
mix, top products) are 100% real for all 17 non-test branches, sourced from
LoyverseDaily.

Month/YTD availability is gated on backfill completeness — see
`_available_months()` — so a month is only ever offered once every day in
it has actually synced. No month is ever shown as "zero sales" because it
simply isn't backfilled yet.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import LoyverseDaily
from app.etl.loyverse_pnl import LOYVERSE_TEST_BRANCHES
from app.etl.loyverse_store_map import STORE_ID_TO_ODOO_NAME
from app.etl.odoo_pnl import LOYVERSE_MAP  # odoo_name -> Loyverse-style display name

_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "sample_data.json"
_SAMPLE_ONLY_SCALAR_FIELDS = [
    "food_cost_pct", "standard_cost_pct", "labor_cost_pct", "waste_pct",
    "inv_variance_pct", "rating", "gross_margin_pct",
]
_SAMPLE_ONLY_MONTHLY_FIELDS = ["target", "waste_value", "complaints", "gross_profit", "contribution"]

ALL_ODOO_BRANCH_NAMES = sorted(set(STORE_ID_TO_ODOO_NAME.values()) | {"ARBEEN"})
REAL_BRANCH_NAMES = sorted(set(STORE_ID_TO_ODOO_NAME.values()) - LOYVERSE_TEST_BRANCHES)


def _slugify(name: str) -> str:
    out = "".join(c.lower() if c.isalnum() else "-" for c in name)
    while "--" in out:
        out = out.replace("--", "-")
    return out.strip("-")


def _load_sample() -> dict:
    with open(_FIXTURE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _sample_averages(sample: dict) -> dict:
    """Averages the 8 hand-built sample branches' still-sample fields, for
    use as a placeholder on the 10 branches that never got sample values."""
    branches = sample["branches"]
    n = len(branches)
    scalars = {f: sum(b[f] for b in branches) / n for f in _SAMPLE_ONLY_SCALAR_FIELDS}
    # monthly fields, averaged per month index
    monthly_by_mi: dict[int, dict] = defaultdict(lambda: defaultdict(float))
    counts: dict[int, int] = defaultdict(int)
    for b in branches:
        for row in b["monthly"]:
            counts[row["mi"]] += 1
            for f in _SAMPLE_ONLY_MONTHLY_FIELDS:
                monthly_by_mi[row["mi"]][f] += row[f]
    monthly_avg = {
        mi: {f: vals[f] / counts[mi] for f in _SAMPLE_ONLY_MONTHLY_FIELDS}
        for mi, vals in monthly_by_mi.items()
    }
    return {
        "scalars": scalars,
        "monthly": monthly_avg,
        "waste_breakdown": branches[0]["waste_breakdown"],
        "peak_hours": branches[0]["peak_hours"],
        "daily_ly": branches[0]["daily_ly"],
    }


def _available_months(rows: list[LoyverseDaily], year: int, today: date) -> list[tuple[int, str, date, date, bool]]:
    """
    Returns [(month_index, "Month YYYY", month_start, month_end_inclusive,
    is_partial)] for every month from January through the current month
    that's actually backfilled — a past month only appears once every day
    in it has synced; the current month appears once at least one day has
    synced, flagged is_partial until it's synced through yesterday.
    """
    if not rows:
        return []
    dates = sorted(datetime.strptime(r.date, "%Y-%m-%d").date() for r in rows)
    earliest, latest = dates[0], dates[-1]

    months = []
    for mi in range(1, 13):
        month_start = date(year, mi, 1)
        if month_start > today or month_start < earliest:
            continue
        month_end = date(year, mi + 1, 1) - timedelta(days=1) if mi < 12 else date(year, 12, 31)
        is_current_month = (today.year, today.month) == (year, mi)
        if is_current_month:
            covered_through = today - timedelta(days=1)  # "yesterday" — today itself is still accumulating
            if latest < month_start:
                continue
            is_partial = latest < covered_through
        else:
            if latest < month_end:
                continue  # this and every later month aren't backfilled yet
            is_partial = False
        months.append((mi, f"{date(year, mi, 1).strftime('%B')} {year}", month_start, month_end, is_partial))
    return months


def build_data_response(db: Session) -> dict:
    sample = _load_sample()
    sample_by_name = {b["name"]: b for b in sample["branches"]}
    # a couple of sample branch display names differ slightly from the
    # canonical odoo_pnl.LOYVERSE_MAP spelling — normalize by matching on
    # the odoo_name -> loyverse display name mapping instead of assuming
    # sample["branches"][i]["name"] lines up 1:1 with LOYVERSE_MAP values.
    odoo_name_to_sample = {}
    for odoo_name, loy_name in LOYVERSE_MAP.items():
        if loy_name in sample_by_name:
            odoo_name_to_sample[odoo_name] = sample_by_name[loy_name]

    avg = _sample_averages(sample)

    rows = db.scalars(
        select(LoyverseDaily).where(LoyverseDaily.branch.in_(REAL_BRANCH_NAMES)).order_by(LoyverseDaily.date)
    ).all()
    today = datetime.now(timezone.utc).date()
    year = today.year
    months = _available_months(rows, year, today)
    n_days = (months[-1][3] - date(year, 1, 1)).days + 1 if months else 0

    rows_by_branch: dict[str, dict[str, LoyverseDaily]] = defaultdict(dict)
    for r in rows:
        rows_by_branch[r.branch][r.date] = r

    branches_out = []
    for odoo_name in ALL_ODOO_BRANCH_NAMES:
        loy_name = LOYVERSE_MAP.get(odoo_name, odoo_name)
        sample_b = odoo_name_to_sample.get(odoo_name)
        is_real = odoo_name in REAL_BRANCH_NAMES

        # --- real, day-indexed sales array (length n_days, from Jan 1) ---
        daily = [0.0] * n_days
        month_agg: dict[int, dict] = {}
        cat_sales: dict[str, float] = defaultdict(float)
        item_agg: dict[str, dict] = {}
        total_sales = total_orders = total_discount = total_refund = 0.0
        if is_real:
            branch_rows = rows_by_branch.get(odoo_name, {})
            for mi, _label, m_start, m_end, _partial in months:
                m_sales = m_orders = m_discount = m_refund = 0.0
                d = m_start
                while d <= m_end and d <= today:
                    row = branch_rows.get(d.isoformat())
                    if row:
                        offset = (d - date(year, 1, 1)).days
                        if 0 <= offset < n_days:
                            daily[offset] = round(row.sales, 2)
                        m_sales += row.sales
                        m_orders += row.orders
                        m_discount += row.discount_amt
                        m_refund += row.refund_amt
                        total_sales += row.sales
                        total_orders += row.orders
                        total_discount += row.discount_amt
                        total_refund += row.refund_amt
                        for name, info in row.line_items.items():
                            cat_sales[info["cat"]] += info["sales"]
                            entry = item_agg.setdefault(name, {"name": name, "cat": info["cat"], "qty": 0.0, "sales": 0.0})
                            entry["qty"] += info["qty"]
                            entry["sales"] += info["sales"]
                    d += timedelta(days=1)
                month_agg[mi] = {"sales": round(m_sales, 2), "orders": int(m_orders),
                                  "discount_amt": round(m_discount, 2), "refund_amt": round(m_refund, 2)}

        aov = (total_sales / total_orders) if total_orders else 0.0
        discount_pct = (total_discount / total_sales * 100) if total_sales else 0.0
        refund_pct = (total_refund / total_sales * 100) if total_sales else 0.0
        cat_total = sum(cat_sales.values())
        categories = {c: round(cat_sales.get(c, 0) / cat_total * 100, 2) if cat_total else 0.0
                      for c in sample["meta"]["categories"]}
        items = sorted(
            [{"name": v["name"], "cat": v["cat"], "qty": round(v["qty"]), "sales": round(v["sales"])} for v in item_agg.values()],
            key=lambda x: -x["sales"],
        )[:20]

        monthly_rows = []
        for mi, label, _s, _e, _partial in months:
            real_part = month_agg.get(mi, {"sales": 0.0, "orders": 0, "discount_amt": 0.0, "refund_amt": 0.0})
            sample_part = (
                {f: sample_b["monthly"][mi - 1][f] for f in _SAMPLE_ONLY_MONTHLY_FIELDS}
                if sample_b and mi - 1 < len(sample_b["monthly"])
                else avg["monthly"].get(mi, {f: 0.0 for f in _SAMPLE_ONLY_MONTHLY_FIELDS})
            )
            monthly_rows.append({
                "mi": mi, "m": label,
                "sales": real_part["sales"], "sales_prev": None,
                "orders": real_part["orders"],
                "discount_amt": real_part["discount_amt"], "refund_amt": real_part["refund_amt"],
                "refund_orders": 0,
                **sample_part,
            })

        scalars = {f: (sample_b[f] if sample_b else avg["scalars"][f]) for f in _SAMPLE_ONLY_SCALAR_FIELDS}

        branches_out.append({
            "id": _slugify(odoo_name),
            "name": loy_name,
            "region": sample_b["region"] if sample_b else "—",
            "aov": round(aov, 2) if is_real else (sample_b["aov"] if sample_b else avg["scalars"].get("aov", 0)),
            **scalars,
            "discount_pct": round(discount_pct, 2) if is_real else (sample_b["discount_pct"] if sample_b else 0),
            "refund_pct": round(refund_pct, 2) if is_real else (sample_b["refund_pct"] if sample_b else 0),
            "categories": categories if is_real else (sample_b["categories"] if sample_b else {c: 0 for c in sample["meta"]["categories"]}),
            "items": items if (is_real and items) else (sample_b["items"] if sample_b else []),
            "waste_breakdown": sample_b["waste_breakdown"] if sample_b else avg["waste_breakdown"],
            "peak_hours": sample_b["peak_hours"] if sample_b else avg["peak_hours"],
            "daily": daily if is_real else (sample_b["daily"][:n_days] if sample_b else [0.0] * n_days),
            "daily_ly": sample_b["daily_ly"][:n_days] if sample_b else avg["daily_ly"][:n_days],
            "monthly": monthly_rows,
            "is_real_sales": is_real,
        })

    month_names = [f"{label}{' (partial — syncing)' if partial else ''}" for _mi, label, _s, _e, partial in months]
    month_ranges = []
    offset = 0
    for _mi, _label, m_start, m_end, _partial in months:
        days_in_month = (min(m_end, today) - m_start).days + 1
        month_ranges.append([offset, offset + days_in_month])
        offset += days_in_month

    return {
        "branches": branches_out,
        "meta": {
            "categories": sample["meta"]["categories"],
            "daily_start": date(year, 1, 1).isoformat(),
            "days": n_days,
            "month_names": month_names,
            "month_ranges": month_ranges,
            "daily_start_last_year": sample["meta"]["daily_start_last_year"],
        },
        "sync_status": {
            "months_available": len(months),
            "backfill_target": "2026-01-01",
            "earliest_synced": rows[0].date if rows else None,
            "latest_synced": rows[-1].date if rows else None,
        },
    }
