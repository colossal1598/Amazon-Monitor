"""Offer-state readiness wait (extraction-timing fix, 2026-07-15).

Regression for the false `degraded_page` skeleton-skip loop: the old worker gated on
CONTAINER presence (#availability appears seconds before price widgets hydrate on a slow
machine), so extraction ran on a half-built page and `_page_offers_skeleton_async` fired
on price nodes that simply had not rendered yet. The fix waits for a RESOLVABLE offer
state (price / oos / alt_offers / accordion) and only runs the skeleton check when that
wait TIMED OUT. These tests exercise the real production functions.
"""

from __future__ import annotations

import asyncio
import unittest

from pdp_scraper import (
    _ACCORDION_ROW_SELECTOR,
    _page_offers_skeleton_async,
    _wait_for_offer_state_async,
)

_CORE_PRICE_SELECTOR = "#corePrice_feature_div"
_A_PRICE_OFFSCREEN_SELECTOR = ".a-price .a-offscreen"


class _FakeEl:
    def __init__(self, text: str = "") -> None:
        self._text = text

    async def inner_text(self) -> str:
        return self._text

    async def text_content(self) -> str:
        return self._text


class _MapPage:
    """Static fake page: fixed selector -> element (query_selector) and
    selector -> element list (query_selector_all) maps."""

    def __init__(
        self,
        selectors: dict[str, object] | None = None,
        selector_all: dict[str, list] | None = None,
    ) -> None:
        self._sel = selectors or {}
        self._all = selector_all or {}

    async def query_selector(self, selector: str):
        return self._sel.get(selector)

    async def query_selector_all(self, selector: str):
        return list(self._all.get(selector, []))


class _SequencedPage:
    """Fake page whose DOM advances one step each poll sweep.

    A sweep is counted when the detector issues its first probe
    (``query_selector_all('.a-price .a-offscreen')``). Results are drawn from
    ``states[min(sweep, last)]`` so the page settles on its final state.
    """

    def __init__(self, states: list[dict[str, object]]) -> None:
        self._states = states
        self._sweep = -1

    def _state(self) -> dict[str, object]:
        idx = min(max(self._sweep, 0), len(self._states) - 1)
        return self._states[idx]

    async def query_selector(self, selector: str):
        return self._state().get(selector)

    async def query_selector_all(self, selector: str):
        if selector == _A_PRICE_OFFSCREEN_SELECTOR:
            self._sweep += 1
        val = self._state().get(selector)
        return list(val) if isinstance(val, list) else []


def _wait(page, *, timeout_s: float = 5.0, poll: float = 0.02) -> str:
    return asyncio.run(
        _wait_for_offer_state_async(page, timeout_s=timeout_s, poll_interval_s=poll)
    )


class WaitForOfferStateTests(unittest.TestCase):
    def test_price_offscreen_node_resolves_price(self) -> None:
        page = _MapPage(selector_all={_A_PRICE_OFFSCREEN_SELECTOR: [_FakeEl("$42.50")]})
        self.assertEqual(_wait(page), "price")

    def test_core_price_text_resolves_price(self) -> None:
        page = _MapPage(selectors={_CORE_PRICE_SELECTOR: _FakeEl("$19.99")})
        self.assertEqual(_wait(page), "price")

    def test_outofstock_resolves_oos(self) -> None:
        page = _MapPage(selectors={"#outOfStock": _FakeEl("Currently unavailable.")})
        self.assertEqual(_wait(page), "oos")

    def test_availability_text_resolves_oos(self) -> None:
        page = _MapPage(
            selectors={"#availability": _FakeEl("Currently unavailable.")}
        )
        self.assertEqual(_wait(page), "oos")

    def test_accordion_rows_resolve_accordion(self) -> None:
        page = _MapPage(
            selector_all={_ACCORDION_ROW_SELECTOR: [_FakeEl(), _FakeEl()]}
        )
        self.assertEqual(_wait(page), "accordion")

    def test_single_accordion_row_does_not_resolve(self) -> None:
        # One row is not enough — must be >= 2. Nothing else present -> timeout.
        page = _MapPage(selector_all={_ACCORDION_ROW_SELECTOR: [_FakeEl()]})
        self.assertEqual(_wait(page, timeout_s=0.2, poll=0.05), "timeout")

    def test_never_ready_returns_timeout(self) -> None:
        self.assertEqual(_wait(_MapPage(), timeout_s=0.3, poll=0.1), "timeout")

    def test_price_appears_on_a_later_poll(self) -> None:
        # Nothing on the first two sweeps, then a parseable price node appears.
        page = _SequencedPage(
            [
                {},
                {},
                {_A_PRICE_OFFSCREEN_SELECTOR: [_FakeEl("$12.00")]},
            ]
        )
        self.assertEqual(_wait(page, timeout_s=5.0, poll=0.02), "price")

    def test_oos_appears_on_a_later_poll(self) -> None:
        page = _SequencedPage(
            [
                {},
                {"#availability": _FakeEl("Temporarily out of stock.")},
            ]
        )
        self.assertEqual(_wait(page, timeout_s=5.0, poll=0.02), "oos")

    def test_price_wins_over_oos_same_tick(self) -> None:
        # A hydrated price out-ranks a stale OOS marker in the same sweep.
        page = _MapPage(
            selectors={"#outOfStock": _FakeEl("unavailable")},
            selector_all={_A_PRICE_OFFSCREEN_SELECTOR: [_FakeEl("$9.99")]},
        )
        self.assertEqual(_wait(page), "price")


