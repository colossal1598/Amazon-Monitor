"""Pull the list price from Amazon search card text.

Your remote scrape shows US dollars on the card. We only read plain money
amounts (optional $) and take the largest value that is at least $5 so a
3.x star rating is not mistaken for the price.
"""

from __future__ import annotations

import html
import re

# Match money-like numbers, but ignore "8K"/"8k" style counts (e.g. "8K bought").
_MONEY = re.compile(r"\$?\s*([0-9]+(?:[.,][0-9]{1,2})?)(?!\s*[kK])")


# Pull out all the money-like numbers from a piece of card text so we can later pick the most likely “real price”.
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


# Guess the product’s list price from the card by taking the biggest money amount that looks like a real price, not a star rating.
def card_list_price(text: str, *, min_price: float = 5.0) -> float | None:
    """Main price on the card: lowest amount ≥ min_price (avoid list/was prices; stars are usually < 5)."""
    ok = [v for v in _money_amounts(text) if v >= min_price]
    return min(ok) if ok else None
