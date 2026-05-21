"""AES LLC pipeline: optional stage-1 shipping gate."""

import unittest

from filter_pipeline import run_search_filter_pipeline


class TestAesPipelineShipping(unittest.TestCase):
    def test_no_shipping_row_kept_when_shipping_optional(self) -> None:
        raw = [
            {
                "asin": "B012345678",
                "title": "Pokemon TCG: Example Tin",
                "price": 29.99,
                "price_text": "$29.99",
                "shipping_text": "",
                "seller_text": "",
                "availability_text": "",
                "in_stock": True,
            }
        ]
        cfg: dict = {"required_keywords": [], "blacklist": []}
        filtered, meta = run_search_filter_pipeline(raw, cfg, require_shipping_signal=False)
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["asin"], "B012345678")
        # PDP-style: valid price without "in stock" SERP text still counts as in stock
        self.assertTrue(filtered[0]["in_stock"])
        self.assertFalse(meta["require_shipping_signal"])

    def test_explicit_oos_on_card_is_not_in_stock(self) -> None:
        raw = [
            {
                "asin": "B012345679",
                "title": "Pokemon TCG: Example Box",
                "price": 19.99,
                "price_text": "$19.99",
                "shipping_text": "FREE delivery",
                "availability_text": "Currently unavailable.",
                "in_stock": False,
            }
        ]
        cfg: dict = {"required_keywords": [], "blacklist": []}
        filtered, _ = run_search_filter_pipeline(raw, cfg, require_shipping_signal=False)
        self.assertEqual(len(filtered), 1)
        self.assertFalse(filtered[0]["in_stock"])


if __name__ == "__main__":
    unittest.main()
