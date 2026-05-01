import re
import unicodedata
from pathlib import Path
from typing import Any, Literal


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
    """Allow only retail Amazon.com or Amazon Export LLC (exclude Services/Warehouse/etc.)."""
    clean = _normalize_ascii(seller_text)
    if not clean:
        return None
    # PDP often exposes only the merchant link text (no "Sold by" prefix).
    if "amazon export sales llc" in clean:
        return "Amazon Export Sales LLC"
    if "amazon export llc" in clean:
        return "Amazon Export LLC"
    if "sold by" not in clean:
        return None
    # Substring "amazon.com" also appears in "Amazon.com Services LLC" — reject those.
    if re.search(r"amazon\s+warehouse", clean) or re.search(r"amazon\.com\s+services", clean):
        return None
    if "services llc" in clean and "amazon.com" in clean:
        return None
    if re.search(r"\bamazon\.com\b", clean):
        return "Amazon.com"
    return None


_FREE_SHIP_TO_ISRAEL_RE = re.compile(
    r"free\s+(shipping|delivery).*?to\s+israel",
    re.IGNORECASE,
)


def _has_free_shipping_to_israel(shipping_text: str) -> bool:
    clean = _normalize_ascii(shipping_text)
    if "free shipping to israel" in clean or "free delivery to israel" in clean:
        return True
    return bool(_FREE_SHIP_TO_ISRAEL_RE.search(clean))


def classify_seller(seller_blob: str) -> tuple[Literal["confirmed", "rejected", "unknown"], str | None]:
    """Classify seller text: allowed Amazon sellers, explicit third-party, or unknown."""
    clean = _normalize_ascii(seller_blob)
    allowed = _extract_allowed_seller(seller_blob)
    if allowed:
        return "confirmed", allowed
    if "sold by" in clean:
        return "rejected", None
    return "unknown", None


def filter_by_blacklist_only(
    products: list[dict[str, Any]],
    blacklist_file: str,
) -> list[dict[str, Any]]:
    """Drop ASIN/title matches from blacklist.txt (same ASIN + title patterns as filter_search_results)."""
    blocked_asins, blocked_patterns = _read_blacklist(blacklist_file)
    out: list[dict[str, Any]] = []
    for product in products:
        asin = (product.get("asin") or "").upper()
        title = _normalize_text(product.get("title") or "")
        if not asin:
            continue
        if asin in blocked_asins:
            continue
        if any(pattern.search(title) for pattern in blocked_patterns):
            continue
        out.append(product)
    return out


def filter_stage1_candidates(raw_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fast pass: Pokemon TCG title + valid price + free shipping/delivery to Israel (regex)."""
    out: list[dict[str, Any]] = []
    for item in raw_items:
        title = item.get("title") or ""
        if not _has_pokemon_tcg_title(title):
            continue
        if not _is_valid_price(item):
            continue
        if not _has_free_shipping_to_israel(item.get("shipping_text") or ""):
            continue
        out.append(dict(item))
    return out


def _state_engine_row(item: dict[str, Any], seller_name: str) -> dict[str, Any]:
    return {
        "asin": (item.get("asin") or "").upper(),
        "title": item.get("title") or "",
        "price": item.get("price"),
        "in_stock": bool(item.get("in_stock")),
        "seller": "main_search",
        "seller_name": seller_name,
        "image_url": item.get("image_url"),
        "shipping_text": item.get("shipping_text"),
        "seller_text": item.get("seller_text"),
    }


def state_engine_row_from_queue_record(asin: str, rec: dict[str, Any], seller_name: str) -> dict[str, Any]:
    """Build a state-engine row from a pending-queue snapshot (PDP confirmed seller)."""
    item: dict[str, Any] = {
        "asin": asin,
        "title": rec.get("title") or "",
        "price": rec.get("price"),
        "in_stock": True,
        "image_url": rec.get("image_url"),
        "shipping_text": rec.get("shipping_text") or "",
        "seller_text": rec.get("seller_text") or "",
    }
    return _state_engine_row(item, seller_name)


def build_confirmed_candidates(
    stage1_rows: list[dict[str, Any]],
    pdp_seller_text_by_asin: dict[str, str],
) -> list[dict[str, Any]]:
    """Merge card seller_text with optional PDP merchant blob; emit only confirmed allowlist sellers."""
    by_asin: dict[str, dict[str, Any]] = {}
    for row in stage1_rows:
        asin = (row.get("asin") or "").upper()
        if asin:
            by_asin[asin] = row

    confirmed: list[dict[str, Any]] = []
    seen: set[str] = set()
    for asin, row in by_asin.items():
        card_blob = row.get("seller_text") or row.get("seller") or ""
        pdp_blob = pdp_seller_text_by_asin.get(asin, "")
        merged = f"{card_blob}\n{pdp_blob}".strip()
        status, seller_name = classify_seller(merged)
        if status != "confirmed" or not seller_name:
            continue
        if asin in seen:
            continue
        seen.add(asin)
        confirmed.append(_state_engine_row(row, seller_name))
    return confirmed


def filter_marketplace_items(raw_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Single-pass filter: same as stage1 + in-card seller only (no PDP). For tests/back-compat."""
    filtered: list[dict[str, Any]] = []
    for item in filter_stage1_candidates(raw_items):
        status, seller_name = classify_seller(item.get("seller_text") or item.get("seller") or "")
        if status != "confirmed" or not seller_name:
            continue
        filtered.append(_state_engine_row(item, seller_name))
    return filtered

