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
from exceptions import CaptchaBlocked, NetworkAccessDenied
from filter_pipeline import run_search_filter_pipeline
from pdp_helpers import valid_asin
from pdp_scraper import _scrape_pdp_on_context, pdp_skip_log_label
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

    def update_watch_list(self, watch: list[str], now: float) -> None:
        current = set(watch)
        for asin in list(self.next_due):
            if asin not in current:
                del self.next_due[asin]
                self.checked_out.discard(asin)
        new_asins = [a for a in watch if a not in self.next_due]
        for idx, asin in enumerate(new_asins):
            # Stagger new arrivals across one interval.
            self.next_due[asin] = now + (idx * self.interval / max(1, len(new_asins)))

    def pop_due(self, now: float) -> str | None:
        """Most-overdue due ASIN not currently being checked, or None."""
        best: str | None = None
        best_due = float("inf")
        for asin, due in self.next_due.items():
            if asin in self.checked_out or due > now:
                continue
            if due < best_due:
                best, best_due = asin, due
        if best is not None:
            self.checked_out.add(best)
        return best

    def complete(self, asin: str, now: float) -> None:
        self.checked_out.discard(asin)
        if asin in self.next_due:
            self.next_due[asin] = now + self.interval

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
        self._meter: Any = None
        self._meter_baseline: dict[str, int] = {}

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

    def _aes_interval_seconds(self) -> float:
        try:
            return max(60.0, float(self.config.get("aes_check_minutes", 5)) * 60.0)
        except (TypeError, ValueError):
            return 300.0

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
        sweep_sec = time.monotonic() - self._sweep_started
        self.last_sweep_seconds = round(sweep_sec, 1)
        usage_metrics.record_pdp_phase(sweep_sec, 0, ok=self._sweep_ok, skip=self._sweep_skip)
        if self._meter is not None:
            try:
                usage_metrics.flush_meter(_SweepMeterView(self._meter, self._meter_baseline))
            except Exception:  # noqa: BLE001
                pass
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
        LOGGER.info(
            "%s Sweep done. checked=%s ok=%s skip=%s alerts=%s sweep_sec=%s watch=%s",
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            self._sweep_checks,
            self._sweep_ok,
            self._sweep_skip,
            self._sweep_alerts + aes_alerts,
            round(sweep_sec, 1),
            len(self.watch),
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

        alerts, _summary = self.state_engine.process_pdp_watch_candidates(
            [row],
            {asin},
            config=config,
            telemetry=self.telemetry,
            cycle_id=self._cycle_id,
        )
        for alert in alerts:
            LOGGER.info(
                "%s ALERT %s %s price=%s (dispatched immediately)",
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
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
        started = time.monotonic()
        try:
            aes_items, scrape_data = await scrape_search_on_context_async(
                context,
                aes_llc_url,
                source="aes_llc",
                scrape_mode="newest_front",
                pagination_mode="fixed",
                fixed_pages=1,
                max_search_pages=1,
                collect_debug=False,
                max_cycle_seconds=_AES_BUDGET_SECONDS,
                serp_scroll_profile="minimal",
                serp_inner_retries=1,
                selector_timeout_ms=int(config.get("aes_selector_timeout_ms", 12_000)),
            )
        except (CaptchaBlocked, NetworkAccessDenied):
            raise
        except Exception as exc:  # noqa: BLE001 - AES is best-effort discovery
            LOGGER.warning("AES/SERP scrape failed (stream continues): %s", exc, extra={"channel": "debug"})
            self.mark_job("aes", "error", str(exc))
            self._sweep_aes_outcome = {"navigation_ok": False, "cards_found": 0, "error": str(exc)}
            return
        finally:
            usage_metrics.record_aes_phase(time.monotonic() - started, 0)

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
            except (CaptchaBlocked, NetworkAccessDenied) as exc:
                failure.setdefault("error", exc)
                stop.set()
            except Exception as exc:  # noqa: BLE001 - one bad check must not kill the loop
                LOGGER.warning("check failed asin=%s: %s", asin, exc, extra={"channel": "debug"})
            finally:
                self.scheduler.complete(asin, time.monotonic())
                self.write_health()

    async def _run_session(self) -> None:
        """One browser lifetime: stream checks until recycle time or captcha."""
        from playwright.async_api import async_playwright

        self._reload_config(force=True)
        if not self.watch:
            LOGGER.warning("No pdp_watch_asins configured; engine idle.")
            self.engine_status = "idle_no_watch"
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
                        except (CaptchaBlocked, NetworkAccessDenied) as exc:
                            failure.setdefault("error", exc)
                            stop.set()
                        finally:
                            self._meter.set_phase("pdp")
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

    async def _pause_for_recovery(self, reason: str) -> None:
        pause_s = max(0, int(self.config.get("captcha_recovery_pause_seconds", 120)))
        LOGGER.warning("%s: pausing stream %ss, browser will be recycled.", reason, pause_s)
        self.engine_status = "captcha_pause"
        self.mark_job("stream", "error", reason)
        self.write_health(force=True)
        await asyncio.sleep(float(pause_s))

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
                await self._pause_for_recovery(f"Network blocked: {exc}")
            except Exception as exc:  # noqa: BLE001 - engine must survive anything
                LOGGER.exception("Engine session crashed: %s", exc)
                client_alerts.maybe_alert("cycle_failed", self.config, self.telemetry, cycle_id=self._cycle_id, detail=str(exc))
                self.mark_job("stream", "error", str(exc))
                await asyncio.sleep(15)
