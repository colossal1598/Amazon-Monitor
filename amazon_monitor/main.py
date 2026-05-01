import logging
import json
import time
import argparse
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

import requests
import yaml
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv

from browser_factory import init_global_rate_limiter
from exceptions import CaptchaBlocked, ModemIPUnchanged, NetworkAccessDenied
from filter_pipeline import filter_marketplace_items
from modem_rotator import reconnect_modem
from search_scraper import scrape_search
from state_engine import StateEngine
from webhook_sender import send_alert, send_heartbeat, send_modem_trigger, send_operational_error

LOGGER = logging.getLogger("monitor")


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
    output_dir = Path("data/test_scrape")
    output_dir.mkdir(parents=True, exist_ok=True)

    raw_items: list[dict] = []
    selector_debug: list[dict] = []
    pdp_debug: dict[str, dict] = {}
    for source, url in resolve_search_urls(config):
        items, debug = scrape_search(url, pages=pages, source=source, collect_debug=True)
        raw_items.extend(items)
        selector_debug.extend(debug.get("selector_debug", []))
        pdp_debug.update(debug.get("pdp_debug", {}))

    filtered_items = filter_marketplace_items(raw_items)
    (output_dir / "raw_items.json").write_text(json.dumps(raw_items, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "filtered_items.json").write_text(
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
        "test_scrape_complete raw=%s filtered=%s output_dir=%s",
        len(raw_items),
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


def main(test_scrape: bool = False, pages_override: int | None = None) -> None:
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
            for source, url in resolve_search_urls(config):
                source_items, _debug = scrape_search(
                    url,
                    pages=config["search_pages"],
                    source=source,
                    collect_debug=False,
                )
                results.extend(source_items)
            filtered = filter_marketplace_items(results)
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
    parser.add_argument("--pages", type=int, default=None, help="Override page count for --test-scrape.")
    args = parser.parse_args()
    main(test_scrape=args.test_scrape, pages_override=args.pages)

