"""
Live Loyverse REST API client.

STATUS: connectivity attempted but not yet confirmed — the first live call
(via the diag endpoint, from Render) hung and hit a read timeout rather than
returning a fast success or a fast auth error. That pointed at the raw
`urllib` implementation this started with: `urllib.request` is known to
sometimes stall mid-response-body-read against servers using chunked
transfer encoding + keep-alive, rather than failing fast. Switched to
`httpx` (already a project dependency, used elsewhere for FastAPI's async
stack) which handles that correctly and gives proper separate connect/read
timeouts instead of one lump per-call timeout.

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

import httpx

from app.config import Settings


class LoyverseError(RuntimeError):
    pass


# Separate connect/read timeouts rather than one lump number — a slow-to-
# establish connection and a slow-to-stream response are different failure
# modes and worth distinguishing when this inevitably needs debugging again.
_TIMEOUT = httpx.Timeout(connect=10.0, read=20.0, write=10.0, pool=10.0)


def _client(settings: Settings) -> httpx.Client:
    return httpx.Client(
        base_url=settings.loyverse_base_url,
        headers={
            "Authorization": f"Bearer {settings.loyverse_api_token}",
            "Accept": "application/json",
        },
        timeout=_TIMEOUT,
    )


def _get(settings: Settings, path: str, params: dict | None = None) -> dict:
    if not settings.loyverse_configured:
        raise LoyverseError(
            "Loyverse is not configured — set LOYVERSE_API_TOKEN as an environment variable."
        )
    clean_params = {k: v for k, v in (params or {}).items() if v is not None}
    try:
        with _client(settings) as client:
            resp = client.get(path, params=clean_params)
    except httpx.TimeoutException as exc:
        raise LoyverseError(f"Loyverse API timed out on {path} ({type(exc).__name__}): {exc}") from exc
    except httpx.HTTPError as exc:
        raise LoyverseError(f"Loyverse API connection failed on {path}: {exc}") from exc

    if resp.status_code >= 400:
        raise LoyverseError(f"Loyverse API {resp.status_code} on {path}: {resp.text[:500]}")
    return resp.json()


def list_stores(settings: Settings) -> list[dict]:
    """[{id, name, address, ...}] — one per physical outlet."""
    data = _get(settings, "/stores")
    return data.get("stores", [])


def list_categories(settings: Settings) -> list[dict]:
    """[{id, name, color}] — used to label each item's category on receipts,
    since line_items only carry item_id/item_name, not category."""
    data = _get(settings, "/categories")
    return data.get("categories", [])


def list_items_page(settings: Settings, cursor: str | None = None, limit: int = 250) -> dict:
    """One page of the item catalog — {id, item_name, category_id, ...}."""
    return _get(settings, "/items", {"cursor": cursor, "limit": limit})


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


def list_all_receipts(
    settings: Settings,
    created_at_min: str,
    created_at_max: str,
    max_pages: int = 2000,
) -> list[dict]:
    """
    Pages through every receipt in [created_at_min, created_at_max) via the
    cursor Loyverse returns. `max_pages` is a hard backstop (at 250/page
    that's 500k receipts) so a bug can't spin this forever against a live
    API — not expected to ever be hit in practice.
    """
    receipts: list[dict] = []
    cursor: str | None = None
    for _ in range(max_pages):
        page = list_receipts_page(
            settings, created_at_min=created_at_min, created_at_max=created_at_max,
            cursor=cursor, limit=250,
        )
        receipts.extend(page.get("receipts", []))
        cursor = page.get("cursor")
        if not cursor:
            break
    return receipts


def list_all_items(settings: Settings, max_pages: int = 200) -> list[dict]:
    """Pages through the full item catalog — {id, item_name, category_id, ...}."""
    items: list[dict] = []
    cursor: str | None = None
    for _ in range(max_pages):
        page = list_items_page(settings, cursor=cursor, limit=250)
        items.extend(page.get("items", []))
        cursor = page.get("cursor")
        if not cursor:
            break
    return items
