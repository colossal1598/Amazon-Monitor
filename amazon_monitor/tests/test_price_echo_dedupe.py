"""Echo-alert dedupe: the PDP (~55s) and AES (~2min) pipelines catch the same pricing
event independently, and the slower one re-announced it minutes later at the identical
price (2026-07-23 B0G3CV6Z9D: 4 alerts for 2 real events; client: "always 2 alerts").

A price_drop is suppressed when the LATEST product alert for the ASIN (any source,
any type) already announced the same new_price inside the cross-source window; a weak
back_in_stock is suppressed when a recent price_drop already announced its price.
Strong page-text sellouts keep the fast re-alert path.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from state_engine import StateEngine, utc_now

_CONFIG = {
    "cross_source_alert_dedupe_minutes": 15,
    "stock_alert_cooldown_minutes": 15,
    "stock_alert_confirmed_cooldown_minutes": 3,
    "stock_alert_same_price_dedupe_minutes": 360,
}


def _pdp_row(asin: str, price: float) -> dict:
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


def _aes_row(asin: str, price: float) -> dict:
    return {
        "asin": asin,
        "title": "Pokemon Card",
        "price": price,
        "in_stock": True,
        "shipping_text": "FREE delivery",
        "image_url": None,
        "source": "aes_llc",
    }


def _seed_products(se: StateEngine, asin: str, *, in_stock: int, price: float,
                   last_oos_reason: str | None = None) -> None:
    now = "2020-01-01T00:00:00+00:00"
    se.conn.execute(
        """
        INSERT INTO products (asin, title, seller, price, in_stock, first_seen, last_seen)
        VALUES (?, ?, 'pdp_watch', ?, ?, ?, ?)
        """,
        (asin, "Pokemon Card", price, in_stock, now, now),
    )
    if last_oos_reason is not None:
        se.conn.execute(
            "UPDATE products SET last_oos_reason = ? WHERE asin = ?", (last_oos_reason, asin)
        )
    se.conn.commit()


def _seed_aes(se: StateEngine, asin: str, *, in_stock: int, price: float) -> None:
    now = "2020-01-01T00:00:00+00:00"
    se.conn.execute(
        """
        INSERT INTO aes_products (asin, title, price, in_stock, first_seen, last_seen)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (asin, "Pokemon Card", price, in_stock, now, now),
    )
    se.conn.commit()


def _insert_alert(se: StateEngine, *, asin: str, alert_type: str, source: str,
                  new_price: float, sent_at: datetime) -> None:
    se.conn.execute(
        """
        INSERT INTO alerts (asin, alert_type, source, old_price, new_price, sent_at)
        VALUES (?, ?, ?, NULL, ?, ?)
        """,
        (asin, alert_type, source, new_price, sent_at.isoformat()),
    )
    se.conn.commit()


