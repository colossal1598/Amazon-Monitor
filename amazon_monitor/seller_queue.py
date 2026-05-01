"""Persistent JSON queue for ASINs awaiting PDP seller confirmation."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_pending_queue(path: str) -> dict[str, dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        LOGGER.warning("Failed to read pending seller queue %s: %s", path, exc)
        return {}
    if isinstance(data, dict) and "entries" in data:
        raw_entries = data["entries"]
    else:
        raw_entries = data
    if not isinstance(raw_entries, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for key, val in raw_entries.items():
        if not isinstance(val, dict):
            continue
        asin = str(key or val.get("asin") or "").upper()
        if len(asin) == 10 and asin.isalnum():
            out[asin] = val
    return out


def save_pending_queue(path: str, queue: dict[str, dict[str, Any]]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {"updated_at": utc_iso(), "entries": queue}
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def prune_stale_entries(queue: dict[str, dict[str, Any]], ttl_days: int) -> int:
    if ttl_days <= 0:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=ttl_days)
    removed = 0
    for asin in list(queue.keys()):
        rec = queue[asin]
        last_raw = rec.get("last_seen") or rec.get("first_seen")
        if not last_raw:
            continue
        try:
            dt = datetime.fromisoformat(str(last_raw).replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt < cutoff:
            del queue[asin]
            removed += 1
    if removed:
        LOGGER.info("pruned_stale_pending_seller_entries count=%s ttl_days=%s", removed, ttl_days)
    return removed
