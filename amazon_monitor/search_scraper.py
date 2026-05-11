import logging
import json
import math
import random
import re
import time
import unicodedata
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

import browser_factory
from browser_factory import close_context, create_stealth_context
from exceptions import CaptchaBlocked, NetworkAccessDenied
from filter_pipeline import normalize_title_line
from serp_card_price import card_list_price

LOGGER = logging.getLogger(__name__)

ScrapeMode = Literal["featured_full", "newest_front"]
PaginationMode = Literal["auto", "fixed"]


# Recognize the “internet is blocked/broken” kinds of failures so the monitor can pause and recover instead of just retrying a page forever.
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


# Read the page’s built-in metadata so we can estimate how many pages exist and stop scraping at a sensible point.
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


# Pull a human-readable product title from a search result card by trying a few common spots until something looks good.
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
            return normalize_title_line(text) or ""
    return ""


# Try a list of selectors and return the first non-empty text plus which selector worked (useful for debugging).
def _extract_by_selectors(card, selectors: tuple[str, ...]) -> tuple[str, str | None]:
    for selector in selectors:
        node = card.query_selector(selector)
        if not node:
            continue
        text = (node.inner_text() or "").strip()
        if text:
            return text, selector
    return "", None


# Get the product’s card price (as a number) by reading Amazon’s visible price elements on the search card.
def _extract_price(card) -> float | None:
    """List price from Amazon’s price UI only (`price-recipe` / `a-offscreen`). No whole-card scrape."""
    price_recipe = card.query_selector('[data-cy="price-recipe"]')
    if price_recipe:
        for off in price_recipe.query_selector_all("span.a-price span.a-offscreen"):
            raw = (off.inner_text() or "").strip()
            value = card_list_price(raw)
            if value is not None:
                return value
        blob = (price_recipe.inner_text() or "").strip()
        value = card_list_price(blob)
        if value is not None:
            return value

    for selector in ("span.a-price span.a-offscreen", "span[data-a-color='base'] span.a-offscreen"):
        node = card.query_selector(selector)
        if not node:
            continue
        value = card_list_price((node.inner_text() or "").strip())
        if value is not None:
            return value

    return None


# Get the “price as text” from the card (like “$12.34”) so we can log or display what Amazon actually showed.
def _extract_price_text(card) -> tuple[str, str | None]:
    price_recipe = card.query_selector('[data-cy="price-recipe"]')
    if price_recipe:
        for off in price_recipe.query_selector_all("span.a-price span.a-offscreen"):
            text = (off.inner_text() or "").strip()
            if text:
                return text, '[data-cy="price-recipe"] span.a-price span.a-offscreen'
    selectors = (
        "span.a-price span.a-offscreen",
        "span[data-a-color='base'] span.a-offscreen",
    )
    return _extract_by_selectors(card, selectors)


# Turn availability wording into a simple in-stock / out-of-stock guess for alerts and state tracking.
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


# Find the best availability text we can (or fall back to the whole card text) so stock guessing has something to work with.
def _extract_availability_text(card, card_text: str) -> tuple[str, str | None]:
    selectors = (
        "span.a-size-base.a-color-price",
        "span[class*='availability']",
        "div[class*='availability'] span",
    )
    text, selector = _extract_by_selectors(card, selectors)
    return (text or card_text, selector)


# Decide whether a chunk of text is probably the “sold by / ships from” info and not something unrelated like reviews.
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


