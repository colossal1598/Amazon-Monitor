import logging
import re
import unicodedata
from typing import Any
from urllib.parse import unquote

LOGGER = logging.getLogger(__name__)

_ASIN_LINE = re.compile(r"^[A-Z0-9]{10}$")


def _normalize_text(value: str) -> str:
    """Lowercase and strip diacritics for accent-insensitive matching."""
    lowered = (value or "").lower().strip()
    decomposed = unicodedata.normalize("NFKD", lowered)
    normalized = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    normalized = normalized.replace("trading card game", "tcg")
    return normalized


def _keyword_matches_title(title_norm: str, keyword: str, raw_title: str) -> bool:
    if not keyword:
        return True
    if keyword in title_norm:
        return True
    if keyword == "tcg":
        return _title_signals_pokemon_tcg_scope(raw_title)
    return False


def _config_asin_set(config: dict[str, Any], key: str) -> set[str]:
    """Normalize config whitelist/blacklist entries to uppercase 10-char ASINs."""
    raw = config.get(key)
    if not isinstance(raw, list):
        return set()
    out: set[str] = set()
    for x in raw:
        s = str(x).strip().upper().replace("-", "")
        if _ASIN_LINE.match(s):
            out.add(s)
    return out


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
    # Scarlet & Violet and other SV-era lines often omit a literal "tcg" token in the visible title.
    if "scarlet" in text or "violet" in text or "paldea" in text or "hisui" in text or "alola" in text:
        return True
    product_tokens = (
        "booster",
        "blister",
        "elite trainer",
        " etb",
        " tin",
        "ex box",
        " battle deck",
        "poster box",
        "collection box",
        "booster bundle",
        "sleeve booster",
        "three pack",
        "blister pack",
        "premium pro",
        "mini portfolio",
        "tech sticker",
        "sticker collection",
        "world championship",
    )
    if any(tok in text for tok in product_tokens):
        return True
    return False


def _has_pokemon_tcg_title(title: str) -> bool:
    return bool(_title_signals_pokemon_tcg_scope(title))


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


def _merge_whitelist_raw(
    raw_items: list[dict[str, Any]],
    stage1_core: list[dict[str, Any]],
    whitelist_set: set[str],
) -> list[dict[str, Any]]:
    """Append raw SERP rows for whitelisted ASINs that Stage 1 did not keep (bypass Stage 1)."""
    core_asins = {(p.get("asin") or "").strip().upper() for p in stage1_core if (p.get("asin") or "").strip()}
    out = [dict(p) for p in stage1_core]
    for item in raw_items:
        a = (item.get("asin") or "").strip().upper()
        if not a or a not in whitelist_set or a in core_asins:
            continue
        out.append(dict(item))
        core_asins.add(a)
    return out


def stage1_reject_reason(item: dict[str, Any]) -> str | None:
    """If this row fails Stage 1, return a short machine reason; if it passes, return None."""
    title = item.get("title") or ""
    if not _has_pokemon_tcg_title(title):
        return "no_pokemon_tcg_title"
    if not _is_valid_price(item):
        if item.get("price") is None:
            return "no_price_on_card"
        pt = _normalize_ascii(str(item.get("price_text") or ""))
        if "see price" in pt:
            return "see_price_placeholder"
        return "invalid_price_value"
    if not _has_shipping_qualifier_on_card(item):
        return "no_shipping_or_delivery_signal"
    return None


