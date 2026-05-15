"""PDP watch + AES SERP fallback resolution."""

import tempfile
import unittest
from pathlib import Path

from pdp_serp_fallback import (
    aes_row_usable_for_fallback,
    build_aes_fallback_index,
    pdp_row_needs_serp_fallback,
    resolve_pdp_watch_observations,
)
from pdp_scraper import _pdp_row, _pdp_skip_row
from state_engine import StateEngine


class TestPdpSerpFallbackLogic(unittest.TestCase):
    def test_needs_fallback_on_skip_and_disqualified(self) -> None:
        self.assertTrue(pdp_row_needs_serp_fallback(_pdp_skip_row("B011111111", "goto_failed")))
        bad = _pdp_row(
            "B011111111",
            title="T",
            price=19.99,
            shipping_text="",
            image_url=None,
            merchant_blob="Sold by Other Shop",
            allowed=["amazon.com"],
        )
        self.assertTrue(pdp_row_needs_serp_fallback(bad))

    def test_qualifying_pdp_no_fallback(self) -> None:
        good = _pdp_row(
            "B011111111",
            title="T",
            price=19.99,
            shipping_text="FREE delivery",
            image_url=None,
            merchant_blob="Sold by Amazon.com",
            allowed=["amazon.com"],
        )
        self.assertFalse(pdp_row_needs_serp_fallback(good))

    def test_resolve_uses_aes_when_pdp_wrong_seller(self) -> None:
        pdp_bad = _pdp_row(
            "B011111111",
            title="PDP Title",
            price=50.0,
            shipping_text="FREE delivery",
            image_url=None,
            merchant_blob="Sold by Other Shop",
            allowed=["amazon.com", "amazon export"],
        )
        aes = {
            "asin": "B011111111",
            "title": "AES Title",
            "price": 45.0,
            "in_stock": True,
            "shipping_text": "FREE delivery",
            "seller_text": "Amazon Export Sales LLC",
        }
        resolved, stats = resolve_pdp_watch_observations(
            [pdp_bad],
            {"B011111111": aes},
            ["amazon export"],
        )
        self.assertEqual(stats["aes_fallback"], 1)
        self.assertEqual(resolved[0]["source"], "aes_serp_fallback")
        self.assertEqual(resolved[0]["price"], 45.0)
        self.assertTrue(resolved[0]["in_stock"])

    def test_disqualified_pdp_without_aes_becomes_skip(self) -> None:
        pdp_bad = _pdp_row(
            "B022222222",
            title="T",
            price=10.0,
            shipping_text="FREE delivery",
            image_url=None,
            merchant_blob="Sold by Other Shop",
            allowed=["amazon.com"],
        )
        resolved, stats = resolve_pdp_watch_observations([pdp_bad], {}, ["amazon.com"])
        self.assertEqual(stats["skip_no_fallback"], 1)
        self.assertTrue(resolved[0].get("_skip_update"))

    def test_build_index_only_watch_asins(self) -> None:
        rows = [
            {"asin": "B011111111", "price": 1.0},
            {"asin": "B022222222", "price": 2.0},
        ]
        idx = build_aes_fallback_index(rows, {"B011111111"})
        self.assertEqual(set(idx.keys()), {"B011111111"})

    def test_aes_seller_text_rejected_when_wrong(self) -> None:
        aes = {
            "asin": "B011111111",
            "price": 40.0,
            "seller_text": "Sold by Random LLC",
        }
        self.assertFalse(aes_row_usable_for_fallback(aes, ["amazon export"]))


class TestPdpAesFallbackStateEngine(unittest.TestCase):
    def test_fallback_updates_price_without_false_oos(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            se = StateEngine(str(Path(tmp) / "m.db"), price_drop_percent=10)
            try:
                now = "2020-01-01T00:00:00+00:00"
                se.conn.execute(
                    """
                    INSERT INTO products (asin, title, seller, price, in_stock, first_seen, last_seen)
                    VALUES (?, ?, ?, ?, 1, ?, ?)
                    """,
                    ("B011111111", "Old", "pdp_watch", 60.0, now, now),
                )
                se.conn.commit()
                pdp_bad = _pdp_row(
                    "B011111111",
                    title="PDP",
                    price=55.0,
                    shipping_text="FREE delivery",
                    image_url=None,
                    merchant_blob="Sold by Other",
                    allowed=["amazon.com"],
                )
                aes = {
                    "asin": "B011111111",
                    "title": "AES",
                    "price": 40.0,
                    "in_stock": True,
                    "shipping_text": "FREE delivery",
                    "seller_text": "Amazon Export Sales LLC",
                }
                resolved, _ = resolve_pdp_watch_observations(
                    [pdp_bad], {"B011111111": aes}, ["amazon export"]
                )
                alerts = se.process_pdp_watch_candidates(resolved, {"B011111111"})
                types = [a["type"] for a in alerts]
                self.assertIn("price_drop", types)
                row = se.conn.execute(
                    "SELECT price, in_stock FROM products WHERE asin = ?", ("B011111111",)
                ).fetchone()
                self.assertEqual(float(row[0]), 40.0)
                self.assertEqual(int(row[1]), 1)
            finally:
                se.conn.close()


if __name__ == "__main__":
    unittest.main()
