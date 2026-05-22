"""Local disk cache for product images (cellular data reduction)."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

LOGGER = logging.getLogger(__name__)

_CONTENT_TYPE_EXT = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
}


def cache_dir(config: dict[str, Any]) -> Path:
    raw = config.get("image_cache_dir") or "data/product_images"
    return Path(str(raw))


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_asin(asin: str) -> str:
    return str(asin).strip().upper()


def _meta_path(cache: Path, asin: str) -> Path:
    return cache / f"{asin}.meta.json"


def _image_path_for_asin(cache: Path, asin: str) -> Path | None:
    for path in cache.glob(f"{asin}.*"):
        if path.name.endswith(".meta.json"):
            continue
        if path.is_file():
            return path
    return None


def _ext_from_content_type(content_type: str | None) -> str:
    if not content_type:
        return ".jpg"
    main = content_type.split(";")[0].strip().lower()
    return _CONTENT_TYPE_EXT.get(main, ".jpg")


def _read_meta(meta_path: Path) -> dict[str, Any] | None:
    if not meta_path.is_file():
        return None
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError) as exc:
        LOGGER.warning("image_cache: bad meta %s: %s", meta_path, exc)
        return None


def _remove_stale_images(cache: Path, asin: str, keep: Path) -> None:
    for path in cache.glob(f"{asin}.*"):
        if path.name.endswith(".meta.json") or path == keep:
            continue
        try:
            path.unlink()
        except OSError as exc:
            LOGGER.warning("image_cache: could not remove %s: %s", path, exc)


def ensure_cached_image(asin: str, remote_url: str | None, config: dict[str, Any]) -> Path | None:
    asin_norm = _normalize_asin(asin)
    if not asin_norm:
        return None

    cache = cache_dir(config)
    cache.mkdir(parents=True, exist_ok=True)

    existing = _image_path_for_asin(cache, asin_norm)
    meta_path = _meta_path(cache, asin_norm)
    meta = _read_meta(meta_path)

    remote = (str(remote_url).strip() if remote_url else "") or None

    if remote and meta and meta.get("remote_url") == remote and existing and existing.is_file():
        return existing

    if not remote:
        if existing and existing.is_file():
            return existing
        return None

    try:
        resp = requests.get(remote, timeout=15)
        resp.raise_for_status()
    except Exception as exc:
        LOGGER.warning("image_cache: fetch failed asin=%s url=%s: %s", asin_norm, remote, exc)
        if existing and existing.is_file():
            return existing
        return None

    ext = _ext_from_content_type(resp.headers.get("Content-Type"))
    dest = cache / f"{asin_norm}{ext}"
    try:
        dest.write_bytes(resp.content)
        meta_path.write_text(
            json.dumps({"remote_url": remote, "cached_at": _utc_iso()}, indent=2),
            encoding="utf-8",
        )
        _remove_stale_images(cache, asin_norm, dest)
    except OSError as exc:
        LOGGER.warning("image_cache: write failed asin=%s: %s", asin_norm, exc)
        if existing and existing.is_file():
            return existing
        return None

    return dest
