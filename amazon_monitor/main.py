"""Entrypoint: loads config, then runs the continuous streaming engine.

All scraping/alerting logic lives in monitor_engine.py. This file only wires
logging, bootstrap config, SQLite stores, and the heartbeat side-task.
"""

import argparse
import asyncio
import logging
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
    formatter = logging.Formatter("%(message)s")

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


async def _run(engine: MonitorEngine, db_path: str) -> None:
    heartbeat = asyncio.create_task(_heartbeat_loop(engine, db_path))
    try:
        await engine.run_forever()
    finally:
        heartbeat.cancel()


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
