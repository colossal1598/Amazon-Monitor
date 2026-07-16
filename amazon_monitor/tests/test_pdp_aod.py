"""F1 — AOD (All Offers Display) offer check: parse, seller matching, row outcomes.

When a 3P seller holds the buybox (seller_mismatch/confirmed_out) the allowed Amazon offer
can still live in the AOD panel (B0DLQJ613B: 926 consecutive seller_mismatch OOS while
Amazon Export's offer lived in AOD). These tests pin the static-HTML parser and the pure
outcome mapping so the classification is verified without Playwright.

Live-verified guard (2026-07-15): every FBA 3P offer renders "Ships from Amazon.com"; seller
matching must use the sold-by NAME only, never the shipsFrom fulfiller text.
"""

from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path

from pdp_scraper import (
    _apply_aod_offer_check,
    _apply_aod_outcome,
    _fetch_aod_offers_async,
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
        self.assertEqual(row["aod_outcome"], "offer_found")

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
        self.assertEqual(row["aod_outcome"], "offer_found")

    def test_offers_present_none_allowed_keeps_confirmed_out(self) -> None:
        offers = parse_aod_offers(AOD_HTML)
        original = _seller_mismatch_row()
        row = _apply_aod_outcome("B0DLQJ613B", original, offers, ["amazon export"])
        self.assertFalse(row["in_stock"])
        self.assertEqual(row["stock_confidence"], "confirmed_out")
        self.assertEqual(row["stock_reason"], "seller_mismatch")
        self.assertTrue(row["aod_checked"])
        self.assertEqual(row["aod_outcome"], "no_allowed_offer")

    def test_no_offers_keeps_original_row_not_a_skip(self) -> None:
        # Prod fix 2026-07-16: AOD fetch/parse failure (None or []) must NOT become a degraded
        # skip. The PDP itself was healthy and fully readable, so keep the original buybox
        # seller_mismatch/confirmed_out row and tag aod_outcome=fetch_failed. The C9 debounce
        # still guards flips; aod_checked=True lets the engine throttle instead of hot-looping.
        for offers in (None, []):
            original = _seller_mismatch_row()
            row = _apply_aod_outcome("B0DLQJ613B", original, offers, ["amazon.com"])
            self.assertNotIn("_skip_update", row)
            self.assertFalse(row["in_stock"])
            self.assertEqual(row["stock_confidence"], "confirmed_out")
            self.assertEqual(row["stock_reason"], "seller_mismatch")
            self.assertTrue(row["aod_checked"])
            self.assertEqual(row["aod_outcome"], "fetch_failed")


class _FakeAodPage:
    """Minimal async page stub: evaluate() returns a canned AOD fragment, set_content/content
    round-trip it (mirrors what the real product page does before it is closed)."""

    def __init__(self, fetch_result: str) -> None:
        self._fetch_result = fetch_result
        self._content = ""
        self.evaluate_calls: list[str] = []

    async def evaluate(self, expression: str, arg: str | None = None) -> str:
        self.evaluate_calls.append(str(arg or ""))
        return self._fetch_result

    async def set_content(self, html: str) -> None:
        self._content = html

    async def content(self) -> str:
        return self._content


class TestFetchAodOffersInPage(unittest.TestCase):
    """The in-page XHR path (_fetch_aod_offers_async) mapped through a fake product page."""

    def test_fixture_html_parses_to_offers(self) -> None:
        page = _FakeAodPage(AOD_HTML)
        offers = asyncio.run(_fetch_aod_offers_async(page, "B0DLQJ613B"))
        self.assertIsNotNone(offers)
        self.assertEqual(len(offers), MANIFEST["aod-offers.html"]["expected_offer_count"])
        # The fetched URL carried the ASIN (built from _AOD_URL_TEMPLATE).
        self.assertIn("B0DLQJ613B", page.evaluate_calls[0])

    def test_empty_fetch_result_is_failure(self) -> None:
        for empty in ("", "   ", "\n\t "):
            offers = asyncio.run(_fetch_aod_offers_async(_FakeAodPage(empty), "B0DLQJ613B"))
            self.assertIsNone(offers)

    def test_check_success_folds_offer_into_row(self) -> None:
        page = _FakeAodPage(AOD_HTML)
        row = asyncio.run(
            _apply_aod_offer_check(
                page, "B0DLQJ613B", ["amazon.com", "amazon export"], _seller_mismatch_row()
            )
        )
        self.assertTrue(row["in_stock"])
        self.assertEqual(row["price"], 65.94)
        self.assertEqual(row["aod_outcome"], "offer_found")

    def test_check_empty_fetch_keeps_row_and_tags_fetch_failed(self) -> None:
        page = _FakeAodPage("")
        row = asyncio.run(
            _apply_aod_offer_check(page, "B0DLQJ613B", ["amazon.com"], _seller_mismatch_row())
        )
        self.assertNotIn("_skip_update", row)
        self.assertEqual(row["stock_confidence"], "confirmed_out")
        self.assertEqual(row["stock_reason"], "seller_mismatch")
        self.assertTrue(row["aod_checked"])
        self.assertEqual(row["aod_outcome"], "fetch_failed")


if __name__ == "__main__":
    unittest.main()
