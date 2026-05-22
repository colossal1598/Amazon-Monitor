import os
import random
import threading
import time
from pathlib import Path
from typing import Optional

from playwright.sync_api import BrowserContext, Route, sync_playwright
from playwright_stealth.stealth import Stealth

import usage_metrics

_HEAVY_RESOURCE_TYPES = frozenset({"image", "media", "font"})

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


def should_abort_heavy_request(route: Route) -> bool:
    return route.request.resource_type in _HEAVY_RESOURCE_TYPES


def _heavy_resource_route_handler(route: Route) -> None:
    if should_abort_heavy_request(route):
        usage_metrics.bump_blocked()
        route.abort()
    else:
        route.continue_()


def register_heavy_resource_blocking_sync(context: BrowserContext) -> None:
    context.route("**/*", _heavy_resource_route_handler)


async def register_heavy_resource_blocking_async(context) -> None:
    async def handler(route: Route) -> None:
        if should_abort_heavy_request(route):
            usage_metrics.bump_blocked()
            await route.abort()
        else:
            await route.continue_()

    await context.route("**/*", handler)


def _apply_amazon_cookie_prefs(context: BrowserContext) -> None:
    """USD + English storefront prefs before first navigation (Israel locale/geo unchanged)."""
    context.add_cookies(
        [
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
    )


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


# Close the browser cleanly and also stop the Playwright runner we started with it.
def close_context(context: BrowserContext) -> None:
    pw_runner = getattr(context, "_pw_runner", None)
    context.close()
    if pw_runner is not None:
        pw_runner.stop()
