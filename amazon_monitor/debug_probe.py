"""Debug-mode NDJSON logger (session da423a). Remove after verification."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

_LOG_PATH = Path(__file__).resolve().parent / "debug-da423a.log"
_SESSION = "da423a"


def agent_log(
    location: str,
    message: str,
    data: dict[str, Any],
    hypothesis_id: str,
    *,
    run_id: str = "pre-fix",
) -> None:
    # region agent log
    payload = {
        "sessionId": _SESSION,
        "timestamp": int(time.time() * 1000),
        "location": location,
        "message": message,
        "data": data,
        "hypothesisId": hypothesis_id,
        "runId": run_id,
    }
    try:
        with _LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, default=str) + "\n")
    except OSError:
        pass
    # endregion
