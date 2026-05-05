"""Pure helpers for combining SERP search observations."""

from __future__ import annotations

import re
from typing import Any


_ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")


def _valid_asin(value: str | None) -> bool:
    return bool(value and _ASIN_RE.match(value.strip().upper()))


def should_reconcile_missing_asins(config: dict, filtered_count: int) -> tuple[bool, str | None]:
    if not config.get("enable_missing_asin_oos", True):
        return False, "disabled_by_config"
    min_results = int(config.get("min_results_for_absence_reconcile", 1))
    if filtered_count < min_results:
        return False, f"filtered_count_below_min:{filtered_count}<{min_results}"
    return True, None


def exclude_asins_from_candidates(
    rows: list[dict[str, Any]],
    exclude: set[str] | None,
) -> list[dict[str, Any]]:
    """Drop rows whose ASIN is in ``exclude`` (used to keep PDP watch ASINs out of SERP updates)."""
    if not exclude:
        return list(rows)
    excl = {str(a or "").strip().upper() for a in exclude}
    return [row for row in rows if (row.get("asin") or "").strip().upper() not in excl]


def merge_search_candidates_by_asin(*candidate_lists: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge filtered SERP rows into one presence-derived observation per ASIN."""
    by_asin: dict[str, dict[str, Any]] = {}
    original_stock_by_asin: dict[str, bool] = {}

    for candidates in candidate_lists:
        for row in candidates:
            asin = (row.get("asin") or "").strip().upper()
            if not _valid_asin(asin):
                continue

            original_in_stock = bool(row.get("in_stock"))
            existing = by_asin.get(asin)
            if existing is not None and (original_stock_by_asin[asin] or not original_in_stock):
                continue

            merged = dict(row)
            merged["asin"] = asin
            # For the search pass, a filtered SERP hit is the stock signal.
            # Absence reconciliation handles the OOS transition.
            merged["in_stock"] = True
            by_asin[asin] = merged
            original_stock_by_asin[asin] = original_in_stock

    return list(by_asin.values())
