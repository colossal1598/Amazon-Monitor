"""Pull the list price from Amazon search card text.

Your remote scrape shows US dollars on the card. We only read money amounts
that carry a leading "$" (so bare numbers like a star rating's "4.7" or the
"5" in "out of 5 stars" never look like a price), and additionally require
at least $5 so tiny per-unit shipping amounts are not mistaken for the price.
"""

from __future__ import annotations

import html
import re

# Match money-like numbers with a leading "$", ignoring "8K"/"8k" style counts
# (e.g. "8K bought"). The "$" is required: without it, plain numbers such as a
# star rating ("4.7") or its scale ("out of 5") would look like prices.
_MONEY = re.compile(r"\$\s*([0-9]+(?:[.,][0-9]{1,2})?)(?!\s*[kK])")


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


# Guess the product’s list price from the card by taking the money amount most likely to be the current price, not a star rating or a struck-through "was" price.
def card_list_price(text: str, *, min_price: float = 5.0) -> float | None:
    """Main price on the card.

    Card text can show a discounted item as two amounts, e.g. "$54.99 List:
    $69.99" - the current price first, then the (larger) struck-through list
    price. Taking max() there picks the inflated list price, so instead we
    take the FIRST amount ≥ min_price in text order (Amazon renders the
    current price before the list price on the card). The min_price guard
    still skips star ratings / shipping micro-prices.
    """
    ok = [v for v in _money_amounts(text) if v >= min_price]
    return ok[0] if ok else None
