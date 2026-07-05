import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

HEALTH_FILE = Path("data/health.json")


# Turn a saved timestamp string into a datetime so we can check how old the last success was.
def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


# Best-effort WhatsApp ping so a stuck/crashed/silently-hung monitor doesn't go unnoticed
# just because nobody is watching PM2 logs or this cron job's exit code. Rate-limited via
# the normal client-alert cooldown/window (shared SQLite state), so this can safely run
# every 10 minutes without spamming once a real incident is already reported.
def _notify(failed_items: list[str]) -> None:
    try:
        from dotenv import load_dotenv

        import client_alerts
        from settings_store import load_runtime_config
        from telemetry_store import TelemetryStore

        load_dotenv()
        try:
            import yaml

            bootstrap = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8")) or {}
        except Exception:
            bootstrap = {}
        db_path = str(bootstrap.get("db_path", "data/monitor.db"))
        telemetry_db_path = str(bootstrap.get("telemetry_db_path", "data/telemetry.db"))
        config = load_runtime_config(db_path)
        telemetry = TelemetryStore(telemetry_db_path)
        client_alerts.maybe_alert(
            "health_check_failed",
            config,
            telemetry,
            detail="; ".join(failed_items),
        )
    except Exception as exc:  # noqa: BLE001 - notification is best-effort, never crash the check
        print(f"(healthcheck notify failed: {exc})")


# Check the monitor’s health file and exit with a non-zero code when jobs are stale or have recent errors.
def main() -> int:
    if not HEALTH_FILE.exists():
        print("FAIL: health file not found at data/health.json")
        _notify(["health file not found at data/health.json"])
        return 2

    data = json.loads(HEALTH_FILE.read_text(encoding="utf-8"))
    jobs: dict = data.get("jobs", {})
    now = datetime.now(timezone.utc)

    limits = {
        "pdp": timedelta(minutes=10),
        "heartbeat": timedelta(minutes=40),
    }

    failed = []
    for job, max_age in limits.items():
        info = jobs.get(job, {})
        success = parse_dt(info.get("last_success_at"))
        error_at = parse_dt(info.get("last_error_at"))
        error = info.get("last_error_message")
        if success is None:
            failed.append(f"{job}: never succeeded")
            continue
        if now - success > max_age:
            failed.append(f"{job}: stale success ({success.isoformat()})")
        if error and (error_at is None or error_at > success):
            failed.append(f"{job}: last error present ({error})")

    if failed:
        print("FAIL")
        for item in failed:
            print(f"- {item}")
        _notify(failed)
        return 1

    print("PASS: all monitored jobs are healthy")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
