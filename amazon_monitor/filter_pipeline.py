import re
from pathlib import Path
from typing import Any


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
) -> list[dict[str, Any]]:
    req = [k.lower().strip() for k in required_keywords if k.strip()]
    blocked_asins, blocked_patterns = _read_blacklist(blacklist_file)
    filtered: list[dict[str, Any]] = []
    for product in products:
        asin = (product.get("asin") or "").upper()
        title = (product.get("title") or "").lower()
        if not asin or not title:
            continue
        if asin in blocked_asins:
            continue
        if any(not keyword or keyword not in title for keyword in req):
            continue
        if any(pattern.search(title) for pattern in blocked_patterns):
            continue
        filtered.append(product)
    return filtered

