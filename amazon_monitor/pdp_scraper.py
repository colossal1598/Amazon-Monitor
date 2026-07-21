"""PDP (product detail page) scrape for configured watch ASINs (Amazon first-party offers)."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import browser_factory
from browser_factory import STEALTH, close_async_browser, create_async_stealth_context
from exceptions import BrowserDisconnected, NetworkAccessDenied, is_driver_disconnected_error
from image_urls import pick_amazon_image_url
from pdp_helpers import is_not_shippable_text, normalize_ascii, valid_asin
import usage_metrics

LOGGER = logging.getLogger(__name__)

# Navigation uses commit (not domcontentloaded) when heavy resources are blocked.
_PDP_GOTO_TIMEOUT_MS = 12_000
_PDP_SETTLE_SECONDS_DEFAULT = 8.0
_PDP_CONTINUE_SHOPPING_MAX_CLICKS_DEFAULT = 3
_PDP_MAX_ATTEMPTS = 1
# Pay-price extraction only (tight; verified against tests/fixtures/pdp HTML).
# The precise apex "price to pay" selectors come first (they never grab a
# strike-through list price). The generic `.a-price` selectors are appended as a
# fallback for transitioning/simplified buybox layouts that render a plain price
# node without the apex wrapper; `:not(.a-text-price)` keeps them off list prices.
_PDP_PRICE_PAY_SELECTORS = (
    "#qualifiedBuybox .apex-pricetopay-value .a-offscreen",
    "#corePrice_feature_div .apex-pricetopay-value .a-offscreen",
    "#corePriceDisplay_desktop_feature_div #apex-pricetopay-accessibility-label",
    "#tp_price_block_total_price_ww .a-offscreen",
    "#qualifiedBuybox .a-price:not(.a-text-price) .a-offscreen",
    "#corePrice_feature_div .a-price:not(.a-text-price) .a-offscreen",
    "#corePriceDisplay_desktop_feature_div .a-price:not(.a-text-price) .a-offscreen",
    "#buybox .a-price:not(.a-text-price) .a-offscreen",
    "#price_inside_buybox",
    "#newBuyBoxPrice",
)
_CONTINUE_SHOPPING_CLICK_SELECTORS = (
    'button:has-text("Continue shopping")',
    'input[type="submit"]',
    "button.a-button-text",
    "form button[type=submit]",
    "a.a-button-text",
)

_PRICE_RE = re.compile(r"\$?\s*([0-9][0-9,]*)(?:\.(\d{2}))?")
_DELIVERY_RELEVANT_RE = re.compile(
    r"delivery|shipping|ship to|ships to|arrives|import charges|^\$[\d,.]+\s*delivery|₪|ils",
    re.IGNORECASE,
)

# Phrases where the page explicitly states the item cannot be bought right now.
_EXPLICIT_OOS_STRONG_RE = re.compile(
    r"currently unavailable|temporarily out of stock|out of stock|unavailable|"
    r"we don't know when or if this item will be back in stock|"
    r"not available to ship",
    re.IGNORECASE,
)
# Phrases that only mean the featured buybox is gone (often just buybox rotation to a
# third-party seller), not that the item sold out. Production alert history showed
# products flapping on these every few minutes; treating them as "confirmed OOS"
# armed the short 10-minute re-alert cooldown and produced dozens of spam alerts
# per day for a single ASIN. Still counts as OOS, but with weak evidence.
_EXPLICIT_OOS_WEAK_RE = re.compile(
    r"see all buying options|no featured offers",
    re.IGNORECASE,
)

# Preorder pages look buyable (price + enabled "Pre-order Now" button) but are not
# real restocks: allocation waves open/sell out repeatedly before release, which
# produced dozens of repeated back_in_stock alerts for a single unreleased product.
_PREORDER_RE = re.compile(
    r"pre-?order|will be released|releases on|item has not been released",
    re.IGNORECASE,
)

# When primary buy box has no price, these DOM hints mean OOS (not a scrape miss).
_PDP_SEE_ALL_BUYING_SELECTORS = (
    "#buybox-see-all-buying-choices",
    "#buybox-see-all-buying-choices-announce",
    "#accSeeAllBuyingConsoles",
    "#all-offers-display",
    "a[href*='buying-options']",
)
_PDP_PURCHASE_BUTTON_SELECTORS = (
    "#add-to-cart-button",
    "#buy-now-button",
    "input[name='submit.add-to-cart']",
    "#submit.add-to-cart",
)

# Accordion buybox: some ASINs render several offers as accordion rows
# (div[id^="newAccordionRow_"]) inside #buyBoxAccordion. The page-level extractors
# span the WHOLE accordion, so they can pair the featured row's price/shipping with a
# different row's "Sold by: Amazon.com" merchant line and alert on the wrong offer
# (B0GYTRYV7P, 2026-07-14: featured Kings Games $79.95 alerted while seller validation
# passed on a separate Amazon.com $65.94 row). Extraction is therefore scoped to the
# single allowed-seller row whenever an accordion is present.
_ACCORDION_ROW_SELECTOR = '[id^="newAccordionRow_"]'
_ACCORDION_ROW_FALLBACK_SELECTOR = '[data-a-accordion-row-name="newAccordionRow"]'
_ACCORDION_ACTIVE_ROW_SELECTORS = (
    '[id^="newAccordionRow_"].a-accordion-active',
    '.a-accordion-active[data-a-accordion-row-name="newAccordionRow"]',
)
# Row-scoped pay-price selectors. The apex "price to pay" node carries both `a-price`
# and `apex-pricetopay-value`; the generic `.a-price` fallback keeps `:not(.a-text-price)`
# so it never grabs the strike-through list price.
_PDP_ROW_PRICE_SELECTORS = (
    ".apex-pricetopay-value .a-offscreen",
    ".a-price:not(.a-text-price) .a-offscreen",
)

# Sentinel: an accordion buybox is present but no row is an allowed seller. Distinct
# from None (no accordion at all) so the caller can build the seller_mismatch
# diagnostic path instead of falling back to page-level extraction.
_ACCORDION_NO_ALLOWED_OFFER = object()


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


def _clamp_pdp_settle_seconds(raw: float) -> float:
    return max(1.0, min(30.0, float(raw)))


def _clamp_pdp_settle_poll_interval(raw: float) -> float:
    return max(0.1, min(5.0, float(raw)))


def _clamp_pdp_unknown_retry_seconds(raw: float) -> float:
    return max(0.0, min(10.0, float(raw)))


def _clamp_continue_shopping_clicks(raw: int) -> int:
    return max(0, min(10, int(raw)))


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


def _parse_hidden_buybox_amount(raw: str | None) -> float | None:
    """Parse Amazon add-to-cart hidden customerVisiblePrice amount (e.g. 119.99, 90.0)."""
    text = (raw or "").strip()
    if not text:
        return None
    try:
        value = float(text.replace(",", ""))
    except ValueError:
        return None
    return value if value > 0 else None


def _extract_hidden_buybox_price_from_root(root: Any) -> float | None:
    """Read pay price from server-rendered hidden inputs inside #qualifiedBuybox."""
    if root is None:
        return None
    selectors = (
        'input[name="items[0.base][customerVisiblePrice][amount]"]',
        'input[id="items[0.base][customerVisiblePrice][amount]"]',
        'input[name*="customerVisiblePrice"][name*="amount"]',
    )
    for sel in selectors:
        try:
            el = root.query_selector(sel)
            if not el:
                continue
            value = el.get_attribute("value")
            price = _parse_hidden_buybox_amount(value)
            if price is not None:
                return price
        except Exception:
            continue
    return None


async def _extract_hidden_buybox_price_async(page: Any) -> float | None:
    """Qualified buybox forms often ship hidden pay price before visible price nodes hydrate."""
    try:
        root = await page.query_selector("#qualifiedBuybox")
    except Exception:
        root = None
    if root is not None:
        selectors = (
            'input[name="items[0.base][customerVisiblePrice][amount]"]',
            'input[id="items[0.base][customerVisiblePrice][amount]"]',
            'input[name*="customerVisiblePrice"][name*="amount"]',
        )
        for sel in selectors:
            try:
                el = await root.query_selector(sel)
                if not el:
                    continue
                value = await el.get_attribute("value")
                price = _parse_hidden_buybox_amount(value)
                if price is not None:
                    return price
            except Exception:
                continue
    # Page-level hidden pay-price inputs used by some buybox/attach layouts that do
    # not render a #qualifiedBuybox form (transitioning offers, add-to-cart attach).
    for sel in ('input#attach-base-product-price', 'input[name="displayedPrice"]'):
        try:
            el = await page.query_selector(sel)
            if not el:
                continue
            value = await el.get_attribute("value")
            price = _parse_hidden_buybox_amount(value)
            if price is not None:
                return price
        except Exception:
            continue
    return None


# Try a few known Amazon page spots to find the buy-box price and return quickly when the page doesn’t have one.
def _extract_pdp_price(page) -> float | None:
    """Use query_selector only (no locator auto-wait) so missing buy box returns fast. Unused in prod."""
    for sel in _PDP_PRICE_PAY_SELECTORS:
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
    for root_sel in ("#qualifiedBuybox", "#corePriceDisplay_desktop_feature_div"):
        try:
            root = page.query_selector(root_sel)
            if not root:
                continue
            pay = root.query_selector(".apex-pricetopay-value")
            if not pay:
                continue
            whole = pay.query_selector(".a-price-whole")
            frac = pay.query_selector(".a-price-fraction")
            if whole:
                w = (whole.inner_text() or "").strip().replace(",", "").replace(".", "")
                f = (frac.inner_text() or "").strip() if frac else ""
                if w.isdigit():
                    cents = f if f.isdigit() else "00"
                    return float(f"{w}.{cents}")
        except Exception:
            continue
    try:
        root = page.query_selector("#qualifiedBuybox")
    except Exception:
        root = None
    return _extract_hidden_buybox_price_from_root(root)


# Saved page snapshots for price-extraction misses (C1 diagnostics). Bounded so the
# directory never grows without limit on a long-running client machine.
_NO_PRICE_DUMP_DIR = Path("data/debug_no_price")
_NO_PRICE_DUMP_KEEP = 20


