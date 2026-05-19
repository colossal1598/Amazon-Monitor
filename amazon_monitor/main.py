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

import fx_rate
from alert_dedupe import dedupe_alerts_by_asin
from browser_factory import init_global_rate_limiter
from exceptions import CaptchaBlocked, NetworkAccessDenied
from pdp_helpers import valid_asin
from pdp_scraper import scrape_pdp_watch
from state_engine import StateEngine
from webhook_sender import send_alert, send_heartbeat, send_operational_error

LOGGER = logging.getLogger("monitor")


def _kv_tail(**fields: Any) -> str:
    """Normalized key=value tail for compact lifecycle/debug logs."""

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

    return " ".join(f"{k}={fmt(v)}" for k, v in sorted(fields.items()) if k)


def _english_head(event: str) -> str:
    heads = {
        "monitor_started": "Monitor started.",
        "monitor_shutdown": "Monitor shutting down.",
        "cycle_start": "PDP cycle start.",
        "scrape_pdp_watch_start": "Scraping PDP watch list.",
        "pdp_watch_counts": "PDP watch counts.",
        "pdp_cycle_done": "PDP cycle done.",
    }
    return heads.get(event, "Log.")


def _log(channel: str, event: str, *, cycle_stamp: bool = False, **fields: Any) -> None:
    head = _english_head(event)
    tail = _kv_tail(**fields)
    msg = f"{head} {tail}".strip() if tail else head
    if cycle_stamp:
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


def load_config(path: str = "config.yaml") -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as file:
        loaded = yaml.safe_load(file)
    return loaded if isinstance(loaded, dict) else {}


