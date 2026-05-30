"""PDP (product detail page) scrape for configured watch ASINs (Amazon first-party offers)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import time
from typing import Any

import browser_factory
from browser_factory import USER_AGENTS, STEALTH, register_heavy_resource_blocking_async
from exceptions import NetworkAccessDenied
from image_urls import pick_amazon_image_url
from pdp_helpers import is_not_shippable_text, normalize_ascii, valid_asin
from usage_metrics import NetMeter
import usage_metrics

LOGGER = logging.getLogger(__name__)

# Navigation uses commit (not domcontentloaded) when heavy resources are blocked; title selectors gate scrape readiness.
_PDP_GOTO_TIMEOUT_MS = 12_000
_PDP_TITLE_WAIT_MS_DEFAULT = 15_000
_PDP_PRICE_WAIT_MS_DEFAULT = 4_000
_PDP_MAX_ATTEMPTS = 3
_PDP_TITLE_READY_SELECTORS = "#productTitle, #title, h1.a-size-large"
# Post-nav gate: any of these means the PDP shell is far enough along to scrape.
_PDP_DOM_GATE_SELECTORS = (
    "#productTitle, #title, h1.a-size-large, "
    "#desktop_buybox, #buybox, "
    "#corePrice_feature_div, #corePriceDisplay_desktop_feature_div, "
    "#availability, #outOfStock"
)
# Buy-box containers (attach before leaf price nodes hydrate).
_PDP_PRICE_CONTAINER_SELECTORS = (
    "#corePrice_feature_div, #corePriceDisplay_desktop_feature_div, "
    "#desktop_buybox, #buybox, #tabular-buybox, #qualifiedBuybox"
)
_PDP_PRICE_LEAF_SELECTORS = (
    "#corePrice_feature_div .a-price .a-offscreen, "
    "#corePriceDisplay_desktop_feature_div .a-price .a-offscreen, "
    ".reinventPricePriceToPayMargin .a-price .a-offscreen, "
    ".apex-pricetopay-value .a-offscreen, "
    "#apex-pricetopay-accessibility-label, "
    "#tp_price_block_total_price_ww .a-offscreen, "
    "span.a-price.a-text-price .a-offscreen, "
    "#desktop_buybox .a-price-whole, #buybox .a-price-whole, "
    ".a-price .a-offscreen"
)
_PDP_PRICE_WAIT_SELECTORS = f"{_PDP_PRICE_CONTAINER_SELECTORS}, {_PDP_PRICE_LEAF_SELECTORS}"
_PDP_DOM_GATE_MS_DEFAULT = 5_000
_PDP_RETRY_BACKOFF_SECONDS = (1.5, 3.0)

_PRICE_RE = re.compile(r"\$?\s*([0-9][0-9,]*)(?:\.(\d{2}))?")
_DELIVERY_RELEVANT_RE = re.compile(
    r"delivery|shipping|ship to|ships to|arrives|import charges|^\$[\d,.]+\s*delivery|₪|ils",
    re.IGNORECASE,
)

_EXPLICIT_OOS_RE = re.compile(
    r"currently unavailable|temporarily out of stock|out of stock|unavailable|"
    r"we don't know when or if this item will be back in stock|"
    r"see all buying options",
    re.IGNORECASE,
)


# Simplify text into an easy-to-compare form so seller and shipping wording matches even when formatting differs.
def _normalize_for_match(value: str) -> str:
    return normalize_ascii(value)


# Check if the product’s seller/shipping text includes any of the allowed seller hints you configured.
def merchant_matches_allowed(merchant_blob: str, allowed_substrings: list[str]) -> bool:
    """True if any normalized substring appears in the normalized merchant/shipping blob."""
    blob = _normalize_for_match(merchant_blob)
    for sub in allowed_substrings:
        s = _normalize_for_match(str(sub))
        if s and s in blob:
            return True
    return False


# Recognize “the whole connection is failing” errors so the monitor can pause and recover instead of treating it like a single bad product page.
def _is_network_error(error: Exception) -> bool:
    """True only for clear global-network failures; a single-page timeout is per-ASIN, not global."""
    err_str = str(error).lower()
    network_patterns = (
        "err_network_access_denied",
        "err_network_changed",
        "err_connection_refused",
        "err_connection_reset",
        "err_connection_timed_out",
        "err_internet_disconnected",
        "net::err_",
    )
    return any(p in err_str for p in network_patterns)


def _clamp_pdp_concurrency(raw: int) -> int:
    return max(1, min(3, int(raw)))


def _coerce_tab_jitter_pair(raw: Any, default: tuple[float, float]) -> tuple[float, float]:
    if isinstance(raw, (list, tuple)) and len(raw) >= 2:
        try:
            lo, hi = float(raw[0]), float(raw[1])
            if lo <= hi and lo >= 0:
                return lo, hi
        except (TypeError, ValueError):
            pass
    return default


# Pull a usable price number out of a chunk of page text so we can decide if the offer is actually buyable.
def _parse_price_text(text: str) -> float | None:
    m = _PRICE_RE.search(text or "")
    if not m:
        return None
    dollars = m.group(1).replace(",", "")
    cents = m.group(2) or "00"
    try:
        return float(f"{dollars}.{cents}")
    except ValueError:
        return None


# Try a few known Amazon page spots to find the buy-box price and return quickly when the page doesn’t have one.
def _extract_pdp_price(page) -> float | None:
    """Use query_selector only (no locator auto-wait) so missing buy box returns fast."""
    for sel in (
        "#corePrice_feature_div .a-price .a-offscreen",
        "#corePriceDisplay_desktop_feature_div .a-price .a-offscreen",
        ".reinventPricePriceToPayMargin .a-price .a-offscreen",
        ".apex-pricetopay-value .a-offscreen",
        "#apex-pricetopay-accessibility-label",
        "#tp_price_block_total_price_ww .a-offscreen",
        "span.a-price.a-text-price .a-offscreen",
    ):
        try:
            el = page.query_selector(sel)
            if not el:
                continue
            raw = (el.inner_text() or "").strip()
        except Exception:
            raw = ""
        p = _parse_price_text(raw)
        if p is not None:
            return p
    for root_sel in ("#desktop_buybox", "#buybox", "#offerDisplayFeature_feature_div", "body"):
        try:
            root = page.query_selector(root_sel)
            if not root:
                continue
            whole = root.query_selector(".a-price-whole")
            frac = root.query_selector(".a-price-fraction")
            if whole and frac:
                w = (whole.inner_text() or "").strip().replace(",", "").replace(".", "")
                f = (frac.inner_text() or "").strip()
                if w.isdigit() and f.isdigit():
                    return float(f"{w}.{f}")
        except Exception:
            continue
    return None


async def _await_pdp_dom_gate_async(page: Any, timeout_ms: int) -> bool:
    """Best-effort wait for PDP shell after navigation commit."""
    wait_ms = max(1_000, int(timeout_ms))
    try:
        await page.wait_for_selector(
            _PDP_DOM_GATE_SELECTORS,
            state="attached",
            timeout=wait_ms,
        )
        return True
    except Exception:
        return False


_PDP_BUYBOX_PRESENT_SELECTORS = (
    "#desktop_buybox",
    "#buybox",
    "#tabular-buybox",
    "#qualifiedBuybox",
    "#unqualifiedBuyBox",
    "#corePrice_feature_div",
    "#corePriceDisplay_desktop_feature_div",
    "#add-to-cart-button",
    "#buy-now-button",
    "#priceblock_ourprice",
    "#priceblock_dealprice",
    ".a-price",
)


async def _pdp_buybox_present_async(page: Any) -> bool:
    """True when a buy-box or any price region exists (price may still be hydrating)."""
    for sel in _PDP_BUYBOX_PRESENT_SELECTORS:
        try:
            if await page.query_selector(sel):
                return True
        except Exception:
            continue
    return False


async def _pdp_present_markers_async(page: Any) -> str:
    """Comma list of price/buybox markers found on the page (for missing-price diagnostics)."""
    found: list[str] = []
    for sel in _PDP_BUYBOX_PRESENT_SELECTORS:
        try:
            if await page.query_selector(sel):
                found.append(sel)
        except Exception:
            continue
    return ",".join(found) if found else "none"


async def _resolve_buybox_price_async(
    page: Any,
    max_wait_ms: int,
    *,
    asin: str = "",
    skip_wait: bool = False,
) -> tuple[float | None, bool]:
    """Extract buy-box price; wait for price DOM only when the first extract misses."""
    price = await _extract_pdp_price_async(page)
    if price is not None:
        return price, False
    if skip_wait:
        return None, False
    if not await _pdp_buybox_present_async(page):
        LOGGER.info(
            "PDP price wait skipped asin=%s (no buybox on page)",
            asin or "?",
            extra={"channel": "debug"},
        )
        return None, False
    wait_ms = max(1_000, int(max_wait_ms))
    try:
        await page.wait_for_selector(
            _PDP_PRICE_WAIT_SELECTORS,
            state="attached",
            timeout=wait_ms,
        )
    except Exception:
        LOGGER.info(
            "PDP price wait timeout asin=%s ms=%s",
            asin or "?",
            wait_ms,
            extra={"channel": "debug"},
        )
    price = await _extract_pdp_price_async(page)
    if asin:
        if price is None:
            markers = await _pdp_present_markers_async(page)
            LOGGER.info(
                "PDP price still missing asin=%s after wait markers=%s",
                asin,
                markers,
                extra={"channel": "debug"},
            )
        else:
            LOGGER.info(
                "PDP price wait used asin=%s price_found=True",
                asin,
                extra={"channel": "debug"},
            )
    return price, True


async def _resolve_title_async(page: Any, max_wait_ms: int) -> tuple[str, bool]:
    """Extract title after price; wait for title DOM only when the first extract is empty."""
    title = await _extract_pdp_title_async(page) or (await page.title() or "").strip()
    if title:
        return title, False
    wait_ms = max(1_000, int(max_wait_ms))
    try:
        await page.wait_for_selector(
            _PDP_TITLE_READY_SELECTORS,
            state="attached",
            timeout=wait_ms,
        )
    except Exception:
        LOGGER.info(
            "PDP title wait timeout ms=%s",
            wait_ms,
            extra={"channel": "debug"},
        )
    title = await _extract_pdp_title_async(page) or (await page.title() or "").strip()
    return title, True


# Read the product’s visible title from the page so alerts show a clean name instead of a generic page title.
def _extract_pdp_title(page) -> str:
    try:
        node = page.query_selector("#productTitle") or page.query_selector("#title")
        if not node:
            return ""
        return (node.inner_text() or "").strip()
    except Exception:
        return ""


# Grab a main product image URL (when available) so WhatsApp alerts can include a picture.
def _extract_pdp_image(page) -> str | None:
    for sel in ("#landingImage", "#imgBlkFront", "#main-image"):
        try:
            el = page.query_selector(sel)
            if not el:
                continue
            dynamic_attr = el.get_attribute("data-a-dynamic-image") or ""
            if dynamic_attr:
                try:
                    candidates = json.loads(dynamic_attr)
                    if isinstance(candidates, dict) and candidates:
                        picked = pick_amazon_image_url(candidates, rank=1)
                        if picked:
                            return picked
                except Exception:
                    pass
            href = el.get_attribute("src")
            if href and href.startswith("http"):
                return href.strip()
        except Exception:
            continue
    return None


# Extract the delivery/shipping message from the product page so alerts can show whether it ships to your location.
def _extract_pdp_shipping(page) -> str:
    lines: list[str] = []

    def add_line(value: str | None) -> None:
        if not value:
            return
        for part in re.split(r"[\r\n]+", value):
            line = " ".join(part.split())
            if line and line not in lines:
                lines.append(line)

    for root_sel in ("#qualifiedBuybox", "#desktop_buybox", "#buybox", "#offerDisplayFeature_feature_div"):
        try:
            root = page.query_selector(root_sel)
            if not root:
                continue
            for el in root.query_selector_all("span.a-color-secondary"):
                text = (el.inner_text() or "").strip()
                if text and _DELIVERY_RELEVANT_RE.search(text):
                    add_line(text)
        except Exception:
            continue

    for sel in (
        "[id^='mir-layout-DELIVERY_BLOCK-slot-']",
        "#deliveryBlockMessage",
        "#mir-layout-DELIVERY_BLOCK-slot-PRIMARYDELIVERYBLOCKLARGE",
        "#mir-layout-DELIVERY_BLOCK-slot-PRIMARY_DELIVERY_MESSAGE_LARGE",
        "#ddmDeliveryMessage",
        "[data-cy='delivery-recipe']",
    ):
        try:
            el = page.query_selector(sel)
            if not el:
                continue
            t = (el.inner_text() or "").strip()
            if t:
                add_line(t)
        except Exception:
            continue

    try:
        for el in page.query_selector_all("[data-csa-c-delivery-price]"):
            price = (el.get_attribute("data-csa-c-delivery-price") or "").strip()
            text = (el.inner_text() or "").strip()
            add_line(" ".join(x for x in (price, text) if x))
    except Exception:
        pass

    return "\n".join(lines)


def _explicit_oos_from_text(text: str | None) -> bool:
    if not text:
        return False
    clean = " ".join(str(text).split())
    return bool(_EXPLICIT_OOS_RE.search(clean))


async def _extract_availability_text_async(page: Any) -> str:
    for sel in ("#availability", "#outOfStock", "#desktop_buybox #availability"):
        try:
            el = await page.query_selector(sel)
            if not el:
                continue
            t = (await el.inner_text() or "").strip()
            if t:
                return t
        except Exception:
            continue
    return ""


async def _detect_explicit_oos_async(page: Any, *, availability_text: str) -> tuple[bool, str | None]:
    """Best-effort explicit out-of-stock detection.

    If we can *explicitly* detect OOS, we return (True, reason). Otherwise (False, None).
    """
    if _explicit_oos_from_text(availability_text):
        return True, "explicit_oos_text"
    try:
        node = await page.query_selector("#outOfStock")
        if node:
            return True, "outofstock_container"
    except Exception:
        pass
    return False, None


# Collect the page’s merchant and buy-box text into one blob so we can confirm the seller matches what you allow.
def _pdp_merchant_blob(page) -> str:
    parts: list[str] = []
    for feature_name in ("desktop-merchant-info", "desktop-fulfiller-info"):
        try:
            root = page.query_selector(
                f'.offer-display-feature-text[offer-display-feature-name="{feature_name}"]'
            )
            if not root:
                continue
            text = (root.inner_text() or "").strip()
            if text and text not in parts:
                parts.append(text)
        except Exception:
            continue
    for sel in (
        "#merchantInfoFeature_feature_div",
        "#tabular-buybox",
        "#offerDisplayFeature_feature_div",
        "#desktop_buybox",
        "#buybox",
        "#desktop_accordion",
    ):
        try:
            node = page.query_selector(sel)
            if not node:
                continue
            t = (node.inner_text() or "").strip()
            if t and t not in parts:
                parts.append(t)
        except Exception:
            continue
    return "\n".join(parts)


# Build one standardized row for the state engine from a PDP scrape so the rest of the monitor can treat it like a normal observation.
def _pdp_row(
    asin: str,
    *,
    title: str,
    price: float | None,
    shipping_text: str,
    image_url: str | None,
    merchant_blob: str,
    allowed: list[str],
    availability_text: str = "",
    explicit_oos: bool = False,
) -> dict[str, Any]:
    seller_ok = merchant_matches_allowed(merchant_blob, allowed)
    shippable_ok = not is_not_shippable_text(shipping_text)
    qualifies = price is not None and seller_ok and shippable_ok

    stock_confidence = "unknown"
    stock_reason: str | None = None
    if qualifies:
        stock_confidence = "confirmed_in"
    elif explicit_oos:
        stock_confidence = "confirmed_out"
        stock_reason = "explicit_oos"
    else:
        if price is None:
            stock_reason = "missing_price"
        elif not seller_ok:
            stock_reason = "seller_mismatch"
        elif not shippable_ok:
            stock_reason = "not_shippable"
        else:
            stock_reason = "not_qualified"
    return {
        "asin": asin,
        "title": title,
        "price": price if qualifies else None,
        "in_stock": bool(qualifies),
        "stock_confidence": stock_confidence,
        "stock_reason": stock_reason,
        "shipping_text": shipping_text,
        "availability_text": availability_text,
        "image_url": image_url,
        "seller": "pdp_watch",
        "seller_text": merchant_blob[:2000],
        "product_url": f"https://www.amazon.com/dp/{asin}",
        "source": "pdp_watch",
    }


# Create a “do not update this ASIN” marker when a single product page fails, so a bad scrape doesn’t flip the database state.
def _pdp_skip_row(asin: str, reason: str) -> dict[str, Any]:
    """Marker row for one-page operational failures: state engine must not touch the DB row."""
    return {
        "asin": asin,
        "_skip_update": True,
        "skip_reason": reason,
        "source": "pdp_watch",
    }


# --- Async PDP helpers (Playwright async API; matches sync selector logic above). -----------------

async def _extract_pdp_title_async(page: Any) -> str:
    try:
        node = await page.query_selector("#productTitle") or await page.query_selector("#title")
        if not node:
            return ""
        return (await node.inner_text() or "").strip()
    except Exception:
        return ""


async def _read_text_async(el: Any) -> str:
    """Read element text, falling back to text_content for clipped (.a-offscreen) nodes."""
    raw = ""
    try:
        raw = (await el.inner_text() or "").strip()
    except Exception:
        raw = ""
    if raw:
        return raw
    try:
        return (await el.text_content() or "").strip()
    except Exception:
        return ""


async def _extract_pdp_price_async(page: Any) -> float | None:
    for sel in (
        "#corePrice_feature_div .a-price .a-offscreen",
        "#corePriceDisplay_desktop_feature_div .a-price .a-offscreen",
        ".reinventPricePriceToPayMargin .a-price .a-offscreen",
        ".apex-pricetopay-value .a-offscreen",
        "#apex-pricetopay-accessibility-label",
        "#tp_price_block_total_price_ww .a-offscreen",
        "span.a-price.a-text-price .a-offscreen",
        "#tabular-buybox .a-price .a-offscreen",
        "#qualifiedBuybox .a-price .a-offscreen",
        "#priceblock_ourprice",
        "#priceblock_dealprice",
        "#price",
        ".a-price .a-offscreen",
    ):
        try:
            el = await page.query_selector(sel)
            if not el:
                continue
            raw = await _read_text_async(el)
        except Exception:
            raw = ""
        p = _parse_price_text(raw)
        if p is not None:
            return p
    for root_sel in (
        "#desktop_buybox",
        "#buybox",
        "#tabular-buybox",
        "#qualifiedBuybox",
        "#corePriceDisplay_desktop_feature_div",
        "#offerDisplayFeature_feature_div",
        "body",
    ):
        try:
            root = await page.query_selector(root_sel)
            if not root:
                continue
            whole = await root.query_selector(".a-price-whole")
            frac = await root.query_selector(".a-price-fraction")
            if whole:
                w = (await _read_text_async(whole)).replace(",", "").replace(".", "")
                f = (await _read_text_async(frac)) if frac else ""
                if w.isdigit():
                    cents = f if f.isdigit() else "00"
                    return float(f"{w}.{cents}")
        except Exception:
            continue
    return None


async def _extract_pdp_image_async(page: Any) -> str | None:
    for sel in ("#landingImage", "#imgBlkFront", "#main-image"):
        try:
            el = await page.query_selector(sel)
            if not el:
                continue
            dynamic_attr = await el.get_attribute("data-a-dynamic-image") or ""
            if dynamic_attr:
                try:
                    candidates = json.loads(dynamic_attr)
                    if isinstance(candidates, dict) and candidates:
                        picked = pick_amazon_image_url(candidates, rank=1)
                        if picked:
                            return picked
                except Exception:
                    pass
            href = await el.get_attribute("src")
            if href and href.startswith("http"):
                return href.strip()
        except Exception:
            continue
    return None


async def _extract_pdp_shipping_async(page: Any) -> str:
    lines: list[str] = []

    def add_line(value: str | None) -> None:
        if not value:
            return
        for part in re.split(r"[\r\n]+", value):
            line = " ".join(part.split())
            if line and line not in lines:
                lines.append(line)

    for root_sel in ("#qualifiedBuybox", "#desktop_buybox", "#buybox", "#offerDisplayFeature_feature_div"):
        try:
            root = await page.query_selector(root_sel)
            if not root:
                continue
            for el in await root.query_selector_all("span.a-color-secondary"):
                text = (await el.inner_text() or "").strip()
                if text and _DELIVERY_RELEVANT_RE.search(text):
                    add_line(text)
        except Exception:
            continue

    for sel in (
        "[id^='mir-layout-DELIVERY_BLOCK-slot-']",
        "#deliveryBlockMessage",
        "#mir-layout-DELIVERY_BLOCK-slot-PRIMARYDELIVERYBLOCKLARGE",
        "#mir-layout-DELIVERY_BLOCK-slot-PRIMARY_DELIVERY_MESSAGE_LARGE",
        "#ddmDeliveryMessage",
        "[data-cy='delivery-recipe']",
    ):
        try:
            el = await page.query_selector(sel)
            if not el:
                continue
            t = (await el.inner_text() or "").strip()
            if t:
                add_line(t)
        except Exception:
            continue

    try:
        for el in await page.query_selector_all("[data-csa-c-delivery-price]"):
            price = (await el.get_attribute("data-csa-c-delivery-price") or "").strip()
            text = (await el.inner_text() or "").strip()
            add_line(" ".join(x for x in (price, text) if x))
    except Exception:
        pass

    return "\n".join(lines)


async def _pdp_merchant_blob_async(page: Any) -> str:
    parts: list[str] = []
    for feature_name in ("desktop-merchant-info", "desktop-fulfiller-info"):
        try:
            root = await page.query_selector(
                f'.offer-display-feature-text[offer-display-feature-name="{feature_name}"]'
            )
            if not root:
                continue
            text = (await root.inner_text() or "").strip()
            if text and text not in parts:
                parts.append(text)
        except Exception:
            continue
    for sel in (
        "#merchantInfoFeature_feature_div",
        "#tabular-buybox",
        "#offerDisplayFeature_feature_div",
        "#desktop_buybox",
        "#buybox",
        "#desktop_accordion",
    ):
        try:
            node = await page.query_selector(sel)
            if not node:
                continue
            t = (await node.inner_text() or "").strip()
            if t and t not in parts:
                parts.append(t)
        except Exception:
            continue
    return "\n".join(parts)


async def _run_pdp_watch_async(
    normalized: list[str],
    allowed: list[str],
    *,
    max_cycle_seconds: float,
    scroll_delay_range: tuple[float, float],
    max_concurrent: int,
    jitter_range: tuple[float, float],
    max_attempts: int,
    headless: bool = True,
    pdp_title_wait_ms: int = _PDP_TITLE_WAIT_MS_DEFAULT,
    pdp_price_wait_ms: int = _PDP_PRICE_WAIT_MS_DEFAULT,
) -> list[dict[str, Any]]:
    from playwright.async_api import async_playwright

    cycle_started = time.monotonic()
    sem = asyncio.Semaphore(max_concurrent)
    captcha_abort = asyncio.Event()
    pdp_net_bytes = 0
    net_lock = asyncio.Lock()
    stealth_ctx_applied = False
    stealth_page_fallback_warned = False
    title_wait_ms = max(3_000, int(pdp_title_wait_ms))
    price_wait_ms = max(1_000, int(pdp_price_wait_ms))
    dom_gate_ms = min(title_wait_ms, _PDP_DOM_GATE_MS_DEFAULT)
    LOGGER.info(
        "pdp_watch starting concurrent_tabs=%s jitter=%.2f-%.2f asins=%s headless=%s "
        "title_wait_ms=%s price_wait_ms=%s",
        max_concurrent,
        jitter_range[0],
        jitter_range[1],
        len(normalized),
        headless,
        title_wait_ms,
        price_wait_ms,
    )

    async with async_playwright() as pw:
        proxy_url = os.getenv("PROXY_URL")
        launch_kwargs: dict[str, Any] = {"channel": "chrome", "headless": headless}
        if proxy_url:
            launch_kwargs["proxy"] = {"server": proxy_url}

        browser = await pw.chromium.launch(**launch_kwargs)
        ua = random.choice(USER_AGENTS)
        viewport = {"width": random.randint(1870, 1970), "height": random.randint(1030, 1130)}
        ctx_kwargs = {
            "user_agent": ua,
            "viewport": viewport,
            "locale": "en-IL",
            "timezone_id": "Asia/Jerusalem",
            "geolocation": {"latitude": 31.5, "longitude": 34.8},
            "permissions": ["geolocation"],
        }
        context = await browser.new_context(**ctx_kwargs)
        await register_heavy_resource_blocking_async(context)
        await context.set_extra_http_headers({"Accept-Language": "en-IL,en;q=0.9"})
        await context.add_cookies(
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

        # Apply stealth at the BrowserContext level (async-safe). This should mirror browser_factory.create_stealth_context
        # behavior where stealth is applied to all pages via a "page" event hook.
        try:
            try:
                # playwright-stealth >=2.0.x (2026) API
                from playwright_stealth import Stealth as AsyncStealth  # type: ignore
            except Exception:
                # Older import style used in this repo for sync mode
                from playwright_stealth.stealth import Stealth as AsyncStealth  # type: ignore

            stealth_obj = AsyncStealth()
            apply_ctx = getattr(stealth_obj, "apply_stealth_async", None)
            if callable(apply_ctx):
                await apply_ctx(context)
                stealth_ctx_applied = True
            else:
                raise AttributeError("Stealth.apply_stealth_async not available")
        except Exception as exc:
            LOGGER.warning("pdp_watch stealth not applied (continuing): %s", exc)

        async def worker(idx: int, asin: str) -> tuple[int, dict[str, Any]]:
            nonlocal stealth_page_fallback_warned, pdp_net_bytes
            if captcha_abort.is_set():
                return idx, _pdp_skip_row(asin, "captcha_run_aborted")
            async with sem:
                if captcha_abort.is_set():
                    return idx, _pdp_skip_row(asin, "captcha_run_aborted")
                if time.monotonic() - cycle_started > max_cycle_seconds:
                    return idx, _pdp_skip_row(asin, "cycle_budget_exceeded")

                await asyncio.sleep(random.uniform(jitter_range[0], jitter_range[1]))
                if captcha_abort.is_set():
                    return idx, _pdp_skip_row(asin, "captcha_run_aborted")

                if browser_factory.global_rate_limiter:
                    await asyncio.to_thread(browser_factory.global_rate_limiter.acquire)

                if captcha_abort.is_set():
                    return idx, _pdp_skip_row(asin, "captcha_run_aborted")
                if time.monotonic() - cycle_started > max_cycle_seconds:
                    return idx, _pdp_skip_row(asin, "cycle_budget_exceeded")

                url = f"https://www.amazon.com/dp/{asin}"
                last_reason = "navigation_failed"
                for attempt in range(1, max_attempts + 1):
                    if captcha_abort.is_set():
                        return idx, _pdp_skip_row(asin, "captcha_run_aborted")
                    page = await context.new_page()
                    meter = NetMeter()
                    meter.attach_async(page)
                    try:
                        page.set_default_timeout(2_000)
                        page.set_default_navigation_timeout(_PDP_GOTO_TIMEOUT_MS)
                        if not stealth_ctx_applied:
                            # Best-effort fallback: try existing sync stealth against the page.
                            # If it fails, warn once per run and proceed.
                            try:
                                await asyncio.to_thread(STEALTH.apply_stealth_sync, page)
                            except Exception as exc:
                                if not stealth_page_fallback_warned:
                                    stealth_page_fallback_warned = True
                                    LOGGER.warning("pdp_watch per-page stealth fallback failed (continuing): %s", exc)

                        try:
                            await page.goto(
                                url,
                                wait_until=browser_factory.NAV_WAIT_UNTIL,
                                timeout=_PDP_GOTO_TIMEOUT_MS,
                            )
                        except Exception as e:
                            if _is_network_error(e):
                                raise NetworkAccessDenied(f"PDP network error for {asin}: {e}", e) from e
                            last_reason = "navigation_failed"
                            LOGGER.warning(
                                "PDP navigation failed asin=%s attempt=%s/%s wait_until=%s: %s",
                                asin,
                                attempt,
                                max_attempts,
                                browser_factory.NAV_WAIT_UNTIL,
                                e,
                            )
                            if attempt < max_attempts:
                                await asyncio.sleep(random.uniform(*_PDP_RETRY_BACKOFF_SECONDS))
                                continue
                            await asyncio.sleep(random.uniform(scroll_delay_range[0], scroll_delay_range[1]))
                            return idx, _pdp_skip_row(asin, last_reason)

                        LOGGER.info(
                            "PDP navigation committed asin=%s attempt=%s/%s; resolving price then title",
                            asin,
                            attempt,
                            max_attempts,
                        )
                        title_l = (await page.title() or "").lower()
                        cap_el = await page.query_selector("form[action*='validateCaptcha']")
                        if "robot check" in title_l or cap_el:
                            LOGGER.warning("PDP captcha detected asin=%s (skipping update)", asin)
                            captcha_abort.set()
                            await asyncio.sleep(random.uniform(scroll_delay_range[0], scroll_delay_range[1]))
                            return idx, _pdp_skip_row(asin, "captcha")

                        dom_ok = await _await_pdp_dom_gate_async(page, dom_gate_ms)
                        if not dom_ok:
                            LOGGER.info(
                                "PDP dom gate miss asin=%s within %sms (continuing)",
                                asin,
                                dom_gate_ms,
                                extra={"channel": "debug"},
                            )

                        availability_text = await _extract_availability_text_async(page)
                        explicit_oos, explicit_reason = await _detect_explicit_oos_async(
                            page, availability_text=availability_text
                        )
                        price_wait_used = False
                        if explicit_oos:
                            price = None
                        else:
                            price, price_wait_used = await _resolve_buybox_price_async(
                                page,
                                price_wait_ms,
                                asin=asin,
                            )
                        title, _title_wait_used = await _resolve_title_async(page, title_wait_ms)
                        merchant_blob = await _pdp_merchant_blob_async(page)
                        shipping = await _extract_pdp_shipping_async(page)
                        image_url = await _extract_pdp_image_async(page)
                        if not explicit_oos and not title and price is None:
                            LOGGER.warning(
                                "PDP scrape empty asin=%s after price/title resolve (skipping update)",
                                asin,
                            )
                            await asyncio.sleep(random.uniform(scroll_delay_range[0], scroll_delay_range[1]))
                            return idx, _pdp_skip_row(asin, "parse_failed")
                        row = _pdp_row(
                            asin,
                            title=title,
                            price=price,
                            shipping_text=shipping,
                            image_url=image_url,
                            merchant_blob=merchant_blob,
                            allowed=allowed,
                            availability_text=availability_text,
                            explicit_oos=explicit_oos,
                        )
                        if explicit_oos and explicit_reason:
                            row["stock_reason"] = explicit_reason
                        row["price_wait_used"] = price_wait_used
                        await asyncio.sleep(random.uniform(scroll_delay_range[0], scroll_delay_range[1]))
                        return idx, row
                    except NetworkAccessDenied:
                        raise
                    except Exception as exc:
                        last_reason = "parse_failed"
                        LOGGER.warning("PDP row parse failed asin=%s: %s (skipping update)", asin, exc)
                        await asyncio.sleep(random.uniform(scroll_delay_range[0], scroll_delay_range[1]))
                        return idx, _pdp_skip_row(asin, last_reason)
                    finally:
                        await page.close()
                        async with net_lock:
                            pdp_net_bytes += meter.total_bytes

                return idx, _pdp_skip_row(asin, last_reason)

        tasks = [worker(idx, asin) for idx, asin in enumerate(normalized)]
        gathered: list[Any] = []
        try:
            gathered = await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            await context.close()
            await browser.close()

    for item in gathered:
        if isinstance(item, NetworkAccessDenied):
            raise item
    for item in gathered:
        if isinstance(item, Exception):
            LOGGER.warning("pdp_watch unexpected task error: %s", item)
            raise item

    pairs: list[tuple[int, dict[str, Any]]] = []
    for item in gathered:
        if isinstance(item, tuple) and len(item) == 2:
            pairs.append((item[0], item[1]))

    pairs.sort(key=lambda x: x[0])
    rows_out = [row for _, row in pairs]
    ok = sum(1 for r in rows_out if isinstance(r, dict) and not r.get("_skip_update"))
    skip = len(rows_out) - ok
    usage_metrics.record_pdp_phase(
        time.monotonic() - cycle_started,
        pdp_net_bytes,
        ok=ok,
        skip=skip,
    )
    return rows_out


async def scrape_pdp_watch_async(
    asins: list[str],
    allowed_seller_substrings: list[str],
    *,
    max_cycle_seconds: int = 170,
    scroll_delay_range: tuple[float, float] = (0.25, 0.65),
    max_concurrent_tabs: int = 2,
    tab_jitter_seconds: tuple[float, float] | list[float] | None = None,
    max_attempts: int = _PDP_MAX_ATTEMPTS,
    headless: bool = True,
    pdp_title_wait_ms: int = _PDP_TITLE_WAIT_MS_DEFAULT,
    pdp_price_wait_ms: int = _PDP_PRICE_WAIT_MS_DEFAULT,
) -> list[dict[str, Any]]:
    """Async version of scrape_pdp_watch for callers that already run an event loop."""
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in asins:
        a = (raw or "").strip().upper()
        if not valid_asin(a) or a in seen:
            continue
        seen.add(a)
        normalized.append(a)
    if not normalized:
        return []

    allowed = [str(s) for s in allowed_seller_substrings if str(s).strip()]
    if not allowed:
        LOGGER.warning("pdp_watch: no allowed_seller_substrings; all rows will be out of stock")

    jitter = _coerce_tab_jitter_pair(tab_jitter_seconds, (0.15, 0.55))
    conc = _clamp_pdp_concurrency(max_concurrent_tabs)

    return await _run_pdp_watch_async(
        normalized,
        allowed,
        max_cycle_seconds=float(max_cycle_seconds),
        scroll_delay_range=scroll_delay_range,
        max_concurrent=conc,
        jitter_range=jitter,
        max_attempts=max(1, int(max_attempts)),
        headless=headless,
        pdp_title_wait_ms=pdp_title_wait_ms,
        pdp_price_wait_ms=pdp_price_wait_ms,
    )


# Visit each watched product page and return a simple stock/price snapshot for each ASIN without letting one slow page break the whole cycle.
def scrape_pdp_watch(
    asins: list[str],
    allowed_seller_substrings: list[str],
    *,
    max_cycle_seconds: int = 170,
    scroll_delay_range: tuple[float, float] = (0.25, 0.65),
    max_concurrent_tabs: int = 2,
    tab_jitter_seconds: tuple[float, float] | list[float] | None = None,
    max_attempts: int = _PDP_MAX_ATTEMPTS,
    headless: bool = True,
    pdp_title_wait_ms: int = _PDP_TITLE_WAIT_MS_DEFAULT,
    pdp_price_wait_ms: int = _PDP_PRICE_WAIT_MS_DEFAULT,
) -> list[dict[str, Any]]:
    """Visit each watch ASIN on amazon.com PDP; return exactly one dict per unique valid ASIN (order preserved).

    Uses concurrent Playwright tabs (async API), capped at 3, sharing the global token bucket.

    ``in_stock`` is True only when a parseable buy-box price exists and merchant blob matches
    ``allowed_seller_substrings`` (substring match after ASCII normalization).
    """
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in asins:
        a = (raw or "").strip().upper()
        if not valid_asin(a) or a in seen:
            continue
        seen.add(a)
        normalized.append(a)
    if not normalized:
        return []

    allowed = [str(s) for s in allowed_seller_substrings if str(s).strip()]
    if not allowed:
        LOGGER.warning("pdp_watch: no allowed_seller_substrings; all rows will be out of stock")

    jitter = _coerce_tab_jitter_pair(tab_jitter_seconds, (0.15, 0.55))
    conc = _clamp_pdp_concurrency(max_concurrent_tabs)

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError(
            "scrape_pdp_watch() cannot be called while an event loop is running. "
            "Use await scrape_pdp_watch_async(...) instead."
        )

    return asyncio.run(
        _run_pdp_watch_async(
            normalized,
            allowed,
            max_cycle_seconds=float(max_cycle_seconds),
            scroll_delay_range=scroll_delay_range,
            max_concurrent=conc,
            jitter_range=jitter,
            max_attempts=max(1, int(max_attempts)),
            headless=headless,
            pdp_title_wait_ms=pdp_title_wait_ms,
            pdp_price_wait_ms=pdp_price_wait_ms,
        )
    )
