import logging
import json
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


def _extract_title(card) -> str:
    selectors = (
        "h2 a span",
        "h2 span",
        "a.a-link-normal.s-line-clamp-2 span",
        "[data-cy='title-recipe'] span",
    )
    for selector in selectors:
        node = card.query_selector(selector)
        if not node:
            continue
        text = (node.inner_text() or "").strip()
        if text:
            return text
    return ""


def _extract_price(card, card_text: str) -> float | None:
    # Prefer Amazon's explicit visible/full price field when present.
    for selector in ("span.a-price span.a-offscreen", "span[data-a-color='base'] span.a-offscreen"):
        node = card.query_selector(selector)
        if not node:
            continue
        value = _parse_price((node.inner_text() or "").strip())
        if value is not None and value >= 5:
            return value

    # Fallback to parsed card text but reject obvious noise values.
    value = _parse_price(card_text)
    if value is None:
        return None
    return value if value >= 5 else None


def _stock_flag(text: str) -> bool:
    lowered = (text or "").lower()
    out_of_stock_terms = (
        "currently unavailable",
        "out of stock",
        "unavailable",
    )
    if any(term in lowered for term in out_of_stock_terms):
        return False
    in_stock_terms = (
        "in stock",
        "only ",
        "left in stock",
    )
    return any(term in lowered for term in in_stock_terms)


def _is_sold_by_amazon(card_text: str) -> bool:
    lowered = (card_text or "").lower()
    positive_terms = (
        "sold by amazon.com",
        "sold by amazon",
        "ships from and sold by amazon.com",
        "ships from and sold by amazon",
        "dispatched from and sold by amazon",
    )
    if any(term in lowered for term in positive_terms):
        return True
    # If card text contains a seller marker but not Amazon, treat as non-Amazon.
    if "sold by " in lowered:
        return False
    if "other sellers on amazon" in lowered:
        return False
    return False


def _extract_image_url(card) -> str | None:
    image_el = card.query_selector("img.s-image")
    if not image_el:
        return None

    # Best source: Amazon often provides a JSON map of URLs in data-a-dynamic-image.
    dynamic_attr = image_el.get_attribute("data-a-dynamic-image") or ""
    if dynamic_attr:
        try:
            candidates = json.loads(dynamic_attr)
            if isinstance(candidates, dict) and candidates:
                return max(candidates.keys(), key=len)
        except Exception:
            pass

    # Next best: srcset can include larger variants.
    srcset = image_el.get_attribute("srcset") or ""
    if srcset:
        parts = [p.strip() for p in srcset.split(",") if p.strip()]
        urls = [p.split(" ")[0] for p in parts if p]
        if urls:
            return urls[-1]

    src = image_el.get_attribute("src")
    return src.strip() if src else None


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
                    product_title = _extract_title(card)
                    card_text = card.inner_text()
                    amazon_sold = _is_sold_by_amazon(card_text) if seller == "amazon_com" else False
                    if seller == "amazon_com" and not amazon_sold:
                        continue
                    price = _extract_price(card, card_text)
                    image_url = _extract_image_url(card)
                    all_products.append(
                        {
                            "asin": asin,
                            "title": product_title,
                            "price": price,
                            "in_stock": _stock_flag(card_text),
                            "seller": seller,
                            "amazon_sold": amazon_sold,
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

