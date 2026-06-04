import os
import random
import threading
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from playwright.sync_api import BrowserContext, Route, sync_playwright
from playwright_stealth.stealth import Stealth

import usage_metrics

_HEAVY_RESOURCE_TYPES = frozenset({"image", "media", "font"})

# Substrings matched case-insensitively against request URLs.
# Safe for PDP scraping (third-party ads/analytics; not Amazon product HTML/API):
#   amazon-adsystem, googletagmanager, google-analytics, doubleclick, googlesyndication,
#   facebook.net, hotjar, scorecardresearch, newrelic, adsystem
# Use with care (Amazon-owned telemetry; usually safe but verify if something breaks):
#   fls-na (fulfillment/logging beacons on amazon domains)
DEFAULT_BANDWIDTH_BLOCK_URL_SUBSTRINGS: tuple[str, ...] = (
    "amazon-adsystem",
    "googletagmanager",
    "google-analytics",
    "doubleclick",
    "googlesyndication",
    "facebook.net",
    "hotjar",
    "scorecardresearch",
    "newrelic",
    "adsystem",
    "fls-na",
)

_AMAZON_SCRIPT_ALLOW_HOST_SUFFIXES = (
    ".amazon.com",
    ".media-amazon.com",
    ".ssl-images-amazon.com",
)

# Blocking images/fonts can prevent domcontentloaded; commit + downstream selector waits gate readiness.
NAV_WAIT_UNTIL = "commit"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.6613.120 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.6668.90 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.6723.69 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.6778.86 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.6834.110 Safari/537.36",
]

_bandwidth_config: dict[str, Any] | None = None


class TokenBucketRateLimiter:
    # Set up a simple “budget” of requests so the scraper doesn’t hit Amazon too fast.
    def __init__(self, capacity: int, refill_per_second: float) -> None:
        self.capacity = max(1, capacity)
        self.tokens = float(self.capacity)
        self.refill_per_second = max(0.0001, refill_per_second)
        self.last_refill = time.monotonic()
        self.lock = threading.Lock()

    # Wait until it’s “safe” to make another request by spending tokens and sleeping when we run out.
    def acquire(self, tokens: float = 1.0) -> None:
        while True:
            with self.lock:
                now = time.monotonic()
                elapsed = now - self.last_refill
                self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_second)
                self.last_refill = now
                if self.tokens >= tokens:
                    self.tokens -= tokens
                    return
                wait_seconds = (tokens - self.tokens) / self.refill_per_second
            time.sleep(max(0.05, wait_seconds))


global_rate_limiter: Optional[TokenBucketRateLimiter] = None
STEALTH = Stealth()


def set_bandwidth_config(config: dict[str, Any] | None) -> None:
    """Called at cycle start from main (later); stores runtime bandwidth toggles."""
    global _bandwidth_config
    _bandwidth_config = dict(config) if config else None


def get_bandwidth_config() -> dict[str, Any]:
    return dict(_bandwidth_config) if _bandwidth_config else {}


def _merged_url_block_substrings(config: dict[str, Any] | None) -> tuple[str, ...]:
    extra = (config or {}).get("bandwidth_block_url_substrings")
    if extra is None:
        return DEFAULT_BANDWIDTH_BLOCK_URL_SUBSTRINGS
    if not isinstance(extra, list) or not extra:
        return DEFAULT_BANDWIDTH_BLOCK_URL_SUBSTRINGS
    merged: list[str] = list(DEFAULT_BANDWIDTH_BLOCK_URL_SUBSTRINGS)
    for item in extra:
        token = str(item).strip().lower()
        if token and token not in merged:
            merged.append(token)
    return tuple(merged)


def should_abort_url(url: str, config: dict[str, Any] | None = None) -> bool:
    lower = (url or "").lower()
    if not lower:
        return False
    for substring in _merged_url_block_substrings(config):
        if substring in lower:
            return True
    return False


