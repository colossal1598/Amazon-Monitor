"""Unit tests for pick_amazon_image_url (no network)."""

import pytest

from image_urls import pick_amazon_image_url


def test_rank_0_picks_longest_http_url() -> None:
    candidates = {
        "https://m.media-amazon.com/images/I/short.jpg": 1,
        "https://m.media-amazon.com/images/I/much-longer-filename.jpg": 2,
    }
    assert pick_amazon_image_url(candidates, rank=0) == (
        "https://m.media-amazon.com/images/I/much-longer-filename.jpg"
    )


def test_rank_1_picks_second_longest() -> None:
    candidates = {
        "https://example.com/a.jpg": 1,
        "https://example.com/aa.jpg": 2,
        "https://example.com/aaa.jpg": 3,
    }
    assert pick_amazon_image_url(candidates, rank=1) == "https://example.com/aa.jpg"


def test_single_url_rank_0_and_1_same() -> None:
    url = "https://m.media-amazon.com/images/I/only.jpg"
    assert pick_amazon_image_url([url], rank=0) == url
    assert pick_amazon_image_url([url], rank=1) == url


def test_empty_dict_returns_none() -> None:
    assert pick_amazon_image_url({}) is None


def test_non_http_candidates_filtered() -> None:
    candidates = [
        "ftp://cdn.example.com/img.jpg",
        "//relative/no-scheme",
        "not-a-url",
        "https://m.media-amazon.com/images/I/ok.jpg",
    ]
    assert pick_amazon_image_url(candidates) == "https://m.media-amazon.com/images/I/ok.jpg"
