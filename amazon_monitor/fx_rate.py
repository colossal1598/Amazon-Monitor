"""Cached USD/ILS rate for WhatsApp price lines (Frankfurter API, refresh every N search ticks)."""

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
_search_ticks: int = 0


def _cache_path(config: dict[str, Any]) -> Path:
    raw = config.get("fx_cache_path") or "data/fx_usd_ils.json"
    return Path(str(raw))


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


def _fallback_rate(config: dict[str, Any]) -> float | None:
    v = config.get("fx_fallback_usd_ils")
    if v is None:
        return None
    try:
        x = float(v)
        return x if x > 0 else None
    except (TypeError, ValueError):
        return None


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


def _fetch_and_cache(config: dict[str, Any]) -> None:
    global _cached_usd_ils
    rate = _fetch_usd_ils(config)
    if rate is not None and rate > 0:
        _cached_usd_ils = rate
        _write_cache_file(config, rate)
        LOGGER.info("fx_rate: refreshed usd_ils=%.4f", rate)
        return
    fb = _fallback_rate(config)
    if fb is not None:
        _cached_usd_ils = fb
        LOGGER.info("fx_rate: using fallback usd_ils=%.4f", fb)
        return
    if _cached_usd_ils is None:
        _load_cache_file(config)
        if _cached_usd_ils is not None:
            LOGGER.info("fx_rate: restored from file usd_ils=%.4f", _cached_usd_ils)


def bump_search_tick(config: dict[str, Any]) -> None:
    """Call once per successful search_loop. Refreshes FX every ``fx_refresh_every_runs`` ticks."""
    global _search_ticks, _cached_usd_ils
    if config.get("fx_enabled") is False:
        return
    _search_ticks += 1
    every = max(1, int(config.get("fx_refresh_every_runs", 10) or 10))
    if _cached_usd_ils is None:
        _load_cache_file(config)
    if _cached_usd_ils is None:
        _fetch_and_cache(config)
    elif _search_ticks % every == 0:
        _fetch_and_cache(config)


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
