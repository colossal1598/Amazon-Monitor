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

    def test_current_price_before_strikethrough_list_price_wins(self) -> None:
        """Discounted item: current price appears before the (larger) "List:" price in card text."""
        blob = "current $54.99 List: $69.99"
        self.assertEqual(card_list_price(blob), 54.99)

    def test_star_rating_out_of_five_not_used_as_price(self) -> None:
        """Bare "5" from "4.7 out of 5" must not be mistaken for a $5+ price (no leading $)."""
        blob = "4.7 out of 5 stars\n$45.99"
        self.assertEqual(card_list_price(blob), 45.99)

    def test_single_price_returns_that_price(self) -> None:
        self.assertEqual(card_list_price("$19.99"), 19.99)

    def test_bare_numbers_without_dollar_sign_are_ignored(self) -> None:
        """Only "$"-prefixed amounts count as money; a bare number is never picked."""
        self.assertIsNone(card_list_price("4.7 out of 5 stars"))


if __name__ == "__main__":
    unittest.main()
