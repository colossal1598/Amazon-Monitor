import unittest

from pdp_scraper import _pdp_row_would_be_unknown


class TestPdpRowWouldBeUnknown(unittest.TestCase):
    def test_confirmed_in_is_not_unknown(self) -> None:
        self.assertFalse(
            _pdp_row_would_be_unknown(
                asin="B011111111",
                title="Pokemon Card",
                price=19.99,
                shipping_text="FREE delivery",
                image_url=None,
                merchant_blob="Sold by Amazon.com",
                allowed=["amazon.com"],
                explicit_oos=False,
            )
        )

    def test_explicit_oos_is_not_unknown(self) -> None:
        self.assertFalse(
            _pdp_row_would_be_unknown(
                asin="B011111111",
                title="Pokemon Card",
                price=None,
                shipping_text="",
                image_url=None,
                merchant_blob="",
                allowed=["amazon.com"],
                explicit_oos=True,
            )
        )

    def test_seller_mismatch_is_unknown(self) -> None:
        self.assertTrue(
            _pdp_row_would_be_unknown(
                asin="B011111111",
                title="Pokemon Card",
                price=19.99,
                shipping_text="FREE delivery",
                image_url=None,
                merchant_blob="Sold by Third Party LLC",
                allowed=["amazon.com"],
                explicit_oos=False,
            )
        )

    def test_missing_price_with_title_and_no_oos_signal_is_unknown(self) -> None:
        """Title without price and without an explicit OOS signal must be treated
        as unknown so the in-scrape retry runs — price often hydrates after the
        buy button (preorder buyboxes). Previously classified confirmed_out,
        which skipped the retry and missed a live preorder wave (B0GYTRYV7P)."""
        self.assertTrue(
            _pdp_row_would_be_unknown(
                asin="B011111111",
                title="Pokemon Card",
                price=None,
                shipping_text="",
                image_url=None,
                merchant_blob="",
                allowed=["amazon.com"],
                explicit_oos=False,
            )
        )


if __name__ == "__main__":
    unittest.main()
