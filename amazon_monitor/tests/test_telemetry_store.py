"""telemetry_store cycle stats and debug events."""

from __future__ import annotations

import json

import usage_metrics
from telemetry_store import TelemetryStore


def test_begin_finish_cycle(tmp_path) -> None:
    db = tmp_path / "telemetry.db"
    store = TelemetryStore(str(db))
    cfg = {"telemetry_enabled": True}
    usage_metrics.reset(cfg)
    usage_metrics.record_pdp_phase(1.0, 1024, ok=2, skip=0)
    cycle_id = store.begin_cycle(cfg)
    assert cycle_id > 0
    store.debug(cycle_id, "pdp_watch_no_row", asin="B011111111")
    summary = usage_metrics.to_summary(pdp_poll_minutes=4)
    summary.update(
        {
            "pdp_watch": 1,
            "watch_rows": 1,
            "alerts_sent": 0,
            "exceeds_poll_interval": False,
            "pdp_scrape_errors": 0,
            "pdp_scrape_error_reasons_json": {},
            "aes_scrape_outcome_json": {},
            "pdp_state_summary_json": {},
            "aes_state_summary_json": {},
        }
    )
    store.finish_cycle(cycle_id, summary, cfg)
    row = store.conn.execute("SELECT total_sec, pdp_ok, exceeds_poll_interval FROM cycle_stats WHERE id = ?", (cycle_id,)).fetchone()
    assert row is not None
    assert int(row[1]) == 2
    assert int(row[2]) == 0
    events = store.conn.execute("SELECT event FROM debug_events WHERE cycle_id = ?", (cycle_id,)).fetchall()
    assert [e[0] for e in events] == ["pdp_watch_no_row"]


def test_prune_old_rows(tmp_path) -> None:
    from datetime import datetime, timedelta

    from telemetry_store import _israel_tz

    db = tmp_path / "telemetry.db"
    store = TelemetryStore(str(db))
    cfg = {"telemetry_enabled": True, "telemetry_stats_keep_days": 7, "telemetry_events_keep_days": 7}
    cycle_id = store.begin_cycle(cfg)
    store.debug(cycle_id, "test_event")
    store.finish_cycle(cycle_id, {"exceeds_poll_interval": False}, cfg)
    old_il = (datetime.now(_israel_tz()) - timedelta(days=30)).isoformat(timespec="seconds")
    store.conn.execute("UPDATE cycle_stats SET recorded_at_il = ? WHERE id = ?", (old_il, cycle_id))
    store.conn.execute("UPDATE debug_events SET recorded_at_il = ?", (old_il,))
    store.conn.commit()
    store._last_prune_il = None
    store.maybe_prune(cfg)
    count = store.conn.execute("SELECT COUNT(*) FROM cycle_stats").fetchone()[0]
    assert count == 0
