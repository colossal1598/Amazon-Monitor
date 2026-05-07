"""PDP (product detail page) scrape for configured watch ASINs (Amazon first-party offers)."""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import time
import unicodedata
from typing import Any

import browser_factory
from browser_factory import USER_AGENTS, STEALTH
from exceptions import CaptchaBlocked, NetworkAccessDenied
from filter_pipeline import row_has_free_shipping
from search_scraper import _valid_asin

LOGGER = logging.getLogger(__name__)

# ~15s worst-case per ASIN (goto + title wait); odd PDPs must not block search_loop.
_PDP_GOTO_TIMEOUT_MS = 12_000
_PDP_TITLE_WAIT_MS = 8_000

_PRICE_RE = re.compile(r"\$?\s*([0-9][0-9,]*)(?:\.(\d{2}))?")


# Simplify text into an easy-to-compare form so seller and shipping wording matches even when formatting differs.
def _normalize_for_match(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", (value or "").lower().strip())
    return decomposed.encode("ascii", "ignore").decode("ascii")


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
            href = el.get_attribute("src")
            if href and href.startswith("http"):
                return href.strip()
        except Exception:
            continue
    return None


# Extract the delivery/shipping message from the product page so alerts can show whether it ships to your location.
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


# Collect the page’s merchant and buy-box text into one blob so we can confirm the seller matches what you allow.
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


# Detect the “can’t ship to your address” message so we don’t treat the item as in stock for you.
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
) -> dict[str, Any]:
    qualifies = price is not None and merchant_matches_allowed(merchant_blob, allowed)
    if _is_not_shippable(shipping_text):
        qualifies = False
    if qualifies and not row_has_free_shipping({"shipping_text": shipping_text}):
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


async def _extract_pdp_price_async(page: Any) -> float | None:
    for sel in (
        "#corePrice_feature_div .a-price .a-offscreen",
        "#corePriceDisplay_desktop_feature_div .a-price .a-offscreen",
        ".reinventPricePriceToPayMargin .a-price .a-offscreen",
        "#tp_price_block_total_price_ww .a-offscreen",
        "span.a-price.a-text-price .a-offscreen",
    ):
        try:
            el = await page.query_selector(sel)
            if not el:
                continue
            raw = (await el.inner_text() or "").strip()
        except Exception:
            raw = ""
        p = _parse_price_text(raw)
        if p is not None:
            return p
    try:
        whole = await page.query_selector(".a-price-whole")
        frac = await page.query_selector(".a-price-fraction")
        if whole and frac:
            w = (await whole.inner_text() or "").strip().replace(",", "")
            f = (await frac.inner_text() or "").strip()
            if w.isdigit() and f.isdigit():
                return float(f"{w}.{f}")
    except Exception:
        return None
    return None


async def _extract_pdp_image_async(page: Any) -> str | None:
    for sel in ("#landingImage", "#imgBlkFront", "#main-image"):
        try:
            el = await page.query_selector(sel)
            if not el:
                continue
            href = await el.get_attribute("src")
            if href and href.startswith("http"):
                return href.strip()
        except Exception:
            continue
    return None


async def _extract_pdp_shipping_async(page: Any) -> str:
    for sel in (
        "#deliveryBlockMessage",
        "#mir-layout-DELIVERY_BLOCK-slot-PRIMARYDELIVERYBLOCKLARGE",
        "#ddmDeliveryMessage",
        "[data-cy='delivery-recipe']",
    ):
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


