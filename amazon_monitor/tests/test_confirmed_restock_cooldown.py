"""Fast genuine restocks (confirmed OOS -> in stock) must re-alert quickly, while
weak-evidence flapping stays on the long cooldown."""

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from state_engine import StateEngine, utc_now

_CONFIG = {
    "cross_source_alert_dedupe_minutes": 15,
    "stock_alert_cooldown_minutes": 60,
    "stock_alert_confirmed_cooldown_minutes": 10,
}


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


def _pdp_oos_row(asin: str, stock_reason: str) -> dict:
    return {
        "asin": asin,
        "title": "Pokemon Card",
        "price": None,
        "in_stock": False,
        "shipping_text": "",
        "image_url": None,
        "seller": "pdp_watch",
        "stock_confidence": "confirmed_out",
        "stock_reason": stock_reason,
        "source": "pdp_watch",
    }


def _aes_row(asin: str, *, in_stock: bool = True, explicit_oos: bool = False, price: float = 19.99) -> dict:
    return {
        "asin": asin,
        "title": "Pokemon Card",
        "price": price,
        "in_stock": in_stock,
        "explicit_oos": explicit_oos,
        "shipping_text": "FREE delivery",
        "image_url": None,
        "source": "aes_llc",
    }


def _seed_products_row(
    se: StateEngine,
    asin: str,
    *,
    in_stock: int,
    last_oos_confirmed: int = 0,
    price: float = 19.99,
) -> None:
    now = "2020-01-01T00:00:00+00:00"
    se.conn.execute(
        """
        INSERT INTO products (asin, title, seller, price, in_stock, first_seen, last_seen, last_oos_confirmed)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (asin, "Pokemon Card", "pdp_watch", price, in_stock, now, now, last_oos_confirmed),
    )
    se.conn.commit()


def _seed_aes_products_row(
    se: StateEngine,
    asin: str,
    *,
    in_stock: int,
    last_oos_confirmed: int = 0,
    price: float = 19.99,
) -> None:
    now = "2020-01-01T00:00:00+00:00"
    se.conn.execute(
        """
        INSERT INTO aes_products (asin, title, price, in_stock, first_seen, last_seen, last_oos_confirmed)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (asin, "Pokemon Card", price, in_stock, now, now, last_oos_confirmed),
    )
    se.conn.commit()


def _insert_alert(se: StateEngine, *, asin: str, source: str, sent_at: datetime) -> None:
    se.conn.execute(
        """
        INSERT INTO alerts (asin, alert_type, source, old_price, new_price, sent_at)
        VALUES (?, 'back_in_stock', ?, NULL, 19.99, ?)
        """,
        (asin, source, sent_at.isoformat()),
    )
    se.conn.commit()