def _would_skip_skeleton(page, *, explicit_oos: bool, price, title: str, timeout_s: float):
    """Mirror the worker's degraded-skeleton guard using the real production functions.

    Returns (would_skip, offer_signal). This is the exact composed condition from
    ``_scrape_pdp_on_context``: the skeleton check runs ONLY when the offer-state wait
    timed out.
    """

    async def _run():
        signal = await _wait_for_offer_state_async(
            page, timeout_s=timeout_s, poll_interval_s=0.02
        )
        offer_state_resolved = signal != "timeout"
        skip = (
            not offer_state_resolved
            and not explicit_oos
            and price is None
            and bool(title)
            and await _page_offers_skeleton_async(page, asin="B0TEST0001")
        )
        return skip, signal

    return asyncio.run(_run())


class SkeletonSuppressionTests(unittest.TestCase):
    """Skeleton skip must be suppressed whenever an offer signal was seen."""

    def test_skeleton_markers_but_accordion_signal_suppresses_skip(self) -> None:
        # #corePrice empty + no offscreen price => _page_offers_skeleton_async is True,
        # but two accordion rows mean the page hydrated to a resolvable state. The worker
        # must NOT emit a degraded_page skip.
        page = _MapPage(
            selectors={_CORE_PRICE_SELECTOR: _FakeEl("   ")},
            selector_all={
                _A_PRICE_OFFSCREEN_SELECTOR: [],
                _ACCORDION_ROW_SELECTOR: [_FakeEl(), _FakeEl()],
            },
        )
        # Sanity: the detector alone would still flag this as a skeleton.
        self.assertTrue(
            asyncio.run(_page_offers_skeleton_async(page, asin="B0TEST0001"))
        )
        would_skip, signal = _would_skip_skeleton(
            page, explicit_oos=False, price=None, title="Some Product", timeout_s=5.0
        )
        self.assertEqual(signal, "accordion")
        self.assertFalse(would_skip)

    def test_skeleton_markers_and_timeout_emits_skip(self) -> None:
        # Same empty-core skeleton, but no offer signal at all -> wait times out ->
        # the skeleton check runs and the row is skipped.
        page = _MapPage(
            selectors={_CORE_PRICE_SELECTOR: _FakeEl("")},
            selector_all={_A_PRICE_OFFSCREEN_SELECTOR: []},
        )
        would_skip, signal = _would_skip_skeleton(
            page, explicit_oos=False, price=None, title="Some Product", timeout_s=0.2
        )
        self.assertEqual(signal, "timeout")
        self.assertTrue(would_skip)

    def test_hydrated_priceless_page_not_skipped_even_on_timeout(self) -> None:
        # A page that is NOT a skeleton (offscreen price nodes exist) but resolved no
        # pay-price for our selectors: the detector returns False so no skip, regardless.
        page = _MapPage(
            selector_all={_A_PRICE_OFFSCREEN_SELECTOR: [_FakeEl("$5.00")]},
        )
        would_skip, signal = _would_skip_skeleton(
            page, explicit_oos=False, price=None, title="Some Product", timeout_s=5.0
        )
        self.assertEqual(signal, "price")
        self.assertFalse(would_skip)


if __name__ == "__main__":
    unittest.main()
