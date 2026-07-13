"""Entrypoint: loads config, then runs the continuous streaming engine.

All scraping/alerting logic lives in monitor_engine.py. This file only wires
logging, bootstrap config, SQLite stores, and the heartbeat side-task.
"""

import argparse
import asyncio
import logging
import os
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from browser_factory import init_global_rate_limiter
from monitor_engine import MonitorEngine
from settings_store import load_runtime_config, migrate_yaml_to_db
from state_engine import StateEngine
from telemetry_store import TelemetryStore
from webhook_sender import send_heartbeat

LOGGER = logging.getLogger("monitor")

_HEARTBEAT_INTERVAL_SECONDS = 30 * 60
# How often the watchdog checks for engine progress.
_WATCHDOG_POLL_SECONDS = 60
# Fallback stall threshold if watchdog_stall_seconds isn't set in config.
_DEFAULT_WATCHDOG_STALL_SECONDS = 600


class _LifecycleFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.WARNING:
            return True
        return getattr(record, "channel", None) == "lifecycle"


class _DebugFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return getattr(record, "channel", None) == "debug"


def load_config(path: str = "config.yaml") -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as file:
        loaded = yaml.safe_load(file)
    return loaded if isinstance(loaded, dict) else {}


def setup_logging(log_dir: str) -> None:
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    # Every line now carries its own timestamp. Previously only the manually
    # formatted "Sweep done"/"ALERT" lines had one; everything else (including
    # every "check failed"/"unexpected task error" line during the browser-
    # disconnect outage found in logs/monitor.log) had none, making it
    # impossible to measure how long an outage lasted or correlate it against
    # other events without counting lines.
    formatter = logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    lifecycle_file = RotatingFileHandler(
        Path(log_dir) / "monitor.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    lifecycle_file.setFormatter(formatter)
    lifecycle_file.addFilter(_LifecycleFilter())

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console.addFilter(_LifecycleFilter())

    debug_file = RotatingFileHandler(
        Path(log_dir) / "debug.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8"
    )
    debug_file.setFormatter(formatter)
    debug_file.addFilter(_DebugFilter())

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.handlers.clear()
    root.addHandler(lifecycle_file)
    root.addHandler(console)
    root.addHandler(debug_file)


def _telemetry_db_path(bootstrap: dict[str, Any], runtime: dict[str, Any]) -> str:
    return str(
        bootstrap.get("telemetry_db_path")
        or runtime.get("telemetry_db_path")
        or "data/telemetry.db"
    )


async def _heartbeat_loop(engine: MonitorEngine, db_path: str) -> None:
    while True:
        await asyncio.sleep(_HEARTBEAT_INTERVAL_SECONDS)
        config = load_runtime_config(db_path)
        engine.mark_job("heartbeat", "started")
        try:
            await asyncio.to_thread(send_heartbeat, config)
            engine.mark_job("heartbeat", "success")
        except Exception as exc:  # noqa: BLE001 - heartbeat is best-effort
            LOGGER.warning("heartbeat failed: %s", exc)
            engine.mark_job("heartbeat", "error", str(exc))


def _watchdog_stall_seconds(config: dict[str, Any]) -> float:
    try:
        return max(60.0, float(config.get("watchdog_stall_seconds", _DEFAULT_WATCHDOG_STALL_SECONDS)))
    except (TypeError, ValueError):
        return float(_DEFAULT_WATCHDOG_STALL_SECONDS)


async def _watchdog_loop(engine: MonitorEngine, db_path: str) -> None:
    """Hard-exit the process if the engine stops making forward progress.

    A silent hang (e.g. a browser-driver disconnect that no code path detects)
    previously persisted for days: PM2's autorestart only fires on process
    exit, and the engine's own exception handling can't save it from a hang
    inside a call that never raises or returns. This is an independent,
    coarse-grained backstop: it doesn't know *why* the engine stalled, it just
    notices that ``engine.last_progress`` (a cheap monotonic timestamp bumped
    on every sweep finish, per-ASIN check, and deliberate recovery pause) hasn't
    moved in too long, and kills the process so PM2 restarts it fresh.
    """
    while True:
        await asyncio.sleep(_WATCHDOG_POLL_SECONDS)
        try:
            config = load_runtime_config(db_path)
        except Exception as exc:  # noqa: BLE001 - watchdog must not depend on config reads
            LOGGER.warning("watchdog: failed to load config, using default threshold: %s", exc)
            config = {}
        stall_seconds = _watchdog_stall_seconds(config)
        stalled_for = time.monotonic() - engine.last_progress
        if stalled_for >= stall_seconds:
            LOGGER.critical(
                "Watchdog: no engine progress for %.0fs (threshold=%.0fs); exiting for PM2 restart.",
                stalled_for,
                stall_seconds,
                extra={"channel": "lifecycle"},
            )
            logging.shutdown()
            os._exit(3)


async def _run(engine: MonitorEngine, db_path: str) -> None:
    heartbeat = asyncio.create_task(_heartbeat_loop(engine, db_path))
    watchdog = asyncio.create_task(_watchdog_loop(engine, db_path))
    try:
        await engine.run_forever()
    finally:
        heartbeat.cancel()
        watchdog.cancel()


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

    engine = MonitorEngine(db_path, state_engine, telemetry)
    engine.write_health(force=True)
    LOGGER.info("Monitor started (streaming engine).", extra={"channel": "lifecycle"})
    try:
        asyncio.run(_run(engine, db_path))
    except KeyboardInterrupt:
        LOGGER.info("Monitor shutting down.", extra={"channel": "lifecycle"})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Amazon PDP monitor")
    parser.parse_args()
    main()
