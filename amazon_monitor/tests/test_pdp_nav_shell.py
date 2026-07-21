"""Offer-less nav-shell and cached-fallback PDP detection.

Regression for the post-512ba00 degraded windows (2026-07-16/17): Amazon served
watched ASINs a page containing ONLY the navigation chrome — every element id nav-*/
a-page, no #dp / #ppd / #centerCol / #productTitle / #availability, no buybox, no price
markup — while the document <title> still carried the product name. The old code
recovered a title from the <title> tag, found no purchase action, and classified
explicit_oos/no_pay_price → confirmed_out: a scrape failure ingested as OOS EVIDENCE
(oos_miss_streak into the hundreds, false OOS flips during shell windows that hit
multiple ASINs in the same second, duplicate back_in_stock alerts on recovery — the
exact alerts clients dislike-tagged). A shell must become a degraded_page skip row.

Also covers the cached FallbackDetailPage render (B0GW2DK37Q 2026-07-17: hidden input
clientName="FallbackDetailPage", pageLoadTimestampUTC 41h older than the fetch): a
stale cache page is never evidence, whatever it shows.
"""

from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path

from pdp_scraper import (
    _aod_check_worthwhile,
    _page_is_fallback_detail_async,
    _page_is_nav_shell_async,
    _PDP_FALLBACK_CLIENT_SELECTOR,
    _PDP_PRODUCT_BODY_SELECTORS,
    _pdp_skip_row,
    pdp_skip_log_label,
)
from tests.pdp_fixture_helpers import is_fallback_detail_from_html, is_nav_shell_from_html

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "pdp"
MANIFEST = json.loads((FIXTURES_DIR / "manifest.json").read_text(encoding="utf-8"))


def _read(filename: str) -> str:
    return (FIXTURES_DIR / filename).read_text(encoding="utf-8", errors="replace")


class _FakeEl:
    pass


class _FakePage:
    """Minimal async page: query_selector resolves against a fixed selector->element map."""

    def __init__(self, selectors: dict[str, object]) -> None:
        self._selectors = selectors

    async def query_selector(self, selector: str):
        return self._selectors.get(selector)


class _RaisingPage:
    async def query_selector(self, selector: str):
        raise RuntimeError("driver hiccup")


class TestNavShellDetectorAsync(unittest.TestCase):
    """Directly exercise the production async detector via a fake page."""

    def test_all_product_containers_absent_is_shell(self) -> None:
        self.assertTrue(
            asyncio.run(_page_is_nav_shell_async(_FakePage({}), asin="B0TEST0001"))
        )

    def test_any_product_container_present_is_not_shell(self) -> None:
        for sel in _PDP_PRODUCT_BODY_SELECTORS:
            with self.subTest(selector=sel):
                self.assertFalse(
                    asyncio.run(
                        _page_is_nav_shell_async(_FakePage({sel: _FakeEl()}), asin="B0TEST0001")
                    )
                )

    def test_selector_error_is_not_shell(self) -> None:
        # An inconclusive read must never classify a page as a shell.
        self.assertFalse(asyncio.run(_page_is_nav_shell_async(_RaisingPage(), asin="B0TEST0001")))


class TestFallbackDetectorAsync(unittest.TestCase):
    def test_fallback_client_input_detected(self) -> None:
        page = _FakePage({_PDP_FALLBACK_CLIENT_SELECTOR: _FakeEl()})
        self.assertTrue(asyncio.run(_page_is_fallback_detail_async(page, asin="B0TEST0001")))

    def test_normal_page_is_not_fallback(self) -> None:
        self.assertFalse(
            asyncio.run(_page_is_fallback_detail_async(_FakePage({}), asin="B0TEST0001"))
        )

    def test_selector_error_is_not_fallback(self) -> None:
        self.assertFalse(
            asyncio.run(_page_is_fallback_detail_async(_RaisingPage(), asin="B0TEST0001"))
        )


class TestNavShellFixtures(unittest.TestCase):
    """Static-HTML mirror against saved fixtures (live captures, 2026-07-17)."""

    def test_nav_shell_fixture_is_detected(self) -> None:
        self.assertTrue(is_nav_shell_from_html(_read("nav-shell.html")))

    def test_every_real_page_fixture_is_not_nav_shell(self) -> None:
        # Includes the skeleton and fallback fixtures: both render a product body, so
        # neither may ever classify as a shell (their own detectors own them). AOD
        # fixtures are ajax fragments, not pages — the detector never sees one.
        for filename in MANIFEST:
            if MANIFEST[filename].get("nav_shell") or MANIFEST[filename].get("aod"):
                continue
            with self.subTest(fixture=filename):
                self.assertFalse(is_nav_shell_from_html(_read(filename)))

    def test_fallback_fixture_is_detected(self) -> None:
        self.assertTrue(is_fallback_detail_from_html(_read("fallback-detail.html")))

    def test_every_other_fixture_is_not_fallback(self) -> None:
        for filename in MANIFEST:
            if MANIFEST[filename].get("fallback_page"):
                continue
            with self.subTest(fixture=filename):
                self.assertFalse(is_fallback_detail_from_html(_read(filename)))

    def test_manifest_marks_new_fixtures(self) -> None:
        self.assertTrue(MANIFEST["nav-shell.html"].get("nav_shell"))
        self.assertTrue(MANIFEST["fallback-detail.html"].get("fallback_page"))


