"""Same-price back_in_stock dedupe for rotation/flap-driven restocks.

Regression for the 2026-07-16/17 duplicate-alert storm: the buybox rotates between
Amazon and 3P sellers, two consecutive seller_mismatch observations flip the DB OOS
with last_oos_confirmed=1 (C9), and when Amazon rotates back a priced back_in_stock
re-fires under the short confirmed cooldown — at the SAME price, every 15-100 min
(B0GYVHLP4L: 31 alerts at $99.99 in one day; 35/61 client-rated alerts tagged
"duplicate"). The AES mirror produced the same churn from SERP presence flapping.

Fix under test: the state engine stores WHY each OOS period exists
(products/aes_products.last_oos_reason). On restock, unless that reason is a strong
page-text sellout, a back_in_stock at the same price as the last one inside
``stock_alert_same_price_dedupe_minutes`` is suppressed. Genuine sellouts (strong OOS
text) keep the fast wave re-alert behavior, and any price change always alerts.
Also: an aes_products re-insert must not fire "new_product" for an ASIN the client
already watches on the PDP side (live alert id 1601).
"""

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from state_engine import StateEngine, utc_now

_CONFIG = {
    "cross_source_alert_dedupe_minutes": 15,
    "stock_alert_cooldown_minutes": 60,
    "stock_alert_confirmed_cooldown_minutes": 10,
    "stock_alert_same_price_dedupe_minutes": 360,
}


def _pdp_in_stock_row(asin: str, price: float = 99.99) -> dict:
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


def _aes_row(asin: str, *, in_stock: bool = True, explicit_oos: bool = False, price: float = 99.99) -> dict:
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


