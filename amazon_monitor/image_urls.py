"""Pick Amazon product image URLs from candidate sets (dynamic-image JSON, srcset, etc.)."""

from __future__ import annotations

from typing import Any


def pick_amazon_image_url(candidates: Any, rank: int = 1) -> str | None:
    """Return the URL at ``rank`` after sorting HTTP candidates by length descending.

    ``rank`` is clamped to ``len(urls) - 1`` so a single candidate always resolves to index 0.
    """
    if isinstance(candidates, dict):
        urls = list(candidates.keys())
    elif isinstance(candidates, (list, tuple, set)):
        urls = list(candidates)
    else:
        return None

    http_urls = [u for u in urls if isinstance(u, str) and u.startswith("http")]
    if not http_urls:
        return None

    sorted_urls = sorted(http_urls, key=len, reverse=True)
    idx = min(rank, len(sorted_urls) - 1)
    return sorted_urls[idx]
