"""Multi-page SERP scraping: later pages are best-effort.

Regression for prod 2026-07-21 13:59: with aes_max_pages=2, a soft-error
interstitial on &page=2 raised out of the scrape and discarded page 1's items —
the whole AES cycle failed where the old single-page behavior would have
succeeded. A failed page 2+ must break the loop and return what earlier pages
collected; only page 1 failures (and fatal captcha/disconnect/network errors)
fail the cycle.
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

import search_scraper
from exceptions import CaptchaBlocked


class _FakeCard:
    pass


class _FakeNextButton:
    async def get_attribute(self, _name: str) -> str:
        return "false"


class _FakePage:
    def __init__(self) -> None:
        self.goto_urls: list[str] = []

    async def goto(self, url: str, **_kw) -> None:
        self.goto_urls.append(url)

    async def content(self) -> str:
        return "<html></html>"

    async def query_selector(self, _selector: str):
        return _FakeNextButton()

    async def query_selector_all(self, _selector: str):
        return [_FakeCard()]

    async def close(self) -> None:
        pass


class _FakeContext:
    def __init__(self, page: _FakePage) -> None:
        self._page = page

    async def new_page(self) -> _FakePage:
        return self._page


async def _noop_async(*_args, **_kw) -> None:
    return None


async def _fake_collect_row(_card, *, source: str, current_url: str) -> dict:
    # Distinct ASIN per page: the scrape dedupes globally by ASIN before returning.
    asin = "B0TESTPAGE2" if "page=2" in current_url else "B0TESTPAGE1"
    return {"asin": asin, "title": "Pokemon Card", "source": source}


def _run_scrape(wait_cards_fn, fixed_pages: int = 2):
    page = _FakePage()

    async def go():
        return await search_scraper.scrape_search_on_context_async(
            _FakeContext(page),
            "https://www.amazon.com/s?i=merchant-items&me=TEST",
            source="aes_llc",
            scrape_mode="newest_front",
            pagination_mode="fixed",
            fixed_pages=fixed_pages,
            max_search_pages=fixed_pages,
            collect_debug=False,
            max_cycle_seconds=60,
            serp_scroll_profile="minimal",
            pagination_delay_range=(0.0, 0.0),
            scroll_delay_range=(0.0, 0.0),
        )

    with (
        patch.object(search_scraper, "_serp_stabilize_page_async", _noop_async),
        patch.object(search_scraper, "_serp_captcha_or_raise_async", _noop_async),
        patch.object(search_scraper, "_wait_serp_result_cards_async", wait_cards_fn),
        patch.object(search_scraper, "_scroll_serp_to_settle_async", _noop_async),
        patch.object(search_scraper, "_collect_product_row_async", _fake_collect_row),
        patch.object(search_scraper, "_fallback_main_slot_asin_roots_async", _noop_async_list),
        patch.object(search_scraper, "_carousel_tile_roots_async", _noop_async_list),
    ):
        return asyncio.run(go()), page


async def _noop_async_list(*_args, **_kw) -> list:
    return []


class TestMultipageBestEffort(unittest.TestCase):
    def test_page2_soft_error_keeps_page1_items(self) -> None:
        async def wait_cards(_page, current_url: str, _source, **_kw) -> None:
            if "page=2" in current_url:
                raise RuntimeError("Amazon soft-error interstitial detected")

        (items, _debug), page = _run_scrape(wait_cards)
        self.assertEqual(len(page.goto_urls), 2)
        self.assertIn("page=2", page.goto_urls[1])
        self.assertEqual([i["asin"] for i in items], ["B0TESTPAGE1"])

    def test_page1_failure_still_raises(self) -> None:
        async def wait_cards(_page, _current_url, _source, **_kw) -> None:
            raise RuntimeError("Amazon soft-error interstitial detected")

        with self.assertRaises(RuntimeError):
            _run_scrape(wait_cards)

    def test_captcha_on_page2_still_raises(self) -> None:
        # Captcha is session-level state the engine must handle (pause + recycle) —
        # never swallowed as a best-effort page failure.
        async def wait_cards(_page, current_url: str, _source, **_kw) -> None:
            if "page=2" in current_url:
                raise CaptchaBlocked("captcha on SERP page 2")

        with self.assertRaises(CaptchaBlocked):
            _run_scrape(wait_cards)

    def test_both_pages_ok_collects_both(self) -> None:
        (items, _debug), page = _run_scrape(_noop_async)
        self.assertEqual(len(page.goto_urls), 2)
        self.assertEqual(sorted(i["asin"] for i in items), ["B0TESTPAGE1", "B0TESTPAGE2"])


if __name__ == "__main__":
    unittest.main()
