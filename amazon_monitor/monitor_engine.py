"""Continuous streaming monitor engine.

Replaces the old batch-cycle model (launch browser -> scrape all -> process all
-> alert -> teardown -> sleep) with one persistent browser and a ring scheduler:

    pick most-overdue watch ASIN -> scrape its PDP -> diff via StateEngine ->
    send WhatsApp IMMEDIATELY -> next ASIN

There is no end-of-cycle wait; an alert fires the second its scrape returns.
AES SERP discovery runs interleaved on the same browser every
``aes_check_minutes``. The browser is recycled every ``browser_recycle_minutes``
or after a captcha/network pause, instead of per cycle.

Telemetry compatibility: one full ring pass over the watch list is recorded as
a "cycle" in telemetry (bandwidth cards and cycle stats in the admin UI keep
working), but alert dispatch never waits for the pass to finish.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import client_alerts
import fx_rate
import usage_metrics
from alert_dedupe import dedupe_alerts_by_asin
from browser_factory import (
    close_async_browser,
    create_async_stealth_context,
    init_global_rate_limiter,
    set_bandwidth_config,
)
from exceptions import BrowserDisconnected, CaptchaBlocked, NetworkAccessDenied
from filter_pipeline import run_search_filter_pipeline
from pdp_helpers import valid_asin
from pdp_scraper import _PDP_RETRY_UNKNOWN_REASONS, _scrape_pdp_on_context, pdp_skip_log_label
from search_scraper import scrape_search_on_context_async
from settings_store import load_runtime_config
from state_engine import StateEngine
from telemetry_store import TelemetryStore
from webhook_sender import send_alert

LOGGER = logging.getLogger("monitor")

# Budget for a single PDP scrape (nav retries + settle). Generous; typical is 6-12s.
_PER_SCRAPE_BUDGET_SECONDS = 55.0
# Budget for one AES SERP scrape.
_AES_BUDGET_SECONDS = 90.0
# How often the engine re-reads settings + watch list from SQLite.
_CONFIG_RELOAD_SECONDS = 30.0
# Minimum sleep when no ASIN is due yet.
_IDLE_SLEEP_SECONDS = 0.5


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_watch(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for x in raw:
        asin = str(x).strip().upper()
        if valid_asin(asin) and asin not in seen:
            seen.add(asin)
            out.append(asin)
    return out


def _allowed_seller_substrings(config: dict[str, Any]) -> list[str]:
    raw = config.get("pdp_allowed_seller_substrings")
    if isinstance(raw, list):
        values = [str(x).strip() for x in raw if str(x).strip()]
        if values:
            return values
    return ["amazon.com", "amazon export"]


def _blacklist_asins(config: dict[str, Any]) -> set[str]:
    raw = config.get("blacklist")
    if not isinstance(raw, list):
        return set()
    return {str(v).strip().upper() for v in raw if valid_asin(str(v).strip().upper())}


def resolve_aes_llc_url(config: dict[str, Any]) -> str:
    raw_urls = config.get("search_urls")
    if isinstance(raw_urls, dict):
        url = str(raw_urls.get("aes_llc") or raw_urls.get("newest_arrivals") or "").strip()
        if url:
            return url
    raise ValueError("Set search_urls.aes_llc in DB settings (admin UI or settings table)")


def aes_fallback_url(primary_url: str) -> str | None:
    """Alternate URL form for the same seller, used on the last retry attempt.

    Production telemetry showed the keyword-filtered SERP (``s?k=...&me=<seller>``)
    gets soft-error throttled for minutes at a time while other endpoints from the
    same IP keep working. The storefront listing (``s?i=merchant-items&me=<seller>``)
    hits a different backend query path that may not share the throttle bucket, and
    renders the same standard SERP result cards the parser already handles.
    Returns None when no seller id can be extracted or the primary is already the
    merchant-items form.
    """
    match = re.search(r"[?&]me=([A-Z0-9]+)", primary_url)
    if not match or "i=merchant-items" in primary_url:
        return None
    return f"https://www.amazon.com/s?i=merchant-items&me={match.group(1)}"


def _coerce_range(raw: Any, default: tuple[float, float]) -> tuple[float, float]:
    if isinstance(raw, (list, tuple)) and len(raw) >= 2:
        try:
            low, high = float(raw[0]), float(raw[1])
        except (TypeError, ValueError):
            return default
        if 0 <= low <= high:
            return low, high
    return default


def _clamp_tabs(raw: Any) -> int:
    try:
        return max(1, min(4, int(raw)))
    except (TypeError, ValueError):
        return 2


def _compute_fast_retry(
    *,
    confidence: str,
    reason: str,
    prior_count: int,
    max_retries: int,
    retry_seconds: float,
) -> tuple[float | None, int]:
    """Decide the next-check interval override for an unknown price-miss result.

    Returns ``(interval_override, new_count)``. ``interval_override`` is ``retry_seconds``
    while the ASIN keeps returning an unknown no_pay_price/priceless_purchasable row and
    the consecutive fast-retry count is under ``max_retries``; otherwise ``None`` (normal
    interval). The count resets to 0 on any other result, per C2.
    """
    if (
        confidence == "unknown"
        and reason in _PDP_RETRY_UNKNOWN_REASONS
        and prior_count < max_retries
    ):
        return retry_seconds, prior_count + 1
    return None, 0


class RingScheduler:
    """Round-robin freshness scheduler: always hand out the most-overdue ASIN.

    Each ASIN carries a ``next_due`` monotonic timestamp; after a check it is
    rescheduled ``interval`` seconds out. New ASINs are staggered so a fresh
    start doesn't burst the whole list at once. Pure logic, fully testable.
    """

    def __init__(self, interval_seconds: float) -> None:
        self.interval = max(10.0, float(interval_seconds))
        self.next_due: dict[str, float] = {}
        self.checked_out: set[str] = set()
        # ASINs on a short fast-retry override. Under saturation (real cycle time
        # >= interval) every ASIN is overdue, so a plain most-overdue pop_due can
        # NEVER hand back a freshly fast-retried ASIN before a full ring pass: the
        # short override makes it the LEAST overdue, so it always loses the
        # most-overdue contest. Preempting for hot ASINs restores the 15s cadence.
        self.hot: set[str] = set()

    def update_watch_list(self, watch: list[str], now: float) -> None:
        current = set(watch)
        for asin in list(self.next_due):
            if asin not in current:
                del self.next_due[asin]
                self.checked_out.discard(asin)
                self.hot.discard(asin)
        new_asins = [a for a in watch if a not in self.next_due]
        for idx, asin in enumerate(new_asins):
            # Stagger new arrivals across one interval.
            self.next_due[asin] = now + (idx * self.interval / max(1, len(new_asins)))

    def pop_due(self, now: float) -> str | None:
        """Most-overdue due ASIN not currently being checked, or None.

        Hot (fast-retried) ASINs are preempted first: a short interval_override is
        worthless under a saturated ring unless it wins the pop, so hot ASINs get
        their own most-overdue contest before the normal scan.
        """
        best = self._most_overdue(now, self.hot)
        if best is None:
            best = self._most_overdue(now, None)
        if best is not None:
            self.checked_out.add(best)
        return best

    def _most_overdue(self, now: float, only: set[str] | None) -> str | None:
        best: str | None = None
        best_due = float("inf")
        for asin, due in self.next_due.items():
            if asin in self.checked_out or due > now:
                continue
            if only is not None and asin not in only:
                continue
            if due < best_due:
                best, best_due = asin, due
        return best

    def complete(self, asin: str, now: float, interval_override: float | None = None) -> None:
        self.checked_out.discard(asin)
        if interval_override is not None:
            self.hot.add(asin)
        else:
            self.hot.discard(asin)
        if asin in self.next_due:
            # interval_override lets the engine schedule a short fast re-check (e.g.
            # after a no_pay_price/priceless_purchasable miss) instead of waiting the
            # full freshness interval; None keeps the normal cadence.
            interval = self.interval if interval_override is None else max(0.0, float(interval_override))
            self.next_due[asin] = now + interval

    def seconds_to_next(self, now: float) -> float:
        pending = [d for a, d in self.next_due.items() if a not in self.checked_out]
        if not pending:
            return _IDLE_SLEEP_SECONDS
        return max(0.0, min(pending) - now)


class _SweepMeterView:
    """Per-sweep delta view over a session-cumulative BandwidthMeter."""

    def __init__(self, meter: Any, baseline: dict[str, int]) -> None:
        self._meter = meter
        self._baseline = baseline

    def totals(self) -> dict[str, int]:
        current = self._meter.totals()
        return {k: max(0, v - self._baseline.get(k, 0)) for k, v in current.items()}


class MonitorEngine:
    """Owns the persistent browser and the forever loop."""

    def __init__(
        self,
        db_path: str,
        state_engine: StateEngine,
        telemetry: TelemetryStore,
        *,
        health_path: str = "data/health.json",
    ) -> None:
        self.db_path = db_path
        self.state_engine = state_engine
        self.telemetry = telemetry
        self.health_path = Path(health_path)
        self.config: dict[str, Any] = {}
        self.watch: list[str] = []
        self.scheduler = RingScheduler(60.0)
        self.asin_status: dict[str, dict[str, Any]] = {}
        # C2 fast re-check bookkeeping: consecutive fast retries per ASIN, and the
        # interval override the worker's finally hands to scheduler.complete().
        self._fast_retry_counts: dict[str, int] = {}
        self._pending_overrides: dict[str, float | None] = {}
        self.jobs: dict[str, dict[str, str | None]] = {
            "stream": {"last_started_at": None, "last_success_at": None, "last_error_at": None, "last_error_message": None},
            "aes": {"last_started_at": None, "last_success_at": None, "last_error_at": None, "last_error_message": None},
            "heartbeat": {"last_started_at": None, "last_success_at": None, "last_error_at": None, "last_error_message": None},
        }
        self.engine_status = "starting"
        self.last_sweep_seconds: float | None = None
        self._last_health_write = 0.0
        self._config_loaded_at = 0.0
        # Sweep (telemetry cycle) bookkeeping.
        self._cycle_id = 0
        self._sweep_started = 0.0
        self._sweep_checks = 0
        self._sweep_ok = 0
        self._sweep_skip = 0
        self._sweep_in_stock = 0
        self._sweep_alerts = 0
        self._sweep_rows: list[dict[str, Any]] = []
        self._sweep_aes_summary: dict[str, Any] = {}
        self._sweep_aes_counts = (0, 0, 0)  # raw, candidates, alerts
        self._sweep_aes_outcome: dict[str, Any] = {}
        self._last_aes_at = 0.0
        self._aes_fail_streak = 0
        self._meter: Any = None
        self._meter_baseline: dict[str, int] = {}
        # Watchdog liveness marker (see watchdog_loop in main.py). Updated on any
        # forward progress: a finished sweep, a completed per-ASIN check, a
        # deliberate recovery pause, or a fresh session start. Plain monotonic
        # attribute assignment so it stays cheap to update from hot paths.
        self.last_progress = time.monotonic()

    # ------------------------------------------------------------- health --

    def write_health(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_health_write < 5.0:
            return
        self._last_health_write = now
        target_interval = self.scheduler.interval
        payload = {
            "updated_at": _utc_iso(),
            "jobs": self.jobs,
            "engine": {
                "status": self.engine_status,
                "watch_count": len(self.watch),
                "target_interval_seconds": target_interval,
                "sweep_seconds": self.last_sweep_seconds,
            },
            "asins": self.asin_status,
        }
        try:
            self.health_path.parent.mkdir(parents=True, exist_ok=True)
            self.health_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001 - health file is best-effort
            LOGGER.error("Failed to write health file %s: %s", self.health_path, exc)

    def mark_job(self, job: str, field: str, message: str | None = None) -> None:
        self.jobs[job][f"last_{field}_at"] = _utc_iso()
        if field == "success":
            self.jobs[job]["last_error_message"] = None
        elif field == "error":
            self.jobs[job]["last_error_message"] = message
        self.write_health()

    # ------------------------------------------------------------- config --

    def _reload_config(self, *, force: bool = False) -> None:
        if not force and time.monotonic() - self._config_loaded_at < _CONFIG_RELOAD_SECONDS:
            return
        self.config = load_runtime_config(self.db_path)
        self._config_loaded_at = time.monotonic()
        self.watch = _normalize_watch(self.config.get("pdp_watch_asins"))
        interval = self._target_interval()
        if abs(interval - self.scheduler.interval) > 0.01:
            self.scheduler.interval = interval
        self.scheduler.update_watch_list(self.watch, time.monotonic())
        self.state_engine.price_drop_percent = float(self.config.get("price_drop_percent", 10))
        init_global_rate_limiter(int(self.config.get("max_requests_per_minute", 10)))
        set_bandwidth_config(self.config)

    def _target_interval(self) -> float:
        try:
            return max(15.0, float(self.config.get("asin_check_interval_seconds", 60)))
        except (TypeError, ValueError):
            return 60.0

    def _fast_retry_seconds(self) -> float:
        try:
            return max(1.0, float(self.config.get("pdp_unknown_fast_retry_seconds", 15)))
        except (TypeError, ValueError):
            return 15.0

    def _fast_retry_max(self) -> int:
        try:
            return max(0, int(self.config.get("pdp_unknown_fast_retry_max", 3)))
        except (TypeError, ValueError):
            return 3

    def _aes_interval_seconds(self) -> float:
        try:
            base = max(60.0, float(self.config.get("aes_check_minutes", 5)) * 60.0)
        except (TypeError, ValueError):
            base = 300.0
        # Adaptive backoff on consecutive fully-failed AES cycles. Telemetry showed
        # the soft-error throttle is a rolling window that stays hot for minutes;
        # returning at the fixed cadence (with all its retries) kept refreshing it,
        # producing streaks of up to 21 consecutive lost cycles (~40 min blind).
        # Backing off up to 3x lets the window cool, then the cadence resets on the
        # first success.
        if self._aes_fail_streak > 0:
            multiplier = min(1.0 + 0.5 * self._aes_fail_streak, 3.0)
            return base * multiplier * random.uniform(0.9, 1.1)
        return base

    def _recycle_seconds(self) -> float:
        try:
            return max(300.0, float(self.config.get("browser_recycle_minutes", 60)) * 60.0)
        except (TypeError, ValueError):
            return 3600.0

    # -------------------------------------------------------------- sweep --

    def _begin_sweep(self) -> None:
        usage_metrics.reset(self.config)
        if self._meter is not None:
            self._meter_baseline = self._meter.totals()
            self._meter.set_phase("pdp")
            # The body-read budget is meant to be per-cycle; without this reset it
            # was exhausted during the first sweep of a session and every later
            # sweep undercounted to near-zero (observed: 316 KB then 0.9 KB).
            self._meter.reset_cycle_budget()
        self._cycle_id = self.telemetry.begin_cycle(self.config)
        self._sweep_started = time.monotonic()
        self._sweep_checks = 0
        self._sweep_ok = 0
        self._sweep_skip = 0
        self._sweep_in_stock = 0
        self._sweep_alerts = 0
        self._sweep_rows = []
        self._sweep_aes_summary = {}
        self._sweep_aes_counts = (0, 0, 0)
        self._sweep_aes_outcome = {}

    def _finish_sweep(self) -> None:
        self.last_progress = time.monotonic()
        sweep_sec = time.monotonic() - self._sweep_started
        self.last_sweep_seconds = round(sweep_sec, 1)
        usage_metrics.record_pdp_phase(sweep_sec, 0, ok=self._sweep_ok, skip=self._sweep_skip)
        if self._meter is not None:
            try:
                usage_metrics.flush_meter(_SweepMeterView(self._meter, self._meter_baseline))
            except Exception as exc:  # noqa: BLE001 - bandwidth flush is best-effort
                LOGGER.warning("Failed to flush bandwidth meter: %s", exc, extra={"channel": "debug"})
        # Stall detection treats the per-ASIN freshness target as "poll interval".
        target_minutes = max(1, round(self.scheduler.interval * max(1, len(self.watch)) / 60))
        scrape_errors, reason_counts = client_alerts.count_pdp_scrape_errors(self._sweep_rows)
        aes_raw, aes_candidates, aes_alerts = self._sweep_aes_counts
        summary = {
            **usage_metrics.to_summary(pdp_poll_minutes=target_minutes),
            "pdp_watch": len(self.watch),
            "watch_rows": len(self._sweep_rows),
            "in_stock": self._sweep_in_stock,
            "skip_update": self._sweep_skip,
            "captcha_skip": 0,
            "captcha_aborted": 0,
            "alerts_sent": self._sweep_alerts + aes_alerts,
            "aes_raw": aes_raw,
            "aes_candidates": aes_candidates,
            "aes_alerts": aes_alerts,
            "captcha_aborted_flag": False,
            "pdp_scrape_errors": scrape_errors,
            "pdp_scrape_error_reasons_json": reason_counts,
            "aes_scrape_outcome_json": self._sweep_aes_outcome,
            "pdp_state_summary_json": {},
            "aes_state_summary_json": self._sweep_aes_summary,
        }
        if self._cycle_id:
            self.telemetry.finish_cycle(self._cycle_id, summary, self.config)
            client_alerts.on_cycle_timing(summary, self.config, self.telemetry, cycle_id=self._cycle_id)
            # Evaluates this sweep's PDP/AES scrape failure rate against a
            # degradation threshold and alerts the operator if too many scrapes
            # failed. Without this, a sweep where a large fraction of PDP pages
            # failed to scrape (or AES navigation kept failing) looked identical
            # in the logs to "checked everything, nothing is in stock" — exactly
            # the silent false-negative pattern reported.
            client_alerts.check_scrape_degraded(
                self._sweep_rows,
                self._sweep_aes_outcome,
                len(self.watch),
                self.config,
                self.telemetry,
                cycle_id=self._cycle_id,
            )
        LOGGER.info(
            "Sweep done. checked=%s ok=%s skip=%s alerts=%s sweep_sec=%s watch=%s",
            self._sweep_checks,
            self._sweep_ok,
            self._sweep_skip,
            self._sweep_alerts + aes_alerts,
            round(sweep_sec, 1),
            len(self.watch),
            extra={"channel": "lifecycle"},
        )
        LOGGER.info(
            "Sweep net: pdp_kb=%s aes_kb=%s total_kb=%s",
            summary.get("pdp_net_kb_est"),
            summary.get("aes_net_kb_est"),
            summary.get("net_kb_est"),
            extra={"channel": "lifecycle"},
        )
        fx_rate.bump_monitor_tick(self.config)
        self.telemetry.maybe_prune(self.config)
        self.mark_job("stream", "success")

    # ---------------------------------------------------------- one check --

    async def _check_one_asin(self, context: Any, asin: str) -> None:
        """Scrape a single ASIN, apply state, dispatch alerts immediately."""
        config = self.config
        delay_range = _coerce_range(config.get("pdp_watch_scroll_delay_seconds"), (0.25, 0.65))
        jitter = _coerce_range(config.get("pdp_watch_tab_jitter_seconds"), (0.15, 0.55))
        rows, _elapsed = await _scrape_pdp_on_context(
            context,
            [asin],
            _allowed_seller_substrings(config),
            max_cycle_seconds=_PER_SCRAPE_BUDGET_SECONDS,
            scroll_delay_range=delay_range,
            max_concurrent=1,
            jitter_range=jitter,
            max_attempts=int(config.get("pdp_watch_max_attempts", 2)),
            pdp_settle_seconds=float(config.get("pdp_settle_seconds", 8.0)),
            pdp_settle_poll_interval_seconds=float(config.get("pdp_settle_poll_interval_seconds", 1.0)),
            pdp_unknown_retry_seconds=float(config.get("pdp_unknown_retry_seconds", 2.5)),
            pdp_continue_shopping_max_clicks=int(config.get("pdp_continue_shopping_max_clicks", 3)),
            pdp_dump_no_price_html=bool(config.get("pdp_dump_no_price_html", True)),
        )
        row = rows[0] if rows else None
        self._sweep_checks += 1
        status: dict[str, Any] = {"last_checked": _utc_iso()}
        if not isinstance(row, dict):
            self._sweep_skip += 1
            self.asin_status[asin] = {**status, "result": "no_row"}
            return
        self._sweep_rows.append(row)

        if row.get("_skip_update"):
            reason = str(row.get("skip_reason") or "unknown")
            if reason in ("captcha", "captcha_run_aborted"):
                self.asin_status[asin] = {**status, "result": "captcha"}
                raise CaptchaBlocked(f"captcha on PDP {asin}")
            self._sweep_skip += 1
            self.asin_status[asin] = {**status, "result": f"skip:{reason}"}
            LOGGER.info("%s skipped %s", asin, pdp_skip_log_label(row), extra={"channel": "lifecycle"})
            return

        self._sweep_ok += 1
        in_stock = bool(row.get("in_stock"))
        if in_stock:
            self._sweep_in_stock += 1
        self.asin_status[asin] = {**status, "result": "ok", "in_stock": in_stock, "price": row.get("price")}

        # C2: a purchasable-but-priceless (or price-still-hydrating) miss is the exact
        # restock-transition state price extraction keeps discarding. Re-check it in
        # seconds — not the full freshness interval — up to a bounded number of times
        # so a real restock alerts fast (paired with the C3 priceless streak).
        override, new_count = _compute_fast_retry(
            confidence=str(row.get("stock_confidence") or "").strip().lower(),
            reason=str(row.get("stock_reason") or "").strip(),
            prior_count=self._fast_retry_counts.get(asin, 0),
            max_retries=self._fast_retry_max(),
            retry_seconds=self._fast_retry_seconds(),
        )
        if new_count:
            self._fast_retry_counts[asin] = new_count
        else:
            self._fast_retry_counts.pop(asin, None)
        self._pending_overrides[asin] = override

        alerts, _summary = self.state_engine.process_pdp_watch_candidates(
            [row],
            {asin},
            config=config,
            telemetry=self.telemetry,
            cycle_id=self._cycle_id,
        )
        for alert in alerts:
            LOGGER.info(
                "ALERT %s %s price=%s (dispatched immediately)",
                alert.get("type"),
                asin,
                alert.get("price"),
                extra={"channel": "lifecycle"},
            )
            await asyncio.to_thread(send_alert, alert, config)
            self._sweep_alerts += 1

    # ----------------------------------------------------------------- AES --

    async def _run_aes_discovery(self, context: Any) -> None:
        config = self.config
        self.mark_job("aes", "started")
        try:
            aes_llc_url = resolve_aes_llc_url(config)
        except ValueError as exc:
            self.mark_job("aes", "error", str(exc))
            return
        # The AES LLC search page has been observed in production logs failing
        # Amazon's own "Sorry! Something went wrong" soft-error interstitial on
        # many consecutive scheduled attempts in a row. Each attempt already
        # retries internally (serp_inner_retries) but previously a single
        # exhausted attempt burned the *entire* aes_check_minutes interval before
        # trying again. One extra outer attempt (with its own short backoff) lets
        # a transient burst clear within the same cycle instead of silently
        # skipping discovery for minutes at a time.
        outer_attempts = max(1, int(config.get("aes_outer_retries", 1)) + 1)
        inner_retries = max(0, int(config.get("aes_serp_inner_retries", 2)))
        started = time.monotonic()
        aes_items: list[dict[str, Any]] = []
        scrape_data: dict[str, Any] = {}
        last_exc: Exception | None = None
        fallback_url = aes_fallback_url(aes_llc_url)
        try:
            for outer_attempt in range(1, outer_attempts + 1):
                # Each attempt gets the REMAINING share of the overall AES budget
                # (not a fresh _AES_BUDGET_SECONDS each time). Previously a 2-attempt
                # retry could burn up to 2x the budget in the worst case, starving
                # PDP workers that share the same global rate limiter for minutes.
                elapsed = time.monotonic() - started
                remaining = _AES_BUDGET_SECONDS - elapsed
                if outer_attempt > 1 and remaining < 10.0:
                    LOGGER.info(
                        "AES/SERP attempt %s/%s skipped, budget exhausted (remaining=%.1fs)",
                        outer_attempt,
                        outer_attempts,
                        remaining,
                        extra={"channel": "debug"},
                    )
                    break
                attempt_budget = max(10.0, remaining)
                # On the final attempt switch to the merchant-items storefront URL
                # (different backend path, likely a separate throttle bucket) —
                # telemetry showed retries against the identical keyword-SERP URL
                # almost never recover once the soft-error window has started.
                attempt_url = aes_llc_url
                if fallback_url and self._aes_fail_streak > 0:
                    # C6: recent cycles were soft-error throttled on the keyword SERP —
                    # lead with the storefront URL (separate throttle bucket) and swap
                    # the primary keyword URL to the later attempt once it may have cooled.
                    attempt_url = fallback_url if outer_attempt == 1 else aes_llc_url
                    LOGGER.info(
                        "AES/SERP attempt %s/%s leading with fallback URL after fail_streak=%s: %s",
                        outer_attempt,
                        outer_attempts,
                        self._aes_fail_streak,
                        attempt_url,
                        extra={"channel": "debug"},
                    )
                elif outer_attempt == outer_attempts and outer_attempt > 1 and fallback_url:
                    attempt_url = fallback_url
                    LOGGER.info(
                        "AES/SERP attempt %s/%s using fallback storefront URL: %s",
                        outer_attempt,
                        outer_attempts,
                        fallback_url,
                        extra={"channel": "debug"},
                    )
                try:
                    aes_items, scrape_data = await scrape_search_on_context_async(
                        context,
                        attempt_url,
                        source="aes_llc",
                        scrape_mode="newest_front",
                        pagination_mode="fixed",
                        fixed_pages=1,
                        max_search_pages=1,
                        collect_debug=False,
                        max_cycle_seconds=attempt_budget,
                        serp_scroll_profile="minimal",
                        serp_inner_retries=inner_retries,
                        selector_timeout_ms=int(config.get("aes_selector_timeout_ms", 12_000)),
                    )
                    last_exc = None
                    break
                except (CaptchaBlocked, NetworkAccessDenied, BrowserDisconnected):
                    raise
                except Exception as exc:  # noqa: BLE001 - AES is best-effort discovery
                    last_exc = exc
                    if outer_attempt >= outer_attempts:
                        break
                    LOGGER.info(
                        "AES/SERP attempt %s/%s failed, retrying: %s",
                        outer_attempt,
                        outer_attempts,
                        exc,
                        extra={"channel": "debug"},
                    )
                    await asyncio.sleep(random.uniform(1.5, 3.0))
        finally:
            usage_metrics.record_aes_phase(time.monotonic() - started, 0)
        if last_exc is not None:
            self._aes_fail_streak += 1
            LOGGER.warning(
                "AES/SERP scrape failed (stream continues, fail_streak=%s): %s",
                self._aes_fail_streak,
                last_exc,
                extra={"channel": "debug"},
            )
            self.mark_job("aes", "error", str(last_exc))
            self._sweep_aes_outcome = {"navigation_ok": False, "cards_found": 0, "error": str(last_exc)}
            return
        self._aes_fail_streak = 0

        outcome = scrape_data.get("scrape_outcome") if isinstance(scrape_data, dict) else {}
        self._sweep_aes_outcome = outcome if isinstance(outcome, dict) else {}
        pipeline_rows, _meta = run_search_filter_pipeline(aes_items, config, require_shipping_signal=False)
        blocked = _blacklist_asins(config)
        candidates = [r for r in pipeline_rows if (r.get("asin") or "").strip().upper() not in blocked]
        aes_alerts, aes_summary = self.state_engine.process_aes_serp_mirror(
            candidates,
            source="aes_llc",
            reconcile_absence=len(candidates) > 0,
            config=config,
            telemetry=self.telemetry,
            cycle_id=self._cycle_id,
        )
        deduped = dedupe_alerts_by_asin(aes_alerts)
        for alert in deduped:
            await asyncio.to_thread(send_alert, alert, config)
        self._sweep_aes_counts = (len(aes_items), len(candidates), len(deduped))
        self._sweep_aes_summary = aes_summary
        self.mark_job("aes", "success")

    # ------------------------------------------------------------ sessions --

    async def _worker(self, context: Any, stop: asyncio.Event, failure: dict[str, BaseException]) -> None:
        while not stop.is_set():
            now = time.monotonic()
            asin = self.scheduler.pop_due(now)
            if asin is None:
                await asyncio.sleep(min(self.scheduler.seconds_to_next(now), 2.0))
                continue
            try:
                await self._check_one_asin(context, asin)
            except (CaptchaBlocked, NetworkAccessDenied, BrowserDisconnected) as exc:
                # BrowserDisconnected means the Chromium process/driver pipe for
                # this whole session is dead: every other worker will fail the
                # same way on its next check. Confirmed in production logs as a
                # ~10 minute outage where each of 16 watch ASINs kept failing
                # identically every ~60s until the *time-based* browser recycle
                # eventually fired — during which nothing was checked and no
                # alert was sent. Treating it as fatal here (like captcha/network
                # blocks) makes the session stop and recycle within seconds.
                failure.setdefault("error", exc)
                stop.set()
            except Exception as exc:  # noqa: BLE001 - one bad check must not kill the loop
                LOGGER.warning("check failed asin=%s: %s", asin, exc, extra={"channel": "debug"})
            finally:
                self.last_progress = time.monotonic()
                self.scheduler.complete(
                    asin, time.monotonic(), interval_override=self._pending_overrides.pop(asin, None)
                )
                self.write_health()

    async def _run_session(self) -> None:
        """One browser lifetime: stream checks until recycle time or captcha."""
        from playwright.async_api import async_playwright

        self._reload_config(force=True)
        if not self.watch:
            LOGGER.warning("No pdp_watch_asins configured; engine idle.")
            self.engine_status = "idle_no_watch"
            # Deliberately idle, not stalled: keep the watchdog from firing while
            # waiting for a watch list to be configured.
            self.last_progress = time.monotonic()
            self.write_health(force=True)
            await asyncio.sleep(30)
            return

        headless = bool(self.config.get("playwright_headless", True))
        tabs = _clamp_tabs(self.config.get("stream_concurrent_tabs", 2))
        session_started = time.monotonic()
        recycle_after = self._recycle_seconds()

        async with async_playwright() as pw:
            browser, context = await create_async_stealth_context(pw, headless=headless, config=self.config)
            self._meter = usage_metrics.BandwidthMeter(self.config)
            self._meter.attach_context_async(context)
            self.engine_status = "running"
            self.last_progress = time.monotonic()
            self.mark_job("stream", "started")
            self._begin_sweep()
            stop = asyncio.Event()
            failure: dict[str, BaseException] = {}
            workers = [
                asyncio.create_task(self._worker(context, stop, failure)) for _ in range(tabs)
            ]
            try:
                while not stop.is_set():
                    await asyncio.sleep(1.0)
                    self._reload_config()
                    now = time.monotonic()
                    # Sweep bookkeeping: one pass over the watch list = one telemetry cycle.
                    if self._sweep_checks >= max(1, len(self.watch)):
                        self._finish_sweep()
                        self._begin_sweep()
                    # AES discovery interleave.
                    if now - self._last_aes_at >= self._aes_interval_seconds():
                        self._last_aes_at = now
                        self._meter.set_phase("aes")
                        try:
                            await self._run_aes_discovery(context)
                        except (CaptchaBlocked, NetworkAccessDenied, BrowserDisconnected) as exc:
                            failure.setdefault("error", exc)
                            stop.set()
                        finally:
                            self._meter.set_phase("pdp")
                        # Amazon's SERP soft-error throttle is session-scoped:
                        # production logs on 2026-07-13 show a 6-cycle fail streak
                        # (~25 min blind) that cleared on the very next attempt
                        # after the scheduled 30-min browser recycle. Once the
                        # streak shows the throttle is stuck, proactively recycle
                        # the session instead of waiting out the timer. Guarded on
                        # session age so a persistent block can't thrash-relaunch.
                        recycle_streak = max(2, int(self.config.get("aes_recycle_fail_streak", 2)))
                        if (
                            self._aes_fail_streak >= recycle_streak
                            and now - session_started >= 300
                        ):
                            LOGGER.warning(
                                "AES fail streak %s >= %s; recycling browser session to clear SERP soft-block.",
                                self._aes_fail_streak,
                                recycle_streak,
                            )
                            stop.set()
                    # Planned recycle: finish current checks, relaunch fresh browser.
                    if now - session_started >= recycle_after:
                        LOGGER.info("Recycling browser after %.0f min.", (now - session_started) / 60, extra={"channel": "lifecycle"})
                        stop.set()
                    self.write_health()
            finally:
                stop.set()
                await asyncio.gather(*workers, return_exceptions=True)
                if self._sweep_checks:
                    # Record the partial sweep so telemetry never loses a session tail.
                    try:
                        self._finish_sweep()
                    except Exception:  # noqa: BLE001
                        LOGGER.debug("partial sweep finish failed", exc_info=True)
                self._meter = None
                await close_async_browser(browser, context)

        error = failure.get("error")
        if error is not None:
            raise error

    async def _pause_for_recovery(
        self, reason: str, *, pause_seconds: float | None = None, status: str = "captcha_pause"
    ) -> None:
        pause_s = max(0, int(pause_seconds if pause_seconds is not None else self.config.get("captcha_recovery_pause_seconds", 120)))
        LOGGER.warning("%s: pausing stream %ss, browser will be recycled.", reason, pause_s)
        self.engine_status = status
        # A deliberate pause is not a hang: tell the watchdog we're alive so a
        # long captcha/network backoff can't false-trigger a stall exit.
        self.last_progress = time.monotonic()
        self.mark_job("stream", "error", reason)
        self.write_health(force=True)
        await asyncio.sleep(float(pause_s))
        self.last_progress = time.monotonic()

    async def run_forever(self) -> None:
        LOGGER.info("Streaming engine starting.", extra={"channel": "lifecycle"})
        while True:
            try:
                await self._run_session()
            except CaptchaBlocked as exc:
                client_alerts.maybe_alert("captcha", self.config, self.telemetry, cycle_id=self._cycle_id, detail=str(exc))
                await self._pause_for_recovery(f"Captcha: {exc}")
            except NetworkAccessDenied as exc:
                client_alerts.maybe_alert("network_blocked", self.config, self.telemetry, cycle_id=self._cycle_id, detail=str(exc))
                await self._pause_for_recovery(f"Network blocked: {exc}", status="network_pause")
            except BrowserDisconnected as exc:
                # Not a block (captcha/rate-limit) — the browser process or driver
                # pipe itself died. Waiting captcha_recovery_pause_seconds (120s
                # default) would needlessly extend an outage that a fresh browser
                # launch fixes immediately; use a short fixed pause instead so
                # detection resumes in seconds, and alert the operator since this
                # previously failed completely silently for up to
                # browser_recycle_minutes (production logs showed ~10 minutes of
                # zero successful checks with no client-facing signal at all).
                client_alerts.maybe_alert(
                    "browser_disconnected", self.config, self.telemetry, cycle_id=self._cycle_id, detail=str(exc)
                )
                await self._pause_for_recovery(
                    f"Browser disconnected: {exc}", pause_seconds=5, status="browser_restart"
                )
            except Exception as exc:  # noqa: BLE001 - engine must survive anything
                LOGGER.exception("Engine session crashed: %s", exc)
                client_alerts.maybe_alert("cycle_failed", self.config, self.telemetry, cycle_id=self._cycle_id, detail=str(exc))
                self.mark_job("stream", "error", str(exc))
                await asyncio.sleep(15)
