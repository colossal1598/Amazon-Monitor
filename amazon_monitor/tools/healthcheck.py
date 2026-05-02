import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HEALTH_FILE = Path("data/health.json")


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def main() -> int:
    if not HEALTH_FILE.exists():
        print("FAIL: health file not found at data/health.json")
        return 2

    data = json.loads(HEALTH_FILE.read_text(encoding="utf-8"))
    jobs: dict = data.get("jobs", {})
    now = datetime.now(timezone.utc)

    limits = {
        "search": timedelta(minutes=10),
        "heartbeat": timedelta(minutes=40),
    }

    failed = []
    for job, max_age in limits.items():
        info = jobs.get(job, {})
        success = parse_dt(info.get("last_success_at"))
        error = info.get("last_error_message")
        if success is None:
            failed.append(f"{job}: never succeeded")
            continue
        if now - success > max_age:
            failed.append(f"{job}: stale success ({success.isoformat()})")
        if error:
            failed.append(f"{job}: last error present ({error})")

    if failed:
        print("FAIL")
        for item in failed:
            print(f"- {item}")
        return 1

    print("PASS: all monitored jobs are healthy")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
