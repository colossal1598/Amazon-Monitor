import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from pdp_scraper import (
    _PDP_PRICE_LEAF_SELECTORS,
    _PDP_TITLE_READY_SELECTORS,
    _resolve_buybox_price_async,
    _resolve_title_async,
)


class TestResolveBuyboxPriceAsync(unittest.IsolatedAsyncioTestCase):
    async def test_no_wait_when_first_extract_hits(self) -> None:
        page = MagicMock()
        page.wait_for_selector = AsyncMock()
        with patch("pdp_scraper._extract_pdp_price_async", new_callable=AsyncMock, return_value=19.99):
            price, wait_used = await _resolve_buybox_price_async(page, 4_000, asin="B011111111")
        self.assertEqual(price, 19.99)
        self.assertFalse(wait_used)
        page.wait_for_selector.assert_not_called()

    async def test_wait_on_miss_then_second_extract(self) -> None:
        page = MagicMock()
        page.wait_for_selector = AsyncMock()

        async def qs(sel: str):
            return MagicMock() if "buybox" in sel or "corePrice" in sel else None

        page.query_selector = AsyncMock(side_effect=qs)
        extracts = AsyncMock(side_effect=[None, 21.99])
        with patch("pdp_scraper._extract_pdp_price_async", extracts):
            price, wait_used = await _resolve_buybox_price_async(page, 4_000, asin="B011111111")
        self.assertEqual(price, 21.99)
        self.assertTrue(wait_used)
        page.wait_for_selector.assert_awaited_once()
        self.assertEqual(page.wait_for_selector.await_args.kwargs.get("timeout"), 4_000)
        self.assertEqual(extracts.await_count, 2)

    async def test_wait_even_when_buybox_not_yet_attached(self) -> None:
        page = MagicMock()
        page.wait_for_selector = AsyncMock()
        page.query_selector = AsyncMock(return_value=None)
        with patch("pdp_scraper._extract_pdp_price_async", new_callable=AsyncMock, return_value=None):
            price, wait_used = await _resolve_buybox_price_async(page, 4_000, asin="B011111111")
        self.assertIsNone(price)
        self.assertTrue(wait_used)
        page.wait_for_selector.assert_awaited_once()

    async def test_skip_wait_when_explicit_oos_path(self) -> None:
        page = MagicMock()
        page.wait_for_selector = AsyncMock()
        with patch("pdp_scraper._extract_pdp_price_async", new_callable=AsyncMock, return_value=None):
            price, wait_used = await _resolve_buybox_price_async(
                page, 4_000, asin="B011111111", skip_wait=True
            )
        self.assertIsNone(price)
        self.assertFalse(wait_used)
        page.wait_for_selector.assert_not_called()


class TestResolveTitleAsync(unittest.IsolatedAsyncioTestCase):
    async def test_no_wait_when_title_present(self) -> None:
        page = MagicMock()
        page.wait_for_selector = AsyncMock()
        with patch("pdp_scraper._extract_pdp_title_async", new_callable=AsyncMock, return_value="Pokemon Box"):
            title, wait_used = await _resolve_title_async(page, 15_000)
        self.assertEqual(title, "Pokemon Box")
        self.assertFalse(wait_used)
        page.wait_for_selector.assert_not_called()

    async def test_wait_on_empty_title(self) -> None:
        page = MagicMock()
        page.title = AsyncMock(return_value="")
        page.wait_for_selector = AsyncMock()
        extracts = AsyncMock(side_effect=["", "Resolved Title"])
        with patch("pdp_scraper._extract_pdp_title_async", extracts):
            title, wait_used = await _resolve_title_async(page, 15_000)
        self.assertEqual(title, "Resolved Title")
        self.assertTrue(wait_used)
        page.wait_for_selector.assert_awaited_once_with(
            _PDP_TITLE_READY_SELECTORS,
            state="attached",
            timeout=15_000,
        )


class TestPdpSelectorConstants(unittest.TestCase):
    def test_title_selectors_exclude_price_divs(self) -> None:
        self.assertNotIn("corePrice", _PDP_TITLE_READY_SELECTORS)
        self.assertNotIn("a-price-whole", _PDP_TITLE_READY_SELECTORS)


if __name__ == "__main__":
    unittest.main()