def _host_is_amazon_allowed(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return False
    if host == "amazon.com" or host.endswith(".amazon.com"):
        return True
    return any(host.endswith(suffix) for suffix in _AMAZON_SCRIPT_ALLOW_HOST_SUFFIXES if suffix.startswith("."))


def should_abort_heavy_request(route: Route) -> bool:
    return route.request.resource_type in _HEAVY_RESOURCE_TYPES


def _bandwidth_abort_kind(route: Route, config: dict[str, Any] | None) -> str | None:
    cfg = config or {}
    if should_abort_heavy_request(route):
        return "heavy"
    if cfg.get("bandwidth_block_stylesheets") and route.request.resource_type == "stylesheet":
        return "heavy"
    if cfg.get("bandwidth_block_non_amazon_script") and route.request.resource_type == "script":
        if not _host_is_amazon_allowed(route.request.url):
            return "url"
    if should_abort_url(route.request.url, cfg):
        return "url"
    return None


def should_abort_route(route: Route, config: dict[str, Any] | None = None) -> bool:
    return _bandwidth_abort_kind(route, config) is not None


def _record_bandwidth_abort(kind: str) -> None:
    if kind == "heavy":
        usage_metrics.bump_blocked()
    else:
        usage_metrics.bump_blocked_url()


def _bandwidth_route_handler_sync(route: Route) -> None:
    kind = _bandwidth_abort_kind(route, get_bandwidth_config())
    if kind is None:
        route.continue_()
        return
    _record_bandwidth_abort(kind)
    route.abort()


async def _bandwidth_route_handler_async(route: Route) -> None:
    kind = _bandwidth_abort_kind(route, get_bandwidth_config())
    if kind is None:
        await route.continue_()
        return
    _record_bandwidth_abort(kind)
    await route.abort()


def register_heavy_resource_blocking_sync(context: BrowserContext) -> None:
    context.route("**/*", _bandwidth_route_handler_sync)


async def register_heavy_resource_blocking_async(context) -> None:
    await context.route("**/*", _bandwidth_route_handler_async)


# Create one shared rate limiter for the whole run so every scrape call follows the same speed limit.
def init_global_rate_limiter(max_requests_per_minute: int) -> TokenBucketRateLimiter:
    global global_rate_limiter
    global_rate_limiter = TokenBucketRateLimiter(
        capacity=max_requests_per_minute,
        refill_per_second=max_requests_per_minute / 60.0,
    )
    return global_rate_limiter


# Apply the “look like a real browser” tweaks to a page as soon as it appears.
def _stealth_page(page) -> None:
    STEALTH.apply_stealth_sync(page)


_AMAZON_COOKIE_PREFS = [
    {
        "name": "i18n-prefs",
        "value": "USD",
        "domain": ".amazon.com",
        "path": "/",
        "secure": True,
    },
    {
        "name": "lc-main",
        "value": "en_US",
        "domain": ".amazon.com",
        "path": "/",
        "secure": True,
    },
]


def _apply_amazon_cookie_prefs(context: BrowserContext) -> None:
    """USD + English storefront prefs before first navigation (Israel locale/geo unchanged)."""
    context.add_cookies(_AMAZON_COOKIE_PREFS)


async def _apply_amazon_cookie_prefs_async(context) -> None:
    await context.add_cookies(_AMAZON_COOKIE_PREFS)


async def _apply_async_stealth(context) -> bool:
    """Apply playwright-stealth at context level; return True when applied."""
    try:
        try:
            from playwright_stealth import Stealth as AsyncStealth  # type: ignore
        except Exception:
            from playwright_stealth.stealth import Stealth as AsyncStealth  # type: ignore

        stealth_obj = AsyncStealth()
        apply_ctx = getattr(stealth_obj, "apply_stealth_async", None)
        if callable(apply_ctx):
            await apply_ctx(context)
            return True
    except Exception:
        pass
    return False


def _stealth_page_async_hook(page) -> None:
    async def _apply() -> None:
        try:
            try:
                from playwright_stealth import Stealth as AsyncStealth  # type: ignore
            except Exception:
                from playwright_stealth.stealth import Stealth as AsyncStealth  # type: ignore

            stealth_obj = AsyncStealth()
            apply_page = getattr(stealth_obj, "apply_stealth_async", None)
            if callable(apply_page):
                await apply_page(page)
                return
        except Exception:
            pass
        try:
            STEALTH.apply_stealth_sync(page)
        except Exception:
            pass

    try:
        import asyncio

        loop = asyncio.get_running_loop()
        loop.create_task(_apply())
    except RuntimeError:
        pass


# Start a Playwright browser context that tries to look human (location, language, headers) so scraping is less likely to get blocked.
def create_stealth_context(
    persistent_dir: Optional[str] = None,
    headless: bool = False,
) -> BrowserContext:
    p = sync_playwright().start()
    chromium = p.chromium
    proxy_url = os.getenv("PROXY_URL")
    ua = random.choice(USER_AGENTS)
    viewport = {"width": random.randint(1870, 1970), "height": random.randint(1030, 1130)}
    launch_args = {"channel": "chrome", "headless": headless}
    if proxy_url:
        launch_args["proxy"] = {"server": proxy_url}

    context_kwargs = {
        "user_agent": ua,
        "viewport": viewport,
        "locale": "en-IL",
        "timezone_id": "Asia/Jerusalem",
        "geolocation": {"latitude": 31.5, "longitude": 34.8},
        "permissions": ["geolocation"],
    }

    if persistent_dir:
        Path(persistent_dir).mkdir(parents=True, exist_ok=True)
        context = chromium.launch_persistent_context(persistent_dir, **launch_args, **context_kwargs)
    else:
        browser = chromium.launch(**launch_args)
        context = browser.new_context(**context_kwargs)

    context.set_extra_http_headers({"Accept-Language": "en-IL,en;q=0.9"})
    _apply_amazon_cookie_prefs(context)
    context.on("page", _stealth_page)
    for page in context.pages:
        _stealth_page(page)

    register_heavy_resource_blocking_sync(context)
    setattr(context, "_pw_runner", p)
    return context


async def create_async_stealth_context(
    pw,
    *,
    headless: bool,
    config: dict[str, Any] | None = None,
) -> tuple[Any, Any]:
    """Launch Chrome, new async context with cookies, stealth, and bandwidth blocking."""
    set_bandwidth_config(config)
    proxy_url = os.getenv("PROXY_URL")
    ua = random.choice(USER_AGENTS)
    viewport = {"width": random.randint(1870, 1970), "height": random.randint(1030, 1130)}
    launch_args: dict[str, Any] = {"channel": "chrome", "headless": headless}
    if proxy_url:
        launch_args["proxy"] = {"server": proxy_url}

    context_kwargs = {
        "user_agent": ua,
        "viewport": viewport,
        "locale": "en-IL",
        "timezone_id": "Asia/Jerusalem",
        "geolocation": {"latitude": 31.5, "longitude": 34.8},
        "permissions": ["geolocation"],
    }

    browser = await pw.chromium.launch(**launch_args)
    context = await browser.new_context(**context_kwargs)
    await register_heavy_resource_blocking_async(context)
    await context.set_extra_http_headers({"Accept-Language": "en-IL,en;q=0.9"})
    await _apply_amazon_cookie_prefs_async(context)
    stealth_applied = await _apply_async_stealth(context)
    setattr(context, "_stealth_ctx_applied", stealth_applied)
    context.on("page", _stealth_page_async_hook)
    for page in context.pages:
        _stealth_page_async_hook(page)
    return browser, context


async def close_async_browser(browser, context) -> None:
    await context.close()
    await browser.close()


# Close the browser cleanly and also stop the Playwright runner we started with it.
def close_context(context: BrowserContext) -> None:
    pw_runner = getattr(context, "_pw_runner", None)
    context.close()
    if pw_runner is not None:
        pw_runner.stop()
