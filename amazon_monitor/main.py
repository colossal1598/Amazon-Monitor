import argparse
import json
import logging
import re
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import requests
import yaml
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv

from browser_factory import init_global_rate_limiter
from exceptions import CaptchaBlocked, NetworkAccessDenied
from filter_pipeline import keep_asins_not_in_db, run_minimal_scrape_pipeline, run_search_filter_pipeline
from search_scraper import PaginationMode, ScrapeMode, scrape_search
from state_engine import StateEngine
from webhook_sender import send_alert, send_heartbeat, send_operational_error

LOGGER = logging.getLogger("monitor")


def resolve_featured_and_newest_urls(config: dict[str, Any]) -> tuple[str, str, str, str]:
    """Returns (featured_source_key, featured_url, newest_source_key, newest_url)."""
    raw_map = config.get("search_urls")
    if not isinstance(raw_map, dict):
        raise ValueError("config.search_urls must be a dict with `featured` and `newest_arrivals`")
    featured_url = (raw_map.get("featured") or raw_map.get("main_search") or "").strip()
    newest_url = (raw_map.get("newest_arrivals") or "").strip()
    if not featured_url:
        legacy = config.get("search_url")
        if isinstance(legacy, str) and legacy.strip():
            featured_url = legacy.strip()
    if not featured_url:
        raise ValueError("Set search_urls.featured (or legacy search_url)")
    if not newest_url:
        raise ValueError("Set search_urls.newest_arrivals in config.yaml")
    return "featured", featured_url, "newest_arrivals", newest_url


def scrape_delay_ranges(config: dict[str, Any]) -> tuple[tuple[float, float], tuple[float, float]]:
    s = config.get("search_scroll_delay_seconds") or [0.25, 0.65]
    p = config.get("search_pagination_delay_seconds") or [2.0, 4.5]
    return (float(s[0]), float(s[1])), (float(p[0]), float(p[1]))


