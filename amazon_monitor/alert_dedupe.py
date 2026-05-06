"""Alert list utilities (kept dependency-light for tests)."""

from __future__ import annotations

from typing import Any


# Keep only one alert per product by grouping by ASIN and keeping the most important alert type for each one.
def dedupe_alerts_by_asin(alerts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """At most one outbound message per ASIN per scheduler tick; higher-priority types win."""
    priority = {"back_in_stock": 0, "new_product": 1, "price_drop": 2}
    best: dict[str, tuple[int, dict[str, Any]]] = {}
    for alert in alerts:
        asin = (alert.get("asin") or "").strip().upper()
        if not asin:
            continue
        t = str(alert.get("type") or "")
        rank = priority.get(t, 99)
        prev = best.get(asin)
        if prev is None or rank < prev[0]:
            best[asin] = (rank, alert)
    return [best[k][1] for k in sorted(best.keys())]
