"""fx refresh precedence on a FAILED live fetch: last-good memory -> disk cache ->
static fallback LAST. One failed Frankfurter call must never clobber a real known
rate with the configured fallback (2026-07-20 incident: fallback 3.7 replaced a live
~3.05 rate and every ILS line ran ~20% high for ~50 minutes; recurred 2026-07-25)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import fx_rate


def _config(tmp: str) -> dict:
    return {
        "fx_cache_path": str(Path(tmp) / "fx.json"),
        "fx_fallback_usd_ils": 3.7,
    }


class TestFxFetchFailPrecedence(unittest.TestCase):
    def setUp(self) -> None:
        fx_rate._cached_usd_ils = None

    def tearDown(self) -> None:
        fx_rate._cached_usd_ils = None

    def test_failed_fetch_keeps_last_good_in_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fx_rate._cached_usd_ils = 3.0541
            with patch.object(fx_rate, "_fetch_usd_ils", return_value=None):
                fx_rate._fetch_and_cache(_config(tmp))
            self.assertAlmostEqual(fx_rate._cached_usd_ils, 3.0541)

    def test_failed_fetch_prefers_disk_cache_over_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _config(tmp)
            fx_rate._write_cache_file(cfg, 3.11)
            with patch.object(fx_rate, "_fetch_usd_ils", return_value=None):
                fx_rate._fetch_and_cache(cfg)
            self.assertAlmostEqual(fx_rate._cached_usd_ils, 3.11)

    def test_fallback_used_only_when_nothing_known(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(fx_rate, "_fetch_usd_ils", return_value=None):
                fx_rate._fetch_and_cache(_config(tmp))
            self.assertAlmostEqual(fx_rate._cached_usd_ils, 3.7)

    def test_successful_fetch_still_updates_memory_and_disk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _config(tmp)
            with patch.object(fx_rate, "_fetch_usd_ils", return_value=3.2):
                fx_rate._fetch_and_cache(cfg)
            self.assertAlmostEqual(fx_rate._cached_usd_ils, 3.2)
            fx_rate._cached_usd_ils = None
            fx_rate._load_cache_file(cfg)
            self.assertAlmostEqual(fx_rate._cached_usd_ils, 3.2)


if __name__ == "__main__":
    unittest.main()
