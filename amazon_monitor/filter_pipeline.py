import logging
import re
import unicodedata
from pathlib import Path
from typing import Any
from urllib.parse import unquote

LOGGER = logging.getLogger(__name__)


def _normalize_text(value: str) -> str:
    """Lowercase and strip diacritics for accent-insensitive matching."""
    lowered = (value or "").lower().strip()
    decomposed = unicodedata.normalize("NFKD", lowered)
    normalized = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
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


def _keyword_matches_title(title_norm: str, keyword: str, raw_title: str) -> bool:
    if not keyword:
        return True
    if keyword in title_norm:
        return True
    if keyword == "tcg":
        return _title_signals_pokemon_tcg_scope(raw_title)
    return False


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
        raw_title = product.get("title") or ""
        title = _normalize_text(raw_title)
        if not asin or not title:
            continue
        if asin in blocked_asins:
            continue
        if any(not _keyword_matches_title(title, keyword, raw_title) for keyword in req):
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


def _title_signals_pokemon_tcg_scope(title: str) -> bool:
    """Whether the visible title is the card-game product line (not every product with 'Pokemon' in the name)."""
    text = _normalize_ascii(title)
    if "pokemon tcg" in text or "pokemon trading card game" in text:
        return True
    if "pokemon" not in text:
        return False
    if "tcg" in text or "trading card" in text:
        return True



def _has_pokemon_tcg_title(title: str) -> bool:
    return _title_signals_pokemon_tcg_scope(title)


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


def _card_blob_for_delivery_line(item: dict[str, Any]) -> str:
    """Per-card fields from the SERP scrape (incl. UDM delivery-block via shipping_text)."""
    parts = [
        str(item.get("shipping_text") or ""),
        str(item.get("seller_text") or ""),
        str(item.get("availability_text") or ""),
    ]
    return "\n".join(p for p in parts if p.strip())


def _shipping_line_looks_free(line: str, clean_blob: str) -> bool:
    """English or common Hebrew free-shipping cues (no currency parsing)."""
    c = _normalize_ascii(line)
    if "free delivery" in c or "free shipping" in c:
        return True
    if "free delivery" in clean_blob or "free shipping" in clean_blob:
        return True
    if "חינם" in line:
        return True
    return False


def _has_shipping_qualifier_on_card(item: dict[str, Any]) -> bool:
    """True when the card exposes a shipping/delivery line: UDM `shipping_text`, or free phrasing in blob."""
    blob = _card_blob_for_delivery_line(item)
    clean = _normalize_ascii(blob)
    ship = str(item.get("shipping_text") or "").strip()
    if _shipping_line_looks_free(ship, clean):
        return True
    if ship:
        return True
    if "delivery" in clean or "shipping" in clean:
        return True
    return False


def _paid_delivery_price_tail(line: str) -> str:
    """Compact tail after 'משלוח:' — prefer '54₪' from ₪ / ILS; else whole line."""
    m = re.search(r"([0-9]+(?:[.,][0-9]{1,2})?)\s*₪", line)
    if m:
        return f"{m.group(1)}₪"
    m = re.search(r"₪\s*([0-9]+(?:[.,][0-9]{1,2})?)", line)
    if m:
        return f"{m.group(1)}₪"
    m = re.search(r"(?i)ils\s*[:\s]*([0-9]+(?:[.,][0-9]{1,2})?)", line)
    if m:
        return f"{m.group(1)}₪"
    return line.strip()


def shipping_display_hebrew(shipping_text: str | None) -> str:
    """WhatsApp `{shipping}`: free -> 'משלוח חינם'; paid -> 'משלוח: 54₪' (ILS/₪) or 'משלוח: …' from the scraped line."""
    raw = (shipping_text or "").strip()
    line = " ".join(raw.split())
    blob_clean = _normalize_ascii(raw)
    if _shipping_line_looks_free(line, blob_clean):
        return "משלוח חינם"
    if not line:
        return "משלוח"
    return f"משלוח: {_paid_delivery_price_tail(line)}"


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
    """Pokémon TCG title + valid price + any scraped shipping/delivery signal on the card."""
    out: list[dict[str, Any]] = []
    for item in raw_items:
        title = item.get("title") or ""
        if not _has_pokemon_tcg_title(title):
            continue
        if not _is_valid_price(item):
            continue
        if not _has_shipping_qualifier_on_card(item):
            continue
        out.append(dict(item))
    return out


def _url_mixes_free_shipping_and_seller_p6(search_url: str) -> bool:
    """Amazon often ignores or mangles seller (p_6) when combined with free-shipping refines in one rh=."""
    if not search_url:
        return False
    low = search_url.lower()
    if "p_n_is_free_shipping" not in low:
        return False
    decoded = unquote(low)
    return bool(re.search(r"\bp_6\s*:", decoded))


def _maybe_warn_incompatible_url_facets(
    raw_items: list[dict[str, Any]],
    meta: dict[str, Any],
) -> None:
    """If the search URL mixes free-shipping with p_6 seller, log once and set meta."""
    sample_url = (raw_items[0].get("search_url") or "") if raw_items else ""
    if not _url_mixes_free_shipping_and_seller_p6(sample_url):
        return
    LOGGER.warning(
        "search_url combines free-shipping refine with p_6 seller; Amazon often does not honor both on the SERP. "
        "Stage1 requires a delivery/shipping line on the card (non-empty shipping_text or delivery/shipping in blob); do not rely on p_6 in the same URL for seller truth."
    )
    meta["warn_incompatible_url_facets"] = "free_shipping_plus_p_6"


def _apply_blacklist_and_config_keywords(
    candidates: list[dict[str, Any]],
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    """After stage1: blacklist.txt then optional required_keywords / required_any_keywords."""
    bl_file = str(config.get("blacklist_file", "blacklist.txt"))
    stage = filter_by_blacklist_only(candidates, bl_file)
    req_kw = config.get("required_keywords") or []
    req_any = config.get("required_any_keywords")
    if req_kw or req_any:
        stage = filter_search_results(stage, req_kw, bl_file, req_any)
    return stage


def _rows_for_state_engine(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize to state-engine row shape (no merchant resolution)."""
    rows: list[dict[str, Any]] = []
    for item in items:
        asin = (item.get("asin") or "").strip().upper()
        if not asin:
            continue
        rows.append(
            {
                "asin": asin,
                "title": item.get("title") or "",
                "price": item.get("price"),
                "in_stock": bool(item.get("in_stock")),
                "seller": "search",
                "seller_name": "search",
                "image_url": item.get("image_url"),
                "shipping_text": item.get("shipping_text"),
                "seller_text": item.get("seller_text"),
            }
        )
    return rows


def run_search_filter_pipeline(
    raw_items: list[dict[str, Any]],
    config: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Stage1 (Pokémon TCG + price + shipping/delivery line on card) + blacklist + keywords -> state-engine rows."""
    meta: dict[str, Any] = {"pipeline": "search"}
    _maybe_warn_incompatible_url_facets(raw_items, meta)
    stage1 = filter_stage1_candidates(raw_items)
    stage1 = _apply_blacklist_and_config_keywords(stage1, config)
    meta["stage1_count"] = len(stage1)
    filtered = _rows_for_state_engine(stage1)
    meta["filtered_count"] = len(filtered)
    return filtered, meta


def keep_asins_not_in_db(rows: list[dict[str, Any]], known_asins: set[str]) -> list[dict[str, Any]]:
    return [r for r in rows if (r.get("asin") or "").upper() not in known_asins]