def _prune_no_price_dumps(directory: Path, keep: int = _NO_PRICE_DUMP_KEEP) -> None:
    try:
        files = sorted(directory.glob("*.html"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return
    for stale in files[keep:]:
        try:
            stale.unlink()
        except OSError:
            continue


async def _dump_no_price_html(page: Any, asin: str) -> None:
    """Persist the raw page for a price-extraction miss so failures can be replayed offline."""
    try:
        html = await page.content()
    except Exception:
        return
    try:
        _NO_PRICE_DUMP_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
        (_NO_PRICE_DUMP_DIR / f"{asin}_{ts}.html").write_text(html, encoding="utf-8")
        _prune_no_price_dumps(_NO_PRICE_DUMP_DIR)
    except OSError as exc:
        LOGGER.info(
            "PDP no-price html dump failed asin=%s (ignored): %s",
            asin,
            exc,
            extra={"channel": "debug"},
        )


def detect_soft_captcha_from_html(html: str, url: str = "") -> bool:
    """True for Amazon continue-shopping / soft-captcha interstitial (static HTML check)."""
    lower_url = (url or "").lower()
    if "opfcaptcha" in lower_url:
        return True
    body = html or ""
    lower = body.lower()
    if "csm-captcha-instrumentation" in lower:
        return True
    if "click the button below to continue shopping" in lower:
        return True
    return False


async def _is_continue_shopping_interstitial_async(page: Any) -> bool:
    try:
        url = page.url or ""
    except Exception:
        url = ""
    try:
        html = await page.content()
    except Exception:
        html = ""
    return detect_soft_captcha_from_html(html, url)


async def _is_hard_captcha_async(page: Any) -> bool:
    try:
        title_l = (await page.title() or "").lower()
    except Exception:
        title_l = ""
    try:
        cap_el = await page.query_selector("form[action*='validateCaptcha']")
    except Exception:
        cap_el = None
    return "robot check" in title_l or cap_el is not None


async def _click_continue_shopping_once_async(page: Any) -> bool:
    for sel in _CONTINUE_SHOPPING_CLICK_SELECTORS:
        try:
            loc = page.locator(sel).first
            if await loc.count() == 0:
                continue
            await loc.click(timeout=3_000)
            return True
        except Exception:
            continue
    return False


async def _dismiss_continue_shopping_async(
    page: Any,
    *,
    max_clicks: int,
    asin: str = "",
) -> int:
    """Click through continue-shopping interstitial; return number of clicks performed."""
    clicks = 0
    limit = _clamp_continue_shopping_clicks(max_clicks)
    for _ in range(limit):
        if not await _is_continue_shopping_interstitial_async(page):
            try:
                if await page.query_selector("#productTitle"):
                    break
            except Exception:
                pass
            if not await _is_continue_shopping_interstitial_async(page):
                break
        if not await _click_continue_shopping_once_async(page):
            break
        clicks += 1
        await asyncio.sleep(random.uniform(0.5, 1.0))
    if asin:
        still = await _is_continue_shopping_interstitial_async(page)
        LOGGER.info(
            "PDP continue shopping asin=%s clicks=%s still_interstitial=%s",
            asin,
            clicks,
            still,
            extra={"channel": "debug"},
        )
    return clicks


# Tight gate for buybox presence (inferred OOS diagnostics). OOS markers are
# included so a page with no buy box at all (genuinely out of stock) satisfies the
# settle gate in ~1s instead of polling the full pdp_settle_seconds budget.
_PDP_BUYBOX_WAIT_GATE_SELECTORS = (
    "#qualifiedBuybox",
    "#desktop_buybox",
    "#buybox",
    "#corePrice_feature_div",
    "#corePriceDisplay_desktop_feature_div",
    "#add-to-cart-button",
    "#buy-now-button",
    "#outOfStock",
    "#availability",
    "#unqualifiedBuyBox",
)
# Broader markers for missing-price diagnostics only.
_PDP_BUYBOX_PRESENT_SELECTORS = (
    *_PDP_BUYBOX_WAIT_GATE_SELECTORS,
    "#tabular-buybox",
    "#unqualifiedBuyBox",
    "#priceblock_ourprice",
    "#priceblock_dealprice",
    ".a-price",
)


async def _pdp_buybox_present_async(page: Any) -> bool:
    """True when a buy-box region exists (price may still be hydrating)."""
    for sel in _PDP_BUYBOX_WAIT_GATE_SELECTORS:
        try:
            if await page.query_selector(sel):
                return True
        except Exception:
            continue
    return False


async def _wait_for_buybox_ready_async(
    page: Any,
    *,
    timeout_s: float,
    poll_interval_s: float,
) -> bool:
    """Poll until buybox region appears or timeout elapses."""
    deadline = time.monotonic() + max(0.0, float(timeout_s))
    interval = max(0.1, min(float(poll_interval_s), float(timeout_s) or 0.1))
    while time.monotonic() < deadline:
        if await _pdp_buybox_present_async(page):
            return True
        await asyncio.sleep(interval)
    return await _pdp_buybox_present_async(page)


async def _detect_offer_state_signal_async(page: Any) -> str | None:
    """Return the first RESOLVABLE offer-state signal present, or None if none yet.

    Checked in priority order each poll tick — a hydrated price beats an OOS/alt-offers
    marker that may just be pre-render scaffolding. Per-selector exceptions are swallowed
    exactly like the sibling gate helpers do; the signal is only a readiness hint.
      "price"      any `.a-price .a-offscreen` that parses, non-empty #corePrice_feature_div,
                   or a hidden buybox pay-price input.
      "oos"        #outOfStock present, or #availability text reads as OOS (strong/weak).
      "alt_offers" a see-all-buying-options / AOD ingress that other offers exist.
      "accordion"  >= 2 accordion offer rows rendered.
    """
    # price
    try:
        nodes = await page.query_selector_all(".a-price .a-offscreen")
    except Exception:
        nodes = []
    for el in nodes or []:
        try:
            if _parse_price_text(await _read_text_async(el)) is not None:
                return "price"
        except Exception:
            continue
    try:
        core = await page.query_selector("#corePrice_feature_div")
        if core is not None and (await core.inner_text() or "").strip():
            return "price"
    except Exception:
        pass
    try:
        if await _extract_hidden_buybox_price_async(page) is not None:
            return "price"
    except Exception:
        pass
    # oos
    try:
        if await page.query_selector("#outOfStock"):
            return "oos"
    except Exception:
        pass
    try:
        avail = await page.query_selector("#availability")
        if avail is not None and _oos_text_level(await avail.inner_text() or ""):
            return "oos"
    except Exception:
        pass
    # alt_offers
    try:
        if await _see_all_buying_options_present_async(page):
            return "alt_offers"
    except Exception:
        pass
    try:
        if await _aod_ingress_present_async(page):
            return "alt_offers"
    except Exception:
        pass
    # accordion
    try:
        rows = await page.query_selector_all(_ACCORDION_ROW_SELECTOR)
        if rows is not None and len(rows) >= 2:
            return "accordion"
    except Exception:
        pass
    return None


async def _wait_for_offer_state_async(
    page: Any,
    *,
    timeout_s: float,
    poll_interval_s: float,
) -> str:
    """Poll until a RESOLVABLE OFFER STATE appears; return its signal name ("timeout" if none).

    Unlike the container-presence gate, this waits for a state extraction can actually
    classify (a parseable price, an OOS marker, an alt-offers ingress, or a multi-row
    accordion). On a slow machine #availability renders seconds before the price widgets
    hydrate, so the container gate passes almost instantly and extraction ran on a
    half-built page — producing false `degraded_page` skeleton skips. The returned signal
    is a readiness hint only; the caller extracts regardless of which signal (or timeout)
    it gets.
    """
    deadline = time.monotonic() + max(0.0, float(timeout_s))
    interval = max(0.1, min(float(poll_interval_s), float(timeout_s) or 0.1))
    while True:
        signal = await _detect_offer_state_signal_async(page)
        if signal is not None:
            return signal
        if time.monotonic() >= deadline:
            return "timeout"
        await asyncio.sleep(interval)


async def _poll_price_hydration_async(
    page: Any,
    *,
    timeout_s: float,
    interval_s: float,
) -> float | None:
    """Re-try price extraction for a short window while the price block hydrates."""
    deadline = time.monotonic() + max(0.0, float(timeout_s))
    while time.monotonic() < deadline:
        await asyncio.sleep(max(0.1, float(interval_s)))
        price = await _extract_pdp_price_async(page)
        if price is not None:
            return price
    return None


async def _extract_pdp_page_state_async(
    page: Any,
    *,
    asin: str,
    allowed: list[str],
    dump_no_price_html: bool = False,
) -> dict[str, Any]:
    """Extract title, price, merchant, shipping, OOS signals from a loaded PDP page."""
    availability_text = await _extract_availability_text_async(page)
    explicit_oos, explicit_reason = await _detect_explicit_oos_async(
        page, availability_text=availability_text
    )
    title = await _extract_pdp_title_async(page) or _product_title_from_page_title(
        await page.title() or ""
    )

    # Accordion buybox: scope extraction to the single allowed-seller offer row so we
    # never pair the featured row's price/shipping with another row's merchant line
    # (the wrong-offer alert incident). No accordion (or fewer than 2 rows) → fall
    # through to the unchanged page-level flow below.
    if not explicit_oos:
        accordion_offer, accordion_merchants = await _find_allowed_accordion_offer_async(
            page, allowed
        )
        if accordion_offer is not None:
            return await _extract_accordion_offer_state_async(
                page,
                accordion_offer,
                accordion_merchants,
                asin=asin,
                availability_text=availability_text,
                title=title,
            )

    price = None if explicit_oos else await _extract_pdp_price_async(page)
    buybox_purchasable = False

    if not explicit_oos and price is None and title:
        inferred, infer_reason = await _detect_inferred_oos_async(
            page,
            availability_text=availability_text,
        )
        if inferred:
            explicit_oos = True
            explicit_reason = infer_reason
        else:
            buybox_purchasable = await _buybox_purchasable_async(page)
            if buybox_purchasable:
                # Purchasable buybox with no price yet: the price block often hydrates
                # a beat after the buy button (seen on preorder/transitioning pages).
                # Give it a short, bounded poll before giving up — this path is rare,
                # so the extra wait does not affect normal sweep timing.
                price = await _poll_price_hydration_async(page, timeout_s=4.0, interval_s=0.5)
                if price is not None:
                    LOGGER.info(
                        "PDP price hydrated late asin=%s price=%s",
                        asin,
                        price,
                        extra={"channel": "debug"},
                    )
                else:
                    LOGGER.info(
                        "PDP pay price missing with purchase action asin=%s (leaving unknown)",
                        asin,
                        extra={"channel": "debug"},
                    )
            else:
                explicit_oos = True
                explicit_reason = "no_pay_price"
            if price is None and dump_no_price_html:
                await _dump_no_price_html(page, asin)
        if explicit_oos and explicit_reason:
            LOGGER.info(
                "PDP no pay price asin=%s reason=%s",
                asin,
                explicit_reason,
                extra={"channel": "debug"},
            )

    merchant_blob = ""
    shipping = ""
    image_url = None
    is_preorder = False
    if not explicit_oos:
        merchant_blob, shipping, image_url = await asyncio.gather(
            _pdp_merchant_blob_async(page),
            _extract_pdp_shipping_async(page),
            _extract_pdp_image_async(page),
        )
        is_preorder = await _detect_preorder_async(page, availability_text=availability_text)

    return {
        "availability_text": availability_text,
        "explicit_oos": explicit_oos,
        "explicit_reason": explicit_reason,
        "title": title,
        "price": price,
        "buybox_purchasable": buybox_purchasable,
        "merchant_blob": merchant_blob,
        "shipping": shipping,
        "image_url": image_url,
        "is_preorder": is_preorder,
    }


def _product_title_from_page_title(raw: str) -> str:
    """Strip generic Amazon document titles; keep product name when present."""
    title = (raw or "").strip()
    if not title:
        return ""
    lower = title.lower()
    if lower in ("amazon.com", "amazon.com: online shopping", "www.amazon.com"):
        return ""
    for sep in (" : Amazon.com", " | Amazon", " - Amazon"):
        if sep in title:
            title = title.split(sep, 1)[0].strip()
            return title if title.lower() != "amazon.com" else ""
    if re.match(r"^Amazon\.com:\s*", title, re.IGNORECASE):
        rest = re.sub(r"^Amazon\.com:\s*", "", title, flags=re.IGNORECASE).strip()
        if " : " in rest:
            rest = rest.rsplit(" : ", 1)[0].strip()
        return rest
    if title.lower().startswith("amazon.com"):
        return ""
    return title


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
        "#mir-layout-DELIVERY_BLOCK-slot-PRIMARY_DELIVERY_MESSAGE_LARGE",
        "#mir-layout-DELIVERY_BLOCK-slot-NO_PROMISE_UPSELL_MESSAGE",
        "#deliveryBlockMessage",
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


def _oos_text_level(text: str | None) -> str | None:
    """Classify OOS evidence in a text blob: 'strong', 'weak' (buybox churn), or None."""
    if not text:
        return None
    clean = " ".join(str(text).split())
    if _EXPLICIT_OOS_STRONG_RE.search(clean):
        return "strong"
    if _EXPLICIT_OOS_WEAK_RE.search(clean):
        return "weak"
    return None


def _explicit_oos_from_text(text: str | None) -> bool:
    return _oos_text_level(text) is not None


async def _extract_availability_text_async(page: Any) -> str:
    parts: list[str] = []
    seen: set[str] = set()
    for sel in (
        "#availability",
        "#availabilityMessage",
        "#outOfStock",
        "#desktop_buybox #availability",
        "#qualifiedBuybox #availability",
        "#availabilityInsideBuyBox_feature_div",
        "#buybox #availability",
    ):
        try:
            el = await page.query_selector(sel)
            if not el:
                continue
            t = (await el.inner_text() or "").strip()
            if t and t not in seen:
                seen.add(t)
                parts.append(t)
        except Exception:
            continue
    return " ".join(parts)


async def _detect_preorder_async(page: Any, *, availability_text: str) -> bool:
    """True when the page is a preorder listing (release-date text or Pre-order button)."""
    if _PREORDER_RE.search(availability_text or ""):
        return True
    for sel in ("#buy-now-button", "#add-to-cart-button"):
        try:
            el = await page.query_selector(sel)
            if not el:
                continue
            blob = " ".join(
                part
                for part in (
                    (await el.get_attribute("value")) or "",
                    (await el.inner_text() or ""),
                )
                if part
            )
            if _PREORDER_RE.search(blob):
                return True
        except Exception:
            continue
    return False


# Skeleton (degraded-page) detection. During Amazon soft-block windows the PDP is
# served as a server-rendered SKELETON: #corePrice_feature_div renders empty and
# there is no `.a-price .a-offscreen` node anywhere on the ~840KB page — yet a
# purchase button still renders. Left alone this classifies as
# no_pay_price/priceless_purchasable (false price-less alerts) or lets stale
# OOS/mismatch evidence flip state. A skeleton is a scrape failure, not evidence:
# the caller turns it into a degraded_page skip row.
#
# A second marker was originally used here — data-csa-c-is-in-initial-active-row
# ="false" present with no ="true" anywhere — and REMOVED on 2026-07-15: a live
# check of a perfectly healthy in-stock PDP (29 price nodes, full buybox) showed
# 301 ="false" and zero ="true", i.e. the attribute carries no degradation signal
# at all. In production it made every rendered-but-priceless page (a state Amazon
# serves persistently to a flagged IP) register as a degraded burst, driving a
# session-recycle loop every ~2 minutes that disrupted the whole watch list.


async def _page_offers_skeleton_async(page: Any, *, asin: str = "") -> bool:
    """True when the PDP is a server-rendered skeleton (soft-block degraded page).

    Checked ONLY on would-be no_pay_price/priceless_purchasable rows (not explicit_oos,
    price None, title present) AND ONLY when the offer-state wait TIMED OUT — if any offer
    signal (price/oos/alt_offers/accordion) rendered, the page hydrated and a price-less
    result is a real classification, never a skeleton. This gate is what stops the false
    degraded_page skip loop on slow-hydrating ASINs. Real OOS pages carry explicit text and
    never reach here. Single discriminative marker: #corePrice_feature_div exists with empty/whitespace
    inner text AND no `.a-price .a-offscreen` element exists anywhere on the page
    (a healthy PDP carries dozens of offscreen price nodes site-wide).
    """
    marker = ""
    try:
        core = await page.query_selector("#corePrice_feature_div")
        if core is not None:
            core_text = (await core.inner_text() or "").strip()
            if not core_text and await page.query_selector(".a-price .a-offscreen") is None:
                marker = "empty_core_price_no_offscreen"
    except Exception:
        marker = ""
    if marker:
        LOGGER.info(
            "PDP degraded skeleton detected asin=%s marker=%s",
            asin or "?",
            marker,
            extra={"channel": "debug"},
        )
        return True
    return False


# Offer-less nav shell: the OTHER degraded serving mode (post-512ba00 dumps,
# 2026-07-16/17): Amazon returns ONLY the navigation chrome — every element id is
# nav-*/a-page, no #dp / #ppd / #centerCol / #productTitle / #availability, no buybox,
# no price markup — while the document <title> still carries the product name. Extraction
# then recovers a title from the page <title>, every OOS probe is all-negative (the
# "Buying options" skip link points at #buybox, so no alt-offers ingress matches), and
# the page lands on explicit_oos/no_pay_price → confirmed_out: a scrape failure ingested
# as OOS EVIDENCE. Shell windows hit multiple ASINs in the same second (two pairs of
# same-second dumps across Jul 16-17), so this is a session-serving state, not product
# state — it must become a degraded_page skip, like the skeleton above.
_PDP_PRODUCT_BODY_SELECTORS = ("#dp", "#ppd", "#centerCol", "#productTitle", "#availability")


async def _page_is_nav_shell_async(page: Any, *, asin: str = "") -> bool:
    """True when the PDP has no product body at all (nav-chrome-only shell).

    Checked ONLY when the offer-state wait TIMED OUT — every offer signal lives inside
    the product body, so a page that resolved any signal can never be a shell. Requires
    ALL product-body containers absent: any one present means a real (possibly degraded)
    PDP that the skeleton/classification paths own. A selector error counts as present —
    an inconclusive read must never classify a page as a shell.
    """
    for sel in _PDP_PRODUCT_BODY_SELECTORS:
        try:
            if await page.query_selector(sel) is not None:
                return False
        except Exception:
            return False
    LOGGER.info(
        "PDP nav-shell page detected asin=%s (no product body)",
        asin or "?",
        extra={"channel": "debug"},
    )
    return True


# Cached-fallback PDP: when the live render fails, Amazon can serve a stale page out of
# cache — seen live 2026-07-17 (B0GW2DK37Q dump: hidden input clientName=
# "FallbackDetailPage", placeholder session-id, pageLoadTimestampUTC 41h older than the
# fetch). Whatever such a page shows — price, buybox seller, OOS — is up to days old, so
# it must never reach the state engine as evidence in either direction.
_PDP_FALLBACK_CLIENT_SELECTOR = 'input[name="clientName"][value="FallbackDetailPage"]'


async def _page_is_fallback_detail_async(page: Any, *, asin: str = "") -> bool:
    """True when the page marks itself as a cached FallbackDetailPage render."""
    try:
        found = await page.query_selector(_PDP_FALLBACK_CLIENT_SELECTOR) is not None
    except Exception:
        return False
    if found:
        LOGGER.info(
            "PDP fallback (cached) page detected asin=%s",
            asin or "?",
            extra={"channel": "debug"},
        )
    return found


async def _buybox_purchasable_async(page: Any) -> bool:
    """True when an enabled add-to-cart or buy-now control exists."""
    for sel in _PDP_PURCHASE_BUTTON_SELECTORS:
        try:
            el = await page.query_selector(sel)
            if not el:
                continue
            disabled = await el.get_attribute("disabled")
            aria_disabled = (await el.get_attribute("aria-disabled") or "").strip().lower()
            if disabled is not None or aria_disabled in ("true", "1"):
                continue
            return True
        except Exception:
            continue
    return False


async def _see_all_buying_options_present_async(page: Any) -> bool:
    for sel in _PDP_SEE_ALL_BUYING_SELECTORS:
        try:
            if await page.query_selector(sel):
                return True
        except Exception:
            continue
    return False


async def _detect_inferred_oos_async(
    page: Any,
    *,
    availability_text: str,
) -> tuple[bool, str | None]:
    """Infer confirmed OOS when the page loaded but there is no buyable primary offer.

    Called only after price resolve failed and title exists — avoids marking empty/broken
    pages as OOS (those stay parse_failed).
    """
    level = _oos_text_level(availability_text)
    if level:
        return True, "explicit_oos_text" if level == "strong" else "buybox_unavailable_text"
    try:
        if await page.query_selector("#outOfStock"):
            return True, "outofstock_container"
    except Exception:
        pass

    fresh_avail = await _extract_availability_text_async(page)
    level = _oos_text_level(fresh_avail)
    if level:
        return True, "explicit_oos_text" if level == "strong" else "buybox_unavailable_text"

    for sel in ("#qualifiedBuybox", "#desktop_buybox", "#buybox"):
        try:
            node = await page.query_selector(sel)
            if not node:
                continue
            blob = (await node.inner_text() or "")[:2500]
            level = _oos_text_level(blob)
            if level:
                return True, "explicit_oos_buybox_text" if level == "strong" else "buybox_unavailable_text"
        except Exception:
            continue

    if await _see_all_buying_options_present_async(page):
        return True, "inferred_oos_see_all_options"

    if not await _buybox_purchasable_async(page) and await _pdp_buybox_present_async(page):
        return True, "inferred_oos_no_purchase_action"

    return False, None


async def _detect_explicit_oos_async(page: Any, *, availability_text: str) -> tuple[bool, str | None]:
    """Best-effort explicit out-of-stock detection.

    If we can *explicitly* detect OOS, we return (True, reason). Otherwise (False, None).
    """
    level = _oos_text_level(availability_text)
    if level:
        return True, "explicit_oos_text" if level == "strong" else "buybox_unavailable_text"
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
    buybox_purchasable: bool = False,
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
    elif price is None and (title or "").strip():
        # Title present, no price, and no explicit OOS signal: the extraction worker
        # deliberately leaves purchasable-but-priceless pages (price still hydrating,
        # e.g. preorder buyboxes) as ambiguous. Hardening this into confirmed_out
        # bypassed the unknown-retry pass and fed the OOS debounce with false
        # evidence — a live Amazon.com preorder wave was classified OOS in 2s and
        # missed entirely (B0GYTRYV7P, 2026-07-13). Leave it unknown so the retry
        # runs and the state engine skips the update instead of flipping stock.
        #
        # When the buybox is an allowed-seller purchasable offer, tag it
        # priceless_purchasable so the state engine can confirm-and-alert the restock
        # (streak-gated) even though no price rendered. Confidence stays unknown so
        # every existing consumer (and the in-page retry) still treats it as ambiguous.
        stock_confidence = "unknown"
        if seller_ok and buybox_purchasable:
            stock_reason = "priceless_purchasable"
        else:
            stock_reason = "no_pay_price"
    else:
        if price is None:
            stock_reason = "missing_price"
        elif not seller_ok:
            stock_reason = "seller_mismatch"
            # A parsed price with a non-empty merchant blob that does not match any
            # allowed seller is a settled observation: a 3P seller holds the buybox,
            # i.e. the Amazon offer is gone. Promote it from ambiguous "unknown" to
            # weak "confirmed_out" so the state-engine OOS debounce can count it and
            # eventually flip the DB (see C9). Two consecutive of these ~= real
            # "Amazon offer gone", which arms the short confirmed cooldown for the
            # next allocation wave. An empty blob means extraction was incomplete
            # (not evidence) — leave that unknown so the in-page retry still runs.
            if (merchant_blob or "").strip():
                stock_confidence = "confirmed_out"
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
        "buybox_purchasable": bool(buybox_purchasable),
        "shipping_text": shipping_text,
        "availability_text": availability_text,
        "image_url": image_url,
        "seller": "pdp_watch",
        "seller_text": merchant_blob[:2000],
        "product_url": f"https://www.amazon.com/dp/{asin}",
        "source": "pdp_watch",
    }


def _pdp_row_would_be_unknown(
    *,
    asin: str,
    title: str,
    price: float | None,
    shipping_text: str,
    image_url: str | None,
    merchant_blob: str,
    allowed: list[str],
    availability_text: str = "",
    explicit_oos: bool = False,
    buybox_purchasable: bool = False,
) -> bool:
    """True when _pdp_row would classify stock_confidence as unknown (retry candidate)."""
    row = _pdp_row(
        asin,
        title=title,
        price=price,
        shipping_text=shipping_text,
        image_url=image_url,
        merchant_blob=merchant_blob,
        allowed=allowed,
        availability_text=availability_text,
        explicit_oos=explicit_oos,
        buybox_purchasable=buybox_purchasable,
    )
    return str(row.get("stock_confidence") or "") == "unknown"


# Unknown-confidence reasons where a short in-page retry can still resolve the row:
# the price node may hydrate a beat late (no_pay_price) or the purchasable buybox may
# render its price (priceless_purchasable). Shared with monitor_engine's fast-recheck.
_PDP_RETRY_UNKNOWN_REASONS = frozenset({"no_pay_price", "priceless_purchasable"})


def _pdp_row_should_retry_unknown(
    *,
    asin: str,
    title: str,
    price: float | None,
    shipping_text: str,
    image_url: str | None,
    merchant_blob: str,
    allowed: list[str],
    availability_text: str = "",
    explicit_oos: bool = False,
    buybox_purchasable: bool = False,
) -> bool:
    """Reason-aware gate for the in-page unknown retry (C4).

    Only missing-price rows (price still hydrating) or rows whose merchant blob has
    not loaded yet are worth re-reading. A clean seller_mismatch (price parsed,
    non-empty 3P blob with no allowed-seller match) is a settled classification —
    retrying it just burns ~2.5s of tab time per check for no benefit.
    """
    row = _pdp_row(
        asin,
        title=title,
        price=price,
        shipping_text=shipping_text,
        image_url=image_url,
        merchant_blob=merchant_blob,
        allowed=allowed,
        availability_text=availability_text,
        explicit_oos=explicit_oos,
        buybox_purchasable=buybox_purchasable,
    )
    if str(row.get("stock_confidence") or "") != "unknown":
        return False
    reason = str(row.get("stock_reason") or "")
    if reason in _PDP_RETRY_UNKNOWN_REASONS:
        return True
    return not (merchant_blob or "").strip()


# Create a “do not update this ASIN” marker when a single product page fails, so a bad scrape doesn’t flip the database state.
def _pdp_skip_row(
    asin: str,
    reason: str,
    *,
    skip_detail: str = "",
    scrape_attempts: int = 0,
    scrape_elapsed_ms: int = 0,
    html_len: int = 0,
    dom_ok: bool = False,
) -> dict[str, Any]:
    """Marker row for one-page operational failures: state engine must not touch the DB row."""
    row: dict[str, Any] = {
        "asin": asin,
        "_skip_update": True,
        "skip_reason": reason,
        "source": "pdp_watch",
        "scrape_attempts": scrape_attempts,
        "scrape_elapsed_ms": scrape_elapsed_ms,
    }
    if skip_detail:
        row["skip_detail"] = skip_detail
    if html_len:
        row["scrape_html_len"] = html_len
    if not dom_ok and skip_detail in ("skeleton", "not_ready", "empty_parse"):
        row["scrape_dom_ok"] = False
    return row


def pdp_skip_log_label(row: dict[str, Any]) -> str:
    """Short label for main-log skip lines."""
    reason = str(row.get("skip_reason") or "")
    detail = str(row.get("skip_detail") or "")
    if reason == "captcha" or detail == "soft_captcha":
        return "captcha"
    if reason == "cycle_budget_exceeded":
        return "timeout"
    if reason == "captcha_run_aborted":
        return "captcha_aborted"
    # AOD side-fetch failures no longer emit skip rows at all (they keep the buybox row),
    # but if any future path still tags one, label it "aod" — never the misleading "skeleton".
    if detail == "aod_failed":
        return "aod"
    if detail == "nav_shell":
        return "nav_shell"
    if detail == "fallback_page":
        return "fallback"
    if reason == "degraded_page" or detail in ("skeleton", "not_ready"):
        return "skeleton"
    if detail == "empty_parse":
        return "empty"
    if reason == "navigation_failed":
        return "navigation"
    if reason == "parse_failed":
        return detail or "parse_failed"
    return detail or reason or "unknown"


def _attach_scrape_meta(row: dict[str, Any], **fields: Any) -> dict[str, Any]:
    for key, value in fields.items():
        if value is not None and value != "":
            row[key] = value
    return row


def _emit_pdp_cycle_debug_report(rows: list[dict[str, Any]], pdp_sec: float) -> None:
    ok = sum(1 for r in rows if isinstance(r, dict) and not r.get("_skip_update"))
    skip = len(rows) - ok
    lines = [f"--- pdp cycle asins={len(rows)} pdp_sec={pdp_sec:.1f} ---"]
    for row in rows:
        if not isinstance(row, dict):
            continue
        asin = str(row.get("asin") or "?")
        attempts = row.get("scrape_attempts", "?")
        elapsed = row.get("scrape_elapsed_ms")
        elapsed_s = f"{elapsed / 1000.0:.1f}s" if isinstance(elapsed, (int, float)) else "?"
        if row.get("_skip_update"):
            detail = row.get("skip_detail") or row.get("skip_reason") or "?"
            extra = ""
            html_len = row.get("scrape_html_len")
            if html_len:
                extra = f" html_len={html_len}"
            if row.get("scrape_dom_ok") is False:
                extra += " dom_ok=false"
            lines.append(
                f"{asin}  skip     {detail:<16}  attempts={attempts}  {elapsed_s}{extra}"
            )
        else:
            status = "in_stock" if row.get("in_stock") else "oos"
            price = row.get("price")
            price_s = f"${price:.2f}" if isinstance(price, (int, float)) else "-"
            extra = ""
            if not row.get("in_stock"):
                reason = row.get("stock_reason") or "?"
                extra = f"  reason={reason}"
                if reason == "seller_mismatch":
                    snippet = " ".join(str(row.get("seller_text") or "").split())[:120]
                    extra += f"  seller_text={snippet!r}" if snippet else "  seller_text=<empty>"
            lines.append(
                f"{asin}  ok       {status:<9}  {price_s:<8}  attempts={attempts}  {elapsed_s}{extra}"
            )
    lines.append(f"--- end pdp cycle ok={ok} skip={skip} ---")
    LOGGER.info("\n".join(lines), extra={"channel": "debug"})


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
    for sel in _PDP_PRICE_PAY_SELECTORS:
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
    for root_sel in ("#qualifiedBuybox", "#corePriceDisplay_desktop_feature_div"):
        try:
            root = await page.query_selector(root_sel)
            if not root:
                continue
            pay = await root.query_selector(".apex-pricetopay-value")
            if not pay:
                continue
            whole = await pay.query_selector(".a-price-whole")
            frac = await pay.query_selector(".a-price-fraction")
            if whole:
                w = (await _read_text_async(whole)).replace(",", "").replace(".", "")
                f = (await _read_text_async(frac)) if frac else ""
                if w.isdigit():
                    cents = f if f.isdigit() else "00"
                    return float(f"{w}.{cents}")
        except Exception:
            continue
    return await _extract_hidden_buybox_price_async(page)


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
        "#mir-layout-DELIVERY_BLOCK-slot-PRIMARY_DELIVERY_MESSAGE_LARGE",
        "#mir-layout-DELIVERY_BLOCK-slot-NO_PROMISE_UPSELL_MESSAGE",
        "#deliveryBlockMessage",
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


# --- Accordion (per-offer) scoped extraction ------------------------------------------------------

async def _row_merchant_parts_async(row: Any) -> tuple[str, str]:
    """Return (merchant_name, match_blob) for one accordion offer row.

    ``merchant_name`` is the seller name only (desktop-merchant-info) — used for the
    no-allowed-offer diagnostic so it can never carry a fulfiller line like
    "Ships from Amazon" that would spuriously match an allowed substring.
    ``match_blob`` is merchant + fulfiller text (row-scoped) for seller matching, and
    falls back to the full row inner_text when the offer-display features are absent.
    """
    merchant = ""
    fulfiller = ""
    for feature_name in ("desktop-merchant-info", "desktop-fulfiller-info"):
        try:
            el = await row.query_selector(
                f'.offer-display-feature-text[offer-display-feature-name="{feature_name}"]'
            )
            text = (await el.inner_text() or "").strip() if el else ""
        except Exception:
            text = ""
        if feature_name == "desktop-merchant-info":
            merchant = text
        else:
            fulfiller = text
    parts = [p for p in (merchant, fulfiller) if p]
    if parts:
        return merchant, "\n".join(parts)
    try:
        blob = (await row.inner_text() or "").strip()
    except Exception:
        blob = ""
    return merchant, blob


async def _find_allowed_accordion_offer_async(
    page: Any, allowed: list[str]
) -> tuple[Any | None, list[str]]:
    """Locate the allowed-seller offer row inside an accordion buybox.

    Returns one of:
      (None, [])                          — no accordion (fewer than 2 rows); use page-level flow.
      (row_element, merchants)            — first row whose merchant/fulfiller matches an allowed seller.
      (_ACCORDION_NO_ALLOWED_OFFER, merchants) — accordion present but no row is an allowed seller.
    ``merchants`` lists every row's seller name (names only) for diagnostics/seller_text.
    """
    try:
        rows = await page.query_selector_all(_ACCORDION_ROW_SELECTOR)
    except Exception:
        rows = []
    if len(rows) < 2:
        try:
            rows = await page.query_selector_all(_ACCORDION_ROW_FALLBACK_SELECTOR)
        except Exception:
            rows = []
    if len(rows) < 2:
        return None, []
    merchants: list[str] = []
    matched: Any | None = None
    for row in rows:
        merchant_name, match_blob = await _row_merchant_parts_async(row)
        merchants.append(merchant_name or "unknown")
        if matched is None and merchant_matches_allowed(match_blob, allowed):
            matched = row
    if matched is not None:
        return matched, merchants
    return _ACCORDION_NO_ALLOWED_OFFER, merchants


async def _find_active_accordion_row_async(page: Any) -> Any | None:
    """Return the featured/selected accordion row (a-accordion-active), else the first row."""
    for sel in _ACCORDION_ACTIVE_ROW_SELECTORS:
        try:
            el = await page.query_selector(sel)
            if el:
                return el
        except Exception:
            continue
    for sel in (_ACCORDION_ROW_SELECTOR, _ACCORDION_ROW_FALLBACK_SELECTOR):
        try:
            rows = await page.query_selector_all(sel)
            if rows:
                return rows[0]
        except Exception:
            continue
    return None


async def _extract_row_price_async(row: Any) -> float | None:
    """Row-scoped pay price: mirrors _extract_pdp_price_async but rooted at one offer row."""
    for sel in _PDP_ROW_PRICE_SELECTORS:
        try:
            el = await row.query_selector(sel)
            if not el:
                continue
            raw = await _read_text_async(el)
        except Exception:
            raw = ""
        p = _parse_price_text(raw)
        if p is not None:
            return p
    try:
        pay = await row.query_selector(".apex-pricetopay-value")
        if pay is None:
            pay = await row.query_selector(".a-price:not(.a-text-price)")
        if pay is not None:
            whole = await pay.query_selector(".a-price-whole")
            frac = await pay.query_selector(".a-price-fraction")
            if whole:
                w = (await _read_text_async(whole)).replace(",", "").replace(".", "")
                f = (await _read_text_async(frac)) if frac else ""
                if w.isdigit():
                    cents = f if f.isdigit() else "00"
                    return float(f"{w}.{cents}")
    except Exception:
        pass
    return None


async def _extract_row_shipping_async(row: Any) -> str:
    """Row-scoped delivery/shipping lines (import charges, delivery estimates)."""
    lines: list[str] = []

    def add_line(value: str | None) -> None:
        if not value:
            return
        for part in re.split(r"[\r\n]+", value):
            line = " ".join(part.split())
            if line and line not in lines:
                lines.append(line)

    try:
        for el in await row.query_selector_all("span.a-color-secondary"):
            text = (await el.inner_text() or "").strip()
            if text and _DELIVERY_RELEVANT_RE.search(text):
                add_line(text)
    except Exception:
        pass
    try:
        for el in await row.query_selector_all("[data-csa-c-delivery-price]"):
            price = (await el.get_attribute("data-csa-c-delivery-price") or "").strip()
            text = (await el.inner_text() or "").strip()
            add_line(" ".join(x for x in (price, text) if x))
    except Exception:
        pass
    return "\n".join(lines)


async def _row_buybox_purchasable_async(row: Any) -> bool:
    """True when the row has an enabled add-to-cart or buy-now control."""
    for sel in _PDP_PURCHASE_BUTTON_SELECTORS:
        try:
            el = await row.query_selector(sel)
            if not el:
                continue
            disabled = await el.get_attribute("disabled")
            aria_disabled = (await el.get_attribute("aria-disabled") or "").strip().lower()
            if disabled is not None or aria_disabled in ("true", "1"):
                continue
            return True
        except Exception:
            continue
    return False


async def _detect_row_preorder_async(row: Any, *, availability_text: str) -> bool:
    """Row-scoped preorder detection (mirrors _detect_preorder_async on one offer row)."""
    if _PREORDER_RE.search(availability_text or ""):
        return True
    for sel in ("#buy-now-button", "#add-to-cart-button"):
        try:
            el = await row.query_selector(sel)
            if not el:
                continue
            blob = " ".join(
                part
                for part in (
                    (await el.get_attribute("value")) or "",
                    (await el.inner_text() or ""),
                )
                if part
            )
            if _PREORDER_RE.search(blob):
                return True
        except Exception:
            continue
    return False


async def _extract_accordion_offer_state_async(
    page: Any,
    offer: Any,
    merchants: list[str],
    *,
    asin: str,
    availability_text: str,
    title: str,
) -> dict[str, Any]:
    """Build the page-state dict from a single accordion offer row.

    Image/title/availability stay page-level; price/shipping/merchant/purchasable/preorder
    are scoped to the chosen row. When no row is an allowed seller, ``merchant_blob`` is a
    diagnostic built from seller NAMES ONLY (never fulfiller text) so _pdp_row classifies it
    as a clean seller_mismatch → confirmed_out (C9) and the blob cannot leak an allowed
    substring unless a row genuinely matched.
    """
    image_url = await _extract_pdp_image_async(page)

    if offer is _ACCORDION_NO_ALLOWED_OFFER:
        merchant_blob = "accordion offers: " + " | ".join(m for m in merchants if m)
        active = await _find_active_accordion_row_async(page)
        price = await _extract_row_price_async(active) if active is not None else None
        shipping = await _extract_row_shipping_async(active) if active is not None else ""
        is_preorder = (
            await _detect_row_preorder_async(active, availability_text=availability_text)
            if active is not None
            else False
        )
        LOGGER.info(
            "PDP accordion scoping asin=%s rows=%s matched_index=none price=%s",
            asin,
            len(merchants),
            price,
            extra={"channel": "debug"},
        )
        return {
            "availability_text": availability_text,
            "explicit_oos": False,
            "explicit_reason": None,
            "title": title,
            "price": price,
            "buybox_purchasable": False,
            "merchant_blob": merchant_blob,
            "shipping": shipping,
            "image_url": image_url,
            "is_preorder": is_preorder,
        }

    _, merchant_blob = await _row_merchant_parts_async(offer)
    price = await _extract_row_price_async(offer)
    buybox_purchasable = await _row_buybox_purchasable_async(offer)
    shipping = await _extract_row_shipping_async(offer)
    is_preorder = await _detect_row_preorder_async(offer, availability_text=availability_text)
    try:
        matched_index = await offer.get_attribute("data-buying-option-index")
    except Exception:
        matched_index = None
    # Row price missing → the normal priceless/no_pay_price paths apply: a purchasable row
    # stays unknown (priceless_purchasable via _pdp_row) so the restock can still alert,
    # while a non-purchasable row with no price is confirmed_out.
    explicit_oos = price is None and not buybox_purchasable
    explicit_reason = "no_pay_price" if explicit_oos else None
    LOGGER.info(
        "PDP accordion scoping asin=%s rows=%s matched_index=%s price=%s",
        asin,
        len(merchants),
        matched_index if matched_index is not None else "?",
        price,
        extra={"channel": "debug"},
    )
    return {
        "availability_text": availability_text,
        "explicit_oos": explicit_oos,
        "explicit_reason": explicit_reason,
        "title": title,
        "price": price,
        "buybox_purchasable": buybox_purchasable,
        "merchant_blob": merchant_blob,
        "shipping": shipping,
        "image_url": image_url,
        "is_preorder": is_preorder,
    }


# --- F1: All Offers Display (AOD) offer check ----------------------------------------------------
#
# When the buybox/accordion seller mismatches (a 3P seller holds the featured offer) the
# allowed Amazon offer can still be alive inside the "All Offers Display" panel (evidence:
# B0DLQJ613B, 926 consecutive seller_mismatch OOS while Amazon Export's offer lived in AOD).
# On a would-be seller_mismatch/confirmed_out row we open the AOD ajax panel on the same
# context and re-check for an allowed-seller offer.

# Working AOD ajax endpoint. The plan's gp/product/ajax?...experienceId=aodAjaxMain form
# returns 404; this ref-scoped path is the request the live PDP "New (N) from" ingress
# click issues (captured from the live panel for B0DLQJ613B, 2026-07-15) and returns 200
# with the offers HTML fragment.
#
# CRITICAL (prod fix 2026-07-16): this endpoint 404s on direct page navigation — it only
# answers an XHR issued from the product page's own context. The fetch MUST carry the
# `x-requested-with: XMLHttpRequest` header (the ajax marker); same-origin cookies, referer
# and TLS fingerprint ride along automatically. See _fetch_aod_offers_async / _AOD_FETCH_JS.
_AOD_URL_TEMPLATE = (
    "https://www.amazon.com/gp/product/ajax/aodAjaxMain/ref=dp_aod_NEW_mbc"
    "?asin={asin}&m=&qid=&smid=&sourcecustomerorglistid=&sourcecustomerorglistitemid=&sr=&pc=dp"
)

# In-page XHR used to fetch the AOD fragment. This module deliberately avoids page.evaluate
# everywhere else (query_selector is enough and cheaper), but the AOD ajax endpoint rejects a
# direct navigation with a 404/error page (verified in prod); the ONLY thing that returns the
# 200 offers fragment is an XMLHttpRequest issued from the already-open product page context,
# which supplies the right cookies/referer/fingerprint and the required ajax header. Returns the
# response text on r.ok, else an empty string (never throws — the caller treats "" as failure).
_AOD_FETCH_JS = """
async (url) => {
    try {
        const r = await fetch(url, {headers: {'x-requested-with': 'XMLHttpRequest'}});
        if (!r.ok) return '';
        return await r.text();
    } catch (err) {
        return '';
    }
}
"""

# PDP ingress hint that other offers exist. Used as an offer-state readiness signal
# only — NOT as a precondition for the AOD fetch: the old assumption ("ingress absent
# + 3P buybox = no other offers") was disproven live on 2026-07-21 (B0G3CV6Z9D:
# priceless-3P page variants with zero ingress selectors rendered while the allowed
# Amazon Export offer sat in AOD; 2.5 days of false OOS).
_AOD_INGRESS_SELECTORS = (
    'span[data-action="show-all-offers-display"] a',
    'span[data-action="show-all-offers-display"]',
    "#dynamic-aod-ingress",
    "#aod-ingress-link",
    "#buybox-see-all-buying-choices",
)

# Each AOD offer block carries id="aod-pinned-offer" (featured) or id="aod-offer" (list rows).
# The trailing quote keeps id="aod-offer" from also matching aod-offer-soldBy/-price/-list.
_AOD_BLOCK_ID_RE = re.compile(r'id="aod-(?:pinned-offer|offer)"')


async def _aod_ingress_present_async(page: Any) -> bool:
    """True when the PDP shows an 'other offers exist' ingress (AOD worth fetching)."""
    for sel in _AOD_INGRESS_SELECTORS:
        try:
            if await page.query_selector(sel):
                return True
        except Exception:
            continue
    return False


def _split_aod_offer_blocks(html: str) -> list[str]:
    """Split the AOD fragment into one HTML chunk per offer (pinned + list rows)."""
    starts: list[int] = []
    for m in _AOD_BLOCK_ID_RE.finditer(html or ""):
        tag_start = (html or "").rfind("<div", 0, m.start())
        if tag_start != -1:
            starts.append(tag_start)
    starts = sorted(set(starts))
    if not starts:
        return []
    bounds = starts + [len(html)]
    return [html[bounds[i] : bounds[i + 1]] for i in range(len(starts))]


def _aod_offer_seller_name(block: str) -> str:
    """Seller NAME from an offer's ``#aod-offer-soldBy`` region ONLY.

    Critical (live-verified 2026-07-15): every FBA 3P offer renders "Ships from Amazon.com"
    in ``#aod-offer-shipsFrom``. Matching allowed sellers against combined soldBy+shipsFrom
    text false-positives on every FBA offer, so the match blob must NEVER include shipsFrom.
    The soldBy region renders like "Sold by ACG ECOM Seller rating is ..."; we bound the
    region before shipsFrom, strip the leading "Sold by", and cut at "Seller rating".
    """
    idx = block.find('id="aod-offer-soldBy"')
    if idx < 0:
        return ""
    region = block[idx:]
    end = region.find('id="aod-offer-shipsFrom"')
    if end != -1:
        # Back up to the opening '<' of the shipsFrom tag so no partial tag survives the
        # strip (and the fulfiller name can never leak into the seller match blob).
        lt = region.rfind("<", 0, end)
        region = region[:lt] if lt != -1 else region[:end]
    else:
        region = region[:800]
    # The region starts inside the soldBy opening tag (at its id= attribute); drop the rest
    # of that tag so the leftover attributes are not read as text.
    gt = region.find(">")
    if gt != -1:
        region = region[gt + 1 :]
    text = " ".join(re.sub(r"<[^>]+>", " ", region).split())
    text = re.sub(r"(?i)^\s*sold by\s*", "", text)
    text = re.split(r"(?i)\bseller rating\b", text)[0]
    return text.strip()


def _aod_offer_price(block: str) -> float | None:
    """Pay price from one offer block: ``.a-price .a-offscreen`` first, then whole/fraction.

    Live AOD often ships an EMPTY ``.a-offscreen`` node, so the ``.a-price-whole`` /
    ``.a-price-fraction`` spans (same fallback the accordion row parser uses) are required.
    """
    m = re.search(
        r'class="a-price[^"]*"[\s\S]{0,400}?class="a-offscreen">\s*([^<]+?)\s*<',
        block,
        flags=re.IGNORECASE,
    )
    if m:
        price = _parse_price_text(m.group(1))
        if price is not None:
            return price
    wm = re.search(r'class="a-price-whole"[^>]*>\s*([\d,]+)', block)
    if wm:
        whole = wm.group(1).replace(",", "").replace(".", "")
        fm = re.search(r'class="a-price-fraction"[^>]*>\s*(\d+)', block)
        frac = fm.group(1) if fm else ""
        if whole.isdigit():
            cents = frac if frac.isdigit() else "00"
            return float(f"{whole}.{cents}")
    return None


def parse_aod_offers(html: str) -> list[dict[str, Any]]:
    """Parse the AOD fragment into ``[{"seller_text": name, "price": float|None}, ...]``.

    ``seller_text`` is the sold-by seller NAME only (never the shipsFrom fulfiller).
    """
    offers: list[dict[str, Any]] = []
    for block in _split_aod_offer_blocks(html or ""):
        seller = _aod_offer_seller_name(block)
        price = _aod_offer_price(block)
        if seller or price is not None:
            offers.append({"seller_text": seller, "price": price})
    return offers


def select_allowed_aod_offer(
    offers: list[dict[str, Any]], allowed: list[str]
) -> dict[str, Any] | None:
    """First AOD offer whose sold-by seller name matches an allowed substring, else None."""
    for offer in offers:
        if merchant_matches_allowed(str(offer.get("seller_text") or ""), allowed):
            return offer
    return None


def _aod_check_worthwhile(
    row: dict[str, Any], *, merchant_blob: str, allowed: list[str]
) -> bool:
    """True when the buybox row cannot resolve on its own but AOD might. Pure logic.

    Shapes that qualify:
      - ANY confirmed_out row (seller_mismatch, explicit OOS text, no_pay_price):
        the featured slot saying "gone" does not mean the allowed offer is gone —
        B0G3CV6Z9D 2026-07-18..21 accumulated a 3,239-observation explicit-OOS
        streak over 2.5 days while the public page sold the allowed Amazon Export
        offer the whole time. AOD is the only view of the real offer list.
      - priceless purchasable 3P buybox (unknown/no_pay_price, purchase button enabled,
        non-empty merchant blob matching no allowed seller): the page renders no price in
        any extractable form, so the row stays unknown forever and the in-page retry can
        never settle it — only AOD can say whether an allowed offer exists.
    Engine-side gating (per-ASIN min interval, failure backoff) bounds the cost.
    """
    if row.get("in_stock"):
        return False
    confidence = str(row.get("stock_confidence") or "")
    reason = str(row.get("stock_reason") or "")
    if confidence == "confirmed_out":
        return True
    return (
        confidence == "unknown"
        and reason == "no_pay_price"
        and bool(row.get("buybox_purchasable"))
        and bool((merchant_blob or "").strip())
        and not merchant_matches_allowed(merchant_blob, allowed)
    )


def _apply_aod_outcome(
    asin: str,
    row: dict[str, Any],
    offers: list[dict[str, Any]] | None,
    allowed: list[str],
) -> dict[str, Any]:
    """Map AOD offers onto an unresolved buybox row (see _aod_check_worthwhile for which
    rows qualify). Pure logic (no page / no I/O).

    Every outcome carries ``row["aod_checked"] = True`` (so the engine records the per-ASIN
    timestamp and throttles) plus a ``row["aod_outcome"]`` tag.

    Outcomes:
      - fetch/parse failed (no offers: fetch error / empty fragment / zero blocks): KEEP the
        original buybox-derived row unchanged (still seller_mismatch/confirmed_out) and tag
        ``aod_outcome="fetch_failed"``. Rationale: the PDP itself was healthy and fully
        readable — a failed side-fetch must NOT erase good buybox evidence by turning a clean
        page into a degraded skip (that mislabeled perfect pages "skeleton", fed the
        degraded-burst recycle counter, and — because a skip records no AOD timestamp — hot-
        looped AOD on every 3P-buybox check). The existing C9 OOS debounce still protects
        against premature flips. ``aod_outcome="offer_found"``/``"no_allowed_offer"`` mark the
        two success paths.
      - allowed offer WITH a price: in-stock at that price, stock_reason "aod_offer".
      - allowed offer WITHOUT a parseable price (live: AOD ``.a-offscreen`` often blank):
        priceless_purchasable — an in-stock signal without a price, so the existing
        priceless streak/alert path can confirm it instead of dropping to confirmed_out.
      - offers present but none allowed: keep the original seller_mismatch confirmed_out row.
    """
    row = dict(row)
    row["aod_checked"] = True
    if not offers:
        row["aod_outcome"] = "fetch_failed"
        return row
    matched = select_allowed_aod_offer(offers, allowed)
    if matched is None:
        row["aod_outcome"] = "no_allowed_offer"
        return row
    row["aod_outcome"] = "offer_found"
    seller_text = str(matched.get("seller_text") or "").strip()
    price = matched.get("price")
    if isinstance(price, (int, float)):
        row["in_stock"] = True
        row["price"] = price
        row["stock_confidence"] = "confirmed_in"
        row["stock_reason"] = "aod_offer"
    else:
        row["in_stock"] = False
        row["price"] = None
        row["stock_confidence"] = "unknown"
        row["stock_reason"] = "priceless_purchasable"
        row["buybox_purchasable"] = True
    if seller_text:
        row["seller_text"] = seller_text[:2000]
    return row


async def _fetch_aod_offers_async(page: Any, asin: str) -> list[dict[str, Any]] | None:
    """Fetch the AOD fragment via an in-page XHR on the ALREADY-OPEN product page.

    Prod fix (2026-07-16): the previous implementation navigated a NEW page to the ajax URL,
    which 404s in production — the endpoint only answers an XHR from the product page context.
    Here the fetch is issued in-page (see _AOD_FETCH_JS): same cookies/referer/fingerprint, plus
    the required ajax header. Main extraction is already complete and the page is closed right
    after this check, so we set the fetched fragment as the page content and run the existing
    static parser against it — mirroring the verified live experiment 1:1.

    Returns parsed offers, or None on any failure (empty/whitespace fragment, zero offer blocks,
    or a swallowed page error) so the caller degrades to a fetch_failed outcome. Driver-dead /
    global-network errors propagate (fatal → session recycle).
    """
    url = _AOD_URL_TEMPLATE.format(asin=asin)
    try:
        aod_html = await page.evaluate(_AOD_FETCH_JS, url)
    except Exception as exc:
        if _is_network_error(exc):
            raise NetworkAccessDenied(f"AOD network error for {asin}: {exc}", exc) from exc
        if is_driver_disconnected_error(exc):
            raise BrowserDisconnected(
                f"Browser/driver connection lost fetching AOD for {asin}: {exc}", exc
            ) from exc
        LOGGER.info(
            "PDP AOD in-page fetch failed asin=%s: %s", asin, exc, extra={"channel": "debug"}
        )
        return None
    if not aod_html or not str(aod_html).strip():
        return None
    try:
        await page.set_content(aod_html)
        rendered = await page.content()
    except Exception as exc:
        if is_driver_disconnected_error(exc):
            raise BrowserDisconnected(
                f"Browser/driver connection lost rendering AOD for {asin}: {exc}", exc
            ) from exc
        # set_content is a best-effort mirror of the live experiment; if it fails, still parse
        # the raw fetched fragment so a rendering hiccup never loses real offer evidence.
        rendered = str(aod_html)
    offers = parse_aod_offers(rendered)
    if not offers:
        return None
    return offers


async def _apply_aod_offer_check(
    page: Any, asin: str, allowed: list[str], row: dict[str, Any]
) -> dict[str, Any]:
    """Fetch the AOD panel and fold the result into a seller_mismatch row (see _apply_aod_outcome)."""
    offers = await _fetch_aod_offers_async(page, asin)
    result = _apply_aod_outcome(asin, row, offers, allowed)
    outcome = str(result.get("aod_outcome") or "")
    # One line per AOD check, at the outcome's natural level: a real restock (offer_found +
    # in_stock) is lifecycle; a kept-mismatch or a failed side-fetch is debug noise.
    channel = "lifecycle" if outcome == "offer_found" and result.get("in_stock") else "debug"
    LOGGER.info(
        "PDP AOD check asin=%s outcome=%s in_stock=%s price=%s",
        asin,
        outcome or "?",
        bool(result.get("in_stock")),
        result.get("price"),
        extra={"channel": channel},
    )
    return result


async def _scrape_pdp_on_context(
    context: Any,
    normalized: list[str],
    allowed: list[str],
    *,
    max_cycle_seconds: float,
    scroll_delay_range: tuple[float, float],
    max_concurrent: int,
    jitter_range: tuple[float, float],
    max_attempts: int,
    pdp_settle_seconds: float = _PDP_SETTLE_SECONDS_DEFAULT,
    pdp_settle_poll_interval_seconds: float = 1.0,
    pdp_unknown_retry_seconds: float = 2.5,
    pdp_continue_shopping_max_clicks: int = _PDP_CONTINUE_SHOPPING_MAX_CLICKS_DEFAULT,
    pdp_dump_no_price_html: bool = True,
    allow_aod: bool = False,
) -> tuple[list[dict[str, Any]], float]:
    """Scrape watch ASINs on an existing async BrowserContext (caller owns bandwidth meter)."""
    cycle_started = time.monotonic()
    sem = asyncio.Semaphore(max_concurrent)
    stealth_ctx_applied = bool(getattr(context, "_stealth_ctx_applied", False))
    stealth_page_fallback_warned = False
    settle_s = _clamp_pdp_settle_seconds(pdp_settle_seconds)
    settle_poll_s = _clamp_pdp_settle_poll_interval(pdp_settle_poll_interval_seconds)
    unknown_retry_s = _clamp_pdp_unknown_retry_seconds(pdp_unknown_retry_seconds)
    continue_clicks = _clamp_continue_shopping_clicks(pdp_continue_shopping_max_clicks)
    attempt = max(1, int(max_attempts))
    LOGGER.info(
        "pdp_watch starting concurrent_tabs=%s jitter=%.2f-%.2f asins=%s "
        "settle_seconds=%s settle_poll=%s unknown_retry=%s continue_shopping_max_clicks=%s",
        max_concurrent,
        jitter_range[0],
        jitter_range[1],
        len(normalized),
        settle_s,
        settle_poll_s,
        unknown_retry_s,
        continue_clicks,
        extra={"channel": "debug"},
    )

    async def worker(idx: int, asin: str) -> tuple[int, dict[str, Any]]:
        nonlocal stealth_page_fallback_warned
        worker_started = time.monotonic()
        async with sem:
            if time.monotonic() - cycle_started > max_cycle_seconds:
                return idx, _pdp_skip_row(asin, "cycle_budget_exceeded")

            await asyncio.sleep(random.uniform(jitter_range[0], jitter_range[1]))
            if time.monotonic() - cycle_started > max_cycle_seconds:
                return idx, _pdp_skip_row(asin, "cycle_budget_exceeded")

            if browser_factory.global_rate_limiter:
                await asyncio.to_thread(browser_factory.global_rate_limiter.acquire)

            url = f"https://www.amazon.com/dp/{asin}"
            try:
                page = await context.new_page()
            except Exception as exc:
                # new_page() is the first driver round-trip for this check. When the
                # Chromium process/driver pipe has died, this is where it surfaces
                # first (confirmed in production logs: "BrowserContext.new_page:
                # Connection closed while reading from the driver"). Every other
                # ASIN on this context will fail identically, so this must be raised
                # as fatal (not swallowed into a per-ASIN skip row) so the engine
                # recycles the session immediately instead of retrying forever.
                if is_driver_disconnected_error(exc):
                    raise BrowserDisconnected(
                        f"Browser/driver connection lost opening page for {asin}: {exc}", exc
                    ) from exc
                raise
            merchant_blob = ""
            shipping = ""
            image_url = None
            try:
                page.set_default_timeout(2_000)
                page.set_default_navigation_timeout(_PDP_GOTO_TIMEOUT_MS)
                if not stealth_ctx_applied:
                    try:
                        await asyncio.to_thread(STEALTH.apply_stealth_sync, page)
                    except Exception as exc:
                        if not stealth_page_fallback_warned:
                            stealth_page_fallback_warned = True
                            LOGGER.warning(
                                "pdp_watch per-page stealth fallback failed (continuing): %s",
                                exc,
                            )

                nav_error: Exception | None = None
                nav_tries_used = 0
                for nav_try in range(1, attempt + 1):
                    nav_tries_used = nav_try
                    try:
                        await page.goto(
                            url,
                            wait_until=browser_factory.NAV_WAIT_UNTIL,
                            timeout=_PDP_GOTO_TIMEOUT_MS,
                        )
                        nav_error = None
                        break
                    except Exception as e:
                        if _is_network_error(e):
                            raise NetworkAccessDenied(f"PDP network error for {asin}: {e}", e) from e
                        if is_driver_disconnected_error(e):
                            # A dead driver during goto() must be fatal like the
                            # new_page() case — otherwise it degrades into a false
                            # "navigation_failed" skip row while the session keeps
                            # limping on a dead browser.
                            raise BrowserDisconnected(
                                f"Browser/driver connection lost navigating to {asin}: {e}", e
                            ) from e
                        nav_error = e
                        if nav_try >= attempt or time.monotonic() - cycle_started > max_cycle_seconds:
                            break
                        LOGGER.info(
                            "PDP navigation failed asin=%s try=%s/%s wait_until=%s (retrying): %s",
                            asin,
                            nav_try,
                            attempt,
                            browser_factory.NAV_WAIT_UNTIL,
                            e,
                            extra={"channel": "debug"},
                        )
                        await asyncio.sleep(random.uniform(0.4, 0.9))
                if nav_error is not None:
                    LOGGER.info(
                        "PDP navigation failed asin=%s tries=%s/%s wait_until=%s: %s",
                        asin,
                        nav_tries_used,
                        attempt,
                        browser_factory.NAV_WAIT_UNTIL,
                        nav_error,
                        extra={"channel": "debug"},
                    )
                    await asyncio.sleep(random.uniform(scroll_delay_range[0], scroll_delay_range[1]))
                    elapsed_ms = int(round((time.monotonic() - worker_started) * 1000))
                    return idx, _pdp_skip_row(
                        asin,
                        "navigation_failed",
                        scrape_attempts=nav_tries_used,
                        scrape_elapsed_ms=elapsed_ms,
                    )

                LOGGER.info(
                    "PDP navigation committed asin=%s",
                    asin,
                    extra={"channel": "debug"},
                )

                if await _is_hard_captcha_async(page):
                    LOGGER.info(
                        "PDP hard captcha asin=%s (skipping update)",
                        asin,
                        extra={"channel": "debug"},
                    )
                    await asyncio.sleep(random.uniform(scroll_delay_range[0], scroll_delay_range[1]))
                    elapsed_ms = int(round((time.monotonic() - worker_started) * 1000))
                    return idx, _pdp_skip_row(
                        asin,
                        "captcha",
                        skip_detail="robot_check",
                        scrape_attempts=attempt,
                        scrape_elapsed_ms=elapsed_ms,
                    )

                await _dismiss_continue_shopping_async(
                    page, max_clicks=continue_clicks, asin=asin
                )
                # (a) Short container gate — still fast-fails a genuinely dead page. Capped
                # so it cannot eat the whole settle budget: #availability appears seconds
                # before the price widgets hydrate on a slow machine, so this gate passes
                # almost instantly and must NOT be the readiness signal on its own.
                gate_started = time.monotonic()
                buybox_ready = await _wait_for_buybox_ready_async(
                    page,
                    timeout_s=min(3.0, settle_s),
                    poll_interval_s=settle_poll_s,
                )
                # (b) Spend the remaining settle budget polling for a RESOLVABLE offer state
                # (price / oos / alt_offers / accordion) so extraction runs on a hydrated
                # page, not a half-built one (the false-skeleton-skip root cause).
                remaining_settle_s = max(0.0, settle_s - (time.monotonic() - gate_started))
                offer_wait_started = time.monotonic()
                offer_signal = await _wait_for_offer_state_async(
                    page,
                    timeout_s=remaining_settle_s,
                    poll_interval_s=settle_poll_s,
                )
                offer_state_resolved = offer_signal != "timeout"
                await _dismiss_continue_shopping_async(
                    page, max_clicks=continue_clicks, asin=asin
                )
                LOGGER.debug(
                    "PDP offer-state asin=%s signal=%s waited=%.1fs",
                    asin,
                    offer_signal,
                    time.monotonic() - offer_wait_started,
                    extra={"channel": "debug"},
                )
                if not buybox_ready:
                    LOGGER.info(
                        "PDP buybox not ready after settle asin=%s timeout_s=%s",
                        asin,
                        settle_s,
                        extra={"channel": "debug"},
                    )

                state = await _extract_pdp_page_state_async(
                    page, asin=asin, allowed=allowed, dump_no_price_html=pdp_dump_no_price_html
                )
                availability_text = state["availability_text"]
                explicit_oos = bool(state["explicit_oos"])
                explicit_reason = state.get("explicit_reason")
                title = str(state.get("title") or "")
                price = state.get("price")
                buybox_purchasable = bool(state.get("buybox_purchasable"))
                merchant_blob = str(state.get("merchant_blob") or "")
                shipping = str(state.get("shipping") or "")
                image_url = state.get("image_url")

                if _pdp_row_should_retry_unknown(
                    asin=asin,
                    title=title,
                    price=price if isinstance(price, (int, float)) else None,
                    shipping_text=shipping,
                    image_url=image_url if isinstance(image_url, str) else None,
                    merchant_blob=merchant_blob,
                    allowed=allowed,
                    availability_text=availability_text,
                    explicit_oos=explicit_oos,
                    buybox_purchasable=buybox_purchasable,
                ) and unknown_retry_s > 0:
                    LOGGER.info(
                        "PDP unknown confidence asin=%s reason=initial_pass retry_s=%s",
                        asin,
                        unknown_retry_s,
                        extra={"channel": "debug"},
                    )
                    await asyncio.sleep(unknown_retry_s)
                    state = await _extract_pdp_page_state_async(
                        page, asin=asin, allowed=allowed, dump_no_price_html=pdp_dump_no_price_html
                    )
                    availability_text = state["availability_text"]
                    explicit_oos = bool(state["explicit_oos"])
                    explicit_reason = state.get("explicit_reason")
                    title = str(state.get("title") or "")
                    price = state.get("price")
                    buybox_purchasable = bool(state.get("buybox_purchasable"))
                    merchant_blob = str(state.get("merchant_blob") or "")
                    shipping = str(state.get("shipping") or "")
                    image_url = state.get("image_url")
                    resolved = not _pdp_row_should_retry_unknown(
                        asin=asin,
                        title=title,
                        price=price if isinstance(price, (int, float)) else None,
                        shipping_text=shipping,
                        image_url=image_url if isinstance(image_url, str) else None,
                        merchant_blob=merchant_blob,
                        allowed=allowed,
                        availability_text=availability_text,
                        explicit_oos=explicit_oos,
                    )
                    LOGGER.info(
                        "PDP unknown retry asin=%s resolved=%s",
                        asin,
                        resolved,
                        extra={"channel": "debug"},
                    )

                if not explicit_oos and not title and price is None:
                    LOGGER.info(
                        "PDP scrape empty asin=%s (skipping update)",
                        asin,
                        extra={"channel": "debug"},
                    )
                    await asyncio.sleep(random.uniform(scroll_delay_range[0], scroll_delay_range[1]))
                    elapsed_ms = int(round((time.monotonic() - worker_started) * 1000))
                    return idx, _pdp_skip_row(
                        asin,
                        "parse_failed",
                        skip_detail="empty_parse",
                        scrape_attempts=attempt,
                        scrape_elapsed_ms=elapsed_ms,
                        dom_ok=False,
                    )

                # Offer-less nav shell: nothing below the nav rendered, yet a title
                # survives via the document <title>, so extraction lands on
                # explicit_oos/no_pay_price — which both bypasses the skeleton gate
                # below (it requires not explicit_oos) and would flow to the state
                # engine as confirmed_out OOS evidence. Must therefore run BEFORE the
                # skeleton check and without the explicit_oos gate. Shell windows are
                # session state, not product state: emit a degraded_page skip so
                # fast-retry / burst-recycle / the mass-flip breaker absorb it instead
                # of the OOS debounce. The no-price HTML dump already fired inside
                # _extract_pdp_page_state_async.
                if not offer_state_resolved and await _page_is_nav_shell_async(page, asin=asin):
                    await asyncio.sleep(random.uniform(scroll_delay_range[0], scroll_delay_range[1]))
                    elapsed_ms = int(round((time.monotonic() - worker_started) * 1000))
                    return idx, _pdp_skip_row(
                        asin,
                        "degraded_page",
                        skip_detail="nav_shell",
                        scrape_attempts=attempt,
                        scrape_elapsed_ms=elapsed_ms,
                    )

                # Cached FallbackDetailPage render: everything on it is stale —
                # in-stock and OOS alike — so it is never evidence, regardless of how
                # extraction classified it. Not gated on the offer-state wait: a
                # fallback page is fully rendered and its (stale) offer signals resolve.
                if await _page_is_fallback_detail_async(page, asin=asin):
                    await asyncio.sleep(random.uniform(scroll_delay_range[0], scroll_delay_range[1]))
                    elapsed_ms = int(round((time.monotonic() - worker_started) * 1000))
                    return idx, _pdp_skip_row(
                        asin,
                        "degraded_page",
                        skip_detail="fallback_page",
                        scrape_attempts=attempt,
                        scrape_elapsed_ms=elapsed_ms,
                    )

                # Degraded (skeleton) page: title rendered but no price and no explicit
                # OOS text — the exact would-be no_pay_price/priceless_purchasable state.
                # Only reachable when the offer-state wait TIMED OUT: if any offer signal
                # (price/oos/alt_offers/accordion) was seen the page hydrated and a
                # price-less result is a real classification, not a soft-block skeleton —
                # running the skeleton check there caused the false-skip loop on
                # slow-hydrating ASINs. When the wait genuinely timed out and the skeleton
                # markers are present, this is a scrape failure, not evidence: emit a
                # degraded_page skip row so the state engine ignores it. The no-price HTML
                # dump already fired inside _extract_pdp_page_state_async.
                if (
                    not offer_state_resolved
                    and not explicit_oos
                    and price is None
                    and title
                    and await _page_offers_skeleton_async(page, asin=asin)
                ):
                    await asyncio.sleep(random.uniform(scroll_delay_range[0], scroll_delay_range[1]))
                    elapsed_ms = int(round((time.monotonic() - worker_started) * 1000))
                    return idx, _pdp_skip_row(
                        asin,
                        "degraded_page",
                        skip_detail="skeleton_offers",
                        scrape_attempts=attempt,
                        scrape_elapsed_ms=elapsed_ms,
                    )

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
                    buybox_purchasable=buybox_purchasable,
                )
                if explicit_oos and explicit_reason:
                    row["stock_reason"] = explicit_reason
                row["is_preorder"] = bool(state.get("is_preorder"))

                # F1: a would-be confirmed_out row means the featured slot looks gone —
                # but the allowed Amazon offer may still live in the All Offers Display
                # panel. Same story for a priceless purchasable 3P buybox
                # (unknown/no_pay_price with a non-allowed merchant, e.g. B0GW2DK37Q
                # 2026-07-16: "Kings Games" buybox with no price anywhere on the page).
                # NOT gated on an AOD ingress being visible: live evidence 2026-07-21
                # (B0G3CV6Z9D dumps 05:23Z) shows Amazon serving priceless-3P/OOS page
                # variants with NO ingress rendered at all while the allowed offer
                # exists in AOD — the ingress requirement made the offer structurally
                # invisible for 2.5 days (oos_miss_streak 3,239). The in-page ajax
                # fetch works regardless of ingress; the engine's per-ASIN throttle
                # and failure backoff bound the cost.
                if allow_aod and _aod_check_worthwhile(
                    row, merchant_blob=merchant_blob, allowed=allowed
                ):
                    row = await _apply_aod_offer_check(page, asin, allowed, row)

                elapsed_ms = int(round((time.monotonic() - worker_started) * 1000))
                _attach_scrape_meta(
                    row,
                    scrape_attempts=attempt,
                    scrape_elapsed_ms=elapsed_ms,
                )
                await asyncio.sleep(random.uniform(scroll_delay_range[0], scroll_delay_range[1]))
                return idx, row
            except (NetworkAccessDenied, BrowserDisconnected):
                raise
            except Exception as exc:
                if is_driver_disconnected_error(exc):
                    # Same fatal condition as the new_page() check above, just surfaced
                    # later (e.g. mid-extraction "Page.title: Connection closed while
                    # reading from the driver"). Must propagate as fatal too, or the
                    # session keeps limping along issuing doomed per-ASIN retries.
                    raise BrowserDisconnected(
                        f"Browser/driver connection lost scraping {asin}: {exc}", exc
                    ) from exc
                LOGGER.info(
                    "PDP row parse failed asin=%s: %s (skipping update)",
                    asin,
                    exc,
                    extra={"channel": "debug"},
                )
                await asyncio.sleep(random.uniform(scroll_delay_range[0], scroll_delay_range[1]))
                elapsed_ms = int(round((time.monotonic() - worker_started) * 1000))
                return idx, _pdp_skip_row(
                    asin,
                    "parse_failed",
                    skip_detail="exception",
                    scrape_attempts=attempt,
                    scrape_elapsed_ms=elapsed_ms,
                )
            finally:
                # A close-time failure here (e.g. driver already disconnected) must
                # never mask the try/except block's return value or its raised
                # exception — Python replaces a pending return/raise with whatever
                # a `finally` raises, which previously turned a graceful skip-row
                # return into an unhandled "Page.close: Connection closed..." error
                # (confirmed in logs, immediately after a "(skipping update)" line
                # for the same ASIN). Best-effort close only; never re-raise here.
                try:
                    await page.close()
                except Exception as close_exc:
                    LOGGER.info(
                        "PDP page close failed asin=%s (ignored): %s",
                        asin,
                        close_exc,
                        extra={"channel": "debug"},
                    )

    tasks = [worker(idx, asin) for idx, asin in enumerate(normalized)]
    gathered = await asyncio.gather(*tasks, return_exceptions=True)

    for item in gathered:
        if isinstance(item, NetworkAccessDenied):
            raise item
    for item in gathered:
        if isinstance(item, BrowserDisconnected):
            LOGGER.warning("pdp_watch browser disconnected (session will be recycled): %s", item)
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
    pdp_elapsed = time.monotonic() - cycle_started
    _emit_pdp_cycle_debug_report(rows_out, pdp_elapsed)
    return rows_out, pdp_elapsed


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
    pdp_settle_seconds: float = _PDP_SETTLE_SECONDS_DEFAULT,
    pdp_settle_poll_interval_seconds: float = 1.0,
    pdp_unknown_retry_seconds: float = 2.5,
    pdp_continue_shopping_max_clicks: int = _PDP_CONTINUE_SHOPPING_MAX_CLICKS_DEFAULT,
    pdp_dump_no_price_html: bool = True,
    context: Any | None = None,
    config: dict[str, Any] | None = None,
    record_metrics: bool = True,
) -> list[dict[str, Any]]:
    """Run PDP watch on an existing context or launch a standalone browser."""
    scrape_kwargs = {
        "max_cycle_seconds": max_cycle_seconds,
        "scroll_delay_range": scroll_delay_range,
        "max_concurrent": max_concurrent,
        "jitter_range": jitter_range,
        "max_attempts": max_attempts,
        "pdp_settle_seconds": pdp_settle_seconds,
        "pdp_settle_poll_interval_seconds": pdp_settle_poll_interval_seconds,
        "pdp_unknown_retry_seconds": pdp_unknown_retry_seconds,
        "pdp_continue_shopping_max_clicks": pdp_continue_shopping_max_clicks,
        "pdp_dump_no_price_html": pdp_dump_no_price_html,
    }

    if context is not None:
        rows_out, pdp_elapsed = await _scrape_pdp_on_context(
            context, normalized, allowed, **scrape_kwargs
        )
        if record_metrics:
            ok = sum(1 for r in rows_out if isinstance(r, dict) and not r.get("_skip_update"))
            skip = len(rows_out) - ok
            usage_metrics.record_pdp_phase(pdp_elapsed, 0, ok=ok, skip=skip)
        return rows_out

    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser, ctx = await create_async_stealth_context(pw, headless=headless, config=config)
        try:
            rows_out, pdp_elapsed = await _scrape_pdp_on_context(
                ctx, normalized, allowed, **scrape_kwargs
            )
        finally:
            await close_async_browser(browser, ctx)

    if record_metrics:
        ok = sum(1 for r in rows_out if isinstance(r, dict) and not r.get("_skip_update"))
        skip = len(rows_out) - ok
        usage_metrics.record_pdp_phase(pdp_elapsed, 0, ok=ok, skip=skip)
    return rows_out


