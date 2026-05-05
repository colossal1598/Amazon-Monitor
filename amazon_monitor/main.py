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
from filter_pipeline import keep_asins_not_in_db, run_search_filter_pipeline
from alert_dedupe import dedupe_alerts_by_asin
import fx_rate
from pdp_scraper import scrape_pdp_watch
from search_scraper import _valid_asin, scrape_search
from state_engine import StateEngine
from webhook_sender import send_alert, send_heartbeat, send_operational_error

LOGGER = logging.getLogger("monitor")


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


def scrape_delay_ranges(config: dict[str, Any]) -> tuple[tuple[float, float], tuple[float, float]]:
    s = config.get("search_scroll_delay_seconds") or [0.25, 0.65]
    p = config.get("search_pagination_delay_seconds") or [2.0, 4.5]
    return (float(s[0]), float(s[1])), (float(p[0]), float(p[1]))


def should_reconcile_missing_asins(config: dict, filtered_count: int) -> tuple[bool, str | None]:
    if not config.get("enable_missing_asin_oos", True):
        return False, "disabled_by_config"
    min_results = int(config.get("min_results_for_absence_reconcile", 1))
    if filtered_count < min_results:
        return False, f"filtered_count_below_min:{filtered_count}<{min_results}"
    return True, None


def load_config(path: str = "config.yaml") -> dict:
    with open(path, "r", encoding="utf-8") as file:
        return yaml.safe_load(file)


def setup_logging(log_dir: str) -> None:
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(Path(log_dir) / "monitor.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    root.addHandler(handler)
    root.addHandler(logging.StreamHandler())


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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

    scheduler = BackgroundScheduler()
    scraping_paused = {"value": False}
    health_file = Path("data/health.json")
    health_file.parent.mkdir(parents=True, exist_ok=True)
    health_state: dict[str, dict[str, str | None]] = {
        "search": {"last_started_at": None, "last_success_at": None, "last_error_at": None, "last_error_message": None},
        "heartbeat": {"last_started_at": None, "last_success_at": None, "last_error_at": None, "last_error_message": None},
    }

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

    def mark_job_started(job: str) -> None:
        health_state[job]["last_started_at"] = utc_iso()
        write_health()

    def mark_job_success(job: str) -> None:
        health_state[job]["last_success_at"] = utc_iso()
        health_state[job]["last_error_message"] = None
        write_health()

    def mark_job_error(job: str, exc: Exception | str) -> None:
        health_state[job]["last_error_at"] = utc_iso()
        health_state[job]["last_error_message"] = str(exc)
        write_health()

    def handle_captcha_or_network_pause() -> None:
        LOGGER.warning("Captcha or network recovery: pausing search job 120s (no modem rotation)")
        scraping_paused["value"] = True
        for job_id in ("search_loop",):
            if scheduler.get_job(job_id):
                scheduler.pause_job(job_id)
        time.sleep(120)
        for job_id in ("search_loop",):
            if scheduler.get_job(job_id):
                scheduler.resume_job(job_id)
        scraping_paused["value"] = False
        LOGGER.info("Search job resumed")

    def search_loop() -> None:
        if scraping_paused["value"]:
            return
        mark_job_started("search")
        scroll_r, page_r = scrape_delay_ranges(config)
        try:
            pdp_watch_set = set(_normalize_pdp_watch_asins(config.get("pdp_watch_asins")))

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
            )
            amazon_com_filtered, amazon_com_meta = run_search_filter_pipeline(amazon_com_items, config)
            LOGGER.info(
                "search_amazon_com raw=%s stage1=%s filtered=%s",
                len(amazon_com_items),
                amazon_com_meta.get("stage1_count"),
                len(amazon_com_filtered),
            )
            reconcile_missing, skipped_reason = should_reconcile_missing_asins(config, len(amazon_com_filtered))
            if skipped_reason:
                LOGGER.info(
                    "search_reconcile_skipped reason=%s filtered_count=%s",
                    skipped_reason,
                    len(amazon_com_filtered),
                )
            all_alerts: list[dict[str, Any]] = []
            alerts = state_engine.process_search_candidates(
                amazon_com_filtered,
                reconcile_missing=reconcile_missing,
                source="main_search",
                reconcile_exclude_asins=pdp_watch_set,
            )
            all_alerts.extend(alerts)

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
            )
            aes_filtered, aes_meta = run_search_filter_pipeline(aes_items, config)
            known = state_engine.list_known_asins()
            aes_only = keep_asins_not_in_db(aes_filtered, known)
            LOGGER.info(
                "search_aes_llc raw=%s stage1=%s filtered=%s not_in_db=%s",
                len(aes_items),
                aes_meta.get("stage1_count"),
                len(aes_filtered),
                len(aes_only),
            )
            alerts_new = state_engine.process_search_candidates(
                aes_only,
                reconcile_missing=False,
                source="main_search",
            )
            all_alerts.extend(alerts_new)

            watch_list = sorted(pdp_watch_set)
            if watch_list:
                allowed_raw = config.get("pdp_allowed_seller_substrings")
                allowed_subs = (
                    [str(x) for x in allowed_raw if str(x).strip()]
                    if isinstance(allowed_raw, list)
                    else ["amazon.com", "amazon export"]
                )
                pdp_rows = scrape_pdp_watch(
                    watch_list,
                    allowed_subs,
                    max_cycle_seconds=max_cycle_seconds,
                    scroll_delay_range=scroll_r,
                )
                pdp_alerts = state_engine.process_pdp_watch_candidates(
                    pdp_rows,
                    set(watch_list),
                )
                all_alerts.extend(pdp_alerts)

            for alert in dedupe_alerts_by_asin(all_alerts):
                send_alert(alert, config)

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
    LOGGER.info("Monitor started")
    try:
        while True:
            time.sleep(2)
    except KeyboardInterrupt:
        LOGGER.info("Shutting down monitor...")
        scheduler.shutdown(wait=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Pokemon Amazon monitor")
    parser.parse_args()
    main()
