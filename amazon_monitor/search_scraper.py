import logging
import json
import math
import random
import re
import time
import unicodedata
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlparse, urlunparse

import browser_factory
from browser_factory import close_context, create_stealth_context
from exceptions import CaptchaBlocked, NetworkAccessDenied

LOGGER = logging.getLogger(__name__)

ScrapeMode = Literal["featured_full", "newest_front"]
PaginationMode = Literal["auto", "fixed"]

_MERCHANT_TOKEN_RE = re.compile(r"\b(A[0-9A-Z]{12,13})\b")

PRICE_RE = re.compile(r"\$?\s*([0-9]+(?:[.,][0-9]{1,2})?)")


def _is_network_error(error: Exception) -> bool:
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


def parse_search_metadata(html: str) -> dict[str, Any]:
    """Parse Amazon s-metadata JSON blob for pagination and marketplace id."""
    out: dict[str, Any] = {"totalResultCount": None, "asinOnPageCount": None, "marketplaceId": None}
    m = re.search(r'"totalResultCount"\s*:\s*(\d+).{0,500}?"asinOnPageCount"\s*:\s*(\d+)', html, re.DOTALL)
    if m:
        out["totalResultCount"] = int(m.group(1))
        out["asinOnPageCount"] = int(m.group(2))
    m2 = re.search(r'"marketplaceId"\s*:\s*"([A-Z0-9]+)"', html)
    if m2:
        out["marketplaceId"] = m2.group(1).upper()
    return out


def extract_merchant_ids_from_search_url(search_url: str) -> set[str]:
    """Decode rh=… and extract p_6:… seller facet merchant IDs."""
    parsed = urlparse(search_url)
    qs = parse_qs(parsed.query, keep_blank_values=True)
    found: set[str] = set()
    for rh in qs.get("rh", []):
        decoded = unquote(rh)
        for m in re.finditer(r"p_6:([A-Z0-9]{13,14})", decoded, re.IGNORECASE):
            found.add(m.group(1).upper())
    return found


def extract_merchant_tokens_from_blob(blob: str) -> set[str]:
    """Find Amazon-style merchant / marketplace tokens (A + 12–13 alnum) in HTML or text."""
    if not blob:
        return set()
    return {m.group(1).upper() for m in _MERCHANT_TOKEN_RE.finditer(blob)}


def collect_merchant_signals_for_card(
    card_inner_html: str,
    search_url: str,
    page_marketplace_id: str | None,
) -> list[str]:
    tokens: set[str] = set()
    tokens |= extract_merchant_tokens_from_blob(card_inner_html)
    tokens |= extract_merchant_ids_from_search_url(search_url)
    if page_marketplace_id:
        tokens.add(page_marketplace_id.upper())
    return sorted(tokens)


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
        "[data-cy='title-recipe']",
        "h2",
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


def _extract_by_selectors(card, selectors: tuple[str, ...]) -> tuple[str, str | None]:
    for selector in selectors:
        node = card.query_selector(selector)
        if not node:
            continue
        text = (node.inner_text() or "").strip()
        if text:
            return text, selector
    return "", None


def _extract_price(card, card_text: str) -> float | None:
    for selector in ("span.a-price span.a-offscreen", "span[data-a-color='base'] span.a-offscreen"):
        node = card.query_selector(selector)
        if not node:
            continue
        value = _parse_price((node.inner_text() or "").strip())
        if value is not None and value >= 5:
            return value
    value = _parse_price(card_text)
    if value is None:
        return None
    return value if value >= 5 else None


def _extract_price_text(card) -> tuple[str, str | None]:
    selectors = (
        "span.a-price span.a-offscreen",
        "span[data-a-color='base'] span.a-offscreen",
    )
    return _extract_by_selectors(card, selectors)


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


def _extract_availability_text(card, card_text: str) -> tuple[str, str | None]:
    selectors = (
        "span.a-size-base.a-color-price",
        "span[class*='availability']",
        "div[class*='availability'] span",
    )
    text, selector = _extract_by_selectors(card, selectors)
    return (text or card_text, selector)


def _looks_like_seller_blob(value: str) -> bool:
    text = _normalize_ascii(value)
    if not text:
        return False
    if "out of 5 stars" in text:
        return False
    return (
        "sold by" in text
        or "ships from" in text
        or "amazon export llc" in text
        or re.search(r"\bamazon\.com\b", text) is not None
    )


