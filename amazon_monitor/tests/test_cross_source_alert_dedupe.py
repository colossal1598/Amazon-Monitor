import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from state_engine import StateEngine, utc_now


def _pdp_in_stock_row(asin: str, price: float = 19.99) -> dict:
    return {
        "asin": asin,
        "title": "Pokemon Card",
        "price": price,
        "in_stock": True,
        "shipping_text": "FREE delivery",
        "image_url": None,
        "seller": "pdp_watch",
        "stock_confidence": "confirmed_in",
        "source": "pdp_watch",
    }


def _aes_in_stock_row(asin: str, price: float = 19.99) -> dict:
    return {
        "asin": asin,
        "title": "Pokemon Card",
        "price": price,
        "in_stock": True,
        "shipping_text": "FREE delivery",
        "image_url": None,
        "source": "aes_llc",
    }


def _seed_products_row(se: StateEngine, asin: str, *, in_stock: int, price: float = 19.99) -> None:
    now = "2020-01-01T00:00:00+00:00"
    se.conn.execute(
        """
        INSERT INTO products (asin, title, seller, price, in_stock, first_seen, last_seen)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (asin, "Pokemon Card", "pdp_watch", price, in_stock, now, now),
    )
    se.conn.commit()


def _seed_aes_row(se: StateEngine, asin: str, *, in_stock: int, price: float = 19.99) -> None:
    now = "2020-01-01T00:00:00+00:00"
    se.conn.execute(
        """
        INSERT INTO aes_products (asin, title, price, in_stock, first_seen, last_seen)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (asin, "Pokemon Card", price, in_stock, now, now),
    )
    se.conn.commit()