# Simplify text into a plain form so matching works even when Amazon adds symbols, accents, or odd spacing.
def _normalize_ascii(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", (value or "").lower().strip())
    return decomposed.encode("ascii", "ignore").decode("ascii")


# PUIS / search-card regions that usually contain Sold by / Ships from (try before :has-text).
_SELLER_DOM_REGION_SELECTORS: tuple[str, ...] = (
    "div[data-cy='offer-recipe']",
    "div[data-cy='secondary-offer-recipe']",
    "div[data-cy='delivery-recipe']",
    "div[data-cy='seller-recipe']",
    ".puis-min-offer-desktop-container",
    ".puisg-col-inner .a-section.a-spacing-none.a-spacing-top-micro",
    "div.s-delivery-recipe",
)


# Pull the primary delivery message from Amazon’s newer delivery block, which is often the cleanest shipping signal on the card.
def _extract_delivery_block_primary(card) -> tuple[str, str | None]:
    """Amazon UDM delivery row: data-cy=delivery-block, primary line often in udm-primary-delivery-message."""
    block = card.query_selector('div[data-cy="delivery-block"]')
    if not block:
        return "", None
    primary = block.query_selector(".udm-primary-delivery-message")
    if primary:
        text = (primary.inner_text() or "").strip()
        if text:
            return text, 'div[data-cy="delivery-block"] .udm-primary-delivery-message'
    text = (block.inner_text() or "").strip()
    if text:
        return text, 'div[data-cy="delivery-block"]'
    return "", None


# Extract the “sold by / ships from” text from the card so later filters can keep only the sellers you care about.
def _extract_seller_text(card) -> tuple[str, str | None]:
    for selector in _SELLER_DOM_REGION_SELECTORS:
        node = card.query_selector(selector)
        if not node:
            continue
        text = (node.inner_text() or "").strip()
        if _looks_like_seller_blob(text):
            return text, selector
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


# Pull older-style shipping hints from the card for cases where the newer delivery block isn’t present.
def _extract_shipping_text_legacy(card) -> tuple[str, str | None]:
    selectors = (
        "span:has-text('FREE Shipping')",
        "span:has-text('FREE delivery')",
        "span:has-text('to Israel')",
        "span.a-color-secondary",
    )
    return _extract_by_selectors(card, selectors)


# Get the best shipping/delivery text we can from the card, preferring the main delivery block and falling back to older hints.
def _extract_shipping_text(card) -> tuple[str, str | None]:
    """Prefer SERP delivery-block (matches PDP-style UDM); fall back to legacy span hints."""
    text, sel = _extract_delivery_block_primary(card)
    if text:
        return text, sel
    return _extract_shipping_text_legacy(card)


# Find a reasonably large product image URL so WhatsApp alerts can include a picture when available.
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


# Build a full product link from the search card so alerts can point straight to the item.
def _extract_product_url(card) -> str | None:
    link = card.query_selector("h2 a")
    if not link:
        return None
    href = (link.get_attribute("href") or "").strip()
    if not href:
        return None
    return urljoin("https://www.amazon.com", href)


# Create the next-page URL by setting the “page=” query value so we can paginate without guessing link structures.
def _set_page_param(search_url: str, page_num: int) -> str:
    parsed = urlparse(search_url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    query["page"] = [str(page_num)]
    new_query = urlencode(query, doseq=True)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, parsed.fragment))


