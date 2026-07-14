"""Helpers to validate PDP selectors against saved HTML without Playwright."""

from __future__ import annotations

import re

from pdp_scraper import (
    _DELIVERY_RELEVANT_RE,
    _PDP_PRICE_PAY_SELECTORS,
    _parse_hidden_buybox_amount,
    _parse_price_text,
    merchant_matches_allowed,
)


def _first_group(pattern: str, html: str, *, flags: int = 0) -> str | None:
    m = re.search(pattern, html, flags)
    return m.group(1) if m else None


def _chunk_after_id(html: str, element_id: str, limit: int = 30_000) -> str:
    marker = f'id="{element_id}"'
    idx = html.find(marker)
    if idx < 0:
        return ""
    return html[idx : idx + limit]


def _offscreen_in_chunk(chunk: str, class_fragment: str) -> str:
    pat = (
        rf'class="[^"]*{re.escape(class_fragment)}[^"]*"[\s\S]{{0,800}}?'
        r'<span class="a-offscreen">([^<]*)</span>'
    )
    raw = _first_group(pat, chunk, flags=re.IGNORECASE) or ""
    return raw.strip()


def _accessibility_label_price(html: str) -> str:
    raw = _first_group(
        r'id="apex-pricetopay-accessibility-label"[^>]*>([^<]+)<',
        html,
    )
    return (raw or "").strip()


def _a_price_offscreen_excluding_text(chunk: str) -> str:
    """First `.a-price` offscreen amount whose class is not `.a-text-price` (list price)."""
    for m in re.finditer(r'class="(a-price[^"]*)"', chunk):
        cls = m.group(1)
        if "a-text-price" in cls:
            continue
        window = chunk[m.end() : m.end() + 400]
        raw = _first_group(r'<span class="a-offscreen">([^<]*)</span>', window)
        if raw:
            return raw.strip()
    return ""


def _price_element_text(chunk: str) -> str:
    """Bare price element text (e.g. #price_inside_buybox / #newBuyBoxPrice)."""
    return " ".join(re.sub(r"<[^>]+>", " ", chunk).split())


def _pay_price_from_whole_fraction(chunk: str) -> float | None:
    pay = re.search(
        r'class="[^"]*apex-pricetopay-value[^"]*"[\s\S]{0,1200}',
        chunk,
        flags=re.IGNORECASE,
    )
    if not pay:
        return None
    block = pay.group(0)
    whole = _first_group(r'class="a-price-whole"[^>]*>([\d,]+)', block) or ""
    frac = _first_group(r'class="a-price-fraction"[^>]*>(\d+)', block) or ""
    w = whole.replace(",", "").replace(".", "")
    if w.isdigit():
        cents = frac if frac.isdigit() else "00"
        return float(f"{w}.{cents}")
    return None


def extract_pay_price_from_html(html: str) -> float | None:
    """Mirror _extract_pdp_price_async selector order on static HTML."""
    selector_patterns = {
        "#qualifiedBuybox .apex-pricetopay-value .a-offscreen": (
            _chunk_after_id(html, "qualifiedBuybox"),
            "apex-pricetopay-value",
        ),
        "#corePrice_feature_div .apex-pricetopay-value .a-offscreen": (
            _chunk_after_id(html, "corePrice_feature_div"),
            "apex-pricetopay-value",
        ),
        "#corePriceDisplay_desktop_feature_div #apex-pricetopay-accessibility-label": (
            _chunk_after_id(html, "corePriceDisplay_desktop_feature_div"),
            "__accessibility__",
        ),
        "#tp_price_block_total_price_ww .a-offscreen": (
            _chunk_after_id(html, "tp_price_block_total_price_ww", limit=2_000),
            "__offscreen_only__",
        ),
        "#qualifiedBuybox .a-price:not(.a-text-price) .a-offscreen": (
            _chunk_after_id(html, "qualifiedBuybox"),
            "__a_price_no_text__",
        ),
        "#corePrice_feature_div .a-price:not(.a-text-price) .a-offscreen": (
            _chunk_after_id(html, "corePrice_feature_div"),
            "__a_price_no_text__",
        ),
        "#corePriceDisplay_desktop_feature_div .a-price:not(.a-text-price) .a-offscreen": (
            _chunk_after_id(html, "corePriceDisplay_desktop_feature_div"),
            "__a_price_no_text__",
        ),
        "#buybox .a-price:not(.a-text-price) .a-offscreen": (
            _chunk_after_id(html, "buybox"),
            "__a_price_no_text__",
        ),
        "#price_inside_buybox": (
            _chunk_after_id(html, "price_inside_buybox", limit=300),
            "__element_text__",
        ),
        "#newBuyBoxPrice": (
            _chunk_after_id(html, "newBuyBoxPrice", limit=300),
            "__element_text__",
        ),
    }
    for sel in _PDP_PRICE_PAY_SELECTORS:
        chunk, kind = selector_patterns.get(sel, ("", ""))
        if not chunk:
            continue
        if kind == "__accessibility__":
            raw = _accessibility_label_price(chunk)
        elif kind == "__offscreen_only__":
            raw = _offscreen_in_chunk(chunk, "a-price")
        elif kind == "__a_price_no_text__":
            raw = _a_price_offscreen_excluding_text(chunk)
        elif kind == "__element_text__":
            raw = _price_element_text(chunk)
        else:
            raw = _offscreen_in_chunk(chunk, kind)
        price = _parse_price_text(raw)
        if price is not None:
            return price
    for root_id in ("qualifiedBuybox", "corePriceDisplay_desktop_feature_div"):
        chunk = _chunk_after_id(html, root_id)
        price = _pay_price_from_whole_fraction(chunk)
        if price is not None:
            return price
    buybox = _chunk_after_id(html, "qualifiedBuybox")
    if buybox:
        raw = _first_group(
            r'name="items\[0\.base\]\[customerVisiblePrice\]\[amount\]"[^>]*value="([^"]+)"',
            buybox,
        )
        price = _parse_hidden_buybox_amount(raw)
        if price is not None:
            return price
    return None


