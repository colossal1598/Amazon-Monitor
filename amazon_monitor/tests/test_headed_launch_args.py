"""Headed-mode Chrome args (2026-07-16: headless gets served offer-less PDPs)."""

import unittest

from browser_factory import headed_launch_chrome_args


class TestHeadedLaunchArgs(unittest.TestCase):
    def test_default_offscreen_and_anti_throttling(self) -> None:
        args = headed_launch_chrome_args({})
        # Chrome deprioritizes occluded windows; without these the off-screen headed
        # window re-creates the half-hydrated-page misreads headed mode exists to fix.
        self.assertIn("--disable-backgrounding-occluded-windows", args)
        self.assertIn("--disable-renderer-backgrounding", args)
        self.assertIn("--disable-background-timer-throttling", args)
        self.assertIn("--window-position=-32000,-32000", args)

    def test_visible_window_when_offscreen_disabled(self) -> None:
        args = headed_launch_chrome_args({"browser_window_offscreen": False})
        self.assertNotIn("--window-position=-32000,-32000", args)
        self.assertIn("--disable-renderer-backgrounding", args)

    def test_none_config(self) -> None:
        self.assertIn("--window-position=-32000,-32000", headed_launch_chrome_args(None))


if __name__ == "__main__":
    unittest.main()
