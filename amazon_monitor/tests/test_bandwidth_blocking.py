"""Bandwidth route blocking helpers in browser_factory."""

from __future__ import annotations

from types import SimpleNamespace

import browser_factory
from settings_store import DEFAULT_RUNTIME_CONFIG


def _route(url: str, resource_type: str = "document"):
    return SimpleNamespace(request=SimpleNamespace(url=url, resource_type=resource_type))


def test_should_abort_url_blocks_known_ad_hosts() -> None:
    assert browser_factory.should_abort_url("https://www.googletagmanager.com/gtm.js?id=GTM-ABC")
    assert browser_factory.should_abort_url("https://www.google-analytics.com/analytics.js")
    assert browser_factory.should_abort_url("https://s.amazon-adsystem.com/iu3?d=amazon.com")
    assert browser_factory.should_abort_url("https://ads.doubleclick.net/activity")


def test_should_abort_url_allows_amazon_product_pages() -> None:
    assert not browser_factory.should_abort_url("https://www.amazon.com/dp/B012345678")
    assert not browser_factory.should_abort_url("https://m.media-amazon.com/images/I/51abc.jpg")
    assert not browser_factory.should_abort_url("https://images-na.ssl-images-amazon.com/images/I/71xyz.jpg")


def test_should_abort_url_merges_custom_substrings() -> None:
    config = {"bandwidth_block_url_substrings": ["evil-tracker.example"]}
    assert browser_factory.should_abort_url("https://cdn.evil-tracker.example/pixel.gif", config)
    assert browser_factory.should_abort_url("https://www.googletagmanager.com/gtm.js", config)


def test_stylesheet_flag_blocks_stylesheets_when_enabled() -> None:
    config = {"bandwidth_block_stylesheets": True}
    route = _route("https://www.amazon.com/gp/css/style.css", "stylesheet")
    assert browser_factory.should_abort_route(route, config)

    disabled = {"bandwidth_block_stylesheets": False}
    assert not browser_factory.should_abort_route(route, disabled)


def test_heavy_resources_still_blocked() -> None:
    route = _route("https://www.amazon.com/x.jpg", "image")
    assert browser_factory.should_abort_route(route, {})


def test_non_amazon_script_flag_blocks_external_scripts_only() -> None:
    config = {"bandwidth_block_non_amazon_script": True}
    external = _route("https://www.googletagmanager.com/gtm.js", "script")
    amazon = _route("https://www.amazon.com/gp/product/js/aui.js", "script")
    assert browser_factory.should_abort_route(external, config)
    assert not browser_factory.should_abort_route(amazon, config)


def test_settings_defaults_include_bandwidth_keys() -> None:
    assert DEFAULT_RUNTIME_CONFIG["bandwidth_block_url_substrings"] == []
    assert DEFAULT_RUNTIME_CONFIG["bandwidth_block_stylesheets"] is False
    assert DEFAULT_RUNTIME_CONFIG["bandwidth_block_non_amazon_script"] is False
