"""BandwidthMeter pure logic (no Playwright / should_abort_url)."""

from __future__ import annotations

import asyncio

import pytest

from usage_metrics import BandwidthMeter, content_length_from_headers

class _FakeResponse:
    def __init__(self, url: str, headers: dict[str, str] | None = None, body: bytes = b"") -> None:
        self.url = url
        self.headers = headers or {}
        self._body = body

    async def body(self) -> bytes:
        return self._body


@pytest.mark.parametrize(
    "headers, expected",
    [
        ({"content-length": "1234"}, 1234),
        ({"Content-Length": "5678"}, 5678),
        ({}, None),
        ({"content-length": "bad"}, None),
        ({"content-length": "-1"}, None),
    ],
)
def test_content_length_from_headers(headers: dict[str, str], expected: int | None) -> None:
    assert content_length_from_headers(headers) == expected


def test_phase_buckets_amazon_and_other() -> None:
    meter = BandwidthMeter()
    meter.set_phase("pdp")
    meter._add_bytes("https://www.amazon.com/dp/B001", 100)
    meter._add_bytes("https://cdn.example.com/track.js", 50)
    meter.set_phase("aes")
    meter._add_bytes("https://images-na.ssl-images-amazon.com/x.jpg", 200)
    meter._add_bytes("https://analytics.example.com/pixel", 25)
    totals = meter.totals()
    assert totals["pdp_amazon"] == 100
    assert totals["pdp_other"] == 50
    assert totals["aes_amazon"] == 200
    assert totals["aes_other"] == 25
    assert totals["net_bytes_pdp"] == 150
    assert totals["net_bytes_aes"] == 225
    assert totals["net_bytes_total"] == 375


def test_set_phase_get_phase() -> None:
    meter = BandwidthMeter()
    assert meter.get_phase() == "pdp"
    meter.set_phase("aes")
    assert meter.get_phase() == "aes"
    meter.set_phase("invalid")
    assert meter.get_phase() == "aes"


def test_async_handler_uses_body_when_no_content_length() -> None:
    meter = BandwidthMeter({"bandwidth_meter_max_body_reads_per_cycle": 10})
    meter.set_phase("pdp")
    resp = _FakeResponse("https://example.com/asset.bin", body=b"x" * 400)

    async def _run() -> None:
        await meter._on_response_async(resp)

    asyncio.run(_run())
    assert meter.totals()["pdp_other"] == 400
    assert meter._body_reads == 1


def test_async_handler_skips_oversized_body() -> None:
    meter = BandwidthMeter()
    meter.set_phase("pdp")
    resp = _FakeResponse("https://example.com/big.bin", body=b"x" * (5 * 1024 * 1024 + 1))

    async def _run() -> None:
        await meter._on_response_async(resp)

    asyncio.run(_run())
    assert meter.totals()["pdp_other"] == 0
    assert meter._body_reads == 1


def test_async_handler_respects_body_read_cap() -> None:
    meter = BandwidthMeter({"bandwidth_meter_max_body_reads_per_cycle": 2})
    meter.set_phase("pdp")

    async def _run() -> None:
        for i in range(4):
            resp = _FakeResponse(f"https://example.com/{i}.bin", body=b"ab")
            await meter._on_response_async(resp)

    asyncio.run(_run())
    assert meter._body_reads == 2
    assert meter.totals()["pdp_other"] == 4


def test_sync_handler_uses_content_length_only() -> None:
    meter = BandwidthMeter()
    meter.set_phase("aes")
    resp = _FakeResponse(
        "https://www.amazon.com/gp/css",
        headers={"content-length": "999"},
    )
    meter._on_response_sync(resp)
    assert meter.totals()["aes_amazon"] == 999