async def _pdp_merchant_blob_async(page: Any) -> str:
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
) -> list[dict[str, Any]]:
    from playwright.async_api import async_playwright

    cycle_started = time.monotonic()
    sem = asyncio.Semaphore(max_concurrent)
    stealth_ctx_applied = False
    stealth_page_fallback_warned = False
    LOGGER.info(
        "pdp_watch starting concurrent_tabs=%s jitter=%.2f-%.2f asins=%s",
        max_concurrent,
        jitter_range[0],
        jitter_range[1],
        len(normalized),
    )

    async with async_playwright() as pw:
        proxy_url = os.getenv("PROXY_URL")
        launch_kwargs: dict[str, Any] = {"channel": "chrome", "headless": True}
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
            async with sem:
                if time.monotonic() - cycle_started > max_cycle_seconds:
                    return idx, _pdp_skip_row(asin, "cycle_budget_exceeded")

                await asyncio.sleep(random.uniform(jitter_range[0], jitter_range[1]))

                if browser_factory.global_rate_limiter:
                    await asyncio.to_thread(browser_factory.global_rate_limiter.acquire)

                if time.monotonic() - cycle_started > max_cycle_seconds:
                    return idx, _pdp_skip_row(asin, "cycle_budget_exceeded")

                page = await context.new_page()
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

                    url = f"https://www.amazon.com/dp/{asin}"
                    try:
                        await page.goto(url, wait_until="domcontentloaded", timeout=_PDP_GOTO_TIMEOUT_MS)
                    except Exception as e:
                        if _is_network_error(e):
                            raise NetworkAccessDenied(f"PDP network error for {asin}: {e}", e) from e
                        LOGGER.warning("PDP goto failed asin=%s: %s (skipping update)", asin, e)
                        await asyncio.sleep(random.uniform(scroll_delay_range[0], scroll_delay_range[1]))
                        return idx, _pdp_skip_row(asin, "goto_failed")

                    title_l = (await page.title() or "").lower()
                    cap_el = await page.query_selector("form[action*='validateCaptcha']")
                    if "robot check" in title_l or cap_el:
                        raise CaptchaBlocked(f"Captcha on PDP {asin}")

                    try:
                        await page.wait_for_selector(
                            "#productTitle, #title, h1.a-size-large",
                            timeout=_PDP_TITLE_WAIT_MS,
                        )
                    except Exception:
                        pass

                    title = await _extract_pdp_title_async(page) or (await page.title() or "").strip()
                    merchant_blob = await _pdp_merchant_blob_async(page)
                    price = await _extract_pdp_price_async(page)
                    shipping = await _extract_pdp_shipping_async(page)
                    image_url = await _extract_pdp_image_async(page)
                    row = _pdp_row(
                        asin,
                        title=title,
                        price=price,
                        shipping_text=shipping,
                        image_url=image_url,
                        merchant_blob=merchant_blob,
                        allowed=allowed,
                    )
                    await asyncio.sleep(random.uniform(scroll_delay_range[0], scroll_delay_range[1]))
                    return idx, row
                except CaptchaBlocked:
                    raise
                except NetworkAccessDenied:
                    raise
                except Exception as exc:
                    LOGGER.warning("PDP row parse failed asin=%s: %s (skipping update)", asin, exc)
                    await asyncio.sleep(random.uniform(scroll_delay_range[0], scroll_delay_range[1]))
                    return idx, _pdp_skip_row(asin, "parse_failed")
                finally:
                    await page.close()

        tasks = [worker(idx, asin) for idx, asin in enumerate(normalized)]
        gathered: list[Any] = []
        try:
            gathered = await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            await context.close()
            await browser.close()

    for item in gathered:
        if isinstance(item, CaptchaBlocked):
            raise item
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
    return [row for _, row in pairs]


async def scrape_pdp_watch_async(
    asins: list[str],
    allowed_seller_substrings: list[str],
    *,
    max_cycle_seconds: int = 170,
    scroll_delay_range: tuple[float, float] = (0.25, 0.65),
    max_concurrent_tabs: int = 2,
    tab_jitter_seconds: tuple[float, float] | list[float] | None = None,
) -> list[dict[str, Any]]:
    """Async version of scrape_pdp_watch for callers that already run an event loop."""
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

    jitter = _coerce_tab_jitter_pair(tab_jitter_seconds, (0.15, 0.55))
    conc = _clamp_pdp_concurrency(max_concurrent_tabs)

    return await _run_pdp_watch_async(
        normalized,
        allowed,
        max_cycle_seconds=float(max_cycle_seconds),
        scroll_delay_range=scroll_delay_range,
        max_concurrent=conc,
        jitter_range=jitter,
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
) -> list[dict[str, Any]]:
    """Visit each watch ASIN on amazon.com PDP; return exactly one dict per unique valid ASIN (order preserved).

    Uses concurrent Playwright tabs (async API), capped at 3, sharing the global token bucket with SERP.

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
        )
    )