class TestConfirmedRestockCooldown(unittest.TestCase):
    def test_confirmed_oos_restock_realerts_inside_long_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            se = StateEngine(str(Path(tmp) / "m.db"), price_drop_percent=10)
            try:
                asin = "B011111111"
                _seed_products_row(se, asin, in_stock=0, last_oos_confirmed=1)
                # Alerted 20 min ago: inside the 60-min long window, past the 10-min
                # confirmed window -> must fire because the OOS was real.
                _insert_alert(se, asin=asin, source="pdp_watch", sent_at=utc_now() - timedelta(minutes=20))

                alerts, _ = se.process_pdp_watch_candidates(
                    [_pdp_in_stock_row(asin)], {asin}, config=_CONFIG
                )
                self.assertEqual([a["type"] for a in alerts], ["back_in_stock"])
            finally:
                se.conn.close()

    def test_unconfirmed_oos_restock_stays_on_long_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            se = StateEngine(str(Path(tmp) / "m.db"), price_drop_percent=10)
            try:
                asin = "B011111111"
                _seed_products_row(se, asin, in_stock=0, last_oos_confirmed=0)
                _insert_alert(se, asin=asin, source="pdp_watch", sent_at=utc_now() - timedelta(minutes=20))

                alerts, _ = se.process_pdp_watch_candidates(
                    [_pdp_in_stock_row(asin)], {asin}, config=_CONFIG
                )
                self.assertEqual(alerts, [])
            finally:
                se.conn.close()

    def test_confirmed_restock_still_suppressed_inside_short_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            se = StateEngine(str(Path(tmp) / "m.db"), price_drop_percent=10)
            try:
                asin = "B011111111"
                _seed_products_row(se, asin, in_stock=0, last_oos_confirmed=1)
                _insert_alert(se, asin=asin, source="pdp_watch", sent_at=utc_now() - timedelta(minutes=4))

                alerts, _ = se.process_pdp_watch_candidates(
                    [_pdp_in_stock_row(asin)], {asin}, config=_CONFIG
                )
                self.assertEqual(alerts, [])
            finally:
                se.conn.close()

    def test_pdp_explicit_oos_reason_marks_confirmed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            se = StateEngine(str(Path(tmp) / "m.db"), price_drop_percent=10)
            try:
                asin = "B011111111"
                _seed_products_row(se, asin, in_stock=1)
                se.process_pdp_watch_candidates(
                    [_pdp_oos_row(asin, "explicit_oos_text")], {asin}, config=_CONFIG
                )
                row = se.conn.execute(
                    "SELECT in_stock, last_oos_confirmed FROM products WHERE asin = ?", (asin,)
                ).fetchone()
                self.assertEqual((int(row[0]), int(row[1])), (0, 1))
            finally:
                se.conn.close()

    def test_pdp_inferred_oos_reason_marks_unconfirmed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            se = StateEngine(str(Path(tmp) / "m.db"), price_drop_percent=10)
            try:
                asin = "B011111111"
                _seed_products_row(se, asin, in_stock=1)
                # Inferred OOS is weak evidence: needs two consecutive scrapes to flip.
                se.process_pdp_watch_candidates(
                    [_pdp_oos_row(asin, "inferred_oos_see_all_options")], {asin}, config=_CONFIG
                )
                se.process_pdp_watch_candidates(
                    [_pdp_oos_row(asin, "inferred_oos_see_all_options")], {asin}, config=_CONFIG
                )
                row = se.conn.execute(
                    "SELECT in_stock, last_oos_confirmed FROM products WHERE asin = ?", (asin,)
                ).fetchone()
                self.assertEqual((int(row[0]), int(row[1])), (0, 0))
            finally:
                se.conn.close()

    def test_aes_explicit_oos_flips_immediately_and_marks_confirmed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            se = StateEngine(str(Path(tmp) / "m.db"), price_drop_percent=10)
            config = dict(_CONFIG, aes_oos_confirm_cycles=3)
            try:
                asin = "B022222222"
                _seed_aes_products_row(se, asin, in_stock=1)
                # Explicit "currently unavailable" on the card bypasses the debounce.
                se.process_aes_serp_mirror(
                    [_aes_row(asin, in_stock=False, explicit_oos=True)],
                    source="aes_llc",
                    config=config,
                )
                row = se.conn.execute(
                    "SELECT in_stock, last_oos_confirmed FROM aes_products WHERE asin = ?", (asin,)
                ).fetchone()
                self.assertEqual((int(row[0]), int(row[1])), (0, 1))
            finally:
                se.conn.close()

    def test_aes_quick_sellout_and_restock_realerts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            se = StateEngine(str(Path(tmp) / "m.db"), price_drop_percent=10)
            config = dict(_CONFIG, aes_oos_confirm_cycles=3)
            try:
                asin = "B022222222"
                _seed_aes_products_row(se, asin, in_stock=1)
                # Earlier back_in_stock 30 min ago (inside the long window).
                _insert_alert(se, asin=asin, source="aes_llc", sent_at=utc_now() - timedelta(minutes=30))
                # Cycle 1: explicit sell-out.
                se.process_aes_serp_mirror(
                    [_aes_row(asin, in_stock=False, explicit_oos=True)],
                    source="aes_llc",
                    config=config,
                )
                # Cycle 2: it's back -> real restock, must alert.
                alerts, _ = se.process_aes_serp_mirror(
                    [_aes_row(asin, in_stock=True)],
                    source="aes_llc",
                    config=config,
                )
                self.assertEqual([a["type"] for a in alerts], ["back_in_stock"])
            finally:
                se.conn.close()


if __name__ == "__main__":
    unittest.main()
