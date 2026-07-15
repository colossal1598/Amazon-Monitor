"""Degraded-page (soft-block skeleton) detection.

Regression for the 2026-07-14 soft-block windows: during a ~3.5 min block Amazon serves
every watched ASIN a server-rendered SKELETON PDP — #corePrice_feature_div empty, every
offer widget flagged data-csa-c-is-in-initial-active-row="false" with none ="true", zero
dollar strings anywhere — while a purchase button still renders. The old code classified
those as no_pay_price/priceless_purchasable (false price-less alerts) or let stale
OOS/mismatch evidence flip state. A skeleton is a scrape failure, not evidence: it must
become a degraded_page skip row.
"""

from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path

from pdp_scraper import (
    _page_offers_skeleton_async,
    _pdp_skip_row,
    pdp_skip_log_label,
)
from tests.pdp_fixture_helpers import is_offers_skeleton_from_html

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "pdp"
MANIFEST = json.loads((FIXTURES_DIR / "manifest.json").read_text(encoding="utf-8"))

_CORE_PRICE_SELECTOR = "#corePrice_feature_div"
_A_PRICE_OFFSCREEN_SELECTOR = ".a-price .a-offscreen"


def _read(filename: str) -> str:
    return (FIXTURES_DIR / filename).read_text(encoding="utf-8", errors="replace")


class _FakeEl:
    def __init__(self, text: str = "") -> None:
        self._text = text

    async def inner_text(self) -> str:
        return self._text


class _FakePage:
    """Minimal async page: query_selector resolves against a fixed selector->element map."""

    def __init__(self, selectors: dict[str, object]) -> None:
        self._selectors = selectors

    async def query_selector(self, selector: str):
        return self._selectors.get(selector)


def _detect(selectors: dict[str, object]) -> bool:
    return asyncio.run(_page_offers_skeleton_async(_FakePage(selectors), asin="B0TEST0001"))


class TestSkeletonDetectorAsync(unittest.TestCase):
    """Directly exercise the production async detector via a fake page."""

    def test_active_row_marker_removed_carries_no_signal(self) -> None:
        # REGRESSION 2026-07-15: a live healthy in-stock PDP showed 301
        # initial-active-row="false" elements and zero ="true" — the attribute is
        # meaningless as a degradation signal. A page matching only that pattern
        # (no empty-corePrice evidence) must NOT be classified skeleton; in prod
        # it caused rendered-but-priceless pages to trip degraded-burst recycles
        # every ~2 minutes.
        self.assertFalse(
            _detect(
                {
                    '[data-csa-c-is-in-initial-active-row="false"]': _FakeEl(),
                    '[data-csa-c-is-in-initial-active-row="true"]': None,
                }
            )
        )

    def test_marker_b_empty_core_price_no_offscreen(self) -> None:
        # No active-row markers at all, but an empty core price block and no offscreen
        # price node anywhere.
        self.assertTrue(
            _detect(
                {
                    _CORE_PRICE_SELECTOR: _FakeEl("   \n  "),
                    _A_PRICE_OFFSCREEN_SELECTOR: None,
                }
            )
        )

    def test_marker_b_negative_when_core_price_has_text(self) -> None:
        self.assertFalse(
            _detect(
                {
                    _CORE_PRICE_SELECTOR: _FakeEl("$42.50"),
                    _A_PRICE_OFFSCREEN_SELECTOR: None,
                }
            )
        )

    def test_marker_b_negative_when_offscreen_price_present(self) -> None:
        self.assertFalse(
            _detect(
                {
                    _CORE_PRICE_SELECTOR: _FakeEl(""),
                    _A_PRICE_OFFSCREEN_SELECTOR: _FakeEl("$42.50"),
                }
            )
        )

    def test_fully_absent_markers_is_negative(self) -> None:
        self.assertFalse(_detect({}))


class TestSkeletonDetectorFixtures(unittest.TestCase):
    """Static-HTML mirror against saved fixtures (skeleton positive, real pages negative)."""

    def test_skeleton_fixture_is_detected(self) -> None:
        self.assertTrue(is_offers_skeleton_from_html(_read("skeleton-offers.html")))

    def test_real_generic_buybox_is_not_skeleton(self) -> None:
        self.assertFalse(is_offers_skeleton_from_html(_read("generic-buybox.html")))

    def test_real_accordion_is_not_skeleton(self) -> None:
        self.assertFalse(is_offers_skeleton_from_html(_read("accordion-two-offers.html")))

    def test_manifest_marks_skeleton_fixture(self) -> None:
        self.assertTrue(MANIFEST["skeleton-offers.html"].get("skeleton"))

    def test_mirror_marker_a_isolated(self) -> None:
        html = '<div data-csa-c-is-in-initial-active-row="false"></div>'
        self.assertTrue(is_offers_skeleton_from_html(html))
        html_true = html + '<div data-csa-c-is-in-initial-active-row="true"></div>'
        self.assertFalse(is_offers_skeleton_from_html(html_true))

    def test_mirror_marker_b_isolated(self) -> None:
        html = '<div id="corePrice_feature_div"><div class="apex_with_rio_cx"></div></div>'
        self.assertTrue(is_offers_skeleton_from_html(html))


class TestDegradedSkipRow(unittest.TestCase):
    def test_skip_row_shape_and_label(self) -> None:
        row = _pdp_skip_row(
            "B0TEST0001",
            "degraded_page",
            skip_detail="skeleton_offers",
            scrape_attempts=1,
            scrape_elapsed_ms=1234,
        )
        self.assertTrue(row["_skip_update"])
        self.assertEqual(row["skip_reason"], "degraded_page")
        self.assertEqual(row["skip_detail"], "skeleton_offers")
        self.assertEqual(row["scrape_attempts"], 1)
        self.assertEqual(row["scrape_elapsed_ms"], 1234)
        self.assertEqual(pdp_skip_log_label(row), "skeleton")


if __name__ == "__main__":
    unittest.main()