def run_test_scrape(config: dict, pages_override: int | None = None) -> None:
    max_cycle_seconds = int(config.get("max_cycle_seconds", 170))
    max_search_pages = int(config.get("max_search_pages", 50))
    pagination_mode = str(config.get("pagination_mode", "auto")).lower()
    if pagination_mode not in ("auto", "fixed"):
        pagination_mode = "auto"
    fixed_pages = int(pages_override if pages_override is not None else config.get("search_pages", 2))
    output_dir = Path("data/test_scrape")
    output_dir.mkdir(parents=True, exist_ok=True)

    _, f_url, n_src, n_url = resolve_featured_and_newest_urls(config)
    scroll_r, page_r = scrape_delay_ranges(config)

    raw_featured, dbg_f = scrape_search(
        f_url,
        source="featured",
        scrape_mode="featured_full",
        pagination_mode=pagination_mode,
        fixed_pages=fixed_pages,
        max_search_pages=max_search_pages,
        collect_debug=True,
        max_cycle_seconds=max_cycle_seconds,
        html_dump_dir=output_dir,
        scroll_delay_range=scroll_r,
        pagination_delay_range=page_r,
    )
    raw_newest, dbg_n = scrape_search(
        n_url,
        source=n_src,
        scrape_mode="newest_front",
        pagination_mode="fixed",
        fixed_pages=1,
        max_search_pages=1,
        collect_debug=True,
        max_cycle_seconds=max_cycle_seconds,
        html_dump_dir=output_dir,
        scroll_delay_range=scroll_r,
        pagination_delay_range=page_r,
    )

    (output_dir / "raw_featured.json").write_text(
        json.dumps(raw_featured, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output_dir / "raw_newest.json").write_text(
        json.dumps(raw_newest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    ff, meta_f = run_search_filter_pipeline(raw_featured, config)
    nf, meta_n = run_search_filter_pipeline(raw_newest, config)
    known_simulated = {r["asin"] for r in ff}
    nf_new_only = keep_asins_not_in_db(nf, known_simulated)

    (output_dir / "filtered_featured.json").write_text(json.dumps(ff, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "filtered_newest.json").write_text(json.dumps(nf, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "filtered_newest_not_in_featured.json").write_text(
        json.dumps(nf_new_only, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output_dir / "pipeline_meta.json").write_text(
        json.dumps({"featured": meta_f, "newest": meta_n}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output_dir / "selector_debug.json").write_text(
        json.dumps({"featured": dbg_f, "newest": dbg_n}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    LOGGER.info(
        "test_scrape_complete featured_raw=%s newest_raw=%s featured_filtered=%s newest_filtered=%s newest_new_only=%s",
        len(raw_featured),
        len(raw_newest),
        len(ff),
        len(nf),
        len(nf_new_only),
    )


def run_single_test_scrape_url(config: dict, search_url: str, *, pages: int = 1) -> None:
    """Scrape one search URL and write raw + filtered + debug JSON under data/test_scrape/."""
    max_cycle_seconds = int(config.get("max_cycle_seconds", 170))
    max_search_pages = int(config.get("max_search_pages", 50))
    output_dir = Path("data/test_scrape")
    output_dir.mkdir(parents=True, exist_ok=True)
    scroll_r, page_r = scrape_delay_ranges(config)
    pages = max(1, pages)
    if pages == 1:
        scrape_mode: ScrapeMode = "newest_front"
        pagination_mode: PaginationMode = "fixed"
        fixed_pages = 1
        cap = 1
    else:
        scrape_mode = "featured_full"
        pagination_mode = "fixed"
        fixed_pages = pages
        cap = min(max_search_pages, pages)

    raw, dbg = scrape_search(
        search_url.strip(),
        source="single_url",
        scrape_mode=scrape_mode,
        pagination_mode=pagination_mode,
        fixed_pages=fixed_pages,
        max_search_pages=cap,
        collect_debug=True,
        max_cycle_seconds=max_cycle_seconds,
        html_dump_dir=output_dir,
        scroll_delay_range=scroll_r,
        pagination_delay_range=page_r,
    )
    filtered, meta = run_search_filter_pipeline(raw, config)

    (output_dir / "raw_single_url.json").write_text(
        json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output_dir / "filtered_single_url.json").write_text(
        json.dumps(filtered, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output_dir / "pipeline_meta_single_url.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output_dir / "selector_debug_single_url.json").write_text(
        json.dumps(dbg, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    LOGGER.info(
        "single_url_scrape_complete url=%s raw=%s filtered=%s out_dir=%s",
        search_url[:120],
        len(raw),
        len(filtered),
        output_dir.resolve(),
    )


def run_scrape_jobs_from_config(config: dict[str, Any]) -> None:
    """Run each entry in config.scrape_jobs: scrape N pages, then minimal filter (price + optional Israel free delivery)."""
    jobs = config.get("scrape_jobs")
    if not isinstance(jobs, list) or not jobs:
        LOGGER.error("config.scrape_jobs must be a non-empty list of {name, url, pages, require_free_delivery}")
        return
    output_dir = Path("data/test_scrape")
    output_dir.mkdir(parents=True, exist_ok=True)
    scroll_r, page_r = scrape_delay_ranges(config)
    max_cycle_seconds = int(config.get("max_cycle_seconds", 170))
    max_search_pages = int(config.get("max_search_pages", 50))

    for job in jobs:
        if not isinstance(job, dict):
            continue
        name = str(job.get("name") or "job").strip() or "job"
        url = (job.get("url") or "").strip()
        pages = max(1, int(job.get("pages") or 1))
        require_ship = bool(
            job.get("require_free_delivery", job.get("require_free_delivery_to_israel", False))
        )
        if not url:
            LOGGER.warning("scrape_jobs: skip %r — missing url", name)
            continue
        safe = re.sub(r"[^\w\-]+", "_", name).strip("_")[:80] or "job"
        source = f"job_{safe}"

        if pages == 1:
            scrape_mode: ScrapeMode = "newest_front"
            pagination_mode: PaginationMode = "fixed"
            fixed_pages = 1
            cap = 1
        else:
            scrape_mode = "featured_full"
            pagination_mode = "fixed"
            fixed_pages = pages
            cap = min(max_search_pages, pages)

        LOGGER.info("scrape_job start name=%s pages=%s require_free_delivery=%s", name, pages, require_ship)
        raw, dbg = scrape_search(
            url,
            source=source,
            scrape_mode=scrape_mode,
            pagination_mode=pagination_mode,
            fixed_pages=fixed_pages,
            max_search_pages=cap,
            collect_debug=True,
            max_cycle_seconds=max_cycle_seconds,
            html_dump_dir=output_dir,
            scroll_delay_range=scroll_r,
            pagination_delay_range=page_r,
        )
        filtered, meta = run_minimal_scrape_pipeline(raw, require_free_delivery=require_ship)

        (output_dir / f"raw_{safe}.json").write_text(
            json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (output_dir / f"filtered_{safe}.json").write_text(
            json.dumps(filtered, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (output_dir / f"pipeline_meta_{safe}.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (output_dir / f"selector_debug_{safe}.json").write_text(
            json.dumps(dbg, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        LOGGER.info(
            "scrape_job done name=%s raw=%s filtered=%s files=raw_%s.json …",
            name,
            len(raw),
            len(filtered),
            safe,
        )


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


def send_telegram_message(config: dict, text: str) -> None:
    token = config.get("telegram_bot_token")
    chat_id = config.get("telegram_chat_id")
    if not token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=5)
    except Exception as exc:
        LOGGER.warning("Telegram send failed: %s", exc)


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def main(
    test_scrape: bool = False,
    pages_override: int | None = None,
    bootstrap: bool = False,
) -> None:
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
    if test_scrape:
        run_test_scrape(config, pages_override=pages_override)
        return

    f_src, f_url, n_src, n_url = resolve_featured_and_newest_urls(config)
    max_cycle_seconds = int(config.get("max_cycle_seconds", 170))
    max_search_pages = int(config.get("max_search_pages", 50))
    pagination_mode = str(config.get("pagination_mode", "auto")).lower()
    if pagination_mode not in ("auto", "fixed"):
        pagination_mode = "auto"
    fixed_pages = int(config.get("search_pages", 2))
    scroll_r, page_r = scrape_delay_ranges(config)

    if bootstrap:
        f_items, _ = scrape_search(
            f_url,
            source=f_src,
            scrape_mode="featured_full",
            pagination_mode=pagination_mode,
            fixed_pages=fixed_pages,
            max_search_pages=max_search_pages,
            collect_debug=False,
            max_cycle_seconds=max_cycle_seconds,
            scroll_delay_range=scroll_r,
            pagination_delay_range=page_r,
        )
        f_filtered, f_meta = run_search_filter_pipeline(f_items, config)
        state_engine.seed_candidates_without_alerts(f_filtered, source="main_search")
        known = state_engine.list_known_asins()
        n_items, _ = scrape_search(
            n_url,
            source=n_src,
            scrape_mode="newest_front",
            pagination_mode="fixed",
            fixed_pages=1,
            max_search_pages=1,
            collect_debug=False,
            max_cycle_seconds=max_cycle_seconds,
            scroll_delay_range=scroll_r,
            pagination_delay_range=page_r,
        )
        n_filtered, n_meta = run_search_filter_pipeline(n_items, config)
        n_only = keep_asins_not_in_db(n_filtered, known)
        state_engine.seed_candidates_without_alerts(n_only, source="main_search")
        LOGGER.info(
            "bootstrap_complete featured_raw=%s newest_raw=%s featured_state_rows=%s newest_state_rows=%s newest_new_only=%s",
            len(f_items),
            len(n_items),
            len(f_filtered),
            len(n_filtered),
            len(n_only),
        )
        return

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
            f_items, _ = scrape_search(
                f_url,
                source=f_src,
                scrape_mode="featured_full",
                pagination_mode=pagination_mode,
                fixed_pages=fixed_pages,
                max_search_pages=max_search_pages,
                collect_debug=False,
                max_cycle_seconds=max_cycle_seconds,
                scroll_delay_range=scroll_r,
                pagination_delay_range=page_r,
            )
            f_filtered, f_meta = run_search_filter_pipeline(f_items, config)
            LOGGER.info(
                "search_featured raw=%s stage1=%s filtered=%s",
                len(f_items),
                f_meta.get("stage1_count"),
                len(f_filtered),
            )
            reconcile_missing, skipped_reason = should_reconcile_missing_asins(config, len(f_filtered))
            if skipped_reason:
                LOGGER.info(
                    "search_reconcile_skipped reason=%s filtered_count=%s",
                    skipped_reason,
                    len(f_filtered),
                )
            alerts = state_engine.process_search_candidates(
                f_filtered,
                reconcile_missing=reconcile_missing,
                source="main_search",
            )
            for alert in alerts:
                send_alert(alert, config)

            n_items, _ = scrape_search(
                n_url,
                source=n_src,
                scrape_mode="newest_front",
                pagination_mode="fixed",
                fixed_pages=1,
                max_search_pages=1,
                collect_debug=False,
                max_cycle_seconds=max_cycle_seconds,
                scroll_delay_range=scroll_r,
                pagination_delay_range=page_r,
            )
            n_filtered, n_meta = run_search_filter_pipeline(n_items, config)
            known = state_engine.list_known_asins()
            n_only = keep_asins_not_in_db(n_filtered, known)
            LOGGER.info(
                "search_newest raw=%s stage1=%s filtered=%s not_in_db=%s",
                len(n_items),
                n_meta.get("stage1_count"),
                len(n_filtered),
                len(n_only),
            )
            alerts_new = state_engine.process_search_candidates(
                n_only,
                reconcile_missing=False,
                source="main_search",
            )
            for alert in alerts_new:
                send_alert(alert, config)

            mark_job_success("search")
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
            send_telegram_message(config, "Pokemon Amazon monitor heartbeat OK.")
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
    parser.add_argument("--test-scrape", action="store_true", help="Run scrape+filter once and dump JSON outputs.")
    parser.add_argument("--bootstrap", action="store_true", help="Seed DB once without sending WhatsApp alerts.")
    parser.add_argument("--pages", type=int, default=None, help="Override fixed page count for featured in --test-scrape.")
    args = parser.parse_args()
    main(test_scrape=args.test_scrape, pages_override=args.pages, bootstrap=args.bootstrap)