def _normalize_ascii(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", (value or "").lower().strip())
    return decomposed.encode("ascii", "ignore").decode("ascii")


def _extract_seller_text(card) -> tuple[str, str | None]:
    selectors = (
        "span:has-text('Sold by')",
        "span:has-text('Ships from')",
        "div.a-row:has-text('Sold by')",
        "div.a-row:has-text('Ships from')",
        "[data-seller]",
    )
    for selector in selectors:
        node = card.query_selector(selector)
        if not node:
            continue
        text = (node.inner_text() or "").strip()
        if _looks_like_seller_blob(text):
            return text, selector
    card_text = (card.inner_text() or "").strip()
    if _looks_like_seller_blob(card_text):
        m = re.search(
            r"(sold by[^\n]{0,120}|ships from[^\n]{0,120})",
            card_text,
            flags=re.IGNORECASE,
        )
        return (m.group(1).strip() if m else card_text), "card_text_fallback"
    return "", None


def _extract_shipping_text(card) -> tuple[str, str | None]:
    selectors = (
        "span:has-text('FREE Shipping')",
        "span:has-text('FREE delivery')",
        "span:has-text('to Israel')",
        "span.a-color-secondary",
    )
    return _extract_by_selectors(card, selectors)


def _extract_image_url(card) -> str | None:
    image_el = card.query_selector("img.s-image")
    if not image_el:
        return None
    dynamic_attr = image_el.get_attribute("data-a-dynamic-image") or ""
    if dynamic_attr:
        try:
            candidates = json.loads(dynamic_attr)
            if isinstance(candidates, dict) and candidates:
                return max(candidates.keys(), key=len)
        except Exception:
            pass
    srcset = image_el.get_attribute("srcset") or ""
    if srcset:
        parts = [p.strip() for p in srcset.split(",") if p.strip()]
        urls = [p.split(" ")[0] for p in parts if p]
        if urls:
            return urls[-1]
    src = image_el.get_attribute("src")
    return src.strip() if src else None


def _extract_product_url(card) -> str | None:
    link = card.query_selector("h2 a")
    if not link:
        return None
    href = (link.get_attribute("href") or "").strip()
    if not href:
        return None
    return urljoin("https://www.amazon.com", href)


def _set_page_param(search_url: str, page_num: int) -> str:
    parsed = urlparse(search_url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    query["page"] = [str(page_num)]
    new_query = urlencode(query, doseq=True)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, parsed.fragment))


