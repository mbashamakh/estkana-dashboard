"""
Live Loyverse REST API client.

STATUS: just started — connectivity not yet verified against the real
account (same sandbox network restriction as odoo_client.py; this has to be
tested by deploying to Render and checking through a diagnostic endpoint,
not from this dev sandbox).

Auth: simple Bearer token (Settings.loyverse_api_token), no OAuth flow needed
since this is a single-account personal access token, not a multi-merchant
integration.

API docs: https://developer.loyverse.com/docs/ (Loyverse API v1.0, REST/JSON,
base URL https://api.loyverse.com/v1.0). Two endpoints matter most for this
dashboard:
  GET /stores    -> the 18 (or so) physical outlets, each with an id + name.
                     Needed to map Loyverse's own store names onto the
                     Odoo branch names already keyed in odoo_pnl.LOYVERSE_MAP
                     (by name, not id — so this pull also double-checks that
                     mapping is accurate, not just guessed).
  GET /receipts  -> the actual sales transactions. Paginated via `cursor`;
                     filterable by `created_at_min` / `created_at_max` and
                     `store_id`. This is what daily sales trend, orders,
                     AOV, discounts, refunds, and top/bottom products all
                     have to be built from.
"""
from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
import json as jsonlib

from app.config import Settings


class LoyverseError(RuntimeError):
    pass


def _get(settings: Settings, path: str, params: dict | None = None) -> dict:
    if not settings.loyverse_configured:
        raise LoyverseError(
            "Loyverse is not configured — set LOYVERSE_API_TOKEN as an environment variable."
        )
    url = f"{settings.loyverse_base_url}{path}"
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {settings.loyverse_api_token}",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return jsonlib.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise LoyverseError(f"Loyverse API {exc.code} on {path}: {body[:500]}") from exc
    except urllib.error.URLError as exc:
        raise LoyverseError(f"Loyverse API connection failed on {path}: {exc}") from exc


def list_stores(settings: Settings) -> list[dict]:
    """[{id, name, address, ...}] — one per physical outlet."""
    data = _get(settings, "/stores")
    return data.get("stores", [])


def list_receipts_page(
    settings: Settings,
    created_at_min: str | None = None,
    created_at_max: str | None = None,
    cursor: str | None = None,
    limit: int = 250,
) -> dict:
    """
    One page of receipts. `created_at_min`/`created_at_max` are ISO 8601
    strings (e.g. "2026-01-01T00:00:00.000Z"). Returns the raw response
    dict — {"receipts": [...], "cursor": "..." or None}.
    """
    return _get(
        settings, "/receipts",
        {
            "created_at_min": created_at_min,
            "created_at_max": created_at_max,
            "cursor": cursor,
            "limit": limit,
        },
    )
