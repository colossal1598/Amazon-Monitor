"""Continue-shopping interstitial dismiss on PDP."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from pdp_scraper import (
    _dismiss_continue_shopping_async,
    detect_soft_captcha_from_html,
)

INTERSTITIAL_HTML = """<!DOCTYPE html><html><head>
<script src="https://images-na.ssl-images-amazon.com/images/G/01/csminstrumentation/csm-captcha-instrumentation.min.js"></script>
</head><body>
<h4>Click the button below to continue shopping</h4>
<button class="a-button-text">Continue shopping</button>
</body></html>"""

PDP_HTML = """<!DOCTYPE html><html><body>
<span id="productTitle">Pokemon TCG Box</span>
</body></html>"""


class TestDismissContinueShoppingAsync(unittest.IsolatedAsyncioTestCase):
    async def test_no_clicks_when_not_interstitial(self) -> None:
        page = MagicMock()
        with patch(
            "pdp_scraper._is_continue_shopping_interstitial_async",
            new_callable=AsyncMock,
            return_value=False,
        ):
            clicks = await _dismiss_continue_shopping_async(page, max_clicks=3, asin="B011111111")
        self.assertEqual(clicks, 0)

    async def test_clicks_until_clear(self) -> None:
        page = MagicMock()
        states = [True, True, False]

        async def interstitial(_page: MagicMock) -> bool:
            return states.pop(0) if states else False

        with patch(
            "pdp_scraper._is_continue_shopping_interstitial_async",
            side_effect=interstitial,
        ), patch(
            "pdp_scraper._click_continue_shopping_once_async",
            new_callable=AsyncMock,
            return_value=True,
        ) as click_mock:
            clicks = await _dismiss_continue_shopping_async(page, max_clicks=3, asin="B011111111")
        self.assertEqual(clicks, 2)
        self.assertEqual(click_mock.await_count, 2)

    async def test_stops_when_click_fails(self) -> None:
        page = MagicMock()
        with patch(
            "pdp_scraper._is_continue_shopping_interstitial_async",
            new_callable=AsyncMock,
            return_value=True,
        ), patch(
            "pdp_scraper._click_continue_shopping_once_async",
            new_callable=AsyncMock,
            return_value=False,
        ):
            clicks = await _dismiss_continue_shopping_async(page, max_clicks=3)
        self.assertEqual(clicks, 0)


class TestContinueShoppingDetection(unittest.TestCase):
    def test_interstitial_html_detected(self) -> None:
        self.assertTrue(detect_soft_captcha_from_html(INTERSTITIAL_HTML))

    def test_pdp_html_not_interstitial(self) -> None:
        self.assertFalse(detect_soft_captcha_from_html(PDP_HTML))


if __name__ == "__main__":
    unittest.main()
