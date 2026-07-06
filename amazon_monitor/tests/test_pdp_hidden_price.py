import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from pdp_scraper import (
    _extract_hidden_buybox_price_async,
    _parse_hidden_buybox_amount,
)
from tests.pdp_fixture_helpers import extract_pay_price_from_html

POSTER_HTML = Path(
    r"c:\Users\Eyal\Desktop\Amazon Example\oster Collection _ Toys & Games.html"
)
OOS_HTML = Path(r"c:\Users\Eyal\Desktop\Amazon Example\5 Booster Packs _ Toys & Games.html")


class TestHiddenBuyboxPrice(unittest.TestCase):
    def test_parse_amount_variants(self) -> None:
        self.assertEqual(_parse_hidden_buybox_amount("119.99"), 119.99)
        self.assertEqual(_parse_hidden_buybox_amount("90.0"), 90.0)
        self.assertIsNone(_parse_hidden_buybox_amount(""))
        self.assertIsNone(_parse_hidden_buybox_amount("0"))

    def test_poster_html_has_pay_price(self) -> None:
        if not POSTER_HTML.is_file():
            self.skipTest("poster fixture not on disk")
        html = POSTER_HTML.read_text(encoding="utf-8", errors="replace")
        self.assertEqual(extract_pay_price_from_html(html), 119.99)

    def test_oos_html_has_no_qualified_hidden_price(self) -> None:
        if not OOS_HTML.is_file():
            self.skipTest("oos fixture not on disk")
        html = OOS_HTML.read_text(encoding="utf-8", errors="replace")
        self.assertNotIn("customerVisiblePrice", html)
        self.assertNotIn("qualifiedBuybox", html)
        self.assertIsNone(extract_pay_price_from_html(html))

    def test_hidden_price_when_visible_offscreen_stripped(self) -> None:
        if not POSTER_HTML.is_file():
            self.skipTest("poster fixture not on disk")
        html = POSTER_HTML.read_text(encoding="utf-8", errors="replace")
        stripped = html.replace('class="a-offscreen"', 'class="a-offscreen-removed"')
        self.assertEqual(extract_pay_price_from_html(stripped), 119.99)


class TestHiddenBuyboxPriceAsync(unittest.IsolatedAsyncioTestCase):
    async def test_reads_hidden_input_inside_qualified_buybox(self) -> None:
        root = MagicMock()
        hidden = MagicMock()
        hidden.get_attribute = AsyncMock(return_value="119.99")

        async def root_qs(sel: str):
            if "customerVisiblePrice" in sel:
                return hidden
            return None

        root.query_selector = AsyncMock(side_effect=root_qs)
        page = MagicMock()
        page.query_selector = AsyncMock(return_value=root)
        self.assertEqual(await _extract_hidden_buybox_price_async(page), 119.99)

    async def test_no_qualified_buybox_returns_none(self) -> None:
        page = MagicMock()
        page.query_selector = AsyncMock(return_value=None)
        self.assertIsNone(await _extract_hidden_buybox_price_async(page))


if __name__ == "__main__":
    unittest.main()
