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
    usage_metrics.bump_blocked_url()
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
    assert summary["blocked_url"] == 1
    assert summary["net_kb_est"] > 0
    assert summary["net_bytes_image"] == 2048
    assert summary["gb_est_total"] == round(summary["net_kb_est"] * 1024 / 1_000_000_000.0, 3)


def test_content_length_from_headers() -> None:
    assert usage_metrics.content_length_from_headers({"content-length": "42"}) == 42
    assert usage_metrics.content_length_from_headers({"Content-Length": "7"}) == 7
    assert usage_metrics.content_length_from_headers({}) is None


def test_flush_meter_updates_summary_totals() -> None:
    usage_metrics.reset()
    meter = usage_metrics.BandwidthMeter()
    meter.set_phase("pdp")
    meter._add_bytes("https://www.amazon.com/dp/B001", 1000)
    meter._add_bytes("https://cdn.example.com/x.js", 500)
    meter.set_phase("aes")
    meter._add_bytes("https://www.amazon.com/s?k=test", 2000)
    usage_metrics.flush_meter(meter)
    usage_metrics.record_pdp_phase(1.0, 999_999, ok=1, skip=0)
    usage_metrics.record_aes_phase(0.5, 888_888)
    usage_metrics.record_image_fetch(300, cache_hit=False)
    summary = usage_metrics.to_summary(pdp_poll_minutes=4)
    assert summary["net_bytes_pdp"] == 1500
    assert summary["net_bytes_aes"] == 2000
    assert summary["net_bytes_total"] == 3500
    assert summary["net_bytes_image"] == 300
    assert summary["gb_est_total"] == round(3800 / 1_000_000_000.0, 3)
    assert summary["pdp_net_kb_est"] == round(1500 / 1024.0, 1)
    assert summary["aes_net_kb_est"] == round(2000 / 1024.0, 1)
    assert summary["net_kb_est"] == round(3800 / 1024.0, 1)


def test_telemetry_enabled_fallback(tmp_path) -> None:
    store = TelemetryStore(str(tmp_path / "telemetry.db"))
    cfg = {"metrics_enabled": True}
    cycle_id = store.begin_cycle(cfg)
    assert cycle_id > 0


def test_finish_cycle_persists_bandwidth_columns(tmp_path) -> None:
    store = TelemetryStore(str(tmp_path / "telemetry.db"))
    cfg = {"telemetry_enabled": True}
    usage_metrics.reset(cfg)
    meter = usage_metrics.BandwidthMeter()
    meter.set_phase("pdp")
    meter._add_bytes("https://www.amazon.com/dp/B001", 4096)
    usage_metrics.flush_meter(meter)
    usage_metrics.record_pdp_phase(2.0, 0, ok=1, skip=0)
    usage_metrics.record_image_fetch(512, cache_hit=False)
    summary = usage_metrics.to_summary(pdp_poll_minutes=4)
    summary.update(
        {
            "pdp_watch": 0,
            "watch_rows": 0,
            "alerts_sent": 0,
            "exceeds_poll_interval": False,
            "pdp_scrape_errors": 0,
            "pdp_scrape_error_reasons_json": {},
            "aes_scrape_outcome_json": {},
            "pdp_state_summary_json": {},
            "aes_state_summary_json": {},
        }
    )
    cycle_id = store.begin_cycle(cfg)
    store.finish_cycle(cycle_id, summary, cfg)
    row = store.conn.execute(
        """
        SELECT net_bytes_total, net_bytes_pdp, net_bytes_image, gb_est_total, blocked_url
        FROM cycle_stats WHERE id = ?
        """,
        (cycle_id,),
    ).fetchone()
    assert row is not None
    assert int(row[0]) == 4096
    assert int(row[1]) == 4096
    assert int(row[2]) == 512
    assert float(row[3]) == summary["gb_est_total"]
    assert int(row[4]) == 0
    daily = store.conn.execute(
        "SELECT bytes_total, bytes_pdp, bytes_image, cycles FROM daily_bandwidth"
    ).fetchone()
    assert daily is not None
    assert int(daily[0]) == 4096 + 512
    assert int(daily[1]) == 4096
    assert int(daily[2]) == 512
    assert int(daily[3]) == 1
