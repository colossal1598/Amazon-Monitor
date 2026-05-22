"""Unit tests for ensure_cached_image (mocked HTTP)."""

from unittest.mock import MagicMock, patch

import pytest

from image_cache import ensure_cached_image


def _mock_response(content: bytes = b"fake-jpeg", content_type: str = "image/jpeg") -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.headers = {"Content-Type": content_type}
    resp.content = content
    return resp


@patch("image_cache.requests.get")
def test_same_url_twice_skips_second_http(mock_get, tmp_path) -> None:
    mock_get.return_value = _mock_response()
    config = {"image_cache_dir": str(tmp_path)}
    asin = "B012345678"
    url = "https://cdn.example.com/product.jpg"

    first = ensure_cached_image(asin, url, config)
    second = ensure_cached_image(asin, url, config)

    assert mock_get.call_count == 1
    assert first is not None and second == first
    assert first.is_file()


@patch("image_cache.requests.get")
def test_url_change_triggers_new_download(mock_get, tmp_path) -> None:
    mock_get.return_value = _mock_response(b"first")
    config = {"image_cache_dir": str(tmp_path)}
    asin = "B099999999"
    url1 = "https://cdn.example.com/a.jpg"
    url2 = "https://cdn.example.com/b.jpg"

    ensure_cached_image(asin, url1, config)
    mock_get.return_value = _mock_response(b"second")
    ensure_cached_image(asin, url2, config)

    assert mock_get.call_count == 2
    assert mock_get.call_args_list[1][0][0] == url2


@patch("image_cache.requests.get")
def test_no_remote_url_returns_existing_file(mock_get, tmp_path) -> None:
    mock_get.return_value = _mock_response()
    config = {"image_cache_dir": str(tmp_path)}
    asin = "B088888888"
    url = "https://cdn.example.com/cached.jpg"

    cached = ensure_cached_image(asin, url, config)
    assert cached is not None

    result = ensure_cached_image(asin, None, config)

    assert mock_get.call_count == 1
    assert result == cached
    assert result.is_file()
