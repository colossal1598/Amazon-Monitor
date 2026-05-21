import tempfile
import unittest
from pathlib import Path

from state_engine import StateEngine


def _seed_product_row(se: StateEngine, asin: str, in_stock: int, price: float = 19.99) -> None:
    now = "2020-01-01T00:00:00+00:00"
    se.conn.execute(
        """
        INSERT INTO products (asin, title, seller, price, in_stock, first_seen, last_seen)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (asin, "Pokemon Card", "pdp_watch", price, in_stock, now, now),
    )
    se.conn.commit()


def _seed_aes_row(se: StateEngine, asin: str, in_stock: int, price: float = 19.99) -> None:
    now = "2020-01-01T00:00:00+00:00"
    se.conn.execute(
        """
        INSERT INTO aes_products (asin, title, price, in_stock, first_seen, last_seen)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (asin, "AES Seed Title", price, in_stock, now, now),
    )
    se.conn.commit()


def _product_row(se: StateEngine, asin: str) -> tuple[str, float | None, int]:
    row = se.conn.execute(
        "SELECT title, price, in_stock FROM products WHERE asin = ?",
        (asin,),
    ).fetchone()
    return (
        row[0],
        None if row[1] is None else float(row[1]),
        int(row[2]),
    )


def _aes_row(se: StateEngine, asin: str) -> tuple[str, float | None, int]:
    row = se.conn.execute(
        "SELECT title, price, in_stock FROM aes_products WHERE asin = ?",
        (asin,),
    ).fetchone()
    return (
        row[0],
        None if row[1] is None else float(row[1]),
        int(row[2]),
    )


def _aes_candidate(asin: str, *, in_stock: bool = True, price: float = 24.99) -> dict:
    return {
        "asin": asin,
        "title": "AES Title",
        "price": price,
        "in_stock": in_stock,
        "shipping_text": "FREE delivery",
        "image_url": "https://example.com/img.jpg",
        "seller": "aes_llc",
    }


class TestProcessAesSerpMirror(unittest.TestCase):
    def test_unknown_asin_inserts_and_alerts_new_product_without_touching_products(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            se = StateEngine(str(Path(tmp) / "m.db"), price_drop_percent=10)
            try:
                alerts = se.process_aes_serp_mirror(
                    [_aes_candidate("B099999999", in_stock=True, price=24.99)],
                    source="aes_llc",
                    reconcile_absence=False,
                )

                self.assertEqual([a["type"] for a in alerts], ["new_product"])
                title, price, stock = _aes_row(se, "B099999999")
                self.assertEqual(title, "AES Title")
                self.assertEqual(price, 24.99)
                self.assertEqual(stock, 1)
                product_row = se.conn.execute(
                    "SELECT asin FROM products WHERE asin = ?",
                    ("B099999999",),
                ).fetchone()
                self.assertIsNone(product_row)
            finally:
                se.conn.close()

    def test_back_in_stock_alert_when_aes_row_returns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            se = StateEngine(str(Path(tmp) / "m.db"), price_drop_percent=10)
            try:
                _seed_aes_row(se, "B011111111", in_stock=0, price=29.99)
                alerts = se.process_aes_serp_mirror(
                    [_aes_candidate("B011111111", in_stock=True, price=27.99)],
                    source="aes_llc",
                    reconcile_absence=False,
                )

                self.assertEqual([a["type"] for a in alerts], ["back_in_stock"])
                _, price, stock = _aes_row(se, "B011111111")
                self.assertEqual(price, 27.99)
                self.assertEqual(stock, 1)
            finally:
                se.conn.close()

    def test_price_drop_alert_for_still_in_stock_aes_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            se = StateEngine(str(Path(tmp) / "m.db"), price_drop_percent=10)
            try:
                _seed_aes_row(se, "B022222222", in_stock=1, price=20.00)
                alerts = se.process_aes_serp_mirror(
                    [_aes_candidate("B022222222", in_stock=True, price=16.00)],
                    source="aes_llc",
                    reconcile_absence=False,
                )

                self.assertEqual([a["type"] for a in alerts], ["price_drop"])
                self.assertEqual(alerts[0]["old_price"], 20.0)
                self.assertEqual(alerts[0]["new_price"], 16.0)
            finally:
                se.conn.close()

    def test_products_table_row_is_untouched_by_aes_mirror(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            se = StateEngine(str(Path(tmp) / "m.db"), price_drop_percent=10)
            try:
                _seed_product_row(se, "B033333333", in_stock=1, price=50.00)
                before = _product_row(se, "B033333333")
                se.process_aes_serp_mirror(
                    [_aes_candidate("B033333333", in_stock=True, price=10.00)],
                    source="aes_llc",
                    reconcile_absence=False,
                )

                after = _product_row(se, "B033333333")
                self.assertEqual(after, before)
            finally:
                se.conn.close()

    def test_reconcile_absence_marks_missing_rows_out_of_stock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            se = StateEngine(str(Path(tmp) / "m.db"), price_drop_percent=10)
            try:
                _seed_aes_row(se, "B044444444", in_stock=1, price=24.99)
                se.process_aes_serp_mirror([], source="aes_llc", reconcile_absence=True)
                _, _, stock = _aes_row(se, "B044444444")
                self.assertEqual(stock, 0)
            finally:
                se.conn.close()

    def test_reconcile_absence_false_keeps_existing_stock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            se = StateEngine(str(Path(tmp) / "m.db"), price_drop_percent=10)
            try:
                _seed_aes_row(se, "B055555555", in_stock=1, price=24.99)
                se.process_aes_serp_mirror([], source="aes_llc", reconcile_absence=False)
                _, _, stock = _aes_row(se, "B055555555")
                self.assertEqual(stock, 1)
            finally:
                se.conn.close()


if __name__ == "__main__":
    unittest.main()
