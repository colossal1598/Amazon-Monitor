import logging
import json
import random
import re
import time
from typing import Any

import browser_factory
from browser_factory import close_context, create_stealth_context
from exceptions import CaptchaBlocked, NetworkAccessDenied

LOGGER = logging.getLogger(__name__)


def _is_network_error(error: Exception) -> bool:
    """Check if error is a retryable network-level failure."""
    err_str = str(error).lower()
    network_patterns = [
        "err_network_access_denied",
        "err_network_changed",
        "err_connection_refused",
        "err_connection_reset",
        "err_connection_timed_out",
        "err_internet_disconnected",
        "net::err_",
        "timeout",
    ]
    return any(p in err_str for p in network_patterns)
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
        "h2",
        "[data-cy='title-recipe']",
        "a.a-link-normal.s-line-clamp-2",
        "h2 a span",
        "h2 span",
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


def _scrape_single_attempt(search_url: str, pages: int, seller: str) -> list[dict[str, Any]]:
    """Single scrape attempt. Raises CaptchaBlocked or NetworkAccessDenied on failure."""
    all_products: list[dict[str, Any]] = []
    context = create_stealth_context(persistent_dir=None, headless=False)
    try:
        page = context.new_page()
        current_url = search_url
        for page_num in range(1, pages + 1):
            if browser_factory.global_rate_limiter:
                browser_factory.global_rate_limiter.acquire()
            LOGGER.info("Scraping %s page %s", seller, page_num)
            try:
                page.goto(current_url, wait_until="domcontentloaded", timeout=45000)
            except Exception as e:
                if _is_network_error(e):
                    raise NetworkAccessDenied(f"Network error on page {page_num}: {e}", e)
                raise
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
                price = _extract_price(card, card_text)
                image_url = _extract_image_url(card)
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


def scrape_search(search_url: str, pages: int = 5, max_retries: int = 2) -> list[dict[str, Any]]:
    """Scrape one Amazon search results URL with automatic retry on network errors.

    Args:
        search_url: The Amazon search URL to scrape.
        pages: Maximum number of pages to scrape.
        max_retries: Number of retry attempts for network errors (not captcha).

    Raises:
        CaptchaBlocked: If Amazon shows a captcha/robot check.
        NetworkAccessDenied: If network errors persist after all retries.
    """
    seller = "amazon_export"
    last_error: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            LOGGER.info("Scrape attempt %s/%s for %s", attempt + 1, max_retries + 1, seller)
            return _scrape_single_attempt(search_url, pages, seller)
        except NetworkAccessDenied as e:
            last_error = e
            LOGGER.warning("Network error on attempt %s: %s", attempt + 1, e)
            if attempt < max_retries:
                delay = random.uniform(2, 5)
                LOGGER.info("Retrying after %.1fs...", delay)
                time.sleep(delay)
            else:
                LOGGER.error("Network errors persisted after %s attempts", max_retries + 1)
                raise
        except CaptchaBlocked:
            # Don't retry captcha — needs IP rotation
            raise

    # Should never reach here
    if last_error:
        raise last_error
    return []