# --- Skeleton (degraded-page) detection: static-HTML mirror of pdp_scraper's async detector. ---

_SKELETON_ROW_FALSE_RE = re.compile(r'data-csa-c-is-in-initial-active-row="false"')
_SKELETON_ROW_TRUE_RE = re.compile(r'data-csa-c-is-in-initial-active-row="true"')
# `.a-price ... .a-offscreen`: an offscreen amount inside an a-price wrapper.
_A_PRICE_OFFSCREEN_RE = re.compile(
    r'class="a-price[^"]*"[\s\S]{0,800}?class="a-offscreen"', re.IGNORECASE
)


def is_offers_skeleton_from_html(html: str) -> bool:
    """Static mirror of pdp_scraper._page_offers_skeleton_async (regex, no Playwright).

    Marker (a): an initial-active-row="false" element exists and no ="true" element does.
    Marker (b): #corePrice_feature_div exists with empty inner text AND no `.a-price
    .a-offscreen` node anywhere on the page.
    """
    if _SKELETON_ROW_FALSE_RE.search(html) and not _SKELETON_ROW_TRUE_RE.search(html):
        return True
    if 'id="corePrice_feature_div"' in html and not _A_PRICE_OFFSCREEN_RE.search(html):
        chunk = _chunk_after_id(html, "corePrice_feature_div", limit=4_000)
        inner = chunk[chunk.find(">") + 1 :] if ">" in chunk else chunk
        inner_text = " ".join(re.sub(r"<[^>]+>", " ", inner).split())
        # Marker (b) requires the core price block to be empty of any price text; a
        # skeleton renders zero dollar strings anywhere on the page.
        if not re.search(r"[$\d]", inner_text):
            return True
    return False


def extract_list_price_from_html(html: str) -> float | None:
    chunk = _chunk_after_id(html, "corePriceDisplay_desktop_feature_div")
    raw = _offscreen_in_chunk(chunk, "apex-basisprice-value")
    return _parse_price_text(raw)


def extract_delivery_text_from_html(html: str) -> str:
    lines: list[str] = []

    def add_line(value: str | None) -> None:
        if not value:
            return
        for part in re.split(r"[\r\n]+", value):
            line = " ".join(part.split())
            if line and line not in lines:
                lines.append(line)

    buybox = _chunk_after_id(html, "qualifiedBuybox")
    for m in re.finditer(
        r'<span class="a-color-secondary">([^<]{5,300})</span>',
        buybox,
        flags=re.IGNORECASE,
    ):
        text = m.group(1).strip()
        if _DELIVERY_RELEVANT_RE.search(text):
            add_line(text)

    for slot_id in (
        "mir-layout-DELIVERY_BLOCK-slot-PRIMARY_DELIVERY_MESSAGE_LARGE",
        "mir-layout-DELIVERY_BLOCK-slot-NO_PROMISE_UPSELL_MESSAGE",
    ):
        chunk = _chunk_after_id(html, slot_id, limit=1_500)
        if not chunk:
            continue
        text = re.sub(r"<[^>]+>", " ", chunk)
        text = " ".join(text.split())
        if text:
            add_line(text)

    block = _chunk_after_id(html, "deliveryBlockMessage", limit=5_000)
    if block:
        text = re.sub(r"<[^>]+>", " ", block)
        add_line(" ".join(text.split()))

    for m in re.finditer(
        r'data-csa-c-delivery-price="([^"]*)"[^>]*data-csa-c-delivery-time="([^"]*)"[^>]*>([\s\S]{0,400}?)</span>',
        html,
        flags=re.IGNORECASE,
    ):
        add_line(" ".join(x for x in (m.group(1), re.sub(r"<[^>]+>", " ", m.group(3))) if x.strip()))

    return "\n".join(lines)


# --- Accordion (per-offer) scoping: static-HTML mirror of pdp_scraper's row-scoped logic. ---

# Sentinel mirroring pdp_scraper._ACCORDION_NO_ALLOWED_OFFER: accordion present but no
# row is an allowed seller (distinct from None = no accordion at all).
ACCORDION_NO_ALLOWED_OFFER = object()


