"""PDP (product detail page) scrape for configured watch ASINs (Amazon first-party offers)."""

from __future__ import annotations

import logging
import random
import re
import time
import unicodedata
from typing import Any

import browser_factory
from browser_factory import close_context, create_stealth_context
from exceptions import CaptchaBlocked, NetworkAccessDenied
from search_scraper import _valid_asin

LOGGER = logging.getLogger(__name__)

# ~15s worst-case per ASIN (goto + title wait); odd PDPs must not block search_loop.
_PDP_GOTO_TIMEOUT_MS = 12_000
_PDP_TITLE_WAIT_MS = 8_000

_PRICE_RE = re.compile(r"\$?\s*([0-9][0-9,]*)(?:\.(\d{2}))?")


def _normalize_for_match(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", (value or "").lower().strip())
    return decomposed.encode("ascii", "ignore").decode("ascii")


def merchant_matches_allowed(merchant_blob: str, allowed_substrings: list[str]) -> bool:
    """True if any normalized substring appears in the normalized merchant/shipping blob."""
    blob = _normalize_for_match(merchant_blob)
    for sub in allowed_substrings:
        s = _normalize_for_match(str(sub))
        if s and s in blob:
            return True
    return False


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


def _extract_pdp_price(page) -> float | None:
    """Use query_selector only (no locator auto-wait) so missing buy box returns fast."""
    for sel in (
        "#corePrice_feature_div .a-price .a-offscreen",
        "#corePriceDisplay_desktop_feature_div .a-price .a-offscreen",
        ".reinventPricePriceToPayMargin .a-price .a-offscreen",
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
    try:
        whole = page.query_selector(".a-price-whole")
        frac = page.query_selector(".a-price-fraction")
        if whole and frac:
            w = (whole.inner_text() or "").strip().replace(",", "")
            f = (frac.inner_text() or "").strip()
            if w.isdigit() and f.isdigit():
                return float(f"{w}.{f}")
    except Exception:
        return None
    return None


def _extract_pdp_title(page) -> str:
    try:
        node = page.query_selector("#productTitle") or page.query_selector("#title")
        if not node:
            return ""
        return (node.inner_text() or "").strip()
    except Exception:
        return ""


def _extract_pdp_image(page) -> str | None:
    for sel in ("#landingImage", "#imgBlkFront", "#main-image"):
        try:
            el = page.query_selector(sel)
            if not el:
                continue
            href = el.get_attribute("src")
            if href and href.startswith("http"):
                return href.strip()
        except Exception:
            continue
    return None


def _extract_pdp_shipping(page) -> str:
    for sel in (
        "#deliveryBlockMessage",
        "#mir-layout-DELIVERY_BLOCK-slot-PRIMARYDELIVERYBLOCKLARGE",
        "#ddmDeliveryMessage",
        "[data-cy='delivery-recipe']",
    ):
        try:
            el = page.query_selector(sel)
            if not el:
                continue
            t = (el.inner_text() or "").strip()
            if t:
                return t
        except Exception:
            continue
    return ""


def _pdp_merchant_blob(page) -> str:
    parts: list[str] = []
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


def _is_not_shippable(shipping_text: str) -> bool:
    """True when Amazon explicitly states the item can't ship to the selected location."""
    t = _normalize_for_match(shipping_text or "")
    patterns = (
        "cannot be shipped to your selected delivery location",
        "can't be shipped to your selected delivery location",
        "cannot be delivered to your selected delivery location",
        "can't be delivered to your selected delivery location",
        "choose a different delivery location",
    )
    return any(p in t for p in patterns)


def _pdp_row(
    asin: str,
    *,
    title: str,
    price: float | None,
    shipping_text: str,
    image_url: str | None,
    merchant_blob: str,
    allowed: list[str],
) -> dict[str, Any]:
    qualifies = price is not None and merchant_matches_allowed(merchant_blob, allowed)
    if _is_not_shippable(shipping_text):
        qualifies = False
    return {
        "asin": asin,
        "title": title,
        "price": price if qualifies else None,
        "in_stock": bool(qualifies),
        "shipping_text": shipping_text,
        "image_url": image_url,
        "seller": "pdp_watch",
        "seller_text": merchant_blob[:2000],
        "product_url": f"https://www.amazon.com/dp/{asin}",
        "source": "pdp_watch",
    }


def _pdp_skip_row(asin: str, reason: str) -> dict[str, Any]:
    """Marker row for one-page operational failures: state engine must not touch the DB row."""
    return {
        "asin": asin,
        "_skip_update": True,
        "skip_reason": reason,
        "source": "pdp_watch",
    }


def scrape_pdp_watch(
    asins: list[str],
    allowed_seller_substrings: list[str],
    *,
    max_cycle_seconds: int = 170,
    scroll_delay_range: tuple[float, float] = (0.25, 0.65),
) -> list[dict[str, Any]]:
    """Visit each watch ASIN on amazon.com PDP; return exactly one dict per unique valid ASIN (order preserved).

    ``in_stock`` is True only when a parseable buy-box price exists and merchant blob matches
    ``allowed_seller_substrings`` (substring match after ASCII normalization).
    """
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in asins:
        a = (raw or "").strip().upper()
        if not _valid_asin(a) or a in seen:
            continue
        seen.add(a)
        normalized.append(a)
    if not normalized:
        return []

    allowed = [str(s) for s in allowed_seller_substrings if str(s).strip()]
    if not allowed:
        LOGGER.warning("pdp_watch: no allowed_seller_substrings; all rows will be out of stock")

    results: list[dict[str, Any]] = []
    context = create_stealth_context(persistent_dir=None, headless=True)
    cycle_started = time.monotonic()
    try:
        page = context.new_page()
        page.set_default_timeout(2_000)
        page.set_default_navigation_timeout(_PDP_GOTO_TIMEOUT_MS)
        for idx, asin in enumerate(normalized):
            if time.monotonic() - cycle_started > max_cycle_seconds:
                LOGGER.warning(
                    "PDP watch cycle budget exceeded (elapsed=%.1fs); skipping remaining ASINs (DB unchanged)",
                    time.monotonic() - cycle_started,
                )
                for rest in normalized[idx:]:
                    results.append(_pdp_skip_row(rest, "cycle_budget_exceeded"))
                break

            if browser_factory.global_rate_limiter:
                browser_factory.global_rate_limiter.acquire()

            url = f"https://www.amazon.com/dp/{asin}"
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=_PDP_GOTO_TIMEOUT_MS)
            except Exception as e:
                if _is_network_error(e):
                    raise NetworkAccessDenied(f"PDP network error for {asin}: {e}", e) from e
                LOGGER.warning("PDP goto failed asin=%s: %s (skipping update)", asin, e)
                results.append(_pdp_skip_row(asin, "goto_failed"))
                time.sleep(random.uniform(scroll_delay_range[0], scroll_delay_range[1]))
                continue

            title_l = (page.title() or "").lower()
            if "robot check" in title_l or page.query_selector("form[action*='validateCaptcha']"):
                raise CaptchaBlocked(f"Captcha on PDP {asin}")

            try:
                try:
                    page.wait_for_selector("#productTitle, #title, h1.a-size-large", timeout=_PDP_TITLE_WAIT_MS)
                except Exception:
                    pass
                title = _extract_pdp_title(page) or (page.title() or "").strip()
                merchant_blob = _pdp_merchant_blob(page)
                price = _extract_pdp_price(page)
                shipping = _extract_pdp_shipping(page)
                image_url = _extract_pdp_image(page)
                results.append(
                    _pdp_row(
                        asin,
                        title=title,
                        price=price,
                        shipping_text=shipping,
                        image_url=image_url,
                        merchant_blob=merchant_blob,
                        allowed=allowed,
                    )
                )
            except Exception as exc:
                LOGGER.warning("PDP row parse failed asin=%s: %s (skipping update)", asin, exc)
                results.append(_pdp_skip_row(asin, "parse_failed"))
            time.sleep(random.uniform(scroll_delay_range[0], scroll_delay_range[1]))
    finally:
        close_context(context)

    return results
