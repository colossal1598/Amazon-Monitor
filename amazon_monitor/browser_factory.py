import os
import random
import threading
import time
from pathlib import Path
from typing import Optional

from playwright.sync_api import BrowserContext, sync_playwright
from playwright_stealth import stealth_sync

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
    def __init__(self, capacity: int, refill_per_second: float) -> None:
        self.capacity = max(1, capacity)
        self.tokens = float(self.capacity)
        self.refill_per_second = max(0.0001, refill_per_second)
        self.last_refill = time.monotonic()
        self.lock = threading.Lock()

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


def init_global_rate_limiter(max_requests_per_minute: int) -> TokenBucketRateLimiter:
    global global_rate_limiter
    global_rate_limiter = TokenBucketRateLimiter(
        capacity=max_requests_per_minute,
        refill_per_second=max_requests_per_minute / 60.0,
    )
    return global_rate_limiter


def _stealth_page(page) -> None:
    stealth_sync(page)


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

    context.set_extra_http_headers({"Accept-Language": "en-IL,en;q=0.9,he;q=0.8"})
    context.on("page", _stealth_page)
    for page in context.pages:
        _stealth_page(page)

    setattr(context, "_pw_runner", p)
    return context


def close_context(context: BrowserContext) -> None:
    pw_runner = getattr(context, "_pw_runner", None)
    context.close()
    if pw_runner is not None:
        pw_runner.stop()
