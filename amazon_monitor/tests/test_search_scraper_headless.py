"""SERP headless wiring from runtime settings."""

import inspect

from search_scraper import scrape_search


def test_scrape_search_accepts_headless_kwarg() -> None:
    sig = inspect.signature(scrape_search)
    param = sig.parameters["headless"]
    assert param.default is True
