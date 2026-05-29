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


def test_recovery_after_timely_cycle(tmp_path) -> None:
    store = TelemetryStore(str(tmp_path / "t.db"))
    cfg = _config()
    with patch("client_alerts.send_client_message") as send:
        client_alerts.maybe_alert("stalled", cfg, store, detail="slow")
        client_alerts.on_cycle_timing({"exceeds_poll_interval": False}, cfg, store)
        assert send.call_count == 2
        assert "חודשה" in send.call_args_list[1].args[0]
