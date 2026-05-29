"""usage_metrics summary (Israel timestamps)."""

from __future__ import annotations

from datetime import datetime

import usage_metrics
from telemetry_store import TelemetryStore


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


def test_telemetry_enabled_fallback(tmp_path) -> None:
    store = TelemetryStore(str(tmp_path / "telemetry.db"))
    cfg = {"metrics_enabled": True}
    cycle_id = store.begin_cycle(cfg)
    assert cycle_id > 0
