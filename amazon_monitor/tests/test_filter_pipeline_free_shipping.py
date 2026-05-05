import unittest

from filter_pipeline import filter_free_shipping_candidates, row_has_free_shipping


def _row(asin: str, shipping_text: str, **extra: str) -> dict:
    base = {
        "asin": asin,
        "title": "Pokemon TCG Booster Box",
        "price": 24.99,
        "in_stock": True,
        "shipping_text": shipping_text,
        "seller_text": "",
        "availability_text": "",
    }
    base.update(extra)
    return base


class TestFreeShippingFilter(unittest.TestCase):
    def test_free_delivery_passes(self) -> None:
        self.assertTrue(row_has_free_shipping(_row("B011111111", "FREE delivery Mon, Jan 1")))

    def test_free_shipping_phrase_passes(self) -> None:
        self.assertTrue(row_has_free_shipping(_row("B011111111", "FREE Shipping by Amazon")))

    def test_hebrew_free_phrase_passes(self) -> None:
        self.assertTrue(row_has_free_shipping(_row("B011111111", "משלוח חינם")))

    def test_paid_delivery_rejected(self) -> None:
        self.assertFalse(row_has_free_shipping(_row("B011111111", "$5.99 delivery Mon")))

    def test_empty_shipping_rejected(self) -> None:
        self.assertFalse(row_has_free_shipping(_row("B011111111", "")))

    def test_filter_keeps_only_free(self) -> None:
        rows = [
            _row("B011111111", "FREE delivery Tomorrow"),
            _row("B022222222", "$3.99 delivery Mon"),
            _row("B033333333", "FREE Shipping by Amazon"),
            _row("B044444444", ""),
        ]
        kept = filter_free_shipping_candidates(rows)
        self.assertEqual({r["asin"] for r in kept}, {"B011111111", "B033333333"})

    def test_filter_uses_seller_text_as_fallback_blob(self) -> None:
        row = _row("B011111111", "", seller_text="Sold by Amazon.com\nFREE delivery to Israel")
        self.assertTrue(row_has_free_shipping(row))


if __name__ == "__main__":
    unittest.main()
