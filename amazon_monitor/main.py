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

import client_alerts
import fx_rate
from alert_dedupe import dedupe_alerts_by_asin
from browser_factory import init_global_rate_limiter
from exceptions import CaptchaBlocked, NetworkAccessDenied
from filter_pipeline import run_search_filter_pipeline
from pdp_helpers import valid_asin
from pdp_scraper import scrape_pdp_watch
from search_scraper import scrape_search
from settings_store import list_asins, load_runtime_config, migrate_yaml_to_db
from state_engine import StateEngine
from telemetry_store import TelemetryStore
from webhook_sender import send_alert, send_heartbeat
import usage_metrics

LOGGER = logging.getLogger("monitor")


def _fmt_kv_value(v: Any) -> str:
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


def _kv_tail(**fields: Any) -> str:
    """Normalized key=value tail for compact lifecycle logs (sorted keys)."""
    return " ".join(f"{k}={_fmt_kv_value(v)}" for k, v in sorted(fields.items()) if k)


def _ordered_kv_tail(pairs: list[tuple[str, Any]]) -> str:
    """key=value tail with explicit field order."""
    return " ".join(f"{k}={_fmt_kv_value(v)}" for k, v in pairs if k)


def _english_head(event: str) -> str:
    heads = {
        "monitor_started": "Monitor started.",
        "monitor_shutdown": "Monitor shutting down.",
        "cycle_start": "PDP cycle start.",
        "scrape_pdp_watch_start": "Scraping PDP watch list.",
        "pdp_cycle_done": "PDP cycle done.",
    }
    return heads.get(event, "Log.")


def log_lifecycle(
    event: str,
    *,
    cycle_stamp: bool = False,
    ordered: list[tuple[str, Any]] | None = None,
    **fields: Any,
) -> None:
    head = _english_head(event)
    if ordered is not None:
        tail = _ordered_kv_tail(ordered)
    else:
        tail = _kv_tail(**fields)
    msg = f"{head} {tail}".strip() if tail else head
    if cycle_stamp:
        msg = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    LOGGER.info(msg, extra={"channel": "lifecycle"})


class _LifecycleFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.WARNING:
            return True
        ch = getattr(record, "channel", None)
        return ch == "lifecycle"


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
    lifecycle_file.addFilter(_LifecycleFilter())

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console.addFilter(_LifecycleFilter())

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.handlers.clear()
    root.addHandler(lifecycle_file)
    root.addHandler(console)


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _telemetry_db_path(bootstrap: dict[str, Any], runtime: dict[str, Any]) -> str:
    return str(
        bootstrap.get("telemetry_db_path")
        or runtime.get("telemetry_db_path")
        or "data/telemetry.db"
    )


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


def resolve_aes_llc_url(config: dict[str, Any]) -> str:
    raw_urls = config.get("search_urls")
    if isinstance(raw_urls, dict):
        url = str(raw_urls.get("aes_llc") or raw_urls.get("newest_arrivals") or "").strip()
        if url:
            return url
    raise ValueError("Set search_urls.aes_llc in DB settings (admin UI or settings table)")


def _blacklist_asins(config: dict[str, Any]) -> set[str]:
    raw = config.get("blacklist")
    if not isinstance(raw, list):
        return set()
    out: set[str] = set()
    for value in raw:
        asin = str(value).strip().upper()
        if valid_asin(asin):
            out.add(asin)
    return out


