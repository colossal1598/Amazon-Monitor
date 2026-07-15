"""Offline regression tests: verified PDP price/delivery selectors against saved HTML fixtures."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from pdp_scraper import (
    _PDP_BUYBOX_PRESENT_SELECTORS,
    _PDP_PRICE_PAY_SELECTORS,
)
from tests.pdp_fixture_helpers import (
    extract_delivery_text_from_html,
    extract_list_price_from_html,
    extract_pay_price_from_html,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "pdp"
MANIFEST = json.loads((FIXTURES_DIR / "manifest.json").read_text(encoding="utf-8"))


class TestPdpSelectorsFromHtml(unittest.TestCase):
    def test_pay_price_all_fixtures(self) -> None:
        for filename, expected in MANIFEST.items():
            # Accordion fixtures are per-offer scoped (page-level extraction here would
            # return the featured row, i.e. the wrong offer). They are asserted in
            # test_pdp_accordion.py. Skeleton (degraded) fixtures have no price/delivery
            # by design and are asserted in test_pdp_skeleton.py.
            if expected.get("accordion") or expected.get("skeleton") or expected.get("aod"):
                continue
            with self.subTest(fixture=filename):
                html = (FIXTURES_DIR / filename).read_text(encoding="utf-8", errors="replace")
                price = extract_pay_price_from_html(html)
                self.assertEqual(price, expected["pay_price"])

    def test_list_price_not_used_as_pay_price(self) -> None:
        for filename, expected in MANIFEST.items():
            if "list_price" not in expected:
                continue
            with self.subTest(fixture=filename):
                html = (FIXTURES_DIR / filename).read_text(encoding="utf-8", errors="replace")
                list_p = extract_list_price_from_html(html)
                self.assertEqual(list_p, expected["list_price"])
                pay = extract_pay_price_from_html(html)
                self.assertEqual(pay, expected["pay_price"])
                self.assertNotEqual(pay, list_p)

    def test_delivery_text_all_fixtures(self) -> None:
        for filename, expected in MANIFEST.items():
            if expected.get("accordion") or expected.get("skeleton") or expected.get("aod"):
                continue
            with self.subTest(fixture=filename):
                html = (FIXTURES_DIR / filename).read_text(encoding="utf-8", errors="replace")
                shipping = extract_delivery_text_from_html(html)
                self.assertTrue(shipping, msg=f"no delivery text for {filename}")
                for fragment in expected["delivery_contains"]:
                    self.assertIn(
                        fragment,
                        shipping,
                        msg=f"{filename}: expected {fragment!r} in {shipping!r}",
                    )


class TestPdpSelectorConstantsFromPlan(unittest.TestCase):
    def test_pay_selectors_buybox_first(self) -> None:
        self.assertTrue(_PDP_PRICE_PAY_SELECTORS[0].startswith("#qualifiedBuybox"))

    def test_pay_selectors_exclude_list_price_path(self) -> None:
        # a-text-price (strike-through list price) may only appear as a :not() negation
        # guard on the generic .a-price fallbacks, never as a positive selector target.
        for sel in _PDP_PRICE_PAY_SELECTORS:
            stripped = sel.replace(":not(.a-text-price)", "")
            self.assertNotIn("a-text-price", stripped)

    def test_buybox_present_untouched(self) -> None:
        self.assertIn("#tabular-buybox", _PDP_BUYBOX_PRESENT_SELECTORS)
        self.assertIn(".a-price", _PDP_BUYBOX_PRESENT_SELECTORS)


if __name__ == "__main__":
    unittest.main()
