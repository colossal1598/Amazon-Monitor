import tempfile
import unittest
from pathlib import Path

from state_engine import StateEngine


class TestStateEngineSearchStock(unittest.TestCase):
    def test_respects_in_stock_false_on_existing_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "m.db"
            se = StateEngine(str(db), price_drop_percent=10)
            try:
                now = "2020-01-01T00:00:00+00:00"
                se.conn.execute(
                    """
                    INSERT INTO products (asin, title, seller, price, in_stock, first_seen, last_seen)
                    VALUES (?, ?, ?, ?, 1, ?, ?)
                    """,
                    ("B0123456789", "Pokemon Card", "search", 19.99, now, now),
                )
                se.conn.commit()
                se.process_search_candidates(
                    [
                        {
                            "asin": "B0123456789",
                            "title": "Pokemon Card",
                            "price": 19.99,
                            "in_stock": False,
                            "shipping_text": "",
                            "image_url": None,
                            "seller": "search",
                        }
                    ],
                    reconcile_missing=False,
                )
                row = se.conn.execute("SELECT in_stock FROM products WHERE asin = ?", ("B0123456789",)).fetchone()
                self.assertEqual(int(row[0]), 0)
            finally:
                se.conn.close()

    def test_reconcile_skips_pdp_exclude_asins(self) -> None:
        """SERP absence reconcile must not OOS ASINs excluded (e.g. PDP watch list)."""
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "m.db"
            se = StateEngine(str(db), price_drop_percent=10)
            try:
                now = "2020-01-01T00:00:00+00:00"
                se.conn.execute(
                    """
                    INSERT INTO products (asin, title, seller, price, in_stock, first_seen, last_seen)
                    VALUES (?, ?, ?, ?, 1, ?, ?), (?, ?, ?, ?, 1, ?, ?)
                    """,
                    (
                        "B011111111",
                        "A",
                        "s",
                        1.0,
                        now,
                        now,
                        "B022222222",
                        "B",
                        "s",
                        2.0,
                        now,
                        now,
                    ),
                )
                se.conn.commit()
                se.process_search_candidates(
                    [
                        {
                            "asin": "B011111111",
                            "title": "Pokemon TCG A",
                            "price": 1.0,
                            "in_stock": True,
                            "shipping_text": "FREE delivery",
                            "image_url": None,
                            "seller": "search",
                        }
                    ],
                    reconcile_missing=True,
                    reconcile_exclude_asins={"B022222222"},
                )
                r1 = se.conn.execute("SELECT in_stock FROM products WHERE asin = ?", ("B011111111",)).fetchone()
                r2 = se.conn.execute("SELECT in_stock FROM products WHERE asin = ?", ("B022222222",)).fetchone()
                self.assertEqual(int(r1[0]), 1)
                self.assertEqual(int(r2[0]), 1)
            finally:
                se.conn.close()

    def test_back_in_stock_suppresses_price_drop_same_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "m.db"
            se = StateEngine(str(db), price_drop_percent=10)
            try:
                now = "2020-01-01T00:00:00+00:00"
                se.conn.execute(
                    """
                    INSERT INTO products (asin, title, seller, price, in_stock, first_seen, last_seen, last_price_alert)
                    VALUES (?, ?, ?, ?, 0, ?, ?, NULL)
                    """,
                    ("B0999999999", "Pokemon Item", "search", 100.0, now, now),
                )
                se.conn.commit()
                alerts = se.process_search_candidates(
                    [
                        {
                            "asin": "B0999999999",
                            "title": "Pokemon Item",
                            "price": 50.0,
                            "in_stock": True,
                            "shipping_text": "",
                            "image_url": None,
                            "seller": "search",
                        }
                    ],
                    reconcile_missing=False,
                )
                types = [a["type"] for a in alerts]
                self.assertEqual(types, ["back_in_stock"])
            finally:
                se.conn.close()
