import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType
from unittest.mock import patch

# Allow importing fx_rate / webhook_sender when the test runner's Python has no requests.
if "requests" not in sys.modules:
    _req = ModuleType("requests")

    def _no_network(*_a, **_k):
        raise AssertionError("unexpected HTTP in unit test (patch the caller)")

    _req.get = _no_network  # type: ignore[method-assign]
    _req.post = _no_network  # type: ignore[method-assign]
    sys.modules["requests"] = _req

import fx_rate  # noqa: E402
import webhook_sender  # noqa: E402


def _reset_fx_globals() -> None:
    fx_rate._cached_usd_ils = None  # noqa: SLF001
    fx_rate._monitor_ticks = 0  # noqa: SLF001
    fx_rate._search_ticks = 0  # noqa: SLF001


class TestFormatUsd(unittest.TestCase):
    def test_none_and_invalid(self) -> None:
        self.assertEqual(webhook_sender._format_usd(None), "לא זמין")
        self.assertEqual(webhook_sender._format_usd("x"), "לא זמין")

    def test_rounds_two_decimals(self) -> None:
        self.assertEqual(webhook_sender._format_usd(50.0), "$50.00")
        self.assertEqual(webhook_sender._format_usd(49.9), "$49.90")


class TestPriceLineParts(unittest.TestCase):
    def setUp(self) -> None:
        _reset_fx_globals()

    def test_usd_only_when_rate_missing(self) -> None:
        cfg = {"fx_enabled": False}
        full, usd_only, ils = webhook_sender._price_line_parts(10.0, cfg)
        self.assertEqual(full, "$10.00")
        self.assertEqual(usd_only, "$10.00")
        self.assertEqual(ils, "")

    @patch.object(fx_rate, "get_usd_ils", return_value=3.65)
    def test_dual_when_rate_known(self, _mock: object) -> None:
        cfg = {"fx_enabled": True}
        full, usd_only, ils = webhook_sender._price_line_parts(100.0, cfg)
        self.assertEqual(usd_only, "$100.00")
        self.assertEqual(ils, " (כ- 365₪)")
        self.assertEqual(full, "$100.00 (כ- 365₪)")


class TestFormatMessagePriceDrop(unittest.TestCase):
    def setUp(self) -> None:
        _reset_fx_globals()

    @patch.object(fx_rate, "get_usd_ils", return_value=3.65)
    def test_price_drop_shows_new_price_only(self, _mock: object) -> None:
        cfg = {"fx_enabled": True}
        msg = webhook_sender._format_message(
            {
                "type": "price_drop",
                "title": "T",
                "old_price": 100.0,
                "new_price": 80.0,
                "price": 80.0,
            },
            cfg,
        )
        self.assertIn("$80.00 (כ- 292₪)", msg)
        self.assertNotIn("->", msg)
        self.assertNotIn("$100.00", msg)


class TestFormatMessagePriceBelowTarget(unittest.TestCase):
    def setUp(self) -> None:
        _reset_fx_globals()

    def test_price_below_target_mentions_target_and_shipping(self) -> None:
        cfg = {"fx_enabled": False}
        msg = webhook_sender._format_message(
            {
                "type": "price_below_target",
                "title": "Pokemon Booster Box",
                "price": 15.0,
                "new_price": 15.0,
                "target_price": 20.0,
                "shipping": "משלוח: $7.99",
                "affiliate_link": "https://www.amazon.com/dp/B011111111?tag=x",
            },
            cfg,
        )
        self.assertIn("20.00$", msg)
        self.assertIn("$15.00", msg)
        self.assertIn("משלוח: $7.99", msg)
        self.assertIn("https://www.amazon.com/dp/B011111111?tag=x", msg)

    def test_price_below_target_is_user_overridable(self) -> None:
        self.assertIn("price_below_target", webhook_sender.USER_OVERRIDABLE_MESSAGE_KEYS)
        cfg = {
            "fx_enabled": False,
            "wa_message_templates": {"price_below_target": "custom {target_price}"},
        }
        msg = webhook_sender._format_message(
            {"type": "price_below_target", "target_price": 12.5}, cfg
        )
        self.assertEqual(msg, "custom 12.50")


class TestBumpSearchTickRefreshCadence(unittest.TestCase):
    def setUp(self) -> None:
        _reset_fx_globals()

    def test_fetch_on_first_tick_then_every_n(self) -> None:
        cfg = {
            "fx_enabled": True,
            "fx_refresh_every_runs": 3,
            "fx_fallback_usd_ils": 3.7,
        }
        with TemporaryDirectory() as td:
            cfg["fx_cache_path"] = str(Path(td) / "fx.json")
            with patch.object(fx_rate, "_fetch_usd_ils", return_value=3.5) as fetch:
                fx_rate.bump_search_tick(cfg)
                fx_rate.bump_search_tick(cfg)
                fx_rate.bump_search_tick(cfg)
            self.assertEqual(fetch.call_count, 2)
            data = json.loads(Path(cfg["fx_cache_path"]).read_text(encoding="utf-8"))
            self.assertAlmostEqual(data["usd_ils"], 3.5)

    def test_fx_disabled_no_fetch(self) -> None:
        cfg = {"fx_enabled": False, "fx_refresh_every_runs": 1}
        with patch.object(fx_rate, "_fetch_usd_ils", return_value=3.5) as fetch:
            fx_rate.bump_search_tick(cfg)
            fx_rate.bump_search_tick(cfg)
        self.assertEqual(fetch.call_count, 0)


if __name__ == "__main__":
    unittest.main()
