"""Small helpers shared by the PDP-only monitor."""

from __future__ import annotations

import re
import unicodedata
from typing import Any

_ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")
_CURRENCY_RE = re.compile(
    r"(\$\s*[0-9][0-9,]*(?:\.[0-9]{2})?|[0-9]+(?:[.,][0-9]{1,2})?\s*₪|₪\s*[0-9]+(?:[.,][0-9]{1,2})?|ILS\s*[:\s]*[0-9]+(?:[.,][0-9]{1,2})?)",
    re.IGNORECASE,
)
# Keyword(s) that mark the amount actually shown as the delivery charge.
# Checked in priority order: "delivery" is Amazon's "$X delivery <date>" pattern for the
# real charge, while "shipping"/"import charges" often label a details/tooltip link whose
# adjacent amount is a different (e.g. import-fee) figure.
_DELIVERY_PRICE_KEYWORDS: tuple[re.Pattern[str], ...] = (
    re.compile(r"delivery", re.IGNORECASE),
    re.compile(r"shipping|import charges", re.IGNORECASE),
)


def valid_asin(value: str) -> bool:
    return bool(_ASIN_RE.match((value or "").strip().upper()))


def normalize_ascii(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", (value or "").lower().strip())
    return decomposed.encode("ascii", "ignore").decode("ascii")


def normalize_title_line(value: Any) -> str | None:
    """Collapse line breaks and repeated whitespace to a single alert-friendly line."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    s = re.sub(r"[\r\n\v\f\u0085\u2028\u2029]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s or None


def _line_is_not_shippable(line: str) -> bool:
    return is_not_shippable_text(line)


def is_not_shippable_text(text: str | None) -> bool:
    """True when Amazon explicitly says delivery to your configured location is not possible."""
    clean = normalize_ascii(text or "")
    patterns = (
        "cannot be shipped to your selected delivery location",
        "can't be shipped to your selected delivery location",
        "cannot be delivered to your selected delivery location",
        "can't be delivered to your selected delivery location",
        "choose a different delivery location",
    )
    return any(p in clean for p in patterns)


def _line_looks_free(line: str) -> bool:
    clean = normalize_ascii(line)
    return "free delivery" in clean or "free shipping" in clean or "חינם" in line


def _delivery_candidate_lines(shipping_text: str | None) -> list[str]:
    raw = (shipping_text or "").strip()
    if not raw:
        return []
    lines: list[str] = []
    for part in re.split(r"[\r\n]+", raw):
        line = " ".join(str(part).split())
        if line:
            lines.append(line)
    if not lines:
        return [" ".join(raw.split())]
    return lines


def _span_gap(a: tuple[int, int], b: tuple[int, int]) -> int:
    """Character distance between two (start, end) spans; 0 if they touch/overlap."""
    if a[1] <= b[0]:
        return b[0] - a[1]
    if b[1] <= a[0]:
        return a[0] - b[1]
    return 0


def _pick_delivery_price_match(line: str, matches: list[re.Match[str]]) -> re.Match[str]:
    """Pick the currency match closest to a delivery/shipping keyword, not just the first in the line.

    Delivery lines can carry several money-like tokens, e.g. an item subtotal, a
    duplicated/OCR-broken echo of it, and the actual delivery charge (real example:
    "$109.98 $109 . 98 $47.95 Shipping & Import Charges to Israel Details $17.96 delivery
    We[dnesday]..." - here the delivery charge is the $17.96 right next to "delivery").
    """
    for keyword_re in _DELIVERY_PRICE_KEYWORDS:
        keyword_spans = [m.span() for m in keyword_re.finditer(line)]
        if not keyword_spans:
            continue
        return min(
            matches,
            key=lambda m: min(_span_gap(m.span(), kw_span) for kw_span in keyword_spans),
        )
    return matches[0]


def _paid_delivery_price_tail(line: str) -> str:
    """Return a compact paid-delivery price when the delivery line exposes one.

    Returns "" when the line carries no currency amount at all (e.g. SERP cards
    that only show a date estimate like "Delivery Thu, Jul 30") — echoing the
    raw line produced nonsense WhatsApp output like "משלוח: Delivery Thu, Jul 30".
    """
    matches = list(_CURRENCY_RE.finditer(line))
    if not matches:
        return ""
    match = _pick_delivery_price_match(line, matches)
    token = re.sub(r"\s+", "", match.group(1))
    return re.sub(r"(?i)^ils:?", "", token).strip(":")


def is_amazon_export_seller(seller_text: str | None, source: str | None = None) -> bool:
    """True when the offer is sold by Amazon Export Sales LLC.

    Business rule (confirmed by operator): Amazon Export orders always ship free.
    When the page shows a delivery charge or date for an Export-sold item, that's
    an Amazon display bug and must not leak into WhatsApp alerts. The AES SERP
    source is the Export storefront by definition.
    """
    if source and "aes" in str(source).lower():
        return True
    return "amazon export" in normalize_ascii(seller_text or "")


def shipping_display_for(
    shipping_text: str | None, *, seller_text: str | None = None, source: str | None = None
) -> str:
    """Alert shipping line with the Amazon-Export-is-always-free override applied."""
    if is_amazon_export_seller(seller_text, source):
        return "משלוח חינם"
    return shipping_display_hebrew(shipping_text)


def shipping_display_hebrew(shipping_text: str | None) -> str:
    """WhatsApp delivery line: free shipping or the paid delivery line/price."""
    lines = _delivery_candidate_lines(shipping_text)
    if not lines:
        return ""
    for line in lines:
        if _line_is_not_shippable(line):
            return ""
    for line in lines:
        if _line_looks_free(line):
            return "משלוח חינם"
    delivery_lines = [
        line
        for line in lines
        if re.search(r"delivery|shipping|ship|arrives|import charges|₪|\$|ils", line, re.IGNORECASE)
    ]
    # Prefer a delivery line that actually carries an amount; a date-only line
    # ("Delivery Thu, Jul 30") says nothing about the shipping cost. Among priced
    # lines, one naming "delivery" (Amazon's "$X delivery <date>" charge) beats a
    # line that only mentions shipping/import charges (usually a tooltip figure).
    priced_lines = [line for line in delivery_lines if _CURRENCY_RE.search(line)]
    delivery_priced = [line for line in priced_lines if re.search(r"delivery", line, re.IGNORECASE)]
    picked = (delivery_priced or priced_lines or delivery_lines or lines)[0]
    tail = _paid_delivery_price_tail(picked)
    return f"משלוח: {tail}" if tail else ""
