"""When PDP watch scrape is inconclusive, fall back to the same-cycle AES LLC SERP row."""

from __future__ import annotations

import logging
from typing import Any

from pdp_scraper import _pdp_skip_row, merchant_matches_allowed

LOGGER = logging.getLogger(__name__)


def _price_ok(value: Any) -> bool:
    if value is None:
        return False
    try:
        return float(value) > 0
    except (TypeError, ValueError):
        return False


def pdp_row_needs_serp_fallback(row: dict[str, Any]) -> bool:
    """True when PDP did not produce a qualifying first-party offer row."""
    if row.get("_skip_update"):
        return True
    if not bool(row.get("in_stock")):
        return True
    return not _price_ok(row.get("price"))


def build_aes_fallback_index(
    aes_rows: list[dict[str, Any]],
    watch_asins: set[str],
) -> dict[str, dict[str, Any]]:
    """Map watch ASIN -> AES pipeline row from this cycle."""
    watch = {str(a).strip().upper() for a in watch_asins if a}
    out: dict[str, dict[str, Any]] = {}
    for row in aes_rows:
        asin = (row.get("asin") or "").strip().upper()
        if asin and asin in watch:
            out[asin] = row
    return out


def aes_row_usable_for_fallback(
    row: dict[str, Any],
    allowed_seller_substrings: list[str],
) -> bool:
    """AES SERP row must have a price; optional seller-text check when the card shows a seller."""
    if not _price_ok(row.get("price")):
        return False
    seller_text = str(row.get("seller_text") or "").strip()
    if not seller_text or not allowed_seller_substrings:
        return True
    blob = f"{seller_text}\n{row.get('shipping_text') or ''}"
    return merchant_matches_allowed(blob, allowed_seller_substrings)


def to_aes_fallback_observation(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out["asin"] = (out.get("asin") or "").strip().upper()
    out["source"] = "aes_serp_fallback"
    out["seller"] = "aes_serp_fallback"
    out["in_stock"] = True
    return out


def resolve_pdp_watch_observations(
    pdp_rows: list[dict[str, Any]],
    aes_by_asin: dict[str, dict[str, Any]],
    allowed_seller_substrings: list[str],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Prefer qualifying PDP rows; else same-cycle AES SERP; else skip (no false OOS)."""
    stats = {"pdp_used": 0, "aes_fallback": 0, "skip_no_fallback": 0}
    resolved: list[dict[str, Any]] = []
    for pdp_row in pdp_rows:
        asin = (pdp_row.get("asin") or "").strip().upper()
        if not asin:
            continue
        if not pdp_row_needs_serp_fallback(pdp_row):
            stats["pdp_used"] += 1
            resolved.append(pdp_row)
            continue
        aes = aes_by_asin.get(asin)
        if aes and aes_row_usable_for_fallback(aes, allowed_seller_substrings):
            stats["aes_fallback"] += 1
            fb = to_aes_fallback_observation(aes)
            LOGGER.info(
                "pdp_watch_aes_serp_fallback asin=%s pdp_skip=%s pdp_in_stock=%s",
                asin,
                bool(pdp_row.get("_skip_update")),
                pdp_row.get("in_stock"),
                extra={"channel": "debug"},
            )
            resolved.append(fb)
            continue
        if pdp_row.get("_skip_update"):
            stats["skip_no_fallback"] += 1
            resolved.append(pdp_row)
            continue
        stats["skip_no_fallback"] += 1
        resolved.append(_pdp_skip_row(asin, "pdp_disqualified_no_aes_fallback"))
    return resolved, stats