def _finish_cycle(
    telemetry: TelemetryStore,
    cycle_id: int,
    config: dict[str, Any],
    poll_min: int,
    *,
    watch_list: list[str],
    pdp_rows: list[dict[str, Any]],
    skip_rows: int,
    in_stock_rows: int,
    captcha_rows: int,
    captcha_aborted_rows: int,
    sent_alerts: int,
    aes_raw_count: int,
    aes_pipeline_count: int,
    aes_alert_count: int,
    aes_outcome: dict[str, Any] | None,
    pdp_state_summary: dict[str, Any] | None,
    aes_state_summary: dict[str, Any] | None,
) -> None:
    scrape_errors, reason_counts = client_alerts.count_pdp_scrape_errors(pdp_rows)
    metrics = usage_metrics.to_summary(pdp_poll_minutes=poll_min)
    summary: dict[str, Any] = {
        **metrics,
        "pdp_watch": len(watch_list),
        "watch_rows": len(pdp_rows),
        "in_stock": in_stock_rows,
        "skip_update": skip_rows,
        "captcha_skip": captcha_rows,
        "captcha_aborted": captcha_aborted_rows,
        "alerts_sent": sent_alerts,
        "aes_raw": aes_raw_count,
        "aes_candidates": aes_pipeline_count,
        "aes_alerts": aes_alert_count,
        "captcha_aborted_flag": bool(captcha_aborted_rows),
        "pdp_scrape_errors": scrape_errors,
        "pdp_scrape_error_reasons_json": reason_counts,
        "aes_scrape_outcome_json": aes_outcome or {},
        "pdp_state_summary_json": pdp_state_summary or {},
        "aes_state_summary_json": aes_state_summary or {},
    }
    telemetry.finish_cycle(cycle_id, summary, config)
    client_alerts.on_cycle_timing(summary, config, telemetry, cycle_id=cycle_id)
    client_alerts.check_scrape_degraded(
        pdp_rows,
        aes_outcome,
        len(watch_list),
        config,
        telemetry,
        cycle_id=cycle_id,
    )


