import logging
import random
import re
import time
from typing import Any

import browser_factory
from browser_factory import close_context, create_stealth_context
from exceptions import CaptchaBlocked

LOGGER = logging.getLogger(__name__)
PRICE_RE = re.compile(r"\$?\s*([0-9]+(?:[.,][0-9]{1,2})?)")


def _parse_price(raw_text: str) -> float | None:
    if not raw_text:
        return None
    matches = PRICE_RE.findall(raw_text.replace(",", ""))
    if not matches:
        return None
    values = []
    for match in matches:
        try:
            values.append(float(match))
        except ValueError:
            continue
    return min(values) if values else None


def _stock_flag(text: str) -> bool:
    lowered = (text or "").lower()
    terms = ("in stock", "only", "temporarily", "available")
    return any(term in lowered for term in terms)


def scrape_search(urls_dict: dict[str, str], pages: int = 5) -> list[dict[str, Any]]:
    all_products: list[dict[str, Any]] = []
    for seller, url in urls_dict.items():
        context = create_stealth_context(persistent_dir=None, headless=False)
        try:
            page = context.new_page()
            current_url = url
            for page_num in range(1, pages + 1):
                if browser_factory.global_rate_limiter:
                    browser_factory.global_rate_limiter.acquire()
                LOGGER.info("Scraping %s page %s", seller, page_num)
                page.goto(current_url, wait_until="domcontentloaded", timeout=45000)
                title = (page.title() or "").lower()
                if "robot check" in title or page.query_selector("form[action*='validateCaptcha']"):
                    raise CaptchaBlocked(f"Captcha detected while scraping {seller}")

                page.wait_for_selector("div[data-component-type='s-search-result']", timeout=25000)
                page.mouse.wheel(0, random.randint(300, 1300))
                time.sleep(random.uniform(0.5, 1.2))
                cards = page.query_selector_all("div[data-component-type='s-search-result']")
                for card in cards:
                    asin = (card.get_attribute("data-asin") or "").strip()
                    if not asin:
                        continue
                    title_el = card.query_selector("h2 a span")
                    product_title = title_el.inner_text().strip() if title_el else ""
                    card_text = card.inner_text()
                    price = _parse_price(card_text)
                    image_el = card.query_selector("img.s-image")
                    image_url = image_el.get_attribute("src").strip() if image_el else None
                    all_products.append(
                        {
                            "asin": asin,
                            "title": product_title,
                            "price": price,
                            "in_stock": _stock_flag(card_text),
                            "seller": seller,
                            "image_url": image_url,
                        }
                    )

                next_btn = page.query_selector("a.s-pagination-next")
                disabled = (next_btn.get_attribute("aria-disabled") if next_btn else "true") == "true"
                if not next_btn or disabled:
                    break
                if browser_factory.global_rate_limiter:
                    browser_factory.global_rate_limiter.acquire()
                next_btn.click()
                page.wait_for_load_state("domcontentloaded", timeout=30000)
                current_url = page.url
                time.sleep(random.uniform(6, 12))
        finally:
            close_context(context)
    return all_products