def _dedupe_products_by_asin(products: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_asin: dict[str, dict[str, Any]] = {}
    for p in products:
        a = (p.get("asin") or "").strip().upper()
        if not a:
            continue
        by_asin[a] = p
    return list(by_asin.values())


def _scrape_single_attempt(
    search_url: str,
    source: str,
    collect_debug: bool,
    max_cycle_seconds: int,
    html_dump_dir: str | Path | None,
    scrape_mode: ScrapeMode,
    pagination_mode: PaginationMode,
    fixed_pages: int,
    max_search_pages: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    all_products: list[dict[str, Any]] = []
    debug_data: dict[str, Any] = {"selector_debug": [], "scrape_meta": {}}
    context = create_stealth_context(persistent_dir=None, headless=False)
    cycle_started = time.monotonic()
    total_pages_cap = 1
    page_meta_first: dict[str, Any] = {}

    try:
        page = context.new_page()
        page_num = 1
        while True:
            elapsed = time.monotonic() - cycle_started
            if elapsed > max_cycle_seconds:
                LOGGER.warning(
                    "Stopping scrape early due to cycle budget: elapsed=%.1fs limit=%ss pages_done=%s",
                    elapsed,
                    max_cycle_seconds,
                    page_num - 1,
                )
                break
            if browser_factory.global_rate_limiter:
                browser_factory.global_rate_limiter.acquire()

            current_url = _set_page_param(search_url, page_num) if page_num > 1 else search_url
            LOGGER.info("Scraping %s page %s/%s", source, page_num, total_pages_cap)
            try:
                page.goto(current_url, wait_until="domcontentloaded", timeout=45000)
            except Exception as e:
                if _is_network_error(e):
                    raise NetworkAccessDenied(f"Network error on page {page_num}: {e}", e)
                raise
            title = (page.title() or "").lower()
            if "robot check" in title or page.query_selector("form[action*='validateCaptcha']"):
                raise CaptchaBlocked(f"Captcha detected while scraping {source}")

            page.wait_for_selector("div[data-component-type='s-search-result']", timeout=25000)
            page.mouse.wheel(0, random.randint(300, 1300))
            time.sleep(random.uniform(0.5, 1.2))

            html = page.content()
            if page_num == 1:
                page_meta_first = parse_search_metadata(html)
                cards_probe = page.query_selector_all("div[data-component-type='s-search-result']")
                card_count = len(cards_probe)
                ipp = page_meta_first.get("asinOnPageCount") or card_count or 1
                total = page_meta_first.get("totalResultCount") or card_count
                if scrape_mode == "newest_front":
                    total_pages_cap = 1
                elif scrape_mode == "featured_full" and pagination_mode == "auto" and page_meta_first.get("totalResultCount"):
                    computed = max(1, math.ceil(total / max(1, ipp)))
                    total_pages_cap = min(max_search_pages, computed)
                elif scrape_mode == "featured_full":
                    total_pages_cap = min(max_search_pages, max(1, fixed_pages))
                else:
                    total_pages_cap = min(max_search_pages, max(1, fixed_pages))
                debug_data["scrape_meta"] = {
                    "total_pages_cap": total_pages_cap,
                    "page_meta": page_meta_first,
                    "scrape_mode": scrape_mode,
                    "pagination_mode": pagination_mode,
                }
                LOGGER.info(
                    "search_pagination source=%s mode=%s total_pages_cap=%s meta=%s",
                    source,
                    pagination_mode,
                    total_pages_cap,
                    page_meta_first,
                )

            if html_dump_dir is not None:
                dump_dir = Path(html_dump_dir)
                dump_dir.mkdir(parents=True, exist_ok=True)
                safe_source = re.sub(r"[^\w\-]+", "_", source).strip("_")[:80] or "search"
                out_path = dump_dir / f"{safe_source}_page{page_num}_raw.html"
                try:
                    out_path.write_text(html, encoding="utf-8")
                    LOGGER.info("Wrote raw search page HTML to %s", out_path)
                except OSError as exc:
                    LOGGER.warning("Failed to write raw search HTML %s: %s", out_path, exc)

            mpid = page_meta_first.get("marketplaceId")
            cards = page.query_selector_all("div[data-component-type='s-search-result']")
            for card in cards:
                asin = (card.get_attribute("data-asin") or "").strip()
                if not asin:
                    continue
                product_title = _extract_title(card)
                card_text = card.inner_text()
                price = _extract_price(card, card_text)
                price_text, price_selector = _extract_price_text(card)
                image_url = _extract_image_url(card)
                availability_text, availability_selector = _extract_availability_text(card, card_text)
                seller_text, seller_selector = _extract_seller_text(card)
                shipping_text, shipping_selector = _extract_shipping_text(card)
                product_url = _extract_product_url(card)
                inner_html = card.inner_html() or ""
                merchant_id_tokens = collect_merchant_signals_for_card(inner_html, current_url, mpid)

                row = {
                    "asin": asin,
                    "title": product_title,
                    "price": price,
                    "price_text": price_text,
                    "in_stock": _stock_flag(availability_text),
                    "availability_text": availability_text,
                    "seller": seller_text or source,
                    "seller_text": seller_text,
                    "shipping_text": shipping_text,
                    "image_url": image_url,
                    "product_url": product_url,
                    "source": source,
                    "search_url": current_url,
                    "merchant_id_tokens": merchant_id_tokens,
                }
                all_products.append(row)
                if collect_debug:
                    debug_data["selector_debug"].append(
                        {
                            "asin": asin,
                            "title_found": bool(product_title),
                            "price_found": price is not None,
                            "seller_found": bool(seller_text),
                            "shipping_found": bool(shipping_text),
                            "availability_found": bool(availability_text),
                            "price_selector": price_selector,
                            "seller_selector": seller_selector,
                            "shipping_selector": shipping_selector,
                            "availability_selector": availability_selector,
                            "merchant_id_tokens": merchant_id_tokens,
                            "card_html_snippet": inner_html[:4000],
                        }
                    )

            if page_num >= total_pages_cap:
                break
            if scrape_mode == "newest_front":
                break
            next_btn = page.query_selector("a.s-pagination-next")
            disabled = (next_btn.get_attribute("aria-disabled") if next_btn else "true") == "true"
            if not next_btn or disabled:
                LOGGER.info("Stopping pagination: no next page (page=%s)", page_num)
                break
            page_num += 1
            time.sleep(random.uniform(6, 12))
    finally:
        close_context(context)

    deduped = _dedupe_products_by_asin(all_products)
    return deduped, debug_data


def scrape_search(
    search_url: str,
    *,
    source: str = "main_search",
    scrape_mode: ScrapeMode = "featured_full",
    pagination_mode: PaginationMode = "auto",
    fixed_pages: int = 1,
    max_search_pages: int = 50,
    max_retries: int = 2,
    collect_debug: bool = False,
    max_cycle_seconds: int = 170,
    html_dump_dir: str | Path | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Scrape Amazon search results. No PDP visits.

    scrape_mode:
      - featured_full: multi-page (auto or fixed pagination).
      - newest_front: page 1 only.
    """
    last_error: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            LOGGER.info("Scrape attempt %s/%s for %s mode=%s", attempt + 1, max_retries + 1, source, scrape_mode)
            return _scrape_single_attempt(
                search_url,
                source,
                collect_debug=collect_debug,
                max_cycle_seconds=max_cycle_seconds,
                html_dump_dir=html_dump_dir,
                scrape_mode=scrape_mode,
                pagination_mode=pagination_mode,
                fixed_pages=fixed_pages,
                max_search_pages=max_search_pages,
            )
        except NetworkAccessDenied as e:
            last_error = e
            LOGGER.warning("Network error on attempt %s: %s", attempt + 1, e)
            if attempt < max_retries:
                time.sleep(random.uniform(2, 5))
            else:
                LOGGER.error("Network errors persisted after %s attempts", max_retries + 1)
                raise
        except CaptchaBlocked:
            raise
    if last_error:
        raise last_error
    return [], {"selector_debug": [], "scrape_meta": {}}
