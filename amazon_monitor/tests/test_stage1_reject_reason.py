"""Stage1 reject reason codes."""

import unittest

from filter_pipeline import stage1_reject_reason


class Stage1RejectReasonTests(unittest.TestCase):
    def _base(self) -> dict:
        return {
            "asin": "B012345678",
            "title": "Pokemon TCG: Example Tin",
            "price": 29.99,
            "price_text": "$29.99",
            "shipping_text": "$5.99 delivery Mon",
            "seller_text": "",
            "availability_text": "",
        }

    def test_passes_returns_none(self) -> None:
        self.assertIsNone(stage1_reject_reason(self._base()))

    def test_no_title_scope(self) -> None:
        row = self._base()
        row["title"] = "Pokemon Trainer Guess Electronic Game"
        self.assertEqual(stage1_reject_reason(row), "no_pokemon_tcg_title")

    def test_no_price(self) -> None:
        row = self._base()
        row["price"] = None
        self.assertEqual(stage1_reject_reason(row), "no_price_on_card")

    def test_no_shipping_signal(self) -> None:
        row = self._base()
        row["shipping_text"] = ""
        row["availability_text"] = ""
        self.assertEqual(stage1_reject_reason(row), "no_shipping_or_delivery_signal")

    def test_no_shipping_signal_optional(self) -> None:
        row = self._base()
        row["shipping_text"] = ""
        row["availability_text"] = ""
        self.assertIsNone(stage1_reject_reason(row, require_shipping_signal=False))


if __name__ == "__main__":
    unittest.main()
