"""Accordion-aware per-offer PDP extraction: the chosen row drives price/seller/shipping.

Regression for the wrong-offer alert (B0GYTRYV7P, 2026-07-14): a two-offer accordion
buybox paired the featured row's price/shipping with a *different* row's
"Sold by: Amazon.com" merchant line, so the monitor alerted on the wrong offer while
seller validation passed. Extraction must now be scoped to the single allowed-seller row.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from pdp_scraper import _pdp_row
from tests.pdp_fixture_helpers import (
    ACCORDION_NO_ALLOWED_OFFER,
    build_accordion_pdp_state_from_html,
    find_accordion_offer_from_html,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "pdp"
MANIFEST = json.loads((FIXTURES_DIR / "manifest.json").read_text(encoding="utf-8"))


def _read(filename: str) -> str:
    return (FIXTURES_DIR / filename).read_text(encoding="utf-8", errors="replace")


def _row_from_fixture(filename: str, allowed: list[str], title: str) -> dict:
    """Build the same _pdp_row production would build for an accordion PDP."""
    state = build_accordion_pdp_state_from_html(_read(filename), allowed)
    assert state is not None, f"{filename} is not an accordion page"
    return _pdp_row(
        "B0GYTRYV7P",
        title=title,
        price=state["price"],
        shipping_text=state["shipping"],
        image_url=None,
        merchant_blob=state["merchant_blob"],
        allowed=allowed,
        explicit_oos=state["explicit_oos"],
        buybox_purchasable=state["buybox_purchasable"],
    )


class TestAccordionAllowedRowChosen(unittest.TestCase):
    FIXTURE = "accordion-two-offers.html"

    def test_scopes_onto_amazon_row_not_featured_row(self) -> None:
        row = _row_from_fixture(self.FIXTURE, ["Amazon.com"], "Accordion Two Offers Product")
        # The Amazon.com row ($65.94), not the featured Kings Games row ($79.95).
        self.assertTrue(row["in_stock"])
        self.assertEqual(row["price"], 65.94)
        self.assertEqual(row["stock_confidence"], "confirmed_in")

    def test_shipping_comes_from_chosen_row(self) -> None:
        row = _row_from_fixture(self.FIXTURE, ["Amazon.com"], "Accordion Two Offers Product")
        self.assertIn("$17.96", row["shipping_text"])
        # Must NOT carry the featured (row 0) delivery line.
        self.assertNotIn("$5.00", row["shipping_text"])

    def test_seller_text_reflects_only_chosen_offer(self) -> None:
        row = _row_from_fixture(self.FIXTURE, ["Amazon.com"], "Accordion Two Offers Product")
        self.assertIn("Amazon.com", row["seller_text"])
        self.assertNotIn("Kings Games", row["seller_text"])

    def test_manifest_expectations(self) -> None:
        expected = MANIFEST[self.FIXTURE]
        row = _row_from_fixture(self.FIXTURE, expected["allowed"], "Accordion Two Offers Product")
        self.assertEqual(row["price"], expected["expected_price"])
        self.assertEqual(row["in_stock"], expected["expected_in_stock"])
        self.assertEqual(row["stock_confidence"], expected["expected_confidence"])


class TestAccordionNoAllowedRow(unittest.TestCase):
    FIXTURE = "accordion-no-amazon.html"

    def test_no_amazon_row_is_confirmed_out(self) -> None:
        row = _row_from_fixture(self.FIXTURE, ["Amazon.com"], "Accordion No Amazon Product")
        self.assertFalse(row["in_stock"])
        self.assertEqual(row["stock_confidence"], "confirmed_out")
        self.assertEqual(row["stock_reason"], "seller_mismatch")

    def test_price_is_not_leaked_as_in_stock(self) -> None:
        # The 3P rows share $65.94 with the good fixture; a leak would surface as an
        # in_stock 65.94. It must stay out of stock and never report a qualifying price.
        row = _row_from_fixture(self.FIXTURE, ["Amazon.com"], "Accordion No Amazon Product")
        self.assertIsNone(row["price"])
        self.assertFalse(row["in_stock"])

    def test_diagnostic_blob_uses_merchant_names_only(self) -> None:
        # The featured row's fulfiller is "Ships from Amazon"; it must be stripped so the
        # diagnostic blob cannot leak an allowed substring.
        row = _row_from_fixture(self.FIXTURE, ["Amazon.com"], "Accordion No Amazon Product")
        self.assertIn("Kings Games", row["seller_text"])
        self.assertIn("SomeOtherSeller", row["seller_text"])
        self.assertNotIn("amazon", row["seller_text"].lower())


class TestAccordionDetection(unittest.TestCase):
    def test_two_offer_fixture_matches_amazon_row(self) -> None:
        offer, merchants = find_accordion_offer_from_html(
            _read("accordion-two-offers.html"), ["Amazon.com"]
        )
        self.assertNotIn(offer, (None, ACCORDION_NO_ALLOWED_OFFER))
        self.assertEqual(merchants, ["Kings Games", "Amazon.com"])

    def test_no_amazon_fixture_returns_sentinel(self) -> None:
        offer, merchants = find_accordion_offer_from_html(
            _read("accordion-no-amazon.html"), ["Amazon.com"]
        )
        self.assertIs(offer, ACCORDION_NO_ALLOWED_OFFER)
        self.assertEqual(merchants, ["Kings Games", "SomeOtherSeller"])

    def test_non_accordion_page_returns_none(self) -> None:
        offer, merchants = find_accordion_offer_from_html(
            _read("generic-buybox.html"), ["Amazon.com"]
        )
        self.assertIsNone(offer)
        self.assertEqual(merchants, [])
        self.assertIsNone(
            build_accordion_pdp_state_from_html(_read("generic-buybox.html"), ["Amazon.com"])
        )


if __name__ == "__main__":
    unittest.main()