def main() -> None:
    load_dotenv()
    bootstrap_config = load_config()
    setup_logging(str(bootstrap_config.get("log_dir", "logs")))
    Path(bootstrap_config.get("auth_dir", "auth")).mkdir(parents=True, exist_ok=True)
    db_path = str(bootstrap_config.get("db_path", "data/monitor.db"))
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    migrate_yaml_to_db("config.yaml", db_path)
    startup_config = load_runtime_config(db_path)
    telemetry = TelemetryStore(_telemetry_db_path(bootstrap_config, startup_config))

    state_engine = StateEngine(
        db_path=db_path,
        price_drop_percent=float(startup_config.get("price_drop_percent", 10)),
    )
    init_global_rate_limiter(int(startup_config.get("max_requests_per_minute", 10)))

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

    def handle_captcha_or_network_pause(config: dict[str, Any]) -> None:
        pause_s = max(0, int(config.get("captcha_recovery_pause_seconds", 120)))
        LOGGER.warning("Captcha or network recovery: pausing PDP job %ss", pause_s)
        scraping_paused["value"] = True
        if scheduler.get_job("pdp_loop"):
            scheduler.pause_job("pdp_loop")
        time.sleep(float(pause_s))
        if scheduler.get_job("pdp_loop"):
            scheduler.resume_job("pdp_loop")
        scraping_paused["value"] = False
        LOGGER.warning("PDP job resumed")

    def aes_discovery_loop(
        config: dict[str, Any],
        *,
        telemetry_store: TelemetryStore,
        cycle_id: int,
    ) -> tuple[int, int, list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
        aes_llc_url = resolve_aes_llc_url(config)
        aes_items, scrape_data = scrape_search(
            aes_llc_url,
            source="aes_llc",
            scrape_mode="newest_front",
            pagination_mode="fixed",
            fixed_pages=1,
            max_search_pages=1,
            collect_debug=False,
            max_cycle_seconds=int(config.get("max_cycle_seconds", 170)),
            serp_scroll_profile="minimal",
            headless=bool(config.get("playwright_headless", True)),
        )
        aes_outcome = scrape_data.get("scrape_outcome") if isinstance(scrape_data, dict) else {}
        if not isinstance(aes_outcome, dict):
            aes_outcome = {}
        aes_pipeline_rows, _aes_meta = run_search_filter_pipeline(
            aes_items,
            config,
            require_shipping_signal=False,
        )
        blocked_asins = _blacklist_asins(config)
        aes_candidates = [
            row for row in aes_pipeline_rows if (row.get("asin") or "").strip().upper() not in blocked_asins
        ]
        aes_alerts, aes_summary = state_engine.process_aes_serp_mirror(
            aes_candidates,
            source="aes_llc",
            reconcile_absence=len(aes_candidates) > 0,
            config=config,
            telemetry=telemetry_store,
            cycle_id=cycle_id,
        )
        return len(aes_items), len(aes_candidates), aes_alerts, aes_outcome, aes_summary

    def pdp_loop() -> None:
        if scraping_paused["value"]:
            return
        config = load_runtime_config(db_path)
        config["pdp_watch_asins"] = list_asins(db_path, "watch")
        config["blacklist"] = list_asins(db_path, "blacklist")
        state_engine.price_drop_percent = float(config.get("price_drop_percent", 10))
        init_global_rate_limiter(int(config.get("max_requests_per_minute", 10)))
        mark_job_started("pdp")
        cycle_id = 0
        poll_min = int(config.get("pdp_poll_minutes", 4))
        watch_list: list[str] = []
        pdp_rows: list[dict[str, Any]] = []
        skip_rows = 0
        in_stock_rows = 0
        captcha_rows = 0
        captcha_aborted_rows = 0
        aes_outcome: dict[str, Any] = {}
        pdp_state_summary: dict[str, Any] = {}
        aes_state_summary: dict[str, Any] = {}
        try:
            telemetry.maybe_prune(config)
            usage_metrics.reset(config)
            cycle_id = telemetry.begin_cycle(config)
            watch_list = _normalize_pdp_watch_asins(config.get("pdp_watch_asins"))
            log_lifecycle(
                "cycle_start",
                cycle_stamp=True,
                watch=len(watch_list),
            )
            sent_alerts = 0
            aes_raw_count = 0
            aes_pipeline_count = 0
            aes_alert_count = 0
            tick_alerts: list[dict[str, Any]] = []

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
                    headless=bool(config.get("playwright_headless", True)),
                    pdp_title_wait_ms=int(config.get("pdp_title_wait_ms", 6_000)),
                    pdp_price_wait_ms=int(config.get("pdp_price_wait_ms", 2_000)),
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
                pdp_alerts, pdp_state_summary = state_engine.process_pdp_watch_candidates(
                    pdp_rows,
                    set(watch_list),
                    config=config,
                    telemetry=telemetry,
                    cycle_id=cycle_id,
                )
                tick_alerts.extend(pdp_alerts)
            else:
                LOGGER.warning("No pdp_watch_asins configured; PDP cycle did nothing.")

            if captcha_rows:
                captcha_asins = sorted(
                    {
                        str(r.get("asin")).upper()
                        for r in pdp_rows
                        if isinstance(r, dict) and r.get("skip_reason") == "captcha" and r.get("asin")
                    }
                )
                completed_rows = sum(
                    1 for r in pdp_rows if isinstance(r, dict) and not r.get("_skip_update")
                )
                pause_s = max(0, int(config.get("captcha_recovery_pause_seconds", 120)))
                detail = (
                    f"asins={','.join(captcha_asins) or 'unknown'} "
                    f"completed={completed_rows}/{len(watch_list)} "
                    f"captcha_rows={captcha_rows} aborted={captcha_aborted_rows} pause_s={pause_s}"
                )
                LOGGER.warning("Captcha detected: %s", detail)
                deduped_alerts = dedupe_alerts_by_asin(tick_alerts)
                for alert in deduped_alerts:
                    send_alert(alert, config)
                mark_job_error("pdp", detail)
                client_alerts.maybe_alert("captcha", config, telemetry, cycle_id=cycle_id, detail=detail)
                _finish_cycle(
                    telemetry,
                    cycle_id,
                    config,
                    poll_min,
                    watch_list=watch_list,
                    pdp_rows=pdp_rows,
                    skip_rows=skip_rows,
                    in_stock_rows=in_stock_rows,
                    captcha_rows=captcha_rows,
                    captcha_aborted_rows=captcha_aborted_rows,
                    sent_alerts=len(deduped_alerts),
                    aes_raw_count=0,
                    aes_pipeline_count=0,
                    aes_alert_count=0,
                    aes_outcome=aes_outcome,
                    pdp_state_summary=pdp_state_summary,
                    aes_state_summary=aes_state_summary,
                )
                handle_captcha_or_network_pause(config)
                return

            aes_raw_count, aes_pipeline_count, aes_alerts, aes_outcome, aes_state_summary = aes_discovery_loop(
                config,
                telemetry_store=telemetry,
                cycle_id=cycle_id,
            )
            aes_alert_count = len(aes_alerts)
            tick_alerts.extend(aes_alerts)
            deduped_alerts = dedupe_alerts_by_asin(tick_alerts)
            for alert in deduped_alerts:
                send_alert(alert, config)
            sent_alerts = len(deduped_alerts)
            cycle_timing = usage_metrics.to_summary(pdp_poll_minutes=poll_min)
            log_lifecycle(
                "pdp_cycle_done",
                cycle_stamp=True,
                ordered=[
                    ("PDP", len(watch_list)),
                    ("AES", aes_pipeline_count),
                    ("Alerts", sent_alerts),
                    ("captcha", bool(captcha_rows)),
                    ("total_sec", cycle_timing["total_sec"]),
                ],
            )
            _finish_cycle(
                telemetry,
                cycle_id,
                config,
                poll_min,
                watch_list=watch_list,
                pdp_rows=pdp_rows,
                skip_rows=skip_rows,
                in_stock_rows=in_stock_rows,
                captcha_rows=captcha_rows,
                captcha_aborted_rows=captcha_aborted_rows,
                sent_alerts=sent_alerts,
                aes_raw_count=aes_raw_count,
                aes_pipeline_count=aes_pipeline_count,
                aes_alert_count=aes_alert_count,
                aes_outcome=aes_outcome,
                pdp_state_summary=pdp_state_summary,
                aes_state_summary=aes_state_summary,
            )
            mark_job_success("pdp")
            fx_rate.bump_monitor_tick(config)
        except CaptchaBlocked:
            mark_job_error("pdp", "CaptchaBlocked")
            LOGGER.warning("CaptchaBlocked during PDP cycle")
            client_alerts.maybe_alert("captcha", config, telemetry, cycle_id=cycle_id, detail="CaptchaBlocked")
            handle_captcha_or_network_pause(config)
        except NetworkAccessDenied as exc:
            mark_job_error("pdp", f"NetworkAccessDenied: {exc}")
            LOGGER.error("Network access denied: %s", exc)
            client_alerts.maybe_alert("network_blocked", config, telemetry, cycle_id=cycle_id, detail=str(exc))
            handle_captcha_or_network_pause(config)
        except Exception as exc:
            LOGGER.exception("pdp_loop failed: %s", exc)
            mark_job_error("pdp", exc)
            client_alerts.maybe_alert("cycle_failed", config, telemetry, cycle_id=cycle_id, detail=str(exc))

    def heartbeat_loop() -> None:
        config = load_runtime_config(db_path)
        mark_job_started("heartbeat")
        try:
            send_heartbeat(config)
            mark_job_success("heartbeat")
        except Exception as exc:
            LOGGER.warning("heartbeat failed: %s", exc)
            mark_job_error("heartbeat", exc)

    scheduler.add_job(
        pdp_loop,
        "interval",
        minutes=int(startup_config.get("pdp_poll_minutes", 4)),
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
