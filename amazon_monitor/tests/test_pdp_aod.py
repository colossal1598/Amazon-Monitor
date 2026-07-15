"""F1 — AOD (All Offers Display) offer check: parse, seller matching, row outcomes.

When a 3P seller holds the buybox (seller_mismatch/confirmed_out) the allowed Amazon offer
can still live in the AOD panel (B0DLQJ613B: 926 consecutive seller_mismatch OOS while
Amazon Export's offer lived in AOD). These tests pin the static-HTML parser and the pure
outcome mapping so the classification is verified without Playwright.

Live-verified guard (2026-07-15): every FBA 3P offer renders "Ships from Amazon.com"; seller
matching must use the sold-by NAME only, never the shipsFrom fulfiller text.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from pdp_scraper import (
    _apply_aod_outcome,
    _pdp_row,
    parse_aod_offers,
    select_allowed_aod_offer,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "pdp"
MANIFEST = json.loads((FIXTURES_DIR / "manifest.json").read_text(encoding="utf-8"))
AOD_HTML = (FIXTURES_DIR / "aod-offers.html").read_text(encoding="utf-8", errors="replace")


def _seller_mismatch_row() -> dict:
    """The confirmed_out/seller_mismatch row a 3P buybox produces before the AOD check."""
    row = _pdp_row(
        "B0DLQJ613B",
        title="Widget",
        price=79.95,
        shipping_text="",
        image_url=None,
        merchant_blob="Sold by ACG ECOM",
        allowed=["amazon.com", "amazon export"],
    )
    assert row["stock_confidence"] == "confirmed_out"
    assert row["stock_reason"] == "seller_mismatch"
    return row


class TestParseAodOffers(unittest.TestCase):
    def setUp(self) -> None:
        self.offers = parse_aod_offers(AOD_HTML)

    def test_parses_every_offer_block(self) -> None:
        self.assertEqual(len(self.offers), MANIFEST["aod-offers.html"]["expected_offer_count"])

    def test_pinned_3p_offer_name_and_price(self) -> None:
        pinned = self.offers[0]
        self.assertEqual(pinned["seller_text"], "ACG ECOM")
        self.assertEqual(pinned["price"], 79.95)

    def test_amazon_offer_uses_whole_fraction_fallback(self) -> None:
        # The Amazon.com row ships an EMPTY .a-offscreen; price must come from whole/fraction.
        amazon = self.offers[1]
        self.assertEqual(amazon["seller_text"], "Amazon.com")
        self.assertEqual(amazon["price"], 65.94)

    def test_seller_name_excludes_seller_rating_and_shipsfrom(self) -> None:
        # "Sold by ACG ECOM Seller rating is ..." + "Ships from Amazon.com" must NOT leak in.
        for offer in self.offers:
            self.assertNotIn("Seller rating", offer["seller_text"])
            self.assertNotIn("Ships from", offer["seller_text"])
        self.assertEqual([o["seller_text"] for o in self.offers], MANIFEST["aod-offers.html"]["sellers"])


class TestSelectAllowedAodOffer(unittest.TestCase):
    def setUp(self) -> None:
        self.offers = parse_aod_offers(AOD_HTML)

    def test_amazon_offer_selected_when_allowed(self) -> None:
        matched = select_allowed_aod_offer(self.offers, ["amazon.com", "amazon export"])
        self.assertIsNotNone(matched)
        self.assertEqual(matched["seller_text"], "Amazon.com")
        self.assertEqual(matched["price"], 65.94)

    def test_fba_shipsfrom_amazon_offers_do_not_match(self) -> None:
        # Same file, allowed list that no sold-by name matches. Every FBA offer shows
        # "Ships from Amazon.com"; matching must NOT false-positive on the fulfiller.
        matched = select_allowed_aod_offer(self.offers, ["amazon export"])
        self.assertIsNone(matched)

    def test_third_party_only_allowlist_matches_that_seller(self) -> None:
        matched = select_allowed_aod_offer(self.offers, ["ACG ECOM"])
        self.assertIsNotNone(matched)
        self.assertEqual(matched["seller_text"], "ACG ECOM")


class TestApplyAodOutcome(unittest.TestCase):
    def test_allowed_offer_with_price_becomes_in_stock(self) -> None:
        offers = parse_aod_offers(AOD_HTML)
        row = _apply_aod_outcome(
            "B0DLQJ613B", _seller_mismatch_row(), offers, ["amazon.com", "amazon export"]
        )
        self.assertTrue(row["in_stock"])
        self.assertEqual(row["price"], 65.94)
        self.assertEqual(row["stock_confidence"], "confirmed_in")
        self.assertEqual(row["stock_reason"], "aod_offer")
        self.assertIn("Amazon.com", row["seller_text"])
        self.assertTrue(row["aod_checked"])

    def test_allowed_offer_without_price_is_priceless_purchasable(self) -> None:
        # Live AOD often ships a blank .a-offscreen with no whole/fraction — an allowed
        # offer with unparseable price is an in-stock signal, handled by the priceless path.
        offers = [{"seller_text": "Amazon.com", "price": None}]
        row = _apply_aod_outcome("B0DLQJ613B", _seller_mismatch_row(), offers, ["amazon.com"])
        self.assertFalse(row["in_stock"])
        self.assertIsNone(row["price"])
        self.assertEqual(row["stock_confidence"], "unknown")
        self.assertEqual(row["stock_reason"], "priceless_purchasable")
        self.assertTrue(row["buybox_purchasable"])
        self.assertTrue(row["aod_checked"])

    def test_offers_present_none_allowed_keeps_confirmed_out(self) -> None:
        offers = parse_aod_offers(AOD_HTML)
        original = _seller_mismatch_row()
        row = _apply_aod_outcome("B0DLQJ613B", original, offers, ["amazon export"])
        self.assertFalse(row["in_stock"])
        self.assertEqual(row["stock_confidence"], "confirmed_out")
        self.assertEqual(row["stock_reason"], "seller_mismatch")
        self.assertTrue(row["aod_checked"])

    def test_no_offers_is_degraded_skip_not_confirmed_out(self) -> None:
        # AOD fetch failed / empty / captcha / skeleton-like: absence of evidence.
        for offers in (None, []):
            row = _apply_aod_outcome("B0DLQJ613B", _seller_mismatch_row(), offers, ["amazon.com"])
            self.assertTrue(row["_skip_update"])
            self.assertEqual(row["skip_reason"], "degraded_page")
            self.assertEqual(row["skip_detail"], "aod_failed")
            self.assertNotIn("stock_confidence", row)


if __name__ == "__main__":
    unittest.main()
