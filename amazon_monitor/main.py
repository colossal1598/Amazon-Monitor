import logging
import json
import time
import argparse
from datetime import datetime, timezone
from typing import Any
from logging.handlers import RotatingFileHandler
from pathlib import Path

import requests
import yaml
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv

from browser_factory import init_global_rate_limiter
from exceptions import CaptchaBlocked, ModemIPUnchanged, NetworkAccessDenied
from filter_pipeline import (
    build_confirmed_candidates,
    classify_seller,
    filter_by_blacklist_only,
    filter_search_results,
    filter_stage1_candidates,
    state_engine_row_from_queue_record,
)
from modem_rotator import reconnect_modem
from search_scraper import scrape_search, verify_sellers_batch
from seller_queue import load_pending_queue, prune_stale_entries, save_pending_queue
from state_engine import StateEngine
from webhook_sender import send_alert, send_heartbeat, send_modem_trigger, send_operational_error

LOGGER = logging.getLogger("monitor")


def marketplace_candidates_from_scrape(
    results: list[dict[str, Any]],
    config: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Stage-1 filter + PDP verification + optional retry queue -> state-engine rows."""
    meta: dict[str, Any] = {}
    stage1 = filter_stage1_candidates(results)
    bl_file = str(config.get("blacklist_file", "blacklist.txt"))
    stage1 = filter_by_blacklist_only(stage1, bl_file)
    req_kw = config.get("required_keywords") or []
    req_any = config.get("required_any_keywords")
    if req_kw or req_any:
        stage1 = filter_search_results(stage1, req_kw, bl_file, req_any)
    meta["stage1_count"] = len(stage1)

    qpath = str(config.get("pending_seller_queue_path", "data/pending_seller_queue.json"))
    ttl_days = int(config.get("pending_seller_queue_ttl_days", 14))
    retry_queue = load_pending_queue(qpath)
    prune_stale_entries(retry_queue, ttl_days)
    asap_mode = bool(config.get("asap_new_listings_mode", True))

    stage1_by_asin: dict[str, dict[str, Any]] = {}
    for row in stage1:
        asin = (row.get("asin") or "").upper()
        if asin:
            stage1_by_asin[asin] = row

    stage1_unknown_asins: list[str] = []
    for row in stage1:
        asin = (row.get("asin") or "").upper()
        if not asin:
            continue
        st, _ = classify_seller(row.get("seller_text") or row.get("seller") or "")
        if st == "unknown":
            stage1_unknown_asins.append(asin)

    if asap_mode:
        budget_asins = stage1_unknown_asins
    else:
        max_pdp = int(config.get("max_pdp_verifications_per_run", 12))
        budget_asins = stage1_unknown_asins[:max_pdp]
    meta["pdp_scheduled"] = len(budget_asins)
    max_batch_seconds = float(config.get("max_pdp_batch_seconds", 120))
    captcha_stopped = False
    if budget_asins:
        pdp_map, captcha_stopped = verify_sellers_batch(
            budget_asins, len(budget_asins), max_seconds=max_batch_seconds
        )
    else:
        pdp_map = {}
    meta["pdp_captcha_stopped"] = captcha_stopped
    if captcha_stopped:
        LOGGER.warning(
            "marketplace_pdp_partial verified=%s scheduled=%s (captcha mid-batch)",
            len(pdp_map),
            len(budget_asins),
        )
    meta["pdp_verification_keys"] = list(pdp_map.keys())
    meta["pdp_text_by_asin"] = dict(pdp_map)

    filtered = build_confirmed_candidates(list(stage1_by_asin.values()), pdp_map)

    # Retry queue is now only for transient leftovers (timeouts/budget cuts), not seller backlog by design.
    now_iso = utc_iso()
    for asin in list(retry_queue.keys()):
        if asin not in stage1_by_asin:
            retry_queue.pop(asin, None)

    for asin, row in stage1_by_asin.items():
        if asin in pdp_map:
            retry_queue.pop(asin, None)
            continue
        status_card, _ = classify_seller(row.get("seller_text") or row.get("seller") or "")
        if status_card != "unknown":
            retry_queue.pop(asin, None)
            continue
        prev = retry_queue.get(asin, {})
        attempts = int(prev.get("attempts", 0))
        retry_queue[asin] = {
            "asin": asin,
            "title": row.get("title"),
            "price": row.get("price"),
            "price_text": row.get("price_text"),
            "image_url": row.get("image_url"),
            "shipping_text": row.get("shipping_text"),
            "seller_text": row.get("seller_text"),
            "first_seen": prev.get("first_seen") or now_iso,
            "last_seen": now_iso,
            "attempts": attempts,
        }
    # Bump attempts for records we intended to verify but were not visited due batch timeout.
    for asin in budget_asins:
        if asin not in pdp_map and asin in retry_queue:
            retry_queue[asin]["attempts"] = int(retry_queue[asin].get("attempts", 0)) + 1

    save_pending_queue(qpath, retry_queue)
    meta["pending_queue_size"] = len(retry_queue)
    meta["filtered_count"] = len(filtered)
    return filtered, meta


def resolve_search_urls(config: dict) -> list[tuple[str, str]]:
    """Resolve configured search URLs as (source_name, url)."""
    urls: list[tuple[str, str]] = []
    raw_single = config.get("search_url")
    if isinstance(raw_single, str) and raw_single.strip():
        urls.append(("main_search", raw_single.strip()))
    raw_map = config.get("search_urls")
    if isinstance(raw_map, dict):
        for source, url in raw_map.items():
            if isinstance(url, str) and url.strip():
                urls.append((str(source), url.strip()))
    dedup_by_url: dict[str, str] = {}
    for source, url in urls:
        dedup_by_url[url] = source
    resolved = [(source, url) for url, source in dedup_by_url.items()]
    if not resolved:
        raise ValueError("Set `search_urls.main_search` (or `search_url`) in config.yaml")
    return resolved


def run_test_scrape(config: dict, pages_override: int | None = None) -> None:
    pages = pages_override if pages_override is not None else int(config.get("search_pages", 1))
    max_cycle_seconds = int(config.get("max_cycle_seconds", 170))
    max_pdp_fallbacks = int(config.get("max_pdp_fallbacks_per_run", 8))
    output_dir = Path("data/test_scrape")
    output_dir.mkdir(parents=True, exist_ok=True)

    raw_items: list[dict] = []
    selector_debug: list[dict] = []
    pdp_debug: dict[str, dict] = {}
    for source, url in resolve_search_urls(config):
        items, debug = scrape_search(
            url,
            pages=pages,
            source=source,
            collect_debug=True,
            max_cycle_seconds=max_cycle_seconds,
            max_pdp_fallbacks=max_pdp_fallbacks,
            html_dump_dir=output_dir,
        )
        raw_items.extend(items)
        selector_debug.extend(debug.get("selector_debug", []))
        pdp_debug.update(debug.get("pdp_debug", {}))

    (output_dir / "raw_items.json").write_text(json.dumps(raw_items, ensure_ascii=False, indent=2), encoding="utf-8")

    stage1_items = filter_stage1_candidates(raw_items)
    test_config = dict(config)
    test_config["pending_seller_queue_path"] = str(output_dir / "pending_seller_queue_test.json")
    try:
        filtered_items, pipeline_meta = marketplace_candidates_from_scrape(raw_items, test_config)
    except Exception as exc:
        LOGGER.exception("test_scrape pipeline failed after raw scrape: %s", exc)
        filtered_items = []
        pipeline_meta = {
            "error": str(exc),
            "error_type": type(exc).__name__,
            "stage1_count": 0,
            "filtered_count": 0,
            "pdp_scheduled": 0,
            "pending_queue_size": 0,
            "pdp_captcha_stopped": False,
        }
    (output_dir / "stage1_candidates.json").write_text(
        json.dumps(stage1_items, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output_dir / "filtered_items.json").write_text(
        json.dumps(filtered_items, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    pending_test = output_dir / "pending_seller_queue_test.json"
    if pending_test.exists():
        (output_dir / "pending_seller_queue_snapshot.json").write_text(
            pending_test.read_text(encoding="utf-8"), encoding="utf-8"
        )
    meta_for_dump = {k: v for k, v in pipeline_meta.items() if k != "pdp_text_by_asin"}
    (output_dir / "pdp_verification_results.json").write_text(
        json.dumps(
            {
                "scheduled_asins": pipeline_meta.get("pdp_verification_keys", []),
                "pdp_text_by_asin": pipeline_meta.get("pdp_text_by_asin", {}),
                "meta": meta_for_dump,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (output_dir / "confirmed_for_state.json").write_text(
        json.dumps(filtered_items, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output_dir / "selector_debug.json").write_text(
        json.dumps({"selector_debug": selector_debug, "pdp_debug": pdp_debug}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    sample_cards = "\n<hr/>\n".join(
        entry.get("card_html_snippet", "") for entry in selector_debug[:5] if entry.get("card_html_snippet")
    )
    if sample_cards:
        (output_dir / "sample_cards.html").write_text(sample_cards, encoding="utf-8")
    sample_pdp = "\n<hr/>\n".join(
        data.get("merchant_html_snippet", "")
        for data in list(pdp_debug.values())[:5]
        if isinstance(data, dict) and data.get("merchant_html_snippet")
    )
    if sample_pdp:
        (output_dir / "sample_pdp.html").write_text(sample_pdp, encoding="utf-8")
    LOGGER.info(
        "test_scrape_complete raw=%s stage1=%s filtered=%s output_dir=%s",
        len(raw_items),
        len(stage1_items),
        len(filtered_items),
        output_dir,
    )


def should_reconcile_missing_asins(config: dict, filtered_count: int) -> tuple[bool, str | None]:
    """Guard missing-ASIN reconciliation against empty runs."""
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
    if bootstrap:
        results: list[dict[str, Any]] = []
        max_cycle_seconds = int(config.get("max_cycle_seconds", 170))
        max_pdp_fallbacks = int(config.get("max_pdp_fallbacks_per_run", 0))
        for source, url in resolve_search_urls(config):
            source_items, _debug = scrape_search(
                url,
                pages=config["search_pages"],
                source=source,
                collect_debug=False,
                max_cycle_seconds=max_cycle_seconds,
                max_pdp_fallbacks=max_pdp_fallbacks,
            )
            results.extend(source_items)
        filtered, pipeline_meta = marketplace_candidates_from_scrape(results, config)
        state_engine.seed_candidates_without_alerts(filtered, source="main_search")
        LOGGER.info(
            "bootstrap_complete raw=%s stage1=%s filtered=%s pending_queue=%s",
            len(results),
            pipeline_meta.get("stage1_count"),
            len(filtered),
            pipeline_meta.get("pending_queue_size"),
        )
        return
    scheduler = BackgroundScheduler()
    scraping_paused = {"value": False}
    last_ip = {"value": ""}
    health_file = Path("data/health.json")
    health_file.parent.mkdir(parents=True, exist_ok=True)
    health_state: dict[str, dict[str, str | None]] = {
        "search": {"last_started_at": None, "last_success_at": None, "last_error_at": None, "last_error_message": None},
        "heartbeat": {"last_started_at": None, "last_success_at": None, "last_error_at": None, "last_error_message": None},
        "modem": {"last_started_at": None, "last_success_at": None, "last_error_at": None, "last_error_message": None},
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

    def handle_captcha() -> None:
        LOGGER.warning("Captcha blocked, pausing scraping jobs")
        scraping_paused["value"] = True
        for job_id in ("search_loop",):
            scheduler.pause_job(job_id)
        try:
            if config.get("wa_send_modem_trigger", False):
                send_modem_trigger(config)
            reconnect_modem(config)
        except Exception as exc:
            LOGGER.error("Captcha recovery modem step failed: %s", exc)
            send_operational_error("modem_error", str(exc), config)
        time.sleep(120)
        for job_id in ("search_loop",):
            if scheduler.get_job(job_id):
                scheduler.resume_job(job_id)
        scraping_paused["value"] = False
        LOGGER.info("Scraping jobs resumed")

    def search_loop() -> None:
        if scraping_paused["value"]:
            return
        mark_job_started("search")
        try:
            results: list[dict] = []
            max_cycle_seconds = int(config.get("max_cycle_seconds", 170))
            max_pdp_fallbacks = int(config.get("max_pdp_fallbacks_per_run", 8))
            for source, url in resolve_search_urls(config):
                source_items, _debug = scrape_search(
                    url,
                    pages=config["search_pages"],
                    source=source,
                    collect_debug=False,
                    max_cycle_seconds=max_cycle_seconds,
                    max_pdp_fallbacks=max_pdp_fallbacks,
                )
                results.extend(source_items)
            filtered, pipeline_meta = marketplace_candidates_from_scrape(results, config)
            LOGGER.info(
                "search_pipeline raw=%s stage1=%s filtered=%s pdp_scheduled=%s pending_queue=%s",
                len(results),
                pipeline_meta.get("stage1_count"),
                len(filtered),
                pipeline_meta.get("pdp_scheduled"),
                pipeline_meta.get("pending_queue_size"),
            )
            if pipeline_meta.get("pdp_captcha_stopped"):
                LOGGER.warning(
                    "search_pipeline pdp_captcha_stopped partial_pdp_count=%s",
                    len(pipeline_meta.get("pdp_text_by_asin") or {}),
                )
            reconcile_missing, skipped_reason = should_reconcile_missing_asins(config, len(filtered))
            if skipped_reason:
                LOGGER.info(
                    "search_reconcile_skipped reason=%s raw_count=%s filtered_count=%s",
                    skipped_reason,
                    len(results),
                    len(filtered),
                )
            alerts = state_engine.process_search_candidates(
                filtered,
                reconcile_missing=reconcile_missing,
                source="main_search",
            )
            for alert in alerts:
                send_alert(alert, config)
            mark_job_success("search")
        except CaptchaBlocked:
            mark_job_error("search", "CaptchaBlocked")
            handle_captcha()
        except NetworkAccessDenied as exc:
            mark_job_error("search", f"NetworkAccessDenied: {exc}")
            LOGGER.error("Network access denied — triggering modem rotation")
            handle_captcha()  # Same recovery flow: modem rotation + pause/resume
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

    def modem_refresh_loop() -> None:
        mark_job_started("modem")
        try:
            ip_now = requests.get("https://checkip.amazonaws.com", timeout=5).text.strip()
            if not last_ip["value"]:
                last_ip["value"] = ip_now
                mark_job_success("modem")
                return
            if ip_now == last_ip["value"]:
                new_ip = reconnect_modem(config)
                last_ip["value"] = new_ip
            else:
                last_ip["value"] = ip_now
            mark_job_success("modem")
        except ModemIPUnchanged as exc:
            LOGGER.error("Scheduled modem refresh failed: %s", exc)
            mark_job_error("modem", exc)
            send_operational_error("modem_error", str(exc), config)
        except Exception as exc:
            LOGGER.warning("modem_refresh_loop failed: %s", exc)
            mark_job_error("modem", exc)
            send_operational_error("modem_error", str(exc), config)

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
    scheduler.add_job(
        modem_refresh_loop,
        "interval",
        hours=config["modem_auto_refresh_hours"],
        id="modem_refresh_loop",
        max_instances=1,
    )

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
    parser.add_argument("--pages", type=int, default=None, help="Override page count for --test-scrape.")
    args = parser.parse_args()
    main(test_scrape=args.test_scrape, pages_override=args.pages, bootstrap=args.bootstrap)

