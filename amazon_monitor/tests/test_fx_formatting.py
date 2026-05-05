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
        self.assertEqual(ils, " (~₪365 est)")
        self.assertEqual(full, "$100.00 (~₪365 est)")


class TestFormatMessagePriceDrop(unittest.TestCase):
    def setUp(self) -> None:
        _reset_fx_globals()

    @patch.object(fx_rate, "get_usd_ils", return_value=3.65)
    def test_price_drop_arrow(self, _mock: object) -> None:
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
        self.assertIn("$100.00 (~₪365 est) -> $80.00 (~₪292 est)", msg)


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
