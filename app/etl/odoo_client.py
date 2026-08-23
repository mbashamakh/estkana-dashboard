"""
Live Odoo XML-RPC client.

STATUS: implemented, but NOT YET VERIFIED against your real Odoo account.
This backend is currently running in a cloud sandbox whose outbound network
is restricted to an allowlist (package registries etc.) — it cannot reach
`estikanah.odoo.com` directly, so this code has been written correctly to
the best of my knowledge but has never actually been executed against your
data. See "How to verify" at the bottom before trusting this in production.

Credentials (from .env / environment, see app/config.py):
  ODOO_URL=https://estikanah.odoo.com
  ODOO_DB=estikanah              (best guess from the subdomain — confirm)
  ODOO_USERNAME=M.bashamakh@wuthuq.com
  ODOO_API_KEY=<provided, stored only in .env, gitignored>

What model this queries, and why
---------------------------------
The original mrev.json/mcogs.json/mopex.json records ({"a": analytic_account_id,
"n": "[code] Name", "m": "Month YYYY", "v": value}) were pulled by hand earlier
in this project (via browsing Odoo's UI, not a saved script), so the exact
query is not on record. Based on the record shape and how build_pnl.py/
odoo_pnl.py consume it, this implementation's best-supported hypothesis is:

  Model:   account.analytic.line  ("Analytic Items" — one row per journal
           item that carries an analytic distribution; has a direct
           `account_id` Many2one to the analytic account, not the newer
           JSON `analytic_distribution` field on account.move.line, which
           would be much harder to group by directly).
  Split:   revenue / cogs / opex are separated by the linked GL account's
           code prefix on `general_account_id.code`:
             "4%" -> revenue, "5%" -> cogs, "6%" -> opex
           This follows the standard IFRS-style chart of accounts (4=revenue,
           5=cost of sales, 6=operating expenses) and is consistent with the
           Labor Cost GL codes you gave earlier (611xxxxx / 612xxxxx — all
           under the "6" prefix, i.e. opex).
  Group:   read_group by ["account_id", "date:month"], which is what
           produces Odoo's "January 2026"-style month labels.
  Sign:    left as Odoo's native signed amount (revenue positive, cogs/opex
           negative) — matching the *raw* mrev/mcogs/mopex.json files, since
           odoo_pnl.py's build_index() already flips cogs/opex sign itself.

This is a reasoned guess, not a confirmed fact. If the numbers don't
reconcile against known totals (e.g. ARBEEN's January 2026 revenue should
be 148,293.40 SAR per tests/fixtures/mrev.json), the account-code prefixes
or the model itself likely need adjusting — do NOT ship this to production
sync without that check passing.

How to verify
--------------
This needs to run somewhere with real internet access to estikanah.odoo.com:
  1. Connect your desktop app to this session, so I can run it through the
     device bridge (which uses your computer's normal internet), or
  2. Deploy the backend (Cloud Run has full outbound access) and check the
     first sync's numbers against the known-good figures in
     tests/fixtures/pnl_known_good.json, or
  3. Run `python -m app.etl.odoo_client` yourself from a machine with
     internet access and share the output.
"""
from __future__ import annotations

import xmlrpc.client
from collections import defaultdict

from app.config import Settings

_MONTH_GROUPBY = "date:month"


def _authenticate(settings: Settings) -> int:
    common = xmlrpc.client.ServerProxy(f"{settings.odoo_url}/xmlrpc/2/common")
    uid = common.authenticate(settings.odoo_db, settings.odoo_username, settings.odoo_api_key, {})
    if not uid:
        raise RuntimeError(
            f"Odoo authentication failed for {settings.odoo_username} on db "
            f"'{settings.odoo_db}' at {settings.odoo_url} — check ODOO_DB is "
            f"correct (it's currently a guess from the subdomain) and that "
            f"the API key hasn't been revoked/expired."
        )
    return uid


def _execute_kw(settings: Settings, uid: int, model: str, method: str, args: list, kwargs: dict | None = None):
    models = xmlrpc.client.ServerProxy(f"{settings.odoo_url}/xmlrpc/2/object")
    return models.execute_kw(
        settings.odoo_db, uid, settings.odoo_api_key, model, method, args, kwargs or {}
    )


def _pull_group(settings: Settings, uid: int, code_prefix: str, date_from: str = "2026-01-01") -> list[dict]:
    """
    Pulls account.analytic.line rows whose linked GL account code starts
    with `code_prefix` ("4" for revenue, "5" for cogs, "6" for opex),
    grouped by analytic account + month. Returns the {"a","n","m","v"}
    record shape odoo_pnl.build_pnl() expects.
    """
    domain = [
        ("general_account_id.code", "=like", f"{code_prefix}%"),
        ("date", ">=", date_from),
        ("move_id.state", "=", "posted"),
    ]
    groups = _execute_kw(
        settings, uid, "account.analytic.line", "read_group",
        [domain, ["amount"], ["account_id", _MONTH_GROUPBY]],
        {"lazy": False},
    )
    records = []
    for g in groups:
        account = g.get("account_id")
        month = g.get(_MONTH_GROUPBY)
        amount = g.get("amount")
        if not account or not month or amount is None:
            continue
        records.append({"a": account[0], "n": account[1], "m": month, "v": round(amount, 2)})
    return records


def fetch_records(settings: Settings) -> tuple[list[dict], list[dict], list[dict]]:
    """Returns (rev_records, cogs_records, opex_records) in the {"a","n","m","v"} shape."""
    if not settings.odoo_configured:
        raise RuntimeError(
            "Odoo is not configured — set ODOO_URL, ODOO_DB, ODOO_USERNAME, "
            "ODOO_API_KEY as environment variables before running a live sync."
        )
    uid = _authenticate(settings)
    rev = _pull_group(settings, uid, "4")
    cogs = _pull_group(settings, uid, "5")
    opex = _pull_group(settings, uid, "6")
    return rev, cogs, opex


if __name__ == "__main__":
    # Manual verification entrypoint — run this from a machine with real
    # internet access to check the hypothesis above against known totals.
    from app.config import get_settings

    s = get_settings()
    print(f"Connecting to {s.odoo_url} db={s.odoo_db!r} as {s.odoo_username} ...")
    rev, cogs, opex = fetch_records(s)
    print(f"revenue records: {len(rev)}, cogs records: {len(cogs)}, opex records: {len(opex)}")

    arbeen_jan = next(
        (r["v"] for r in rev if "ARBEEN" in r["n"] and r["m"] == "January 2026"), None
    )
    print(f"ARBEEN January 2026 revenue = {arbeen_jan} (expected 148293.4 per tests/fixtures/mrev.json)")
    if arbeen_jan == 148293.4:
        print("MATCH — the account-code hypothesis looks correct.")
    else:
        print("MISMATCH — do not trust this query yet; the code-prefix split or model needs rework.")