def setup_logging(log_dir: str) -> None:
    Path(log_dir).mkdir(parents=True, exist_ok=True)
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


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_pdp_watch_asins(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for x in raw:
        asin = str(x).strip().upper()
        if not valid_asin(asin) or asin in seen:
            continue
        seen.add(asin)
        out.append(asin)
    return out


def _coerce_range(raw: Any, default: tuple[float, float]) -> tuple[float, float]:
    if isinstance(raw, (list, tuple)) and len(raw) >= 2:
        try:
            low, high = float(raw[0]), float(raw[1])
        except (TypeError, ValueError):
            return default
        if 0 <= low <= high:
            return low, high
    return default


def _allowed_seller_substrings(config: dict[str, Any]) -> list[str]:
    raw = config.get("pdp_allowed_seller_substrings")
    if isinstance(raw, list):
        values = [str(x).strip() for x in raw if str(x).strip()]
        if values:
            return values
    return ["amazon.com", "amazon export"]


def main() -> None:
    load_dotenv()
    config = load_config()
    setup_logging(str(config.get("log_dir", "logs")))
    Path(config.get("auth_dir", "auth")).mkdir(parents=True, exist_ok=True)
    db_path = str(config.get("db_path", "data/monitor.db"))
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    state_engine = StateEngine(
        db_path=db_path,
        price_drop_percent=float(config.get("price_drop_percent", 10)),
    )
    init_global_rate_limiter(int(config.get("max_requests_per_minute", 10)))

    scheduler = BackgroundScheduler()
    scraping_paused = {"value": False}
    health_file = Path("data/health.json")
    health_file.parent.mkdir(parents=True, exist_ok=True)
    health_state: dict[str, dict[str, str | None]] = {
        "pdp": {"last_started_at": None, "last_success_at": None, "last_error_at": None, "last_error_message": None},
        "heartbeat": {"last_started_at": None, "last_success_at": None, "last_error_at": None, "last_error_message": None},
    }

    def write_health() -> None:
        try:
            health_file.write_text(
                json.dumps({"updated_at": utc_iso(), "jobs": health_state}, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            LOGGER.error("Failed to write health file %s: %s", health_file, exc)

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
        pause_s = max(0, int(config.get("captcha_recovery_pause_seconds", 120)))
        LOGGER.warning("Captcha or network recovery: pausing PDP job %ss", pause_s)
        scraping_paused["value"] = True
        if scheduler.get_job("pdp_loop"):
            scheduler.pause_job("pdp_loop")
        time.sleep(float(pause_s))
        if scheduler.get_job("pdp_loop"):
            scheduler.resume_job("pdp_loop")
        scraping_paused["value"] = False
        LOGGER.info("PDP job resumed")

    def pdp_loop() -> None:
        if scraping_paused["value"]:
            return
        mark_job_started("pdp")
        try:
            watch_list = _normalize_pdp_watch_asins(config.get("pdp_watch_asins"))
            _log("lifecycle", "cycle_start", cycle_stamp=True, watch=len(watch_list))
            all_alerts: list[dict[str, Any]] = []
            pdp_rows: list[dict[str, Any]] = []
            captcha_rows = 0
            captcha_aborted_rows = 0

            if watch_list:
                allowed_subs = _allowed_seller_substrings(config)
                delay_range = _coerce_range(config.get("pdp_watch_scroll_delay_seconds"), (0.25, 0.65))
                log_lifecycle("scrape_pdp_watch_start", count=len(watch_list))
                pdp_rows = scrape_pdp_watch(
                    watch_list,
                    allowed_subs,
                    max_cycle_seconds=int(config.get("max_cycle_seconds", 170)),
                    scroll_delay_range=delay_range,
                    max_concurrent_tabs=int(config.get("pdp_watch_max_concurrent_tabs", 2)),
                    tab_jitter_seconds=config.get("pdp_watch_tab_jitter_seconds"),
                    max_attempts=int(config.get("pdp_watch_max_attempts", 3)),
                )
                skip_rows = sum(1 for r in pdp_rows if isinstance(r, dict) and r.get("_skip_update"))
                in_stock_rows = sum(1 for r in pdp_rows if isinstance(r, dict) and r.get("in_stock"))
                captcha_rows = sum(
                    1
                    for r in pdp_rows
                    if isinstance(r, dict) and r.get("_skip_update") and r.get("skip_reason") == "captcha"
                )
                captcha_aborted_rows = sum(
                    1
                    for r in pdp_rows
                    if isinstance(r, dict)
                    and r.get("_skip_update")
                    and r.get("skip_reason") == "captcha_run_aborted"
                )
                log_debug(
                    "pdp_watch_counts",
                    watch=len(watch_list),
                    rows=len(pdp_rows),
                    in_stock=in_stock_rows,
                    skip_update=skip_rows,
                    captcha_skip=captcha_rows,
                    captcha_aborted=captcha_aborted_rows,
                )
                all_alerts.extend(state_engine.process_pdp_watch_candidates(pdp_rows, set(watch_list)))
            else:
                LOGGER.warning("No pdp_watch_asins configured; PDP cycle did nothing.")

            outbound = dedupe_alerts_by_asin(all_alerts)
            for alert in outbound:
                send_alert(alert, config)

            log_lifecycle(
                "pdp_cycle_done",
                alerts=len(outbound),
                captcha_aborted=bool(captcha_rows),
                pdp_watch=len(watch_list),
            )
            if captcha_rows:
                captcha_asins = sorted(
                    {
                        str(r.get("asin")).upper()
                        for r in pdp_rows
                        if isinstance(r, dict) and r.get("skip_reason") == "captcha" and r.get("asin")
                    }
                )
                captcha_asin_label = ",".join(captcha_asins) if captcha_asins else "unknown"
                completed_rows = sum(
                    1 for r in pdp_rows if isinstance(r, dict) and not r.get("_skip_update")
                )
                pause_s = max(0, int(config.get("captcha_recovery_pause_seconds", 120)))
                msg = (
                    f"Captcha detected on ASIN(s): {captcha_asin_label}. "
                    f"Completed {completed_rows}/{len(watch_list)}. "
                    f"Captcha rows={captcha_rows}. "
                    f"Aborted rows={captcha_aborted_rows}. "
                    f"Pausing PDP job for {pause_s}s."
                )
                mark_job_error("pdp", msg)
                send_operational_error("pdp_error", msg, config)
                handle_captcha_or_network_pause()
                return
            mark_job_success("pdp")
            fx_rate.bump_monitor_tick(config)
        except CaptchaBlocked:
            mark_job_error("pdp", "CaptchaBlocked")
            send_operational_error("pdp_error", "CaptchaBlocked: PDP scraping paused then resumed", config)
            handle_captcha_or_network_pause()
        except NetworkAccessDenied as exc:
            mark_job_error("pdp", f"NetworkAccessDenied: {exc}")
            LOGGER.error("Network access denied: %s", exc)
            send_operational_error("pdp_error", str(exc), config)
            handle_captcha_or_network_pause()
        except Exception as exc:
            LOGGER.exception("pdp_loop failed: %s", exc)
            mark_job_error("pdp", exc)
            send_operational_error("pdp_error", str(exc), config)

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
        pdp_loop,
        "interval",
        minutes=int(config.get("pdp_poll_minutes", 4)),
        jitter=60,
        next_run_time=datetime.now(timezone.utc),
        id="pdp_loop",
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
    parser = argparse.ArgumentParser(description="Amazon PDP monitor")
    parser.parse_args()
    main()
