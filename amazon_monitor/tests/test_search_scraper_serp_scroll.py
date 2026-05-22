"""SERP scroll profile wiring without Playwright."""

import inspect
from typing import get_args

import pytest

from search_scraper import SerpScrollProfile, scrape_search


def test_scrape_search_accepts_minimal_serp_scroll_profile() -> None:
    sig = inspect.signature(scrape_search)
    param = sig.parameters["serp_scroll_profile"]
    assert param.default == "full"
    assert "minimal" in get_args(SerpScrollProfile)
