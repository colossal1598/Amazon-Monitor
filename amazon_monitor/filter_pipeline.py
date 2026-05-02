import re
import unicodedata
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qs, unquote, urlparse


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


def _seller_display_name(merchant_id: str) -> str:
    """Human-readable seller for DB / alerts (not a fake 'main_search' bucket)."""
    mid = (merchant_id or "").strip().upper()
    if mid == "A2XZ7JICGUQ1CX":
        return "Amazon Export Sales LLC"
    if mid == "ATVPDKIKX0DER":
        return "Amazon.com"
    return merchant_id or "Unknown"


def _pick_primary_merchant_id(hit: set[str]) -> str:
    """When multiple allowlist tokens match, prefer seller facet over marketplace id."""
    if "A2XZ7JICGUQ1CX" in hit:
        return "A2XZ7JICGUQ1CX"
    if "ATVPDKIKX0DER" in hit:
        return "ATVPDKIKX0DER"
    return sorted(hit)[0]


def _state_engine_row(
    item: dict[str, Any],
    merchant_id: str = "",
    *,
    display_override: str | None = None,
) -> dict[str, Any]:
    display = display_override if display_override else _seller_display_name(merchant_id)
    return {
        "asin": (item.get("asin") or "").upper(),
        "title": item.get("title") or "",
        "price": item.get("price"),
        "in_stock": bool(item.get("in_stock")),
        "seller": display,
        "seller_name": display,
        "image_url": item.get("image_url"),
        "shipping_text": item.get("shipping_text"),
        "seller_text": item.get("seller_text"),
    }


def _allowlist_id_for_classified_dom_name(dom_name: str | None) -> str | None:
    """Map visible 'Sold by …' label to the merchant id slot in allowed_merchant_ids."""
    if not dom_name:
        return None
    n = _normalize_ascii(dom_name)
    if "export sales" in n or "amazon export llc" in n:
        return "A2XZ7JICGUQ1CX"
    if re.search(r"\bamazon\.com\b", n) and "services" not in n and "warehouse" not in n:
        return "ATVPDKIKX0DER"
    return None


def _extract_merchant_ids_from_search_url(search_url: str) -> set[str]:
    parsed = urlparse(search_url)
    qs = parse_qs(parsed.query, keep_blank_values=True)
    found: set[str] = set()
    for rh in qs.get("rh", []):
        decoded = unquote(rh)
        for m in re.finditer(r"p_6:([A-Z0-9]{13,14})", decoded, re.IGNORECASE):
            found.add(m.group(1).upper())
    return found


def _merchant_tokens_for_item(item: dict[str, Any]) -> set[str]:
    """Card-scraped ids + URL rh p_6 (facet), not page-global marketplace noise."""
    tokens: set[str] = set()
    raw = item.get("merchant_id_tokens")
    if isinstance(raw, list):
        tokens |= {str(x).upper() for x in raw if x}
    elif isinstance(raw, str) and raw:
        tokens.add(raw.upper())
    for x in item.get("seller_anchor_merchant_ids") or []:
        if x:
            tokens.add(str(x).upper())
    url = (item.get("search_url") or "").strip()
    if url:
        tokens |= _extract_merchant_ids_from_search_url(url)
    return tokens


def filter_by_allowed_merchant_ids(
    items: list[dict[str, Any]],
    allowed_merchant_ids: list[str],
) -> list[dict[str, Any]]:
    """Keep rows when (1) merchant id from card anchors/HTML/URL rh matches allowlist, or
    (2) visible seller text from DOM selectors matches allowlist via classify_seller + label map."""
    allowed = {str(x).strip().upper() for x in allowed_merchant_ids if str(x).strip()}
    if not allowed:
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        asin = (item.get("asin") or "").upper()
        if not asin or asin in seen:
            continue
        tokens = _merchant_tokens_for_item(item)
        hit = tokens & allowed
        if hit:
            seen.add(asin)
            primary_merchant = _pick_primary_merchant_id(hit)
            out.append(_state_engine_row(item, primary_merchant))
            continue
        st, dom_name = classify_seller(item.get("seller_text") or item.get("seller") or "")
        if st != "confirmed" or not dom_name:
            continue
        need_id = _allowlist_id_for_classified_dom_name(dom_name)
        if need_id and need_id in allowed:
            seen.add(asin)
            out.append(_state_engine_row(item, "", display_override=dom_name))
    return out


def keep_asins_not_in_db(rows: list[dict[str, Any]], known_asins: set[str]) -> list[dict[str, Any]]:
    return [r for r in rows if (r.get("asin") or "").upper() not in known_asins]


def filter_marketplace_items(raw_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deprecated path: stage1 + legacy text seller. Prefer filter_by_allowed_merchant_ids."""
    filtered: list[dict[str, Any]] = []
    for item in filter_stage1_candidates(raw_items):
        status, seller_name = classify_seller(item.get("seller_text") or item.get("seller") or "")
        if status != "confirmed" or not seller_name:
            continue
        filtered.append(_state_engine_row(item, seller_name))
    return filtered


def run_search_filter_pipeline(
    raw_items: list[dict[str, Any]],
    config: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Stage1 + blacklist + optional keywords + merchant allowlist -> state-engine rows."""
    meta: dict[str, Any] = {}
    bl_file = str(config.get("blacklist_file", "blacklist.txt"))
    stage1 = filter_stage1_candidates(raw_items)
    stage1 = filter_by_blacklist_only(stage1, bl_file)
    req_kw = config.get("required_keywords") or []
    req_any = config.get("required_any_keywords")
    if req_kw or req_any:
        stage1 = filter_search_results(stage1, req_kw, bl_file, req_any)
    meta["stage1_count"] = len(stage1)
    allowed = config.get("allowed_merchant_ids") or []
    filtered = filter_by_allowed_merchant_ids(stage1, allowed)
    meta["filtered_count"] = len(filtered)
    return filtered, meta

