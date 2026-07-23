"""client_alerts rate limits and scrape degradation."""

from __future__ import annotations

from unittest.mock import patch

import client_alerts
from telemetry_store import TelemetryStore


def _config(**overrides):
    base = {
        "client_alerts_enabled": True,
        "wa_client_to": "1234567890@s.whatsapp.net",
        "client_alert_cooldown_minutes": 30,
        "client_alert_max_per_window": 3,
        "client_alert_window_hours": 6,
        "client_alert_scrape_fail_min": 2,
        "client_alert_scrape_fail_ratio": 0.5,
        "client_alert_chronic_stall_consecutive": 5,
    }
    base.update(overrides)
    return base


def test_disabled_by_default(tmp_path) -> None:
    # Operator decision 2026-07-23: operational error messages to the client's WhatsApp
    # are OFF unless client_alerts_enabled is explicitly set to true.
    store = TelemetryStore(str(tmp_path / "t.db"))
    cfg = _config()
    del cfg["client_alerts_enabled"]
    with patch("client_alerts.send_client_message") as send:
        assert client_alerts.maybe_alert("captcha", cfg, store, detail="test") is False
        assert send.call_count == 0


def test_maybe_alert_sends_once(tmp_path) -> None:
    store = TelemetryStore(str(tmp_path / "t.db"))
    cfg = _config()
    with patch("client_alerts.send_client_message") as send:
        assert client_alerts.maybe_alert("captcha", cfg, store, detail="test") is True
        assert client_alerts.maybe_alert("captcha", cfg, store, detail="test") is False
        assert send.call_count == 1


def test_scrape_degraded_pdp_ratio() -> None:
    rows = [
        {"asin": "A", "_skip_update": True, "skip_reason": "navigation_failed"},
        {"asin": "B", "_skip_update": True, "skip_reason": "parse_failed"},
        {"asin": "C", "in_stock": 1, "price": 10},
    ]
    with patch("client_alerts.send_client_message") as send:
        client_alerts.check_scrape_degraded(rows, {}, 3, _config(), None)
        send.assert_called_once()


def test_scrape_degraded_not_filter_only() -> None:
    """High aes_raw with zero candidates (filters) should not alert without scrape errors."""
    rows = [{"asin": "A", "in_stock": 1, "price": 10}]
    aes_outcome = {"navigation_ok": True, "cards_found": 5, "total_result_count": 100}
    with patch("client_alerts.send_client_message") as send:
        client_alerts.check_scrape_degraded(rows, aes_outcome, 1, _config(), None)
        send.assert_not_called()


def test_count_pdp_scrape_errors() -> None:
    rows = [
        {"_skip_update": True, "skip_reason": "captcha"},
        {"_skip_update": True, "skip_reason": "navigation_failed"},
        {"_skip_update": True, "skip_reason": "parse_failed"},
    ]
    total, reasons = client_alerts.count_pdp_scrape_errors(rows)
    assert total == 2
    assert reasons["navigation_failed"] == 1
    assert reasons["parse_failed"] == 1


def test_scrape_degraded_aes_needs_consecutive_failures(tmp_path) -> None:
    """A single isolated AES/SERP hiccup (now fast-failing and self-recovering) must not alert."""
    store = TelemetryStore(str(tmp_path / "t.db"))
    cfg = _config(client_alert_aes_fail_consecutive=3)
    rows = [{"asin": "A", "in_stock": 1, "price": 10}]
    aes_bad = {"navigation_ok": False, "cards_found": 0, "total_result_count": None}
    aes_ok = {"navigation_ok": True, "cards_found": 5, "total_result_count": 100}
    with patch("client_alerts.send_client_message") as send:
        client_alerts.check_scrape_degraded(rows, aes_bad, 1, cfg, store)
        client_alerts.check_scrape_degraded(rows, aes_bad, 1, cfg, store)
        send.assert_not_called()
        client_alerts.check_scrape_degraded(rows, aes_bad, 1, cfg, store)
        send.assert_called_once()

    # A subsequent healthy cycle resets the counter.
    with patch("client_alerts.send_client_message") as send:
        client_alerts.check_scrape_degraded(rows, aes_ok, 1, cfg, store)
        client_alerts.check_scrape_degraded(rows, aes_bad, 1, cfg, store)
        client_alerts.check_scrape_degraded(rows, aes_bad, 1, cfg, store)
        send.assert_not_called()


def test_stalled_needs_consecutive_cycles(tmp_path) -> None:
    """One over-poll-interval cycle (e.g. a single AES hiccup) should not page anyone."""
    store = TelemetryStore(str(tmp_path / "t.db"))
    cfg = _config(client_alert_stall_min_consecutive=2)
    summary = {"exceeds_poll_interval": True, "total_sec": 130}
    with patch("client_alerts.send_client_message") as send:
        client_alerts.on_cycle_timing(summary, cfg, store)
        send.assert_not_called()
        client_alerts.on_cycle_timing(summary, cfg, store)
        send.assert_called_once()

    # A timely cycle afterwards resets the streak. The "all clear" recovery ping is
    # off by default (judged noise on its own), so nothing further should be sent.
    with patch("client_alerts.send_client_message") as send:
        client_alerts.on_cycle_timing({"exceeds_poll_interval": False}, cfg, store)
        send.assert_not_called()


def test_recovery_disabled_by_default(tmp_path) -> None:
    store = TelemetryStore(str(tmp_path / "t.db"))
    cfg = _config()
    with patch("client_alerts.send_client_message") as send:
        client_alerts.maybe_alert("stalled", cfg, store, detail="slow")
        client_alerts.on_cycle_timing({"exceeds_poll_interval": False}, cfg, store)
        assert send.call_count == 1


def test_recovery_when_explicitly_enabled(tmp_path) -> None:
    store = TelemetryStore(str(tmp_path / "t.db"))
    cfg = _config(client_alert_recovery_enabled=True)
    with patch("client_alerts.send_client_message") as send:
        client_alerts.maybe_alert("stalled", cfg, store, detail="slow")
        client_alerts.on_cycle_timing({"exceeds_poll_interval": False}, cfg, store)
        assert send.call_count == 2
        assert "חודשה" in send.call_args_list[1].args[0]