def _insert_alert(
    se: StateEngine,
    *,
    asin: str,
    alert_type: str,
    source: str,
    sent_at: datetime,
) -> None:
    se.conn.execute(
        """
        INSERT INTO alerts (asin, alert_type, source, old_price, new_price, sent_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (asin, alert_type, source, None, 19.99, sent_at.isoformat()),
    )
    se.conn.commit()


class TestCrossSourceAlertDedupe(unittest.TestCase):
    def test_aes_back_in_stock_suppressed_after_recent_pdp_alert(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            se = StateEngine(str(Path(tmp) / "m.db"), price_drop_percent=10)
            config = {"cross_source_alert_dedupe_minutes": 15}
            try:
                asin = "B011111111"
                _seed_aes_row(se, asin, in_stock=0)
                _insert_alert(
                    se,
                    asin=asin,
                    alert_type="back_in_stock",
                    source="pdp_watch",
                    sent_at=utc_now() - timedelta(minutes=5),
                )

                alerts, _ = se.process_aes_serp_mirror(
                    [_aes_in_stock_row(asin)],
                    source="aes_llc",
                    config=config,
                )
                self.assertEqual(alerts, [])
                row = se.conn.execute(
                    "SELECT in_stock FROM aes_products WHERE asin = ?", (asin,)
                ).fetchone()
                self.assertEqual(int(row[0]), 1)
            finally:
                se.conn.close()

    def test_aes_back_in_stock_fires_after_dedupe_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            se = StateEngine(str(Path(tmp) / "m.db"), price_drop_percent=10)
            config = {
                "cross_source_alert_dedupe_minutes": 15,
                "stock_alert_cooldown_minutes": 15,
            }
            try:
                asin = "B011111111"
                _seed_aes_row(se, asin, in_stock=0)
                _insert_alert(
                    se,
                    asin=asin,
                    alert_type="back_in_stock",
                    source="pdp_watch",
                    sent_at=utc_now() - timedelta(minutes=20),
                )

                # New price vs the prior alert: this test proves window/cooldown expiry,
                # not the same-price dedupe (tested in test_same_price_realert_dedupe).
                alerts, _ = se.process_aes_serp_mirror(
                    [_aes_in_stock_row(asin, 24.99)],
                    source="aes_llc",
                    config=config,
                )
                self.assertEqual([a["type"] for a in alerts], ["back_in_stock"])
            finally:
                se.conn.close()

    def test_same_source_pdp_flapping_suppressed_within_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            se = StateEngine(str(Path(tmp) / "m.db"), price_drop_percent=10)
            config = {
                "cross_source_alert_dedupe_minutes": 15,
                "stock_alert_cooldown_minutes": 60,
            }
            try:
                asin = "B011111111"
                _seed_products_row(se, asin, in_stock=0)

                first, _ = se.process_pdp_watch_candidates(
                    [_pdp_in_stock_row(asin, 21.99)],
                    {asin},
                    config=config,
                )
                self.assertEqual([a["type"] for a in first], ["back_in_stock"])

                se.conn.execute("UPDATE products SET in_stock = 0 WHERE asin = ?", (asin,))
                se.conn.commit()

                second, _ = se.process_pdp_watch_candidates(
                    [_pdp_in_stock_row(asin, 22.99)],
                    {asin},
                    config=config,
                )
                self.assertEqual(second, [])
                row = se.conn.execute(
                    "SELECT in_stock FROM products WHERE asin = ?", (asin,)
                ).fetchone()
                self.assertEqual(int(row[0]), 1)
            finally:
                se.conn.close()

    def test_same_source_alert_fires_again_after_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            se = StateEngine(str(Path(tmp) / "m.db"), price_drop_percent=10)
            config = {
                "cross_source_alert_dedupe_minutes": 15,
                "stock_alert_cooldown_minutes": 60,
            }
            try:
                asin = "B011111111"
                _seed_products_row(se, asin, in_stock=0)
                _insert_alert(
                    se,
                    asin=asin,
                    alert_type="back_in_stock",
                    source="pdp_watch",
                    sent_at=utc_now() - timedelta(minutes=90),
                )

                # New price vs the prior alert: this test proves cooldown expiry, not
                # the same-price dedupe (tested in test_same_price_realert_dedupe).
                alerts, _ = se.process_pdp_watch_candidates(
                    [_pdp_in_stock_row(asin, 24.99)],
                    {asin},
                    config=config,
                )
                self.assertEqual([a["type"] for a in alerts], ["back_in_stock"])
            finally:
                se.conn.close()

    def test_cooldown_zero_disables_same_source_suppression(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            se = StateEngine(str(Path(tmp) / "m.db"), price_drop_percent=10)
            config = {
                "cross_source_alert_dedupe_minutes": 0,
                "stock_alert_cooldown_minutes": 0,
                # All suppression knobs off, including the same-price dedupe window.
                "stock_alert_same_price_dedupe_minutes": 0,
            }
            try:
                asin = "B011111111"
                _seed_products_row(se, asin, in_stock=0)
                _insert_alert(
                    se,
                    asin=asin,
                    alert_type="back_in_stock",
                    source="pdp_watch",
                    sent_at=utc_now() - timedelta(minutes=1),
                )

                alerts, _ = se.process_pdp_watch_candidates(
                    [_pdp_in_stock_row(asin)],
                    {asin},
                    config=config,
                )
                self.assertEqual([a["type"] for a in alerts], ["back_in_stock"])
            finally:
                se.conn.close()

    def test_cooldown_suppresses_repeat_from_other_source_too(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            se = StateEngine(str(Path(tmp) / "m.db"), price_drop_percent=10)
            config = {
                "cross_source_alert_dedupe_minutes": 15,
                "stock_alert_cooldown_minutes": 60,
            }
            try:
                asin = "B011111111"
                _seed_aes_row(se, asin, in_stock=0)
                # PDP alerted 30 minutes ago: outside the 15-minute cross-source
                # window but inside the global cooldown.
                _insert_alert(
                    se,
                    asin=asin,
                    alert_type="back_in_stock",
                    source="pdp_watch",
                    sent_at=utc_now() - timedelta(minutes=30),
                )

                alerts, _ = se.process_aes_serp_mirror(
                    [_aes_in_stock_row(asin)],
                    source="aes_llc",
                    config=config,
                )
                self.assertEqual(alerts, [])
            finally:
                se.conn.close()

    def test_price_drop_not_suppressed_across_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            se = StateEngine(str(Path(tmp) / "m.db"), price_drop_percent=10)
            config = {"cross_source_alert_dedupe_minutes": 15}
            try:
                asin = "B011111111"
                _seed_aes_row(se, asin, in_stock=1, price=100.0)
                _insert_alert(
                    se,
                    asin=asin,
                    alert_type="price_drop",
                    source="pdp_watch",
                    sent_at=utc_now() - timedelta(minutes=5),
                )

                alerts, _ = se.process_aes_serp_mirror(
                    [_aes_in_stock_row(asin, 80.0)],
                    source="aes_llc",
                    config=config,
                )
                self.assertEqual([a["type"] for a in alerts], ["price_drop"])
            finally:
                se.conn.close()


if __name__ == "__main__":
    unittest.main()
