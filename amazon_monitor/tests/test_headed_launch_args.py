"""Headed-mode Chrome args (2026-07-16: headless gets served offer-less PDPs)."""

import unittest

from browser_factory import headed_launch_chrome_args


class TestHeadedLaunchArgs(unittest.TestCase):
    def test_default_is_plain_launch_no_extra_args(self) -> None:
        # REGRESSION 2026-07-16: the off-screen window + anti-throttling flags
        # brought skeletons AND captchas back within hours — they fingerprint as
        # automation. Plain headed Chrome (zero extra args) is what runs clean.
        self.assertEqual(headed_launch_chrome_args({}), [])
        self.assertEqual(headed_launch_chrome_args(None), [])

    def test_offscreen_opt_in_gets_window_and_anti_throttling(self) -> None:
        args = headed_launch_chrome_args({"browser_window_offscreen": True})
        self.assertIn("--window-position=-32000,-32000", args)
        self.assertIn("--disable-backgrounding-occluded-windows", args)
        self.assertIn("--disable-renderer-backgrounding", args)
        self.assertIn("--disable-background-timer-throttling", args)


if __name__ == "__main__":
    unittest.main()
