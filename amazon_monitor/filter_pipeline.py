import re
import unicodedata
from pathlib import Path
from typing import Any


def _normalize_text(value: str) -> str:
    """Lowercase and strip diacritics for accent-insensitive matching."""
    lowered = (value or "").lower().strip()
    decomposed = unicodedata.normalize("NFKD", lowered)
    normalized = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    # Treat full phrase and acronym as equivalent for required keyword matching.
    normalized = normalized.replace("trading card game", "tcg")
    return normalized


def _read_blacklist(blacklist_file: str) -> tuple[set[str], list[re.Pattern[str]]]:
    asins: set[str] = set()
    patterns: list[re.Pattern[str]] = []
    path = Path(blacklist_file)
    if not path.exists():
        return asins, patterns
    for line in path.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue
        if re.fullmatch(r"[A-Z0-9]{10}", raw.upper()):
            asins.add(raw.upper())
        else:
            patterns.append(re.compile(rf"\b{re.escape(raw.lower())}\b"))
    return asins, patterns


def filter_search_results(
    products: list[dict[str, Any]],
    required_keywords: list[str],
    blacklist_file: str,
    required_any_keywords: list[str] | None = None,
) -> list[dict[str, Any]]:
    req = [_normalize_text(k) for k in required_keywords if k.strip()]
    req_any = [_normalize_text(k) for k in (required_any_keywords or []) if k.strip()]
    blocked_asins, blocked_patterns = _read_blacklist(blacklist_file)
    filtered: list[dict[str, Any]] = []
    for product in products:
        asin = (product.get("asin") or "").upper()
        title = _normalize_text(product.get("title") or "")
        if not asin or not title:
            continue
        if asin in blocked_asins:
            continue
        if any(not keyword or keyword not in title for keyword in req):
            continue
        if req_any and not any(keyword in title for keyword in req_any):
            continue
        if any(pattern.search(title) for pattern in blocked_patterns):
            continue
        filtered.append(product)
    return filtered


def _normalize_ascii(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", (value or "").lower().strip())
    return decomposed.encode("ascii", "ignore").decode("ascii")


def _has_pokemon_tcg_title(title: str) -> bool:
    text = _normalize_ascii(title)
    return "pokemon tcg" in text or "pokemon trading card game" in text


def _is_valid_price(item: dict[str, Any]) -> bool:
    price_value = item.get("price")
    price_text = _normalize_ascii(str(item.get("price_text") or ""))
    if price_value is None:
        return False
    try:
        if float(price_value) <= 0:
            return False
    except (TypeError, ValueError):
        return False
    if "see price" in price_text:
        return False
    return True


def _extract_allowed_seller(seller_text: str) -> str | None:
    clean = _normalize_ascii(seller_text)
    if "sold by" not in clean:
        return None
    if "amazon.com" in clean:
        return "Amazon.com"
    if "amazon export llc" in clean:
        return "Amazon Export LLC"
    return None


def _has_free_shipping_to_israel(shipping_text: str) -> bool:
    clean = _normalize_ascii(shipping_text)
    return "free shipping to israel" in clean or "free delivery to israel" in clean


def filter_marketplace_items(raw_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Strictly keep Pokemon TCG listings sold by approved Amazon sellers with free shipping to Israel."""
    filtered: list[dict[str, Any]] = []
    for item in raw_items:
        title = item.get("title") or ""
        if not _has_pokemon_tcg_title(title):
            continue
        if not _is_valid_price(item):
            continue
        seller_name = _extract_allowed_seller(item.get("seller_text") or item.get("seller") or "")
        if not seller_name:
            continue
        if not _has_free_shipping_to_israel(item.get("shipping_text") or ""):
            continue
        filtered.append(
            {
                "asin": (item.get("asin") or "").upper(),
                "title": title,
                "price": item.get("price"),
                "in_stock": bool(item.get("in_stock")),
                "seller": "main_search",
                "seller_name": seller_name,
                "image_url": item.get("image_url"),
                "shipping_text": item.get("shipping_text"),
                "seller_text": item.get("seller_text"),
            }
        )
    return filtered

