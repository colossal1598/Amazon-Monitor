"""Light per-cycle time and estimated network usage (Israel timestamps, no per-ASIN logs)."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

LOGGER = logging.getLogger(__name__)


def _israel_tz():
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo("Asia/Jerusalem")
    except Exception:
        return timezone(timedelta(hours=3), name="Asia/Jerusalem")
_AMAZON_HOST_SUFFIXES = (".amazon.com", ".media-amazon.com", ".ssl-images-amazon.com")


def israel_now_iso() -> str:
    return datetime.now(_israel_tz()).isoformat(timespec="seconds")


@dataclass
class _CycleState:
    started_at_iso: str = ""
    started_monotonic: float = 0.0
    pdp_elapsed_ms: int = 0
    aes_elapsed_ms: int = 0
    pdp_net_bytes: int = 0
    aes_net_bytes: int = 0
    image_net_bytes: int = 0
    image_fetches: int = 0
    image_cache_hits: int = 0
    pdp_ok: int = 0
    pdp_skip: int = 0
    blocked_heavy: int = 0


_state = _CycleState()


def _enabled(config: dict[str, Any] | None) -> bool:
    if config is None:
        return True
    if "telemetry_enabled" in config:
        return bool(config.get("telemetry_enabled"))
    return bool(config.get("metrics_enabled", True))


def reset(config: dict[str, Any] | None = None) -> None:
    if not _enabled(config):
        return
    global _state
    _state = _CycleState(
        started_at_iso=israel_now_iso(),
        started_monotonic=time.monotonic(),
    )


def bump_blocked() -> None:
    _state.blocked_heavy += 1


def record_pdp_phase(elapsed_sec: float, net_bytes: int, *, ok: int, skip: int) -> None:
    _state.pdp_elapsed_ms = int(round(elapsed_sec * 1000))
    _state.pdp_net_bytes = max(0, int(net_bytes))
    _state.pdp_ok = max(0, int(ok))
    _state.pdp_skip = max(0, int(skip))


def record_aes_phase(elapsed_sec: float, net_bytes: int) -> None:
    _state.aes_elapsed_ms = int(round(elapsed_sec * 1000))
    _state.aes_net_bytes = max(0, int(net_bytes))


def record_image_fetch(bytes_downloaded: int, *, cache_hit: bool) -> None:
    if cache_hit:
        _state.image_cache_hits += 1
        return
    _state.image_fetches += 1
    _state.image_net_bytes += max(0, int(bytes_downloaded))


def _kb(n: int) -> float:
    return round(n / 1024.0, 1)


def to_summary(*, pdp_poll_minutes: int) -> dict[str, Any]:
    total_ms = int(round((time.monotonic() - _state.started_monotonic) * 1000)) if _state.started_monotonic else 0
    net_total = _state.pdp_net_bytes + _state.aes_net_bytes + _state.image_net_bytes
    poll_sec = max(1, int(pdp_poll_minutes)) * 60
    return {
        "timestamp_il": israel_now_iso(),
        "cycle_started_il": _state.started_at_iso,
        "total_sec": round(total_ms / 1000.0, 1),
        "pdp_sec": round(_state.pdp_elapsed_ms / 1000.0, 1),
        "aes_sec": round(_state.aes_elapsed_ms / 1000.0, 1),
        "net_kb_est": _kb(net_total),
        "pdp_net_kb_est": _kb(_state.pdp_net_bytes),
        "aes_net_kb_est": _kb(_state.aes_net_bytes),
        "image_net_kb": _kb(_state.image_net_bytes),
        "image_fetches": _state.image_fetches,
        "image_cache_hits": _state.image_cache_hits,
        "pdp_ok": _state.pdp_ok,
        "pdp_skip": _state.pdp_skip,
        "blocked_heavy": _state.blocked_heavy,
        "exceeds_poll_interval": (total_ms / 1000.0) > poll_sec,
        "pdp_poll_minutes": pdp_poll_minutes,
    }


def _host_matches(url: str) -> bool:
    lower = (url or "").lower()
    return any(host in lower for host in _AMAZON_HOST_SUFFIXES)


class NetMeter:
    """Sum Content-Length from Amazon responses on one Playwright page."""

    def __init__(self) -> None:
        self.total_bytes = 0
        self._attached = False

    def _on_response(self, response) -> None:
        try:
            url = response.url
            if not _host_matches(url):
                return
            headers = response.headers
            cl = headers.get("content-length") or headers.get("Content-Length")
            if cl:
                self.total_bytes += int(cl)
        except Exception:
            pass

    def attach_sync(self, page) -> None:
        if self._attached:
            return
        page.on("response", self._on_response)
        self._attached = True

    def attach_async(self, page) -> None:
        self.attach_sync(page)