# Keep only the latest row per ASIN so the final output list doesn’t contain duplicates from multiple cards or tiles.
def _dedupe_products_by_asin(products: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_asin: dict[str, dict[str, Any]] = {}
    for p in products:
        a = (p.get("asin") or "").strip().upper()
        if not a:
            continue
        by_asin[a] = p
    return list(by_asin.values())


_ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")


# Check if an ASIN looks valid so we skip empty or malformed card entries.
def _valid_asin(value: str | None) -> bool:
    return bool(value and _ASIN_RE.match(value.strip().upper()))


# Scroll the “More results” heading into view so lazy blocks below the first fold mount (AES / dense SERPs).
def _scroll_serp_more_results_into_view(page, scroll_delay_range: tuple[float, float]) -> None:
    try:
        page.locator('h2:has-text("More results")').first.scroll_into_view_if_needed(timeout=5000)
        time.sleep(random.uniform(scroll_delay_range[0], scroll_delay_range[1]))
    except Exception as exc:
        LOGGER.debug("serp_more_results_scroll skipped: %s", exc)


# Extra organic row roots under the main slot (same ASINs as s-search-result when Amazon splits sections).
def _fallback_main_slot_asin_roots(page) -> list[Any]:
    roots: list[Any] = []
    seen_id: set[int] = set()
    for sel in (
        "div.s-main-slot div.s-result-item.s-asin[data-asin]",
        "div.s-main-slot div[role='listitem'][data-asin]",
    ):
        try:
            nodes = page.query_selector_all(sel)
        except Exception:
            nodes = []
        for node in nodes:
            try:
                kid = id(node)
            except Exception:
                continue
            if kid in seen_id:
                continue
            asin = (node.get_attribute("data-asin") or "").strip()
            if not _valid_asin(asin):
                continue
            seen_id.add(kid)
            roots.append(node)
    return roots


# Slowly scroll the results page until it settles so lazy-loaded cards and tiles have a chance to appear before we collect them.
def _scroll_serp_to_settle(page, scroll_delay_range: tuple[float, float], max_steps: int = 10) -> None:
    """Step-scroll the results page so lazy-loaded rows/carousel tiles attach before querying."""
    prev_h = -1
    stable_rounds = 0
    for _ in range(max_steps):
        h = page.evaluate(
            "() => Math.max(document.documentElement.scrollHeight, document.body && document.body.scrollHeight || 0)"
        )
        try:
            h_int = int(h) if h is not None else 0
        except (TypeError, ValueError):
            h_int = 0
        vh = page.evaluate("() => window.innerHeight") or 800
        try:
            vh_int = max(400, int(vh))
        except (TypeError, ValueError):
            vh_int = 800

        if h_int > 0 and abs(h_int - prev_h) < 40:
            stable_rounds += 1
            if stable_rounds >= 2:
                break
        else:
            stable_rounds = 0
        prev_h = h_int

        page.mouse.wheel(0, random.randint(int(vh_int * 0.55), int(vh_int * 0.95)))
        time.sleep(random.uniform(scroll_delay_range[0], scroll_delay_range[1]))


# Turn one search result card into a single structured product snapshot (asin/title/price/shipping/etc) for the rest of the pipeline.
def _collect_product_row(
    card,
    *,
    source: str,
    current_url: str,
) -> dict[str, Any] | None:
    """Build one product dict from a PUI card root (organic s-search-result or carousel tile)."""
    a = (card.get_attribute("data-asin") or "").strip().upper()
    if not _valid_asin(a):
        return None

    product_title = _extract_title(card)
    card_text = card.inner_text()
    price = _extract_price(card)
    price_text, price_selector = _extract_price_text(card)
    image_url = _extract_image_url(card)
    availability_text, availability_selector = _extract_availability_text(card, card_text)
    seller_text, seller_selector = _extract_seller_text(card)
    shipping_text, shipping_selector = _extract_shipping_text(card)
    product_url = _extract_product_url(card)
    inner_html = card.inner_html() or ""

    return {
        "asin": a,
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
        "_debug": {
            "price_selector": price_selector,
            "seller_selector": seller_selector,
            "shipping_selector": shipping_selector,
            "availability_selector": availability_selector,
            "inner_html": inner_html,
        },
    }


# Add a small debug record for this row so you can see which selectors did or didn’t work when scraping changes.
def _append_debug_for_row(debug_data: dict[str, Any], row: dict[str, Any]) -> None:
    d = row.get("_debug") or {}
    inner = str(d.get("inner_html") or "")
    debug_data["selector_debug"].append(
        {
            "asin": row.get("asin"),
            "title_found": bool(row.get("title")),
            "price_found": row.get("price") is not None,
            "seller_found": bool(row.get("seller_text")),
            "shipping_found": bool(row.get("shipping_text")),
            "availability_found": bool(row.get("availability_text")),
            "price_selector": d.get("price_selector"),
            "seller_selector": d.get("seller_selector"),
            "shipping_selector": d.get("shipping_selector"),
            "availability_selector": d.get("availability_selector"),
            "card_html_snippet": inner[:4000],
        }
    )


# Remove debug-only data from a row so normal runs store and process only the product fields you care about.
def _strip_debug(row: dict[str, Any]) -> dict[str, Any]:
    row.pop("_debug", None)
    return row


# Find extra product tiles (like featured carousels) that aren’t standard search-result cards but still contain real ASINs.
def _carousel_tile_roots(page) -> list[Any]:
    """Thematic / sponsored horizontal tiles (not wrapped in data-component-type=s-search-result)."""
    roots: list[Any] = []
    seen_el: set[int] = set()
    for sel in (
        ".s-searchgrid-carousel div[data-asin]",
        "[cel_widget_id*='FEATURED_ASINS_LIST'] div[data-asin].s-result-item",
    ):
        try:
            nodes = page.query_selector_all(sel)
        except Exception:
            nodes = []
        for node in nodes:
            try:
                key = id(node)
            except Exception:
                continue
            if key in seen_el:
                continue
            seen_el.add(key)
            asin = (node.get_attribute("data-asin") or "").strip()
            if not _valid_asin(asin):
                continue
            if not node.query_selector('[data-cy="asin-faceout-container"]'):
                continue
            roots.append(node)
    return roots


def _serp_captcha_or_raise(page, source: str) -> None:
    title = (page.title() or "").lower()
    if "robot check" in title or page.query_selector("form[action*='validateCaptcha']"):
        raise CaptchaBlocked(f"Captcha detected while scraping {source}")


def _wait_serp_result_cards(
    page,
    current_url: str,
    source: str,
    *,
    serp_inner_retries: int,
    selector_timeout_ms: int = 25_000,
    goto_timeout_ms: int = 45_000,
) -> None:
    """Wait for SERP result cards; on timeout, re-goto the search URL up to ``serp_inner_retries`` times."""
    recoveries = max(0, serp_inner_retries)
    total_rounds = 1 + recoveries
    for round_idx in range(total_rounds):
        try:
            page.wait_for_selector(
                "div[data-component-type='s-search-result']",
                timeout=selector_timeout_ms,
            )
            return
        except PlaywrightTimeoutError:
            if round_idx >= total_rounds - 1:
                raise
            title_snip = ((page.title() or "").strip())[:160]
            LOGGER.warning(
                "SERP selector timeout source=%s round=%s/%s url=%s title=%r — retrying goto",
                source,
                round_idx + 1,
                total_rounds,
                page.url,
                title_snip,
            )
            time.sleep(random.uniform(0.5, 1.5))
            try:
                page.goto(current_url, wait_until="domcontentloaded", timeout=goto_timeout_ms)
            except Exception as e:
                if _is_network_error(e):
                    raise NetworkAccessDenied(
                        f"Network error during SERP recovery round {round_idx + 1}: {e}",
                        e,
                    ) from e
                raise
            _serp_captcha_or_raise(page, source)


# Do one full scrape attempt across one or more pages, collecting product rows while respecting time and page limits.
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
    *,
    scroll_delay_range: tuple[float, float] = (0.25, 0.65),
    pagination_delay_range: tuple[float, float] = (2.0, 4.5),
    serp_inner_retries: int = 2,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    all_products: list[dict[str, Any]] = []
    debug_data: dict[str, Any] = {"selector_debug": [], "scrape_meta": {}}
    context = create_stealth_context(persistent_dir=None, headless=True)
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
            _serp_captcha_or_raise(page, source)

            _wait_serp_result_cards(
                page,
                current_url,
                source,
                serp_inner_retries=serp_inner_retries,
            )
            _scroll_serp_to_settle(page, scroll_delay_range)
            _scroll_serp_more_results_into_view(page, scroll_delay_range)
            _scroll_serp_to_settle(page, scroll_delay_range, max_steps=6)

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

            seen_asins_page: set[str] = set()
            cards = page.query_selector_all("div[data-component-type='s-search-result']")
            n_primary = 0
            for card in cards:
                row = _collect_product_row(card, source=source, current_url=current_url)
                if not row:
                    continue
                if row["asin"] in seen_asins_page:
                    continue
                seen_asins_page.add(row["asin"])
                n_primary += 1
                if collect_debug:
                    _append_debug_for_row(debug_data, row)
                all_products.append(_strip_debug(dict(row)))

            n_fallback = 0
            for card in _fallback_main_slot_asin_roots(page):
                a = (card.get_attribute("data-asin") or "").strip().upper()
                if a in seen_asins_page:
                    continue
                row = _collect_product_row(card, source=source, current_url=current_url)
                if not row:
                    continue
                seen_asins_page.add(row["asin"])
                n_fallback += 1
                if collect_debug:
                    _append_debug_for_row(debug_data, row)
                all_products.append(_strip_debug(dict(row)))
            LOGGER.debug(
                "serp_card_counts source=%s page=%s primary=%s fallback_added=%s",
                source,
                page_num,
                n_primary,
                n_fallback,
            )

            for card in _carousel_tile_roots(page):
                a = (card.get_attribute("data-asin") or "").strip().upper()
                if a in seen_asins_page:
                    continue
                row = _collect_product_row(card, source=source, current_url=current_url)
                if not row:
                    continue
                seen_asins_page.add(row["asin"])
                if collect_debug:
                    _append_debug_for_row(debug_data, row)
                all_products.append(_strip_debug(dict(row)))

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
            time.sleep(random.uniform(pagination_delay_range[0], pagination_delay_range[1]))
    finally:
        close_context(context)

    deduped = _dedupe_products_by_asin(all_products)
    return deduped, debug_data


# Scrape Amazon search results with retries and simple modes so the monitor can gather candidates without visiting individual product pages.
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
    scroll_delay_range: tuple[float, float] | None = None,
    pagination_delay_range: tuple[float, float] | None = None,
    serp_inner_retries: int = 2,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Scrape Amazon search results. No PDP visits.

    scrape_mode:
      - featured_full: multi-page (auto or fixed pagination).
      - newest_front: page 1 only.
    """
    sdr = scroll_delay_range if scroll_delay_range is not None else (0.25, 0.65)
    pdr = pagination_delay_range if pagination_delay_range is not None else (2.0, 4.5)
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
                scroll_delay_range=sdr,
                pagination_delay_range=pdr,
                serp_inner_retries=serp_inner_retries,
            )
        except NetworkAccessDenied as e:
            last_error = e
            LOGGER.warning("Network error on attempt %s: %s", attempt + 1, e)
            if attempt < max_retries:
                time.sleep(random.uniform(1.5, 3.0))
            else:
                LOGGER.error("Network errors persisted after %s attempts", max_retries + 1)
                raise
        except PlaywrightTimeoutError as e:
            last_error = e
            LOGGER.warning("Scrape timeout on attempt %s: %s", attempt + 1, e)
            if attempt < max_retries:
                time.sleep(random.uniform(1.5, 3.0))
            else:
                LOGGER.error("Scrape timeouts persisted after %s attempts", max_retries + 1)
                raise
        except CaptchaBlocked:
            raise
    if last_error:
        raise last_error
    return [], {"selector_debug": [], "scrape_meta": {}}