def _split_accordion_rows(html: str) -> list[str]:
    """Split page HTML into one chunk per newAccordionRow_ (opening <div> onward)."""
    starts: list[int] = []
    for m in re.finditer(r'id="newAccordionRow_\d+"', html):
        tag_start = html.rfind("<div", 0, m.start())
        if tag_start != -1:
            starts.append(tag_start)
    starts = sorted(set(starts))
    if len(starts) < 2:
        return []
    bounds = starts + [len(html)]
    return [html[bounds[i] : bounds[i + 1]] for i in range(len(starts))]


def _row_feature_message(row_html: str, feature_name: str) -> str:
    """Read one offer-display-feature message (merchant/fulfiller seller name) from a row."""
    m = re.search(
        rf'offer-display-feature-name="{feature_name}"[\s\S]*?'
        r'class="[^"]*offer-display-feature-text-message[^"]*"[^>]*>([^<]+)<',
        row_html,
        flags=re.IGNORECASE,
    )
    return (m.group(1).strip() if m else "")


def _row_merchant_name(row_html: str) -> str:
    return _row_feature_message(row_html, "desktop-merchant-info")


def _row_match_blob(row_html: str) -> str:
    """Merchant + fulfiller text for seller matching; full row text as last resort."""
    parts = [
        v
        for v in (
            _row_feature_message(row_html, "desktop-merchant-info"),
            _row_feature_message(row_html, "desktop-fulfiller-info"),
        )
        if v
    ]
    if parts:
        return "\n".join(parts)
    return " ".join(re.sub(r"<[^>]+>", " ", row_html).split())


def extract_row_pay_price_from_html(row_html: str) -> float | None:
    """Row-scoped pay price: mirror _extract_row_price_async selector order on static HTML."""
    for extractor in (
        lambda c: _offscreen_in_chunk(c, "apex-pricetopay-value"),
        _a_price_offscreen_excluding_text,
    ):
        price = _parse_price_text(extractor(row_html))
        if price is not None:
            return price
    return _pay_price_from_whole_fraction(row_html)


def extract_row_delivery_from_html(row_html: str) -> str:
    lines: list[str] = []

    def add_line(value: str | None) -> None:
        if not value:
            return
        for part in re.split(r"[\r\n]+", value):
            line = " ".join(part.split())
            if line and line not in lines:
                lines.append(line)

    for m in re.finditer(
        r'<span class="a-color-secondary">([^<]{3,300})</span>',
        row_html,
        flags=re.IGNORECASE,
    ):
        text = m.group(1).strip()
        if _DELIVERY_RELEVANT_RE.search(text):
            add_line(text)
    return "\n".join(lines)


def _row_purchasable_from_html(row_html: str) -> bool:
    for m in re.finditer(r'id="(?:buy-now-button|add-to-cart-button)"([^>]*)>', row_html):
        attrs = m.group(1)
        if "disabled" in attrs or 'aria-disabled="true"' in attrs:
            continue
        return True
    return False


def _active_accordion_row_html(rows: list[str]) -> str | None:
    for row in rows:
        head = row[: row.find(">") + 1] if ">" in row else row[:300]
        if "a-accordion-active" in head:
            return row
    return rows[0] if rows else None


def find_accordion_offer_from_html(html: str, allowed: list[str]):
    """Mirror _find_allowed_accordion_offer_async on static HTML.

    Returns (None, []) | (row_html, merchants) | (ACCORDION_NO_ALLOWED_OFFER, merchants).
    """
    rows = _split_accordion_rows(html)
    if len(rows) < 2:
        return None, []
    merchants: list[str] = []
    matched: str | None = None
    for row in rows:
        merchants.append(_row_merchant_name(row) or "unknown")
        if matched is None and merchant_matches_allowed(_row_match_blob(row), allowed):
            matched = row
    if matched is not None:
        return matched, merchants
    return ACCORDION_NO_ALLOWED_OFFER, merchants


def build_accordion_pdp_state_from_html(html: str, allowed: list[str]) -> dict | None:
    """Mirror the accordion branch of _extract_pdp_page_state_async on static HTML.

    Returns None when there is no accordion (fewer than 2 rows), else a state dict with
    price/merchant_blob/shipping/buybox_purchasable/explicit_oos ready for _pdp_row.
    """
    offer, merchants = find_accordion_offer_from_html(html, allowed)
    if offer is None:
        return None

    if offer is ACCORDION_NO_ALLOWED_OFFER:
        merchant_blob = "accordion offers: " + " | ".join(m for m in merchants if m)
        active = _active_accordion_row_html(_split_accordion_rows(html))
        price = extract_row_pay_price_from_html(active) if active else None
        shipping = extract_row_delivery_from_html(active) if active else ""
        return {
            "price": price,
            "merchant_blob": merchant_blob,
            "shipping": shipping,
            "buybox_purchasable": False,
            "explicit_oos": False,
        }

    price = extract_row_pay_price_from_html(offer)
    purchasable = _row_purchasable_from_html(offer)
    return {
        "price": price,
        "merchant_blob": _row_match_blob(offer),
        "shipping": extract_row_delivery_from_html(offer),
        "buybox_purchasable": purchasable,
        "explicit_oos": price is None and not purchasable,
    }
