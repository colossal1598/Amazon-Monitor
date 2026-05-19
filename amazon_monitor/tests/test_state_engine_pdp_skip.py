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


def _pdp_row(asin: str, price: float) -> dict:
    return {
        "asin": asin,
        "title": "Pokemon Card",
        "price": price,
        "in_stock": True,
        "shipping_text": "$12.44 delivery",
        "image_url": None,
        "seller": "pdp_watch",
    }


def _stock_and_price(se: StateEngine, asin: str) -> tuple[int, float | None]:
    row = se.conn.execute(
        "SELECT in_stock, price FROM products WHERE asin = ?", (asin,)
    ).fetchone()
    return int(row[0]), (None if row[1] is None else float(row[1]))


class TestProcessPdpWatchSkip(unittest.TestCase):
    def test_skip_update_row_preserves_db_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            se = StateEngine(str(Path(tmp) / "m.db"), price_drop_percent=10)
            try:
                _seed_row(se, "B011111111", in_stock=1, price=19.99)
                alerts = se.process_pdp_watch_candidates(
                    [
                        {
                            "asin": "B011111111",
                            "_skip_update": True,
                            "skip_reason": "goto_failed",
                            "source": "pdp_watch",
                        }
                    ],
                    {"B011111111"},
                )
                self.assertEqual(alerts, [])
                stock, price = _stock_and_price(se, "B011111111")
                self.assertEqual(stock, 1)
                self.assertEqual(price, 19.99)
            finally:
                se.conn.close()

    def test_captcha_skip_update_preserves_db_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            se = StateEngine(str(Path(tmp) / "m.db"), price_drop_percent=10)
            try:
                _seed_row(se, "B011111111", in_stock=1, price=19.99)
                alerts = se.process_pdp_watch_candidates(
                    [
                        {
                            "asin": "B011111111",
                            "_skip_update": True,
                            "skip_reason": "captcha",
                            "source": "pdp_watch",
                        }
                    ],
                    {"B011111111"},
                )
                self.assertEqual(alerts, [])
                stock, price = _stock_and_price(se, "B011111111")
                self.assertEqual(stock, 1)
                self.assertEqual(price, 19.99)
            finally:
                se.conn.close()

    def test_captcha_abort_skip_update_preserves_db_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            se = StateEngine(str(Path(tmp) / "m.db"), price_drop_percent=10)
            try:
                _seed_row(se, "B011111111", in_stock=1, price=19.99)
                alerts = se.process_pdp_watch_candidates(
                    [
                        {
                            "asin": "B011111111",
                            "_skip_update": True,
                            "skip_reason": "captcha_run_aborted",
                            "source": "pdp_watch",
                        }
                    ],
                    {"B011111111"},
                )
                self.assertEqual(alerts, [])
                stock, price = _stock_and_price(se, "B011111111")
                self.assertEqual(stock, 1)
                self.assertEqual(price, 19.99)
            finally:
                se.conn.close()

    def test_missing_watched_asin_preserves_db_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            se = StateEngine(str(Path(tmp) / "m.db"), price_drop_percent=10)
            try:
                _seed_row(se, "B011111111", in_stock=1, price=19.99)
                alerts = se.process_pdp_watch_candidates([], {"B011111111"})
                self.assertEqual(alerts, [])
                stock, price = _stock_and_price(se, "B011111111")
                self.assertEqual(stock, 1)
                self.assertEqual(price, 19.99)
            finally:
                se.conn.close()

    def test_real_oos_pdp_row_still_marks_oos(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            se = StateEngine(str(Path(tmp) / "m.db"), price_drop_percent=10)
            try:
                _seed_row(se, "B011111111", in_stock=1, price=19.99)
                alerts = se.process_pdp_watch_candidates(
                    [
                        {
                            "asin": "B011111111",
                            "title": "Pokemon Card",
                            "price": None,
                            "in_stock": False,
                            "shipping_text": "",
                            "image_url": None,
                            "seller": "pdp_watch",
                        }
                    ],
                    {"B011111111"},
                )
                self.assertEqual(alerts, [])
                stock, price = _stock_and_price(se, "B011111111")
                self.assertEqual(stock, 0)
                self.assertEqual(price, 19.99)
            finally:
                se.conn.close()

    def test_real_pdp_row_can_emit_back_in_stock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            se = StateEngine(str(Path(tmp) / "m.db"), price_drop_percent=10)
            try:
                _seed_row(se, "B011111111", in_stock=0, price=19.99)
                alerts = se.process_pdp_watch_candidates(
                    [
                        {
                            "asin": "B011111111",
                            "title": "Pokemon Card",
                            "price": 21.99,
                            "in_stock": True,
                            "shipping_text": "FREE delivery",
                            "image_url": None,
                            "seller": "pdp_watch",
                        }
                    ],
                    {"B011111111"},
                )
                types = [a["type"] for a in alerts]
                self.assertEqual(types, ["back_in_stock"])
                stock, price = _stock_and_price(se, "B011111111")
                self.assertEqual(stock, 1)
                self.assertEqual(price, 21.99)
            finally:
                se.conn.close()

    def test_drop_then_recovery_then_drop_alerts_again_same_day(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            se = StateEngine(str(Path(tmp) / "m.db"), price_drop_percent=10)
            try:
                _seed_row(se, "B011111111", in_stock=1, price=100.0)

                first = se.process_pdp_watch_candidates([_pdp_row("B011111111", 80.0)], {"B011111111"})
                self.assertEqual([a["type"] for a in first], ["price_drop"])
                self.assertEqual(_stock_and_price(se, "B011111111"), (1, 80.0))

                recovery = se.process_pdp_watch_candidates([_pdp_row("B011111111", 120.0)], {"B011111111"})
                self.assertEqual(recovery, [])
                self.assertEqual(_stock_and_price(se, "B011111111"), (1, 120.0))

                second = se.process_pdp_watch_candidates([_pdp_row("B011111111", 90.0)], {"B011111111"})
                self.assertEqual([a["type"] for a in second], ["price_drop"])
                self.assertEqual(_stock_and_price(se, "B011111111"), (1, 90.0))
            finally:
                se.conn.close()


if __name__ == "__main__":
    unittest.main()
