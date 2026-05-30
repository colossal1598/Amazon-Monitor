"""Helpers to validate PDP selectors against saved HTML without Playwright."""

from __future__ import annotations

import re

from pdp_scraper import _DELIVERY_RELEVANT_RE, _PDP_PRICE_PAY_SELECTORS, _parse_price_text


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
    }
    for sel in _PDP_PRICE_PAY_SELECTORS:
        chunk, kind = selector_patterns[sel]
        if not chunk:
            continue
        if kind == "__accessibility__":
            raw = _accessibility_label_price(chunk)
        elif kind == "__offscreen_only__":
            raw = _offscreen_in_chunk(chunk, "a-price")
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
    return None


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
