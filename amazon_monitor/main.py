import argparse
import json
import logging
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import yaml
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv

from browser_factory import init_global_rate_limiter
from exceptions import CaptchaBlocked, NetworkAccessDenied
from filter_pipeline import filter_free_shipping_candidates, run_search_filter_pipeline
from alert_dedupe import dedupe_alerts_by_asin
import fx_rate
from pdp_scraper import scrape_pdp_watch
from search_scraper import _valid_asin, scrape_search
from search_union import (
    exclude_asins_from_candidates,
    merge_search_candidates_by_asin,
    should_reconcile_missing_asins,
)
from state_engine import StateEngine
from webhook_sender import send_alert, send_heartbeat, send_operational_error

LOGGER = logging.getLogger("monitor")


# ---------- Logging helpers (lifecycle vs debug) ----------
def _kv_tail(**fields: Any) -> str:
    """Normalized key=value tail (quote values only when needed)."""

    def fmt(v: Any) -> str:
        if v is None:
            return "null"
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return str(v)
        s = str(v)
        if s == "":
            return '""'
        needs_quote = any(ch.isspace() for ch in s) or any(ch in s for ch in ['"', "=", "\\"])
        return json.dumps(s, ensure_ascii=False) if needs_quote else s

    parts: list[str] = []
    for k in sorted(fields.keys()):
        if not k:
            continue
        parts.append(f"{k}={fmt(fields[k])}")
    return " ".join(parts)


def _english_head(event: str, fields: dict[str, Any]) -> str:
    # Lifecycle/action
    if event == "monitor_started":
        return "Monitor started."
    if event == "monitor_shutdown":
        return "Monitor shutting down."
    if event == "cycle_start":
        return "Cycle start."
    if event == "scrape_search_start":
        src = str(fields.get("source") or "")
        if src == "amazon_com":
            return "Scraping amazon.com SERP."
        if src == "aes_llc":
            return "Scraping AES LLC SERP."
        return "Scraping SERP."
    if event == "scrape_pdp_watch_start":
        return "Scraping PDP watch list."
    if event == "search_cycle_done":
        return "Cycle done."
    # Debug
    if event == "search_amazon_com_counts":
        return "Amazon.com SERP counts."
    if event == "search_aes_llc_counts":
        return "AES LLC SERP counts."
    if event == "search_union_counts":
        return "Merged search counts."
    if event == "search_reconcile_skipped":
        return "Reconcile missing skipped."
    if event == "pdp_watch_counts":
        return "PDP watch counts."
    return "Log."


def _log(channel: str, event: str, *, cycle_stamp: bool = False, **fields: Any) -> None:
    head = _english_head(event, fields)
    tail = _kv_tail(**fields)
    msg = f"{head} {tail}".strip() if tail else head
    if cycle_stamp:
        # Timestamp only on the first line of a cycle.
        msg = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    LOGGER.info(msg, extra={"channel": channel})


def log_lifecycle(event: str, **fields: Any) -> None:
    _log("lifecycle", event, **fields)


def log_debug(event: str, **fields: Any) -> None:
    _log("debug", event, **fields)


class _ChannelFilter(logging.Filter):
    def __init__(self, *, allowed_channels: set[str]):
        super().__init__()
        self.allowed_channels = allowed_channels

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.WARNING:
            return True
        ch = getattr(record, "channel", None)
        return isinstance(ch, str) and ch in self.allowed_channels


# Pick the two search page URLs this monitor should watch by reading them from your config and falling back to older config names.
def resolve_monitor_search_urls(config: dict[str, Any]) -> tuple[str, str, str, str]:
    """Returns (amazon_com_source_key, amazon_com_url, aes_llc_source_key, aes_llc_url) for scrape_search source= labels."""
    raw_map = config.get("search_urls")
    if not isinstance(raw_map, dict):
        raise ValueError("config.search_urls must be a dict with `amazon_com` and `aes_llc`")
    amazon_com_url = (
        raw_map.get("amazon_com") or raw_map.get("featured") or raw_map.get("main_search") or ""
    ).strip()
    aes_llc_url = (raw_map.get("aes_llc") or raw_map.get("newest_arrivals") or "").strip()
    if not amazon_com_url:
        legacy = config.get("search_url")
        if isinstance(legacy, str) and legacy.strip():
            amazon_com_url = legacy.strip()
    if not amazon_com_url:
        raise ValueError("Set search_urls.amazon_com (or legacy featured / main_search / search_url)")
    if not aes_llc_url:
        raise ValueError("Set search_urls.aes_llc (or legacy newest_arrivals) in config.yaml")
    return "amazon_com", amazon_com_url, "aes_llc", aes_llc_url


