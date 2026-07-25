"""Cached USD/ILS rate for WhatsApp price lines (Frankfurter API, refresh every N monitor ticks)."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

LOGGER = logging.getLogger(__name__)

FRANKFURTER_URL = "https://api.frankfurter.app/latest?from=USD&to=ILS"

_cached_usd_ils: float | None = None
_monitor_ticks: int = 0
_search_ticks: int = 0


# Decide where to store the saved exchange rate on disk so it survives restarts.
def _cache_path(config: dict[str, Any]) -> Path:
    raw = config.get("fx_cache_path") or "data/fx_usd_ils.json"
    return Path(str(raw))


# Load a previously saved USD→ILS rate from disk so alerts can show ILS even before the first network refresh.
def _load_cache_file(config: dict[str, Any]) -> None:
    global _cached_usd_ils
    path = _cache_path(config)
    if not path.is_file():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        r = data.get("usd_ils")
        if r is not None:
            _cached_usd_ils = float(r)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        LOGGER.warning("fx_rate: could not read cache %s: %s", path, exc)


# Save the latest USD→ILS rate to disk so the monitor can keep using it after restarts.
def _write_cache_file(config: dict[str, Any], rate: float) -> None:
    path = _cache_path(config)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"usd_ils": rate, "updated_at": datetime.now(timezone.utc).isoformat()},
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        LOGGER.warning("fx_rate: could not write cache %s: %s", path, exc)


# Use a configured “good enough” fallback rate when the live lookup fails, so price messages can still include ILS.
def _fallback_rate(config: dict[str, Any]) -> float | None:
    v = config.get("fx_fallback_usd_ils")
    if v is None:
        return None
    try:
        x = float(v)
        return x if x > 0 else None
    except (TypeError, ValueError):
        return None


# Fetch the latest USD→ILS rate from the internet so WhatsApp price lines can show an approximate ILS value.
def _fetch_usd_ils(config: dict[str, Any]) -> float | None:
    timeout = float(config.get("fx_request_timeout_seconds", 5) or 5)
    try:
        resp = requests.get(FRANKFURTER_URL, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        rates = data.get("rates") or {}
        ils = rates.get("ILS")
        if ils is None:
            return None
        return float(ils)
    except Exception as exc:
        LOGGER.warning("fx_rate: fetch failed: %s", exc)
        return None


# Refresh the in-memory rate and update the cache file, falling back to config or disk when the live fetch isn’t available.
def _fetch_and_cache(config: dict[str, Any]) -> None:
    global _cached_usd_ils
    rate = _fetch_usd_ils(config)
    if rate is not None and rate > 0:
        _cached_usd_ils = rate
        _write_cache_file(config, rate)
        LOGGER.info("fx_rate: refreshed usd_ils=%.4f", rate)
        return
    # Failed fetch: the static fallback must NEVER clobber a real last-known rate.
    # Precedence: in-memory last good -> disk cache -> configured fallback. One failed
    # Frankfurter call otherwise put fallback 3.7 over a live ~3.05 rate and every ILS
    # line ran ~20% high until the next successful refresh (2026-07-20 incident,
    # recurred 2026-07-25 after the rollback dropped the first fix).
    if _cached_usd_ils is not None:
        LOGGER.info("fx_rate: fetch failed, keeping last good usd_ils=%.4f", _cached_usd_ils)
        return
    _load_cache_file(config)
    if _cached_usd_ils is not None:
        LOGGER.info("fx_rate: restored from file usd_ils=%.4f", _cached_usd_ils)
        return
    fb = _fallback_rate(config)
    if fb is not None:
        _cached_usd_ils = fb
        LOGGER.info("fx_rate: using fallback usd_ils=%.4f", fb)


# Count successful monitor cycles and occasionally refresh the exchange rate so alerts don’t go stale without calling the network too often.
def bump_monitor_tick(config: dict[str, Any]) -> None:
    """Call once per successful monitor cycle. Refreshes FX every ``fx_refresh_every_runs`` ticks."""
    global _monitor_ticks, _search_ticks, _cached_usd_ils
    if config.get("fx_enabled") is False:
        return
    _monitor_ticks += 1
    _search_ticks = _monitor_ticks
    every = max(1, int(config.get("fx_refresh_every_runs", 10) or 10))
    if _cached_usd_ils is None:
        _load_cache_file(config)
    if _cached_usd_ils is None:
        _fetch_and_cache(config)
    elif _monitor_ticks % every == 0:
        _fetch_and_cache(config)


def bump_search_tick(config: dict[str, Any]) -> None:
    """Backward-compatible alias for older tests/config wording."""
    bump_monitor_tick(config)


# Return the best available USD→ILS rate (from memory, disk, or fallback) so message formatting can add an approximate ILS amount.
def get_usd_ils(config: dict[str, Any]) -> float | None:
    """Effective ILS per 1 USD for formatting (memory, file cache, or fallback)."""
    global _cached_usd_ils
    if config.get("fx_enabled") is False:
        return None
    if _cached_usd_ils is not None and _cached_usd_ils > 0:
        return _cached_usd_ils
    _load_cache_file(config)
    if _cached_usd_ils is not None and _cached_usd_ils > 0:
        return _cached_usd_ils
    return _fallback_rate(config)
