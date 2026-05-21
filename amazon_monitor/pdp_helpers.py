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


def _paid_delivery_price_tail(line: str) -> str:
    """Return a compact paid-delivery price when the delivery line exposes one."""
    match = _CURRENCY_RE.search(line)
    if match:
        token = re.sub(r"\s+", "", match.group(1))
        return re.sub(r"(?i)^ils:?", "", token).strip(":")
    return line.strip()


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
    picked = delivery_lines[0] if delivery_lines else lines[0]
    tail = _paid_delivery_price_tail(picked)
    return f"משלוח: {tail}" if tail else ""
