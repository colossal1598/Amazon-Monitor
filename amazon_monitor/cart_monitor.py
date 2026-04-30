import logging
import re
from pathlib import Path
from typing import Any

import browser_factory
from browser_factory import close_context, create_stealth_context
from exceptions import SessionExpired

LOGGER = logging.getLogger(__name__)
ASIN_RE = re.compile(r"/(?:dp|gp/product)/([A-Z0-9]{10})")
PRICE_RE = re.compile(r"\$?\s*([0-9]+(?:[.,][0-9]{1,2})?)")


def _parse_price(text: str) -> float | None:
    if not text:
        return None
    for match in PRICE_RE.findall(text.replace(",", "")):
        try:
            return float(match)
        except ValueError:
            continue
    return None


def fetch_cart(auth_dir: str = "auth/amazon") -> list[dict[str, Any]]:
    auth_path = Path(auth_dir)
    auth_path.mkdir(parents=True, exist_ok=True)
    context = create_stealth_context(persistent_dir=str(auth_path), headless=False)
    snapshots: list[dict[str, Any]] = []
    try:
        page = context.new_page()
        if browser_factory.global_rate_limiter:
            browser_factory.global_rate_limiter.acquire()
        page.goto("https://www.amazon.com/gp/cart/view.html", wait_until="domcontentloaded", timeout=45000)
        if "ap/signin" in page.url or page.query_selector("input#ap_email"):
            raise SessionExpired("Amazon cart session expired")
        page.wait_for_selector("div#sc-active-cart, form#activeCartViewForm", timeout=25000)
        items = page.query_selector_all("#sc-active-cart .sc-list-item")
        for item in items:
            link = item.query_selector("a.sc-product-link")
            href = link.get_attribute("href") if link else ""
            match = ASIN_RE.search(href or "")
            asin = match.group(1) if match else ""
            if not asin:
                continue
            price_text = ""
            for selector in (".sc-product-price", ".a-price .a-offscreen"):
                node = item.query_selector(selector)
                if node:
                    price_text = node.inner_text()
                    if price_text:
                        break
            stock_text = item.inner_text().lower()
            snapshots.append(
                {
                    "asin": asin,
                    "price": _parse_price(price_text),
                    "in_stock": "out of stock" not in stock_text and ("in stock" in stock_text or "available" in stock_text),
                }
            )
        return snapshots
    finally:
        close_context(context)