async def scrape_pdp_watch_async(
    asins: list[str],
    allowed_seller_substrings: list[str],
    *,
    max_cycle_seconds: int = 170,
    scroll_delay_range: tuple[float, float] = (0.25, 0.65),
    max_concurrent_tabs: int = 3,
    tab_jitter_seconds: tuple[float, float] | list[float] | None = None,
    max_attempts: int = _PDP_MAX_ATTEMPTS,
    headless: bool = True,
    pdp_settle_seconds: float = _PDP_SETTLE_SECONDS_DEFAULT,
    pdp_settle_poll_interval_seconds: float = 1.0,
    pdp_unknown_retry_seconds: float = 2.5,
    pdp_continue_shopping_max_clicks: int = _PDP_CONTINUE_SHOPPING_MAX_CLICKS_DEFAULT,
    pdp_dump_no_price_html: bool = True,
    context: Any | None = None,
    config: dict[str, Any] | None = None,
    record_metrics: bool = True,
) -> list[dict[str, Any]]:
    """Async PDP watch scrape.

    When ``context`` is provided (main monitor cycle), reuses an existing async
    BrowserContext with context-level BandwidthMeter attached by the caller.
    Standalone callers omit ``context`` to launch and close their own browser.
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
    cfg = config if isinstance(config, dict) else {}
    settle_poll = float(cfg.get("pdp_settle_poll_interval_seconds", pdp_settle_poll_interval_seconds))
    unknown_retry = float(cfg.get("pdp_unknown_retry_seconds", pdp_unknown_retry_seconds))
    dump_no_price = bool(cfg.get("pdp_dump_no_price_html", pdp_dump_no_price_html))

    return await _run_pdp_watch_async(
        normalized,
        allowed,
        max_cycle_seconds=float(max_cycle_seconds),
        scroll_delay_range=scroll_delay_range,
        max_concurrent=conc,
        jitter_range=jitter,
        max_attempts=max(1, int(max_attempts)),
        headless=headless,
        pdp_settle_seconds=pdp_settle_seconds,
        pdp_settle_poll_interval_seconds=settle_poll,
        pdp_unknown_retry_seconds=unknown_retry,
        pdp_continue_shopping_max_clicks=pdp_continue_shopping_max_clicks,
        pdp_dump_no_price_html=dump_no_price,
        context=context,
        config=config,
        record_metrics=record_metrics,
    )


# Visit each watched product page and return a simple stock/price snapshot for each ASIN without letting one slow page break the whole cycle.
def scrape_pdp_watch(
    asins: list[str],
    allowed_seller_substrings: list[str],
    *,
    max_cycle_seconds: int = 170,
    scroll_delay_range: tuple[float, float] = (0.25, 0.65),
    max_concurrent_tabs: int = 3,
    tab_jitter_seconds: tuple[float, float] | list[float] | None = None,
    max_attempts: int = _PDP_MAX_ATTEMPTS,
    headless: bool = True,
    pdp_settle_seconds: float = _PDP_SETTLE_SECONDS_DEFAULT,
    pdp_continue_shopping_max_clicks: int = _PDP_CONTINUE_SHOPPING_MAX_CLICKS_DEFAULT,
) -> list[dict[str, Any]]:
    """Visit each watch ASIN on amazon.com PDP; return exactly one dict per unique valid ASIN (order preserved).

    Uses concurrent Playwright tabs (async API), capped at 3, sharing the global token bucket.
    Launches its own browser (backward compat for tests). Production main uses
    ``scrape_pdp_watch_async(..., context=...)`` via ``_scrape_pdp_on_context``.

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
            pdp_settle_seconds=pdp_settle_seconds,
            pdp_continue_shopping_max_clicks=pdp_continue_shopping_max_clicks,
        )
    )
