import logging
import json
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

import requests
import yaml
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv

from browser_factory import init_global_rate_limiter
from exceptions import CaptchaBlocked, ModemIPUnchanged, NetworkAccessDenied
from filter_pipeline import filter_search_results
from modem_rotator import reconnect_modem
from search_scraper import scrape_search
from state_engine import StateEngine
from webhook_sender import send_alert, send_heartbeat, send_modem_trigger, send_operational_error

LOGGER = logging.getLogger("monitor")


def resolve_export_search_url(config: dict) -> str:
    """Single monitored search: `search_url` or `search_urls.amazon_export`."""
    raw = config.get("search_url")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    urls = config.get("search_urls")
    if isinstance(urls, dict):
        exp = urls.get("amazon_export")
        if isinstance(exp, str) and exp.strip():
            return exp.strip()
    raise ValueError("Set `search_url` or `search_urls.amazon_export` in config.yaml")


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
            results = scrape_search(resolve_export_search_url(config), pages=config["search_pages"])
            filtered = filter_search_results(
                results,
                config["required_keywords"],
                "blacklist.txt",
                config.get("required_any_keywords", []),
            )
            reconcile_missing, skipped_reason = should_reconcile_missing_asins(config, len(filtered))
            if skipped_reason:
                LOGGER.info(
                    "search_reconcile_skipped reason=%s raw_count=%s filtered_count=%s",
                    skipped_reason,
                    len(results),
                    len(filtered),
                )
            alerts = state_engine.process_search_candidates(filtered, reconcile_missing=reconcile_missing)
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
    main()

