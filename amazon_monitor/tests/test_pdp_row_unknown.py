import unittest

from pdp_scraper import _pdp_row, _pdp_row_should_retry_unknown, _pdp_row_would_be_unknown


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

    def test_clean_seller_mismatch_is_confirmed_out(self) -> None:
        # C9: price parsed + non-empty 3P blob (no allowed match) is now settled
        # weak OOS evidence, not ambiguous unknown, so the state-engine debounce
        # can count it and eventually flip the DB.
        self.assertFalse(
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
        row = _pdp_row(
            "B011111111",
            title="Pokemon Card",
            price=19.99,
            shipping_text="FREE delivery",
            image_url=None,
            merchant_blob="Sold by Third Party LLC",
            allowed=["amazon.com"],
        )
        self.assertEqual(row["stock_confidence"], "confirmed_out")
        self.assertEqual(row["stock_reason"], "seller_mismatch")
        self.assertFalse(row["in_stock"])
        self.assertIsNone(row["price"])

    def test_seller_mismatch_empty_blob_stays_unknown(self) -> None:
        # Empty merchant blob = incomplete extraction, not evidence: stays unknown.
        self.assertTrue(
            _pdp_row_would_be_unknown(
                asin="B011111111",
                title="Pokemon Card",
                price=19.99,
                shipping_text="FREE delivery",
                image_url=None,
                merchant_blob="",
                allowed=["amazon.com"],
                explicit_oos=False,
            )
        )
        row = _pdp_row(
            "B011111111",
            title="Pokemon Card",
            price=19.99,
            shipping_text="FREE delivery",
            image_url=None,
            merchant_blob="",
            allowed=["amazon.com"],
        )
        self.assertEqual(row["stock_confidence"], "unknown")
        self.assertEqual(row["stock_reason"], "seller_mismatch")

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


class TestPdpRowShouldRetryUnknown(unittest.TestCase):
    """C4: the in-page unknown retry runs only for missing-price rows or empty blobs."""

    def test_no_pay_price_retries(self) -> None:
        self.assertTrue(
            _pdp_row_should_retry_unknown(
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

    def test_priceless_purchasable_retries(self) -> None:
        self.assertTrue(
            _pdp_row_should_retry_unknown(
                asin="B011111111",
                title="Pokemon Card",
                price=None,
                shipping_text="FREE delivery",
                image_url=None,
                merchant_blob="Sold by Amazon.com",
                allowed=["amazon.com"],
                explicit_oos=False,
                buybox_purchasable=True,
            )
        )

    def test_clean_seller_mismatch_skips_retry(self) -> None:
        # Price parsed + non-empty 3P blob with no Amazon match: settled, no retry.
        self.assertFalse(
            _pdp_row_should_retry_unknown(
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

    def test_seller_mismatch_empty_blob_retries(self) -> None:
        # Empty merchant blob means merchant info may not have loaded yet -> retry.
        self.assertTrue(
            _pdp_row_should_retry_unknown(
                asin="B011111111",
                title="Pokemon Card",
                price=19.99,
                shipping_text="FREE delivery",
                image_url=None,
                merchant_blob="",
                allowed=["amazon.com"],
                explicit_oos=False,
            )
        )

    def test_confirmed_in_skips_retry(self) -> None:
        self.assertFalse(
            _pdp_row_should_retry_unknown(
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


class TestPdpRowPricelessPurchasable(unittest.TestCase):
    def test_purchasable_allowed_seller_no_price_is_priceless(self) -> None:
        row = _pdp_row(
            "B011111111",
            title="Pokemon Card",
            price=None,
            shipping_text="FREE delivery",
            image_url=None,
            merchant_blob="Sold by Amazon.com",
            allowed=["amazon.com"],
            buybox_purchasable=True,
        )
        self.assertEqual(row["stock_confidence"], "unknown")
        self.assertEqual(row["stock_reason"], "priceless_purchasable")
        self.assertFalse(row["in_stock"])
        self.assertTrue(row["buybox_purchasable"])

    def test_not_purchasable_no_price_stays_no_pay_price(self) -> None:
        row = _pdp_row(
            "B011111111",
            title="Pokemon Card",
            price=None,
            shipping_text="",
            image_url=None,
            merchant_blob="",
            allowed=["amazon.com"],
            buybox_purchasable=False,
        )
        self.assertEqual(row["stock_reason"], "no_pay_price")

    def test_purchasable_but_seller_mismatch_is_not_priceless(self) -> None:
        row = _pdp_row(
            "B011111111",
            title="Pokemon Card",
            price=None,
            shipping_text="FREE delivery",
            image_url=None,
            merchant_blob="Sold by Third Party LLC",
            allowed=["amazon.com"],
            buybox_purchasable=True,
        )
        self.assertEqual(row["stock_reason"], "no_pay_price")


if __name__ == "__main__":
    unittest.main()