class TestPriceDropEcho(unittest.TestCase):
    def test_pdp_price_drop_suppressed_after_aes_same_price(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            se = StateEngine(str(Path(tmp) / "m.db"), price_drop_percent=10)
            try:
                asin = "B0ECHO0001"
                _seed_products(se, asin, in_stock=1, price=100.0)
                _insert_alert(se, asin=asin, alert_type="price_drop", source="aes_llc",
                              new_price=49.99, sent_at=utc_now() - timedelta(minutes=3))
                alerts, _ = se.process_pdp_watch_candidates(
                    [_pdp_row(asin, 49.99)], {asin}, config=_CONFIG
                )
                self.assertEqual(alerts, [])
            finally:
                se.conn.close()

    def test_aes_price_drop_suppressed_after_pdp_same_price(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            se = StateEngine(str(Path(tmp) / "m.db"), price_drop_percent=10)
            try:
                asin = "B0ECHO0002"
                _seed_aes(se, asin, in_stock=1, price=100.0)
                _insert_alert(se, asin=asin, alert_type="price_drop", source="pdp_watch",
                              new_price=49.99, sent_at=utc_now() - timedelta(minutes=3))
                alerts, _ = se.process_aes_serp_mirror(
                    [_aes_row(asin, 49.99)], source="aes_llc", config=_CONFIG
                )
                self.assertEqual(alerts, [])
            finally:
                se.conn.close()

    def test_price_drop_echo_also_matches_back_in_stock_leader(self) -> None:
        # 2026-07-23 20:49-20:51: pdp back_in_stock $95.78 then aes price_drop $95.78.
        with tempfile.TemporaryDirectory() as tmp:
            se = StateEngine(str(Path(tmp) / "m.db"), price_drop_percent=10)
            try:
                asin = "B0ECHO0003"
                _seed_aes(se, asin, in_stock=1, price=109.95)
                _insert_alert(se, asin=asin, alert_type="back_in_stock", source="pdp_watch",
                              new_price=95.78, sent_at=utc_now() - timedelta(minutes=2))
                alerts, _ = se.process_aes_serp_mirror(
                    [_aes_row(asin, 95.78)], source="aes_llc", config=_CONFIG
                )
                self.assertEqual(alerts, [])
            finally:
                se.conn.close()

    def test_price_drop_fires_when_prior_price_differs(self) -> None:
        # A NEWER price is new information — the previous alert's price doesn't match.
        with tempfile.TemporaryDirectory() as tmp:
            se = StateEngine(str(Path(tmp) / "m.db"), price_drop_percent=10)
            try:
                asin = "B0ECHO0004"
                _seed_products(se, asin, in_stock=1, price=95.78)
                _insert_alert(se, asin=asin, alert_type="price_drop", source="aes_llc",
                              new_price=95.78, sent_at=utc_now() - timedelta(minutes=3))
                alerts, _ = se.process_pdp_watch_candidates(
                    [_pdp_row(asin, 69.99)], {asin}, config=_CONFIG
                )
                self.assertEqual([a["type"] for a in alerts], ["price_drop"])
            finally:
                se.conn.close()

    def test_price_drop_fires_after_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            se = StateEngine(str(Path(tmp) / "m.db"), price_drop_percent=10)
            try:
                asin = "B0ECHO0005"
                _seed_products(se, asin, in_stock=1, price=100.0)
                _insert_alert(se, asin=asin, alert_type="price_drop", source="aes_llc",
                              new_price=49.99, sent_at=utc_now() - timedelta(minutes=40))
                alerts, _ = se.process_pdp_watch_candidates(
                    [_pdp_row(asin, 49.99)], {asin}, config=_CONFIG
                )
                self.assertEqual([a["type"] for a in alerts], ["price_drop"])
            finally:
                se.conn.close()


class TestBackInStockPriceEcho(unittest.TestCase):
    def test_weak_back_in_stock_suppressed_after_recent_price_drop(self) -> None:
        # 2026-07-23 17:14: aes price_drop $95.88 -> pdp back_in_stock $95.88 +21s.
        with tempfile.TemporaryDirectory() as tmp:
            se = StateEngine(str(Path(tmp) / "m.db"), price_drop_percent=10)
            try:
                asin = "B0ECHO0006"
                _seed_products(se, asin, in_stock=0, price=95.88,
                               last_oos_reason="seller_mismatch")
                _insert_alert(se, asin=asin, alert_type="price_drop", source="aes_llc",
                              new_price=95.88, sent_at=utc_now() - timedelta(minutes=1))
                alerts, _ = se.process_pdp_watch_candidates(
                    [_pdp_row(asin, 95.88)], {asin}, config=_CONFIG
                )
                self.assertEqual(alerts, [])
            finally:
                se.conn.close()

    def test_strong_sellout_restock_still_alerts_despite_recent_price_drop(self) -> None:
        # A real page-text sellout keeps the fast re-alert behavior — the echo guard
        # lives inside the weak-evidence block only.
        with tempfile.TemporaryDirectory() as tmp:
            se = StateEngine(str(Path(tmp) / "m.db"), price_drop_percent=10)
            try:
                asin = "B0ECHO0007"
                _seed_products(se, asin, in_stock=0, price=95.88,
                               last_oos_reason="explicit_oos")
                _insert_alert(se, asin=asin, alert_type="price_drop", source="aes_llc",
                              new_price=95.88, sent_at=utc_now() - timedelta(minutes=1))
                alerts, _ = se.process_pdp_watch_candidates(
                    [_pdp_row(asin, 95.88)], {asin}, config=_CONFIG
                )
                self.assertEqual([a["type"] for a in alerts], ["back_in_stock"])
            finally:
                se.conn.close()


if __name__ == "__main__":
    unittest.main()
