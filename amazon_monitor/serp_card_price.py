"""Pull the list price from Amazon search card text.

Your remote scrape shows US dollars on the card. We only read plain money
amounts (optional $) and take the largest value that is at least $5 so a
3.x star rating is not mistaken for the price.
"""

from __future__ import annotations

import html
import re

_MONEY = re.compile(r"\$?\s*([0-9]+(?:[.,][0-9]{1,2})?)")


def _money_amounts(text: str) -> list[float]:
    if not text:
        return []
    t = html.unescape(text).replace("\xa0", " ").strip().replace(",", "")
    out: list[float] = []
    for g in _MONEY.findall(t):
        try:
            out.append(float(g))
        except ValueError:
            continue
    return out


def card_list_price(text: str, *, min_price: float = 5.0) -> float | None:
    """Main price on the card: max amount ≥ min_price (stars are usually < 5)."""
    ok = [v for v in _money_amounts(text) if v >= min_price]
    return max(ok) if ok else None
