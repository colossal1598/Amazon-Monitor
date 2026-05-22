"""usage_metrics summary and JSONL (Israel timestamps)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
import usage_metrics


def test_to_summary_has_israel_timestamps() -> None:
    usage_metrics.reset()
    usage_metrics.record_pdp_phase(10.5, 1024 * 100, ok=5, skip=1)
    usage_metrics.record_aes_phase(3.2, 1024 * 50)
    usage_metrics.record_image_fetch(2048, cache_hit=False)
    usage_metrics.record_image_fetch(0, cache_hit=True)
    usage_metrics.bump_blocked()
    summary = usage_metrics.to_summary(pdp_poll_minutes=4)
    assert "timestamp_il" in summary
    assert "cycle_started_il" in summary
    dt = datetime.fromisoformat(summary["timestamp_il"])
    assert dt.tzinfo is not None
    assert str(dt.tzinfo) in ("Asia/Jerusalem", "UTC+03:00")
    assert summary["pdp_ok"] == 5
    assert summary["pdp_skip"] == 1
    assert summary["image_fetches"] == 1
    assert summary["image_cache_hits"] == 1
    assert summary["blocked_heavy"] == 1
    assert summary["net_kb_est"] > 0


def test_append_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "metrics.jsonl"
    cfg = {"metrics_enabled": True, "metrics_jsonl_path": str(path)}
    usage_metrics.reset(cfg)
    summary = usage_metrics.to_summary(pdp_poll_minutes=1)
    usage_metrics.append_jsonl(cfg, summary)
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["timestamp_il"] == summary["timestamp_il"]
