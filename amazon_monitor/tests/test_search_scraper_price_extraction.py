"""Unit tests for search_scraper's price extraction (`_extract_price` / async twin).

These use light fake Playwright-like nodes (no real browser) to assert the
selector *order of preference*: the current-price selector that excludes
``.a-text-price`` (the struck-through "was" price) must win over the broader
selector / whole-card fallback.
"""

import unittest

from search_scraper import _extract_price, _extract_price_async

_PRECISE_SELECTOR = "span.a-price:not(.a-text-price) span.a-offscreen"
_BROAD_SELECTOR = "span.a-price span.a-offscreen"


class _FakeOffscreen:
    def __init__(self, text: str) -> None:
        self._text = text

    def inner_text(self) -> str:
        return self._text


class _FakePriceRecipe:
    def __init__(self, by_selector: dict[str, list[_FakeOffscreen]], blob: str = "") -> None:
        self._by_selector = by_selector
        self._blob = blob

    def query_selector_all(self, selector: str) -> list[_FakeOffscreen]:
        return self._by_selector.get(selector, [])

    def inner_text(self) -> str:
        return self._blob


class _FakeCard:
    def __init__(self, price_recipe: _FakePriceRecipe | None) -> None:
        self._price_recipe = price_recipe

    def query_selector(self, selector: str):
        if selector == '[data-cy="price-recipe"]':
            return self._price_recipe
        return None


class TestExtractPricePrefersCurrentOverStrikethrough(unittest.TestCase):
    def test_strikethrough_list_price_is_not_picked_even_if_seen_first(self) -> None:
        """Broad selector returns the struck-through list price first; precise selector must win."""
        price_recipe = _FakePriceRecipe(
            {
                _PRECISE_SELECTOR: [_FakeOffscreen("$54.99")],
                _BROAD_SELECTOR: [_FakeOffscreen("$69.99"), _FakeOffscreen("$54.99")],
            },
            blob="$69.99$54.99",
        )
        card = _FakeCard(price_recipe)
        self.assertEqual(_extract_price(card), 54.99)

    def test_falls_back_to_broad_selector_when_precise_selector_empty(self) -> None:
        price_recipe = _FakePriceRecipe(
            {
                _PRECISE_SELECTOR: [],
                _BROAD_SELECTOR: [_FakeOffscreen("$40.00")],
            }
        )
        card = _FakeCard(price_recipe)
        self.assertEqual(_extract_price(card), 40.00)

    def test_falls_back_to_blob_when_no_offscreen_spans(self) -> None:
        price_recipe = _FakePriceRecipe({}, blob="$25.50")
        card = _FakeCard(price_recipe)
        self.assertEqual(_extract_price(card), 25.50)

    def test_no_price_recipe_returns_none(self) -> None:
        card = _FakeCard(None)
        self.assertIsNone(_extract_price(card))


class _FakeOffscreenAsync:
    def __init__(self, text: str) -> None:
        self._text = text

    async def inner_text(self) -> str:
        return self._text


class _FakePriceRecipeAsync:
    def __init__(self, by_selector: dict[str, list[_FakeOffscreenAsync]], blob: str = "") -> None:
        self._by_selector = by_selector
        self._blob = blob

    async def query_selector_all(self, selector: str) -> list[_FakeOffscreenAsync]:
        return self._by_selector.get(selector, [])

    async def inner_text(self) -> str:
        return self._blob


class _FakeCardAsync:
    def __init__(self, price_recipe: _FakePriceRecipeAsync | None) -> None:
        self._price_recipe = price_recipe

    async def query_selector(self, selector: str):
        if selector == '[data-cy="price-recipe"]':
            return self._price_recipe
        return None


class TestExtractPriceAsyncPrefersCurrentOverStrikethrough(unittest.IsolatedAsyncioTestCase):
    async def test_strikethrough_list_price_is_not_picked_even_if_seen_first(self) -> None:
        price_recipe = _FakePriceRecipeAsync(
            {
                _PRECISE_SELECTOR: [_FakeOffscreenAsync("$54.99")],
                _BROAD_SELECTOR: [_FakeOffscreenAsync("$69.99"), _FakeOffscreenAsync("$54.99")],
            },
            blob="$69.99$54.99",
        )
        card = _FakeCardAsync(price_recipe)
        self.assertEqual(await _extract_price_async(card), 54.99)

    async def test_falls_back_to_broad_selector_when_precise_selector_empty(self) -> None:
        price_recipe = _FakePriceRecipeAsync(
            {
                _PRECISE_SELECTOR: [],
                _BROAD_SELECTOR: [_FakeOffscreenAsync("$40.00")],
            }
        )
        card = _FakeCardAsync(price_recipe)
        self.assertEqual(await _extract_price_async(card), 40.00)


if __name__ == "__main__":
    unittest.main()
