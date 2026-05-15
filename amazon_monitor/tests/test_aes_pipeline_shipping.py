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
        cfg: dict = {"required_keywords": [], "whitelist": [], "blacklist": []}
        filtered, meta = run_search_filter_pipeline(raw, cfg, require_shipping_signal=False)
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["asin"], "B012345678")
        self.assertFalse(meta["require_shipping_signal"])


if __name__ == "__main__":
    unittest.main()