class TestDegradedSkipRows(unittest.TestCase):
    def test_nav_shell_skip_row_shape_and_label(self) -> None:
        row = _pdp_skip_row(
            "B0TEST0001",
            "degraded_page",
            skip_detail="nav_shell",
            scrape_attempts=1,
            scrape_elapsed_ms=1234,
        )
        self.assertTrue(row["_skip_update"])
        self.assertEqual(row["skip_reason"], "degraded_page")
        self.assertEqual(row["skip_detail"], "nav_shell")
        self.assertEqual(pdp_skip_log_label(row), "nav_shell")

    def test_fallback_skip_row_shape_and_label(self) -> None:
        row = _pdp_skip_row(
            "B0TEST0001",
            "degraded_page",
            skip_detail="fallback_page",
            scrape_attempts=1,
            scrape_elapsed_ms=1234,
        )
        self.assertTrue(row["_skip_update"])
        self.assertEqual(row["skip_reason"], "degraded_page")
        self.assertEqual(pdp_skip_log_label(row), "fallback")


class TestAodCheckWorthwhile(unittest.TestCase):
    """Gate for the in-page AOD side-fetch (pure logic)."""

    ALLOWED = ["Amazon.com", "Amazon Export Sales LLC"]

    @staticmethod
    def _row(**overrides: object) -> dict[str, object]:
        row: dict[str, object] = {
            "in_stock": False,
            "stock_confidence": "unknown",
            "stock_reason": "no_pay_price",
            "buybox_purchasable": True,
        }
        row.update(overrides)
        return row

    def test_seller_mismatch_confirmed_out_qualifies(self) -> None:
        row = self._row(stock_confidence="confirmed_out", stock_reason="seller_mismatch")
        self.assertTrue(_aod_check_worthwhile(row, merchant_blob="Kings Games", allowed=self.ALLOWED))

    def test_explicit_oos_confirmed_out_qualifies(self) -> None:
        # REGRESSION 2026-07-21 (B0G3CV6Z9D): explicit-OOS page variants ran for 2.5
        # days (oos_miss_streak 3,239) while the allowed offer sat in AOD — every
        # confirmed_out row is worth an AOD look, not just seller_mismatch.
        row = self._row(
            stock_confidence="confirmed_out",
            stock_reason="explicit_oos_text",
            buybox_purchasable=False,
        )
        self.assertTrue(_aod_check_worthwhile(row, merchant_blob="", allowed=self.ALLOWED))

    def test_priceless_purchasable_3p_buybox_qualifies(self) -> None:
        # B0GW2DK37Q 2026-07-16: "Kings Games" buybox, enabled buy button, no price
        # anywhere on the page — only AOD can say whether an allowed offer exists.
        self.assertTrue(
            _aod_check_worthwhile(
                self._row(), merchant_blob="Sold by Kings Games", allowed=self.ALLOWED
            )
        )

    def test_priceless_allowed_seller_does_not_qualify(self) -> None:
        # Allowed-seller priceless buybox is the priceless_purchasable restock path —
        # the streak/alert machinery owns it, not AOD.
        self.assertFalse(
            _aod_check_worthwhile(
                self._row(stock_reason="priceless_purchasable"),
                merchant_blob="Amazon.com",
                allowed=self.ALLOWED,
            )
        )
        self.assertFalse(
            _aod_check_worthwhile(self._row(), merchant_blob="Amazon.com", allowed=self.ALLOWED)
        )

    def test_empty_merchant_blob_does_not_qualify(self) -> None:
        # Empty blob means extraction was incomplete, not a settled 3P observation.
        self.assertFalse(_aod_check_worthwhile(self._row(), merchant_blob="  ", allowed=self.ALLOWED))

    def test_unpurchasable_does_not_qualify(self) -> None:
        self.assertFalse(
            _aod_check_worthwhile(
                self._row(buybox_purchasable=False),
                merchant_blob="Kings Games",
                allowed=self.ALLOWED,
            )
        )

    def test_in_stock_row_never_qualifies(self) -> None:
        row = self._row(in_stock=True, stock_confidence="confirmed_out", stock_reason="seller_mismatch")
        self.assertFalse(_aod_check_worthwhile(row, merchant_blob="Kings Games", allowed=self.ALLOWED))


if __name__ == "__main__":
    unittest.main()
