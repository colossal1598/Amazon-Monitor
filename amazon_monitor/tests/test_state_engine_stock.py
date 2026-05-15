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
                )[0]
                row = se.conn.execute("SELECT in_stock FROM products WHERE asin = ?", ("B0123456789",)).fetchone()
                self.assertEqual(int(row[0]), 0)
            finally:
                se.conn.close()

    def test_first_row_no_new_alert_when_oos_or_no_price(self) -> None:
        """New ASIN is inserted but no new_product WhatsApp when unavailable or price missing."""
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "m.db"
            se = StateEngine(str(db), price_drop_percent=10)
            try:
                now = "2020-01-01T00:00:00+00:00"
                alerts, _meta = se.process_search_candidates(
                    [
                        {
                            "asin": "B033333333",
                            "title": "Pokemon TCG X",
                            "price": None,
                            "in_stock": False,
                            "shipping_text": "FREE delivery",
                            "image_url": None,
                            "seller": "search",
                        }
                    ],
                    reconcile_missing=False,
                )
                self.assertEqual(alerts, [])
                row = se.conn.execute(
                    "SELECT in_stock, price FROM products WHERE asin = ?", ("B033333333",)
                ).fetchone()
                self.assertEqual(int(row[0]), 0)
                self.assertIsNone(row[1])
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
                )[0]
                r1 = se.conn.execute("SELECT in_stock FROM products WHERE asin = ?", ("B011111111",)).fetchone()
                r2 = se.conn.execute("SELECT in_stock FROM products WHERE asin = ?", ("B022222222",)).fetchone()
                self.assertEqual(int(r1[0]), 1)
                self.assertEqual(int(r2[0]), 1)
            finally:
                se.conn.close()

    def test_reconcile_marks_missing_search_asins_oos(self) -> None:
        """A healthy SERP run marks tracked ASINs absent from the union as out-of-stock."""
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
                        "Visible",
                        "s",
                        1.0,
                        now,
                        now,
                        "B022222222",
                        "Missing",
                        "s",
                        2.0,
                        now,
                        now,
                    ),
                )
                se.conn.commit()
                _alerts, meta = se.process_search_candidates(
                    [
                        {
                            "asin": "B011111111",
                            "title": "Pokemon TCG Visible",
                            "price": 1.0,
                            "in_stock": True,
                            "shipping_text": "FREE delivery",
                            "image_url": None,
                            "seller": "search",
                        }
                    ],
                    reconcile_missing=True,
                )
                self.assertEqual(meta.get("marked_oos_count"), 1)
                self.assertEqual(meta.get("marked_oos_asins"), ["B022222222"])
                visible = se.conn.execute("SELECT in_stock FROM products WHERE asin = ?", ("B011111111",)).fetchone()
                missing = se.conn.execute("SELECT in_stock FROM products WHERE asin = ?", ("B022222222",)).fetchone()
                self.assertEqual(int(visible[0]), 1)
                self.assertEqual(int(missing[0]), 0)
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
                alerts, _meta = se.process_search_candidates(
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

    def test_price_increase_persisted_without_alert(self) -> None:
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
                    ("B088888888", "Pokemon Item", "search", 80.0, now, now),
                )
                se.conn.commit()
                alerts, _meta = se.process_search_candidates(
                    [
                        {
                            "asin": "B088888888",
                            "title": "Pokemon Item",
                            "price": 100.0,
                            "in_stock": True,
                            "shipping_text": "FREE delivery",
                            "image_url": None,
                            "seller": "search",
                        }
                    ],
                    reconcile_missing=False,
                )
                self.assertEqual(alerts, [])
                row = se.conn.execute(
                    "SELECT price FROM products WHERE asin = ?", ("B088888888",)
                ).fetchone()
                self.assertEqual(float(row[0]), 100.0)
            finally:
                se.conn.close()

    def test_null_scrape_preserves_price_on_search(self) -> None:
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
                    ("B077777777", "Pokemon Card", "search", 19.99, now, now),
                )
                se.conn.commit()
                se.process_search_candidates(
                    [
                        {
                            "asin": "B077777777",
                            "title": "Pokemon Card",
                            "price": None,
                            "in_stock": True,
                            "shipping_text": "FREE delivery",
                            "image_url": None,
                            "seller": "search",
                        }
                    ],
                    reconcile_missing=False,
                )[0]
                row = se.conn.execute(
                    "SELECT price FROM products WHERE asin = ?", ("B077777777",)
                ).fetchone()
                self.assertEqual(float(row[0]), 19.99)
            finally:
                se.conn.close()

    def test_touch_pipeline_presence_updates_price_from_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "m.db"
            se = StateEngine(str(db), price_drop_percent=10)
            try:
                old = "2019-01-01T00:00:00+00:00"
                se.conn.execute(
                    """
                    INSERT INTO products (asin, title, seller, price, in_stock, first_seen, last_seen)
                    VALUES (?, ?, ?, ?, 0, ?, ?)
                    """,
                    ("B0CCCCCCCC", "x", "s", 50.0, old, old),
                )
                se.conn.commit()
                se.touch_tracked_serp_pipeline_presence(
                    {"B0CCCCCCCC"},
                    pipeline_rows=[{"asin": "B0CCCCCCCC", "price": 75.0, "in_stock": True}],
                )
                row = se.conn.execute(
                    "SELECT price, in_stock FROM products WHERE asin = ?", ("B0CCCCCCCC",)
                ).fetchone()
                self.assertEqual(float(row[0]), 75.0)
                self.assertEqual(int(row[1]), 1)
            finally:
                se.conn.close()

    def test_touch_pipeline_presence_updates_existing_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "m.db"
            se = StateEngine(str(db), price_drop_percent=10)
            try:
                old = "2019-01-01T00:00:00+00:00"
                se.conn.execute(
                    """
                    INSERT INTO products (asin, title, seller, price, in_stock, first_seen, last_seen)
                    VALUES (?, ?, ?, ?, 0, ?, ?), (?, ?, ?, ?, 1, ?, ?)
                    """,
                    ("B0AAAAAAAA", "x", "s", 1.0, old, old, "B0BBBBBBBB", "y", "s", 2.0, old, old),
                )
                se.conn.commit()
                n = se.touch_tracked_serp_pipeline_presence({"B0AAAAAAAA", "B0BBBBBBBB"})
                self.assertEqual(n, 2)
                r_a = se.conn.execute(
                    "SELECT in_stock, last_seen FROM products WHERE asin = ?", ("B0AAAAAAAA",)
                ).fetchone()
                r_b = se.conn.execute(
                    "SELECT in_stock, last_seen FROM products WHERE asin = ?", ("B0BBBBBBBB",)
                ).fetchone()
                self.assertEqual(int(r_a[0]), 1)
                self.assertEqual(int(r_b[0]), 1)
                self.assertNotEqual(str(r_a[1]), old)
                self.assertNotEqual(str(r_b[1]), old)
            finally:
                se.conn.close()

    def test_touch_pipeline_presence_excludes_asins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "m.db"
            se = StateEngine(str(db), price_drop_percent=10)
            try:
                old = "2019-01-01T00:00:00+00:00"
                se.conn.execute(
                    """
                    INSERT INTO products (asin, title, seller, price, in_stock, first_seen, last_seen)
                    VALUES (?, ?, ?, ?, 0, ?, ?), (?, ?, ?, ?, 0, ?, ?)
                    """,
                    ("B0AAAAAAAA", "x", "s", 1.0, old, old, "B0BBBBBBBB", "y", "s", 2.0, old, old),
                )
                se.conn.commit()
                se.touch_tracked_serp_pipeline_presence(
                    {"B0AAAAAAAA", "B0BBBBBBBB"},
                    exclude_asins={"B0BBBBBBBB"},
                )
                r_a = se.conn.execute(
                    "SELECT in_stock, last_seen FROM products WHERE asin = ?", ("B0AAAAAAAA",)
                ).fetchone()
                r_b = se.conn.execute(
                    "SELECT in_stock, last_seen FROM products WHERE asin = ?", ("B0BBBBBBBB",)
                ).fetchone()
                self.assertEqual(int(r_a[0]), 1)
                self.assertNotEqual(str(r_a[1]), old)
                self.assertEqual(int(r_b[0]), 0)
                self.assertEqual(str(r_b[1]), old)
            finally:
                se.conn.close()

    def test_touch_pipeline_presence_chunking(self) -> None:
        prev_chunk = StateEngine._SERP_PRESENCE_CHUNK
        StateEngine._SERP_PRESENCE_CHUNK = 2
        try:
            with tempfile.TemporaryDirectory() as tmp:
                db = Path(tmp) / "m.db"
                se = StateEngine(str(db), price_drop_percent=10)
                try:
                    old = "2019-01-01T00:00:00+00:00"
                    for i in range(5):
                        asin = f"B{100000000 + i}"
                        se.conn.execute(
                            """
                            INSERT INTO products (asin, title, seller, price, in_stock, first_seen, last_seen)
                            VALUES (?, ?, ?, ?, 0, ?, ?)
                            """,
                            (asin, "t", "s", 1.0, old, old),
                        )
                    se.conn.commit()
                    asins = {f"B{100000000 + j}" for j in range(5)}
                    n = se.touch_tracked_serp_pipeline_presence(asins)
                    self.assertEqual(n, 5)
                    cnt = se.conn.execute(
                        "SELECT COUNT(*) FROM products WHERE in_stock = 1 AND last_seen != ?",
                        (old,),
                    ).fetchone()[0]
                    self.assertEqual(cnt, 5)
                finally:
                    se.conn.close()
        finally:
            StateEngine._SERP_PRESENCE_CHUNK = prev_chunk