# Read the “wait a bit between actions” settings from your config so scraping looks more natural and less bursty.
def scrape_delay_ranges(config: dict[str, Any]) -> tuple[tuple[float, float], tuple[float, float]]:
    s = config.get("search_scroll_delay_seconds") or [0.25, 0.65]
    p = config.get("search_pagination_delay_seconds") or [2.0, 4.5]
    return (float(s[0]), float(s[1])), (float(p[0]), float(p[1]))


# Load the monitor’s settings from a YAML file so the rest of the app can use one shared config object.
def load_config(path: str = "config.yaml") -> dict:
    with open(path, "r", encoding="utf-8") as file:
        return yaml.safe_load(file)


# Set up file and console logging so you can review what the monitor did (and why) after it runs.
def setup_logging(log_dir: str) -> None:
    Path(log_dir).mkdir(parents=True, exist_ok=True)

    # No timestamp prefix here; we stamp only the first line of a cycle in-message.
    formatter = logging.Formatter("%(message)s")

    lifecycle_file = RotatingFileHandler(
        Path(log_dir) / "monitor.log",
        maxBytes=2_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    lifecycle_file.setFormatter(formatter)
    lifecycle_file.addFilter(_ChannelFilter(allowed_channels={"lifecycle"}))

    debug_file = RotatingFileHandler(
        Path(log_dir) / "monitor.debug.log",
        maxBytes=5_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    debug_file.setFormatter(formatter)
    debug_file.addFilter(_ChannelFilter(allowed_channels={"debug"}))

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console.addFilter(_ChannelFilter(allowed_channels={"lifecycle"}))

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.handlers.clear()
    root.addHandler(lifecycle_file)
    root.addHandler(debug_file)
    root.addHandler(console)


# Get the current time in a standard text format so logs, alerts, and health files all use the same clock style.
def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# Clean and dedupe the “watch these ASINs by product page” list so it only contains valid, unique ASINs.
def _normalize_pdp_watch_asins(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for x in raw:
        a = str(x).strip().upper()
        if not _valid_asin(a) or a in seen:
            continue
        seen.add(a)
        out.append(a)
    return out


# Run the full monitor loop by scraping search pages and watched product pages, updating the database, and sending WhatsApp alerts on changes.
def main() -> None:
    load_dotenv()
    config = load_config()
    setup_logging(config.get("log_dir", "logs"))
    Path(config.get("auth_dir", "auth")).mkdir(parents=True, exist_ok=True)
    Path(config.get("db_path", "data/monitor.db")).parent.mkdir(parents=True, exist_ok=True)

    state_engine = StateEngine(
        db_path=config["db_path"],
        price_drop_percent=config["price_drop_percent"],
    )
    init_global_rate_limiter(config["max_requests_per_minute"])

    amazon_com_src, amazon_com_url, aes_llc_src, aes_llc_url = resolve_monitor_search_urls(config)
    max_cycle_seconds = int(config.get("max_cycle_seconds", 170))
    max_search_pages = int(config.get("max_search_pages", 50))
    pagination_mode = str(config.get("pagination_mode", "auto")).lower()
    if pagination_mode not in ("auto", "fixed"):
        pagination_mode = "auto"
    fixed_pages = int(config.get("search_pages", 2))
    scroll_r, page_r = scrape_delay_ranges(config)
    serp_inner_retries = max(0, int(config.get("search_serp_inner_retries", 2)))

    scheduler = BackgroundScheduler()
    scraping_paused = {"value": False}
    health_file = Path("data/health.json")
    health_file.parent.mkdir(parents=True, exist_ok=True)
    health_state: dict[str, dict[str, str | None]] = {
        "search": {"last_started_at": None, "last_success_at": None, "last_error_at": None, "last_error_message": None},
        "heartbeat": {"last_started_at": None, "last_success_at": None, "last_error_at": None, "last_error_message": None},
    }

    # Write a small “status snapshot” file so you can quickly see when jobs last ran and whether they succeeded.
    def write_health() -> None:
        health_file.write_text(
            json.dumps(
                {
                    "updated_at": utc_iso(),
                    "jobs": health_state,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    # Mark a job as started in the health file so you can tell the scheduler is actually running.
    def mark_job_started(job: str) -> None:
        health_state[job]["last_started_at"] = utc_iso()
        write_health()

    # Mark a job as successful and clear its last error so the health file shows the latest good run.
    def mark_job_success(job: str) -> None:
        health_state[job]["last_success_at"] = utc_iso()
        health_state[job]["last_error_message"] = None
        write_health()

    # Record a job failure in the health file so you can see what went wrong without digging through full logs.
    def mark_job_error(job: str, exc: Exception | str) -> None:
        health_state[job]["last_error_at"] = utc_iso()
        health_state[job]["last_error_message"] = str(exc)
        write_health()

    # Pause scraping for a short time when Amazon blocks or the network breaks, then resume automatically.
    def handle_captcha_or_network_pause() -> None:
        pause_s = max(0, int(config.get("captcha_recovery_pause_seconds", 120)))
        LOGGER.warning(
            "Captcha or network recovery: pausing search job %ss (no modem rotation)",
            pause_s,
        )
        scraping_paused["value"] = True
        for job_id in ("search_loop",):
            if scheduler.get_job(job_id):
                scheduler.pause_job(job_id)
        time.sleep(float(pause_s))
        for job_id in ("search_loop",):
            if scheduler.get_job(job_id):
                scheduler.resume_job(job_id)
        scraping_paused["value"] = False
        LOGGER.info("Search job resumed")

    # Do one full “check cycle” by scraping both search sources, merging results, updating state, and sending any alerts.
    def search_loop() -> None:
        if scraping_paused["value"]:
            return
        mark_job_started("search")
        scroll_r, page_r = scrape_delay_ranges(config)
        try:
            pdp_watch_set = set(_normalize_pdp_watch_asins(config.get("pdp_watch_asins")))

            _log("lifecycle", "cycle_start", cycle_stamp=True)
            log_lifecycle(
                "scrape_search_start",
                source=amazon_com_src,
                mode="featured_full",
            )
            amazon_com_items, _ = scrape_search(
                amazon_com_url,
                source=amazon_com_src,
                scrape_mode="featured_full",
                pagination_mode=pagination_mode,
                fixed_pages=fixed_pages,
                max_search_pages=max_search_pages,
                collect_debug=False,
                max_cycle_seconds=max_cycle_seconds,
                scroll_delay_range=scroll_r,
                pagination_delay_range=page_r,
                serp_inner_retries=serp_inner_retries,
            )
            amazon_com_filtered, amazon_com_meta = run_search_filter_pipeline(amazon_com_items, config)
            amazon_com_filtered_before_free = len(amazon_com_filtered)
            amazon_com_filtered = filter_free_shipping_candidates(amazon_com_filtered)
            log_debug(
                "search_amazon_com_counts",
                raw=len(amazon_com_items),
                stage1=amazon_com_meta.get("stage1_count"),
                filtered_before_free_shipping=amazon_com_filtered_before_free,
                filtered_free_shipping=len(amazon_com_filtered),
            )
            all_alerts: list[dict[str, Any]] = []

            log_lifecycle(
                "scrape_search_start",
                source=aes_llc_src,
                mode="newest_front",
            )
            aes_items, _ = scrape_search(
                aes_llc_url,
                source=aes_llc_src,
                scrape_mode="newest_front",
                pagination_mode="fixed",
                fixed_pages=1,
                max_search_pages=1,
                collect_debug=False,
                max_cycle_seconds=max_cycle_seconds,
                scroll_delay_range=scroll_r,
                pagination_delay_range=page_r,
                serp_inner_retries=serp_inner_retries,
            )
            aes_filtered, aes_meta = run_search_filter_pipeline(aes_items, config)
            aes_filtered_before_free = len(aes_filtered)
            aes_filtered = filter_free_shipping_candidates(aes_filtered)
            log_debug(
                "search_aes_llc_counts",
                raw=len(aes_items),
                stage1=aes_meta.get("stage1_count"),
                filtered_before_free_shipping=aes_filtered_before_free,
                filtered_free_shipping=len(aes_filtered),
            )

            merged_candidates = merge_search_candidates_by_asin(amazon_com_filtered, aes_filtered)
            search_candidates = exclude_asins_from_candidates(merged_candidates, pdp_watch_set)
            pdp_excluded_from_search = len(merged_candidates) - len(search_candidates)
            reconcile_missing, skipped_reason = should_reconcile_missing_asins(config, len(search_candidates))
            log_debug(
                "search_union_counts",
                amazon_com_filtered=len(amazon_com_filtered),
                aes_filtered=len(aes_filtered),
                union_count=len(search_candidates),
                pdp_excluded_from_search=pdp_excluded_from_search,
                reconcile_missing=reconcile_missing,
            )
            if skipped_reason:
                log_debug("search_reconcile_skipped", reason=skipped_reason, union_count=len(search_candidates))
            alerts = state_engine.process_search_candidates(
                search_candidates,
                reconcile_missing=reconcile_missing,
                source="main_search",
                reconcile_exclude_asins=pdp_watch_set,
            )
            all_alerts.extend(alerts)

            watch_list = sorted(pdp_watch_set)
            if watch_list:
                allowed_raw = config.get("pdp_allowed_seller_substrings")
                allowed_subs = (
                    [str(x) for x in allowed_raw if str(x).strip()]
                    if isinstance(allowed_raw, list)
                    else ["amazon.com", "amazon export"]
                )
                log_lifecycle("scrape_pdp_watch_start", count=len(watch_list))
                pdp_rows = scrape_pdp_watch(
                    watch_list,
                    allowed_subs,
                    max_cycle_seconds=max_cycle_seconds,
                    scroll_delay_range=scroll_r,
                )
                skip_rows = sum(1 for r in pdp_rows if isinstance(r, dict) and r.get("_skip_update"))
                log_debug("pdp_watch_counts", watch=len(watch_list), rows=len(pdp_rows), skip_update=skip_rows)
                pdp_alerts = state_engine.process_pdp_watch_candidates(
                    pdp_rows,
                    set(watch_list),
                )
                all_alerts.extend(pdp_alerts)

            for alert in dedupe_alerts_by_asin(all_alerts):
                send_alert(alert, config)

            log_lifecycle(
                "search_cycle_done",
                alerts=len(all_alerts),
                search_candidates=len(search_candidates),
                pdp_watch=len(watch_list),
            )
            mark_job_success("search")
            fx_rate.bump_search_tick(config)
        except CaptchaBlocked:
            mark_job_error("search", "CaptchaBlocked")
            send_operational_error("search_error", "CaptchaBlocked: scraping paused then resumed", config)
            handle_captcha_or_network_pause()
        except NetworkAccessDenied as exc:
            mark_job_error("search", f"NetworkAccessDenied: {exc}")
            LOGGER.error("Network access denied: %s", exc)
            send_operational_error("search_error", str(exc), config)
            handle_captcha_or_network_pause()
        except Exception as exc:
            LOGGER.exception("search_loop failed: %s", exc)
            mark_job_error("search", exc)
            send_operational_error("search_error", str(exc), config)

    # Send a periodic “still running” message so you’ll notice if the monitor stops sending anything for a long time.
    def heartbeat_loop() -> None:
        mark_job_started("heartbeat")
        try:
            send_heartbeat(config)
            mark_job_success("heartbeat")
        except Exception as exc:
            LOGGER.warning("heartbeat failed: %s", exc)
            mark_job_error("heartbeat", exc)
            send_operational_error("heartbeat_error", str(exc), config)

    scheduler.add_job(
        search_loop,
        "interval",
        minutes=config["search_poll_minutes"],
        jitter=60,
        next_run_time=datetime.now(timezone.utc),
        id="search_loop",
        max_instances=1,
    )
    scheduler.add_job(heartbeat_loop, "interval", minutes=30, id="heartbeat_loop", max_instances=1)

    write_health()
    scheduler.start()
    log_lifecycle("monitor_started")
    try:
        while True:
            time.sleep(2)
    except KeyboardInterrupt:
        log_lifecycle("monitor_shutdown")
        scheduler.shutdown(wait=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Pokemon Amazon monitor")
    parser.parse_args()
    main()