def _seed_products_row(se: StateEngine, asin: str, *, in_stock: int, price: float = 99.99) -> None:
    now = "2020-01-01T00:00:00+00:00"
    se.conn.execute(
        """
        INSERT INTO products (asin, title, seller, price, in_stock, first_seen, last_seen)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (asin, "Pokemon Card", "pdp_watch", price, in_stock, now, now),
    )
    se.conn.commit()


def _seed_aes_products_row(se: StateEngine, asin: str, *, in_stock: int, price: float = 99.99) -> None:
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
    source: str,
    sent_at: datetime,
    price: float = 99.99,
) -> None:
    se.conn.execute(
        """
        INSERT INTO alerts (asin, alert_type, source, old_price, new_price, sent_at)
        VALUES (?, 'back_in_stock', ?, NULL, ?, ?)
        """,
        (asin, source, price, sent_at.isoformat()),
    )
    se.conn.commit()


def _flip_oos_via_seller_mismatch(se: StateEngine, asin: str) -> None:
    """Two consecutive parsed-3P-buybox observations = the C9 confirmed rotation flip."""
    for _ in range(2):
        se.process_pdp_watch_candidates([_pdp_oos_row(asin, "seller_mismatch")], {asin}, config=_CONFIG)


class TestPdpSamePriceDedupe(unittest.TestCase):
    def test_rotation_flip_same_price_realert_suppressed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            se = StateEngine(str(Path(tmp) / "m.db"), price_drop_percent=10)
            try:
                asin = "B011111111"
                _seed_products_row(se, asin, in_stock=1)
                _flip_oos_via_seller_mismatch(se, asin)
                row = se.conn.execute(
                    "SELECT in_stock, last_oos_confirmed, last_oos_reason FROM products WHERE asin = ?",
                    (asin,),
                ).fetchone()
                self.assertEqual((int(row[0]), int(row[1]), row[2]), (0, 1, "seller_mismatch"))
                # Client already got this restock at this price 20 min ago: past the
                # 10-min confirmed cooldown (which never caught these), inside the
                # same-price window -> must NOT re-alert.
                _insert_alert(se, asin=asin, source="pdp_watch", sent_at=utc_now() - timedelta(minutes=20))

                alerts, _ = se.process_pdp_watch_candidates(
                    [_pdp_in_stock_row(asin)], {asin}, config=_CONFIG
                )
                self.assertEqual(alerts, [])
            finally:
                se.conn.close()

    def test_price_change_still_alerts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            se = StateEngine(str(Path(tmp) / "m.db"), price_drop_percent=10)
            try:
                asin = "B011111111"
                _seed_products_row(se, asin, in_stock=1)
                _flip_oos_via_seller_mismatch(se, asin)
                _insert_alert(se, asin=asin, source="pdp_watch", sent_at=utc_now() - timedelta(minutes=20))

                alerts, _ = se.process_pdp_watch_candidates(
                    [_pdp_in_stock_row(asin, price=89.99)], {asin}, config=_CONFIG
                )
                self.assertEqual([a["type"] for a in alerts], ["back_in_stock"])
            finally:
                se.conn.close()

    def test_same_price_outside_window_alerts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            se = StateEngine(str(Path(tmp) / "m.db"), price_drop_percent=10)
            try:
                asin = "B011111111"
                _seed_products_row(se, asin, in_stock=1)
                _flip_oos_via_seller_mismatch(se, asin)
                _insert_alert(se, asin=asin, source="pdp_watch", sent_at=utc_now() - timedelta(hours=7))

                alerts, _ = se.process_pdp_watch_candidates(
                    [_pdp_in_stock_row(asin)], {asin}, config=_CONFIG
                )
                self.assertEqual([a["type"] for a in alerts], ["back_in_stock"])
            finally:
                se.conn.close()

    def test_strong_sellout_same_price_wave_still_alerts(self) -> None:
        # A genuine sell-out (strong page text) followed by a same-price wave must keep
        # today's fast re-alert behavior — the dedupe targets rotation churn only.
        with tempfile.TemporaryDirectory() as tmp:
            se = StateEngine(str(Path(tmp) / "m.db"), price_drop_percent=10)
            try:
                asin = "B011111111"
                _seed_products_row(se, asin, in_stock=1)
                se.process_pdp_watch_candidates(
                    [_pdp_oos_row(asin, "explicit_oos_text")], {asin}, config=_CONFIG
                )
                row = se.conn.execute(
                    "SELECT in_stock, last_oos_reason FROM products WHERE asin = ?", (asin,)
                ).fetchone()
                self.assertEqual((int(row[0]), row[1]), (0, "explicit_oos_text"))
                _insert_alert(se, asin=asin, source="pdp_watch", sent_at=utc_now() - timedelta(minutes=20))

                alerts, _ = se.process_pdp_watch_candidates(
                    [_pdp_in_stock_row(asin)], {asin}, config=_CONFIG
                )
                self.assertEqual([a["type"] for a in alerts], ["back_in_stock"])
            finally:
                se.conn.close()

    def test_window_zero_disables_dedupe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            se = StateEngine(str(Path(tmp) / "m.db"), price_drop_percent=10)
            config = dict(_CONFIG, stock_alert_same_price_dedupe_minutes=0)
            try:
                asin = "B011111111"
                _seed_products_row(se, asin, in_stock=1)
                for _ in range(2):
                    se.process_pdp_watch_candidates(
                        [_pdp_oos_row(asin, "seller_mismatch")], {asin}, config=config
                    )
                _insert_alert(se, asin=asin, source="pdp_watch", sent_at=utc_now() - timedelta(minutes=20))

                alerts, _ = se.process_pdp_watch_candidates(
                    [_pdp_in_stock_row(asin)], {asin}, config=config
                )
                self.assertEqual([a["type"] for a in alerts], ["back_in_stock"])
            finally:
                se.conn.close()

    def test_restock_clears_stored_oos_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            se = StateEngine(str(Path(tmp) / "m.db"), price_drop_percent=10)
            try:
                asin = "B011111111"
                _seed_products_row(se, asin, in_stock=1)
                _flip_oos_via_seller_mismatch(se, asin)
                se.process_pdp_watch_candidates(
                    [_pdp_in_stock_row(asin, price=89.99)], {asin}, config=_CONFIG
                )
                row = se.conn.execute(
                    "SELECT in_stock, last_oos_reason FROM products WHERE asin = ?", (asin,)
                ).fetchone()
                self.assertEqual((int(row[0]), row[1]), (1, None))
            finally:
                se.conn.close()


class TestAesSamePriceDedupe(unittest.TestCase):
    def _flap_oos(self, se: StateEngine, asin: str, config: dict) -> None:
        """Three weak (SERP absence) cycles = the aes_oos_confirm_cycles debounced flip."""
        for _ in range(3):
            se.process_aes_serp_mirror(
                [_aes_row(asin, in_stock=False)], source="aes_llc", config=config
            )

    def test_serp_flap_same_price_realert_suppressed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            se = StateEngine(str(Path(tmp) / "m.db"), price_drop_percent=10)
            config = dict(_CONFIG, aes_oos_confirm_cycles=3)
            try:
                asin = "B022222222"
                _seed_aes_products_row(se, asin, in_stock=1)
                self._flap_oos(se, asin, config)
                row = se.conn.execute(
                    "SELECT in_stock, last_oos_reason FROM aes_products WHERE asin = ?", (asin,)
                ).fetchone()
                self.assertEqual((int(row[0]), row[1]), (0, "serp_absence"))
                # 70 min ago: past the 60-min long cooldown, inside the same-price window.
                _insert_alert(se, asin=asin, source="aes_llc", sent_at=utc_now() - timedelta(minutes=70))

                alerts, _ = se.process_aes_serp_mirror(
                    [_aes_row(asin, in_stock=True)], source="aes_llc", config=config
                )
                self.assertEqual(alerts, [])
            finally:
                se.conn.close()

    def test_serp_flap_new_price_still_alerts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            se = StateEngine(str(Path(tmp) / "m.db"), price_drop_percent=10)
            config = dict(_CONFIG, aes_oos_confirm_cycles=3)
            try:
                asin = "B022222222"
                _seed_aes_products_row(se, asin, in_stock=1)
                self._flap_oos(se, asin, config)
                _insert_alert(se, asin=asin, source="aes_llc", sent_at=utc_now() - timedelta(minutes=70))

                alerts, _ = se.process_aes_serp_mirror(
                    [_aes_row(asin, in_stock=True, price=89.99)], source="aes_llc", config=config
                )
                self.assertEqual([a["type"] for a in alerts], ["back_in_stock"])
            finally:
                se.conn.close()

    def test_explicit_sellout_same_price_restock_still_alerts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            se = StateEngine(str(Path(tmp) / "m.db"), price_drop_percent=10)
            config = dict(_CONFIG, aes_oos_confirm_cycles=3)
            try:
                asin = "B022222222"
                _seed_aes_products_row(se, asin, in_stock=1)
                se.process_aes_serp_mirror(
                    [_aes_row(asin, in_stock=False, explicit_oos=True)],
                    source="aes_llc",
                    config=config,
                )
                _insert_alert(se, asin=asin, source="aes_llc", sent_at=utc_now() - timedelta(minutes=20))

                alerts, _ = se.process_aes_serp_mirror(
                    [_aes_row(asin, in_stock=True)], source="aes_llc", config=config
                )
                self.assertEqual([a["type"] for a in alerts], ["back_in_stock"])
            finally:
                se.conn.close()


def _seed_watch_asin(se: StateEngine, asin: str) -> None:
    now = "2020-01-01T00:00:00+00:00"
    se.conn.execute(
        "INSERT INTO asins (asin, role, enabled, created_at, updated_at) VALUES (?, 'watch', 1, ?, ?)",
        (asin, now, now),
    )
    se.conn.commit()


class TestAesNewProductWatchedGuard(unittest.TestCase):
    def test_new_product_suppressed_for_pdp_watched_asin(self) -> None:
        # Live alert id 1601 (2026-07-17): aes_products row re-inserted for a watched
        # ASIN and re-fired "new_product"; client rated it duplicate. The guard keys
        # on the LIVE watch list (asins), not the products table.
        with tempfile.TemporaryDirectory() as tmp:
            se = StateEngine(str(Path(tmp) / "m.db"), price_drop_percent=10)
            try:
                asin = "B033333333"
                _seed_watch_asin(se, asin)
                _seed_products_row(se, asin, in_stock=1)

                alerts, _ = se.process_aes_serp_mirror(
                    [_aes_row(asin, in_stock=True)], source="aes_llc", config=_CONFIG
                )
                self.assertEqual(alerts, [])
                row = se.conn.execute(
                    "SELECT in_stock FROM aes_products WHERE asin = ?", (asin,)
                ).fetchone()
                self.assertIsNotNone(row)
                self.assertEqual(int(row[0]), 1)
            finally:
                se.conn.close()

    def test_new_product_still_fires_for_unwatched_asin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            se = StateEngine(str(Path(tmp) / "m.db"), price_drop_percent=10)
            try:
                asin = "B044444444"
                alerts, _ = se.process_aes_serp_mirror(
                    [_aes_row(asin, in_stock=True)], source="aes_llc", config=_CONFIG
                )
                self.assertEqual([a["type"] for a in alerts], ["new_product"])
            finally:
                se.conn.close()

    def test_stale_products_row_alone_does_not_silence_new_product(self) -> None:
        # Regression (B0F6PQLR16, 2026-07-21): a dead search-era products row silenced
        # a genuinely-new AES item for 21h. Only a live asins watch row may suppress.
        with tempfile.TemporaryDirectory() as tmp:
            se = StateEngine(str(Path(tmp) / "m.db"), price_drop_percent=10)
            try:
                asin = "B055555555"
                _seed_products_row(se, asin, in_stock=1)  # stale row, NOT on watch list

                alerts, _ = se.process_aes_serp_mirror(
                    [_aes_row(asin, in_stock=True)], source="aes_llc", config=_CONFIG
                )
                self.assertEqual([a["type"] for a in alerts], ["new_product"])
            finally:
                se.conn.close()


if __name__ == "__main__":
    unittest.main()