def filter_stage1_candidates(raw_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pokémon TCG title + valid price + any scraped shipping/delivery signal on the card."""
    out: list[dict[str, Any]] = []
    for item in raw_items:
        if stage1_reject_reason(item) is None:
            out.append(dict(item))
    return out


def stage1_reject_report(raw_items: list[dict[str, Any]]) -> tuple[dict[str, int], list[dict[str, Any]]]:
    """Per-item reject reasons plus aggregate counts (for logs / meta)."""
    counts: dict[str, int] = {}
    rows: list[dict[str, Any]] = []
    for item in raw_items:
        reason = stage1_reject_reason(item)
        if reason is None:
            continue
        counts[reason] = counts.get(reason, 0) + 1
        asin = (item.get("asin") or "").strip().upper() or "?"
        title = (item.get("title") or "").strip()
        rows.append({"asin": asin, "reason": reason, "title": title[:200]})
    return counts, rows


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


def _apply_required_keywords_and_yaml_blacklist(
    candidates: list[dict[str, Any]],
    config: dict[str, Any],
    yaml_blacklist: set[str],
    whitelist: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """YAML blacklist ASINs drop first (wins over whitelist); whitelist skips required_keywords.

    Returns (kept_products, drop_rows) with asin, reason, detail, title_snip per drop.
    """
    req_kw_raw = config.get("required_keywords") or []
    req_norm = [_normalize_text(k) for k in req_kw_raw if str(k).strip()]

    drops: list[dict[str, Any]] = []
    final: list[dict[str, Any]] = []

    for product in candidates:
        asin = (product.get("asin") or "").strip().upper()
        raw_title = product.get("title") or ""
        title = _normalize_text(raw_title)
        title_snip = (raw_title or "")[:140]
        if not asin:
            drops.append({"asin": "-", "reason": "missing_asin", "detail": "", "title_snip": title_snip})
            continue
        if not title:
            drops.append({"asin": asin, "reason": "missing_title", "detail": "", "title_snip": title_snip})
            continue
        if asin in yaml_blacklist:
            drops.append(
                {
                    "asin": asin,
                    "reason": "yaml_blacklist_asin",
                    "detail": "",
                    "title_snip": title_snip,
                }
            )
            continue
        if asin in whitelist:
            final.append(product)
            continue
        failed_kw: str | None = None
        for kw in req_norm:
            if not _keyword_matches_title(title, kw, raw_title):
                failed_kw = kw
                break
        if failed_kw is not None:
            drops.append(
                {
                    "asin": asin,
                    "reason": "required_keyword_missing",
                    "detail": failed_kw,
                    "title_snip": title_snip,
                }
            )
            continue
        final.append(product)

    return final, drops


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
    """Stage1 gates + whitelist merge + YAML blacklist + required_keywords -> state-engine rows."""
    meta: dict[str, Any] = {"pipeline": "search"}
    whitelist_set = _config_asin_set(config, "whitelist")
    blacklist_set = _config_asin_set(config, "blacklist")
    meta["whitelist_count"] = len(whitelist_set)
    meta["yaml_blacklist_count"] = len(blacklist_set)
    _maybe_warn_incompatible_url_facets(raw_items, meta)
    reject_counts, reject_rows = stage1_reject_report(raw_items)
    meta["stage1_reject_counts"] = reject_counts
    meta["stage1_rejects"] = reject_rows
    meta["stage1_pass_title_price_shipping"] = len(raw_items) - sum(reject_counts.values())
    stage1_core = filter_stage1_candidates(raw_items)
    meta["stage1_core_count"] = len(stage1_core)
    merged = _merge_whitelist_raw(raw_items, stage1_core, whitelist_set)
    meta["stage1_after_whitelist_merge"] = len(merged)
    stage1, bl_kw_drops = _apply_required_keywords_and_yaml_blacklist(merged, config, blacklist_set, whitelist_set)
    meta["stage1_blacklist_kw_dropped"] = len(merged) - len(stage1)
    meta["blacklist_kw_drops"] = bl_kw_drops
    meta["stage1_count"] = len(stage1)
    if bl_kw_drops:
        LOGGER.info("search_kw_blacklist_blocked count=%s", len(bl_kw_drops))
        for d in bl_kw_drops:
            LOGGER.info(
                "search_kw_blacklist_blocked asin=%s reason=%s detail=%s title=%s",
                d.get("asin"),
                d.get("reason"),
                d.get("detail") or "-",
                d.get("title_snip") or "",
            )
    filtered = _rows_for_state_engine(stage1)
    meta["filtered_count"] = len(filtered)
    return filtered, meta


def keep_asins_not_in_db(rows: list[dict[str, Any]], known_asins: set[str]) -> list[dict[str, Any]]:
    return [r for r in rows if (r.get("asin") or "").upper() not in known_asins]
