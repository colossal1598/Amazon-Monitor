"""Light per-cycle time and estimated network usage (Israel timestamps, no per-ASIN logs)."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

LOGGER = logging.getLogger(__name__)

_MAX_BODY_SIZE_BYTES = 5 * 1024 * 1024
_AMAZON_HOST_SUFFIXES = (".amazon.com", ".media-amazon.com", ".ssl-images-amazon.com")


def _israel_tz():
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo("Asia/Jerusalem")
    except Exception:
        return timezone(timedelta(hours=3), name="Asia/Jerusalem")


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
    net_bytes_total: int = 0
    net_bytes_pdp: int = 0
    net_bytes_aes: int = 0
    image_net_bytes: int = 0
    image_fetches: int = 0
    image_cache_hits: int = 0
    pdp_ok: int = 0
    pdp_skip: int = 0
    blocked_heavy: int = 0
    blocked_url: int = 0


_state = _CycleState()
_meter_flushed = False


def _enabled(config: dict[str, Any] | None) -> bool:
    if config is None:
        return True
    if "telemetry_enabled" in config:
        return bool(config.get("telemetry_enabled"))
    return bool(config.get("metrics_enabled", True))


def reset(config: dict[str, Any] | None = None) -> None:
    if not _enabled(config):
        return
    global _state, _meter_flushed
    _state = _CycleState(
        started_at_iso=israel_now_iso(),
        started_monotonic=time.monotonic(),
    )
    _meter_flushed = False


def bump_blocked() -> None:
    _state.blocked_heavy += 1


def bump_blocked_url() -> None:
    _state.blocked_url += 1


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


def _gb(n: int) -> float:
    return round(n / 1_000_000_000.0, 3)


def _browser_bytes() -> tuple[int, int]:
    if _meter_flushed:
        return _state.net_bytes_pdp, _state.net_bytes_aes
    return _state.pdp_net_bytes, _state.aes_net_bytes


def to_summary(*, pdp_poll_minutes: int) -> dict[str, Any]:
    total_ms = int(round((time.monotonic() - _state.started_monotonic) * 1000)) if _state.started_monotonic else 0
    pdp_bytes, aes_bytes = _browser_bytes()
    image_bytes = _state.image_net_bytes
    browser_total = pdp_bytes + aes_bytes
    grand_total = browser_total + image_bytes
    poll_sec = max(1, int(pdp_poll_minutes)) * 60
    return {
        "timestamp_il": israel_now_iso(),
        "cycle_started_il": _state.started_at_iso,
        "total_sec": round(total_ms / 1000.0, 1),
        "pdp_sec": round(_state.pdp_elapsed_ms / 1000.0, 1),
        "aes_sec": round(_state.aes_elapsed_ms / 1000.0, 1),
        "net_bytes_total": browser_total,
        "net_bytes_pdp": pdp_bytes,
        "net_bytes_aes": aes_bytes,
        "net_bytes_image": image_bytes,
        "gb_est_total": _gb(grand_total),
        "net_kb_est": _kb(grand_total),
        "pdp_net_kb_est": _kb(pdp_bytes),
        "aes_net_kb_est": _kb(aes_bytes),
        "image_net_kb": _kb(image_bytes),
        "image_fetches": _state.image_fetches,
        "image_cache_hits": _state.image_cache_hits,
        "pdp_ok": _state.pdp_ok,
        "pdp_skip": _state.pdp_skip,
        "blocked_heavy": _state.blocked_heavy,
        "blocked_url": _state.blocked_url,
        "exceeds_poll_interval": (total_ms / 1000.0) > poll_sec,
        "pdp_poll_minutes": pdp_poll_minutes,
    }


def _is_amazon_url(url: str) -> bool:
    lower = (url or "").lower()
    if any(suffix in lower for suffix in _AMAZON_HOST_SUFFIXES):
        return True
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return any(host.endswith(suffix.lstrip(".")) or host == suffix.lstrip(".") for suffix in _AMAZON_HOST_SUFFIXES)


def _host_matches(url: str) -> bool:
    return _is_amazon_url(url)


def content_length_from_headers(headers: dict[str, str] | Any) -> int | None:
    if not headers:
        return None
    cl = None
    if isinstance(headers, dict):
        cl = headers.get("content-length") or headers.get("Content-Length")
    else:
        try:
            cl = headers.get("content-length") or headers.get("Content-Length")
        except Exception:
            return None
    if cl is None or cl == "":
        return None
    try:
        n = int(cl)
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


def flush_meter(meter: BandwidthMeter) -> None:
    global _meter_flushed
    totals = meter.totals()
    _state.net_bytes_pdp = totals["net_bytes_pdp"]
    _state.net_bytes_aes = totals["net_bytes_aes"]
    _state.net_bytes_total = totals["net_bytes_total"]
    _meter_flushed = True


class BandwidthMeter:
    """Context-level Playwright response byte counter with PDP/AES phase buckets."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self._config = config or {}
        self._phase = "pdp"
        self._buckets: dict[str, int] = {
            "pdp_amazon": 0,
            "pdp_other": 0,
            "aes_amazon": 0,
            "aes_other": 0,
        }
        self._body_reads = 0
        self._attached = False

    def set_phase(self, phase: str) -> None:
        if phase in ("pdp", "aes"):
            self._phase = phase

    def get_phase(self) -> str:
        return self._phase

    def _max_body_reads(self) -> int:
        return int(self._config.get("bandwidth_meter_max_body_reads_per_cycle", 200))

    def reset_cycle_budget(self) -> None:
        """Reset the per-cycle body-read budget (call at each sweep start)."""
        self._body_reads = 0

    def _bucket_key(self, url: str) -> str:
        amazon = _is_amazon_url(url)
        if self._phase == "aes":
            return "aes_amazon" if amazon else "aes_other"
        return "pdp_amazon" if amazon else "pdp_other"

    def _add_bytes(self, url: str, nbytes: int) -> None:
        if nbytes <= 0:
            return
        key = self._bucket_key(url)
        self._buckets[key] += nbytes

    def _on_response_sync(self, response) -> None:
        try:
            nbytes = content_length_from_headers(response.headers)
            if nbytes is not None:
                self._add_bytes(response.url, nbytes)
        except Exception:
            pass

    async def _on_response_async(self, response) -> None:
        try:
            url = response.url
            nbytes = content_length_from_headers(response.headers)
            if nbytes is not None:
                self._add_bytes(url, nbytes)
                return
            if self._body_reads >= self._max_body_reads():
                return
            body = await response.body()
            self._body_reads += 1
            size = len(body)
            if size > _MAX_BODY_SIZE_BYTES:
                return
            self._add_bytes(url, size)
        except Exception:
            pass

    def _schedule_async(self, response) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._on_response_async(response))

    def attach_context_sync(self, context) -> None:
        if self._attached:
            return
        context.on("response", self._on_response_sync)
        self._attached = True

    def attach_context_async(self, context) -> None:
        if self._attached:
            return
        context.on("response", self._schedule_async)
        self._attached = True

    def totals(self) -> dict[str, int]:
        pdp = self._buckets["pdp_amazon"] + self._buckets["pdp_other"]
        aes = self._buckets["aes_amazon"] + self._buckets["aes_other"]
        return {
            "net_bytes_pdp": pdp,
            "net_bytes_aes": aes,
            "net_bytes_total": pdp + aes,
            **self._buckets,
        }


class NetMeter:
    """Sum Content-Length from Amazon responses on one Playwright page (legacy)."""

    def __init__(self) -> None:
        self.total_bytes = 0
        self._attached = False

    def _on_response(self, response) -> None:
        try:
            url = response.url
            if not _host_matches(url):
                return
            nbytes = content_length_from_headers(response.headers)
            if nbytes is not None:
                self.total_bytes += nbytes
        except Exception:
            pass

    def attach_sync(self, page) -> None:
        if self._attached:
            return
        page.on("response", self._on_response)
        self._attached = True

    def attach_async(self, page) -> None:
        self.attach_sync(page)
