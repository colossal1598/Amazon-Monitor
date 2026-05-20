import tempfile
import unittest
from pathlib import Path

from state_engine import StateEngine


def _seed_row(se: StateEngine, asin: str, in_stock: int, price: float = 19.99) -> None:
    now = "2020-01-01T00:00:00+00:00"
    se.conn.execute(
        """
        INSERT INTO products (asin, title, seller, price, in_stock, first_seen, last_seen)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (asin, "Pokemon Card", "pdp_watch", price, in_stock, now, now),
    )
    se.conn.commit()


def _product_row(se: StateEngine, asin: str) -> tuple[str, float | None, int, str]:
    row = se.conn.execute(
        "SELECT title, price, in_stock, last_seen FROM products WHERE asin = ?",
        (asin,),
    ).fetchone()
    return (
        row[0],
        None if row[1] is None else float(row[1]),
        int(row[2]),
        row[3],
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


class TestProcessAesDiscoveryCandidates(unittest.TestCase):
    def test_known_asin_is_not_updated_by_aes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            se = StateEngine(str(Path(tmp) / "m.db"), price_drop_percent=10)
            try:
                _seed_row(se, "B011111111", in_stock=1, price=19.99)
                before = _product_row(se, "B011111111")

                alerts = se.process_aes_discovery_candidates(
                    [_aes_candidate("B011111111", in_stock=False, price=9.99)],
                    source="aes_llc",
                )

                self.assertEqual(alerts, [])
                after = _product_row(se, "B011111111")
                self.assertEqual(after, before)
            finally:
                se.conn.close()

    def test_unknown_asin_inserts_and_alerts_new_product(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            se = StateEngine(str(Path(tmp) / "m.db"), price_drop_percent=10)
            try:
                alerts = se.process_aes_discovery_candidates(
                    [_aes_candidate("B099999999", in_stock=True, price=24.99)],
                    source="aes_llc",
                )

                self.assertEqual([a["type"] for a in alerts], ["new_product"])
                title, price, stock, _ = _product_row(se, "B099999999")
                self.assertEqual(title, "AES Title")
                self.assertEqual(price, 24.99)
                self.assertEqual(stock, 1)
            finally:
                se.conn.close()


if __name__ == "__main__":
    unittest.main()
