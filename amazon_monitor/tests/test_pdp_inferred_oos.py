import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from pdp_scraper import (
    _detect_inferred_oos_async,
    _explicit_oos_from_text,
    _pdp_row,
)


class TestExplicitOosFromText(unittest.TestCase):
    def test_currently_unavailable(self) -> None:
        self.assertTrue(_explicit_oos_from_text("Currently unavailable."))

    def test_see_all_buying_options(self) -> None:
        self.assertTrue(_explicit_oos_from_text("See all buying options"))


class TestDetectInferredOosAsync(unittest.IsolatedAsyncioTestCase):
    async def test_see_all_options_without_price(self) -> None:
        page = MagicMock()
        page.query_selector = AsyncMock(
            side_effect=lambda sel: MagicMock()
            if "see-all-buying" in sel or "buying-options" in sel
            else None
        )
        inferred, reason = await _detect_inferred_oos_async(page, availability_text="")
        self.assertTrue(inferred)
        self.assertEqual(reason, "inferred_oos_see_all_options")

    async def test_no_purchase_action_with_buybox_shell(self) -> None:
        page = MagicMock()

        async def qs(sel: str):
            if sel in ("#add-to-cart-button", "#buy-now-button", "input[name='submit.add-to-cart']", "#submit.add-to-cart"):
                return None
            if sel in ("#qualifiedBuybox", "#desktop_buybox", "#buybox", "#corePrice_feature_div"):
                return MagicMock()
            return None

        page.query_selector = AsyncMock(side_effect=qs)
        with patch(
            "pdp_scraper._extract_availability_text_async",
            new_callable=AsyncMock,
            return_value="",
        ):
            inferred, reason = await _detect_inferred_oos_async(page, availability_text="")
        self.assertTrue(inferred)
        self.assertEqual(reason, "inferred_oos_no_purchase_action")

    async def test_no_infer_when_purchase_button_enabled(self) -> None:
        page = MagicMock()
        btn = MagicMock()
        btn.get_attribute = AsyncMock(return_value=None)

        async def qs(sel: str):
            if sel == "#add-to-cart-button":
                return btn
            return None

        page.query_selector = AsyncMock(side_effect=qs)
        with patch(
            "pdp_scraper._extract_availability_text_async",
            new_callable=AsyncMock,
            return_value="",
        ):
            inferred, reason = await _detect_inferred_oos_async(page, availability_text="")
        self.assertFalse(inferred)
        self.assertIsNone(reason)

    async def test_buybox_text_unavailable(self) -> None:
        page = MagicMock()
        buybox = MagicMock()
        buybox.inner_text = AsyncMock(return_value="Currently unavailable. We don't know when or if this item will be back in stock.")

        async def qs(sel: str):
            if sel == "#qualifiedBuybox":
                return buybox
            return None

        page.query_selector = AsyncMock(side_effect=qs)
        with patch(
            "pdp_scraper._extract_availability_text_async",
            new_callable=AsyncMock,
            return_value="",
        ):
            inferred, reason = await _detect_inferred_oos_async(page, availability_text="")
        self.assertTrue(inferred)
        self.assertEqual(reason, "explicit_oos_buybox_text")


class TestPdpRowNoPayPrice(unittest.TestCase):
    def test_title_without_price_is_confirmed_out(self) -> None:
        row = _pdp_row(
            "B011111111",
            title="Pokemon Box",
            price=None,
            shipping_text="",
            image_url=None,
            merchant_blob="",
            allowed=["amazon.com"],
            explicit_oos=True,
        )
        self.assertEqual(row["stock_confidence"], "confirmed_out")
        self.assertFalse(row["in_stock"])

    def test_title_without_price_and_no_oos_signal_is_unknown(self) -> None:
        """Purchasable-but-priceless pages (price still hydrating, e.g. preorders)
        must stay ambiguous: classifying them confirmed_out skipped the unknown
        retry and fed false evidence into the OOS debounce (missed live preorder
        wave, B0GYTRYV7P 2026-07-13)."""
        row = _pdp_row(
            "B011111111",
            title="Pokemon Box",
            price=None,
            shipping_text="",
            image_url=None,
            merchant_blob="",
            allowed=["amazon.com"],
            explicit_oos=False,
        )
        self.assertEqual(row["stock_confidence"], "unknown")
        self.assertEqual(row["stock_reason"], "no_pay_price")
        self.assertFalse(row["in_stock"])


if __name__ == "__main__":
    unittest.main()
