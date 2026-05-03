"""Unit tests for SERP card price parsing (no Playwright)."""

import unittest

from serp_card_price import _money_amounts, card_list_price


class SerpCardPriceTests(unittest.TestCase):
    def test_star_rating_not_used_as_price(self) -> None:
        blob = "3.2\n$29.99\n$17.70 delivery Mon"
        self.assertEqual(card_list_price(blob), 29.99)

    def test_dollar_amounts_only(self) -> None:
        self.assertEqual(card_list_price("$48.99"), 48.99)

    def test_per_unit_small_amounts_ignored(self) -> None:
        blob = "$40.70\n$0.42/count"
        self.assertEqual(card_list_price(blob), 40.70)

    def test_money_amounts_collects(self) -> None:
        self.assertEqual(sorted(_money_amounts("x $10 y $20.50")), [10.0, 20.5])


if __name__ == "__main__":
    unittest.main()
