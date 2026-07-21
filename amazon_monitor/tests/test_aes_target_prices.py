"""AES-side target prices (client request 2026-07-20).

A listed ASIN appearing on the AES SERP alerts ONLY at/below its target price —
overpriced discovery products stay silent without having to blacklist them or add
them to PDP watch. Crossing from above target to at/below while in stock fires a
price_below_target alert (edge on the stored old_price, rate-limited by the target
cooldown). Unlisted ASINs behave exactly as before (covered by existing suites).
"""

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from state_engine import StateEngine, _normalize_target_prices, utc_now

_CONFIG = {
    "cross_source_alert_dedupe_minutes": 15,
    "stock_alert_cooldown_minutes": 60,
    "stock_alert_confirmed_cooldown_minutes": 10,
    "stock_alert_same_price_dedupe_minutes": 360,
    "aes_oos_confirm_cycles": 3,
    "target_price_alert_cooldown_hours": 6,
    "aes_target_prices": {"B0F6PQLR16": 120.0},
}


def _aes_row(asin: str, *, in_stock: bool = True, price: float | None = 99.99) -> dict:
    return {
        "asin": asin,
        "title": "Pokemon Card",
        "price": price,
        "in_stock": in_stock,
        "shipping_text": "FREE delivery",
        "image_url": None,
        "source": "aes_llc",
    }


def _seed_aes(se: StateEngine, asin: str, *, in_stock: int, price: float | None) -> None:
    now = "2020-01-01T00:00:00+00:00"
    se.conn.execute(
        """
        INSERT INTO aes_products (asin, title, price, in_stock, first_seen, last_seen)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (asin, "Pokemon Card", price, in_stock, now, now),
    )
    se.conn.commit()


def _process(se: StateEngine, rows: list[dict], config: dict | None = None):
    return se.process_aes_serp_mirror(rows, source="aes_llc", config=config or _CONFIG)


class TestNormalizeTargetPrices(unittest.TestCase):
    def test_normalizes_and_drops_junk(self) -> None:
        raw = {"b0f6pqlr16": "120", "B0OTHER1111": 0, "B0BAD222222": "abc", "": 50}
        self.assertEqual(_normalize_target_prices(raw), {"B0F6PQLR16": 120.0})

    def test_non_dict_is_empty(self) -> None:
        self.assertEqual(_normalize_target_prices(["B0F6PQLR16"]), {})


class TestAesTargetGating(unittest.TestCase):
    ASIN = "B0F6PQLR16"

    def test_new_product_above_target_suppressed_but_mirrored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            se = StateEngine(str(Path(tmp) / "m.db"), price_drop_percent=10)
            try:
                alerts, _ = _process(se, [_aes_row(self.ASIN, price=149.99)])
                self.assertEqual(alerts, [])
                row = se.conn.execute(
                    "SELECT in_stock, price FROM aes_products WHERE asin = ?", (self.ASIN,)
                ).fetchone()
                self.assertEqual(int(row[0]), 1)
                self.assertEqual(float(row[1]), 149.99)
            finally:
                se.conn.close()

    def test_new_product_at_or_below_target_alerts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            se = StateEngine(str(Path(tmp) / "m.db"), price_drop_percent=10)
            try:
                alerts, _ = _process(se, [_aes_row(self.ASIN, price=119.99)])
                self.assertEqual([a["type"] for a in alerts], ["new_product"])
            finally:
                se.conn.close()

    def test_priceless_listed_row_counts_as_above_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            se = StateEngine(str(Path(tmp) / "m.db"), price_drop_percent=10)
            try:
                alerts, _ = _process(se, [_aes_row(self.ASIN, price=None)])
                self.assertEqual(alerts, [])
            finally:
                se.conn.close()

    def test_back_in_stock_above_target_suppressed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            se = StateEngine(str(Path(tmp) / "m.db"), price_drop_percent=10)
            try:
                _seed_aes(se, self.ASIN, in_stock=0, price=149.99)
                alerts, _ = _process(se, [_aes_row(self.ASIN, price=149.99)])
                self.assertEqual(alerts, [])
            finally:
                se.conn.close()

    def test_back_in_stock_below_target_alerts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            se = StateEngine(str(Path(tmp) / "m.db"), price_drop_percent=10)
            try:
                _seed_aes(se, self.ASIN, in_stock=0, price=119.99)
                alerts, _ = _process(se, [_aes_row(self.ASIN, price=115.00)])
                self.assertEqual([a["type"] for a in alerts], ["back_in_stock"])
            finally:
                se.conn.close()

    def test_price_drop_above_target_suppressed(self) -> None:
        # 200 -> 150 is a 25% drop that would normally alert, but 150 > target 120.
        with tempfile.TemporaryDirectory() as tmp:
            se = StateEngine(str(Path(tmp) / "m.db"), price_drop_percent=10)
            try:
                _seed_aes(se, self.ASIN, in_stock=1, price=200.0)
                alerts, _ = _process(se, [_aes_row(self.ASIN, price=150.0)])
                self.assertEqual(alerts, [])
            finally:
                se.conn.close()


class TestAesTargetCrossing(unittest.TestCase):
    ASIN = "B0F6PQLR16"

    def test_crossing_below_target_fires_price_below_target(self) -> None:
        # 125 -> 118 is only a 5.6% drop (no price_drop), but it crosses the 120
        # target — exactly the moment the client wants.
        with tempfile.TemporaryDirectory() as tmp:
            se = StateEngine(str(Path(tmp) / "m.db"), price_drop_percent=10)
            try:
                _seed_aes(se, self.ASIN, in_stock=1, price=125.0)
                alerts, _ = _process(se, [_aes_row(self.ASIN, price=118.0)])
                self.assertEqual([a["type"] for a in alerts], ["price_below_target"])
                self.assertEqual(alerts[0]["target_price"], 120.0)
            finally:
                se.conn.close()

    def test_steady_below_target_does_not_refire(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            se = StateEngine(str(Path(tmp) / "m.db"), price_drop_percent=10)
            try:
                _seed_aes(se, self.ASIN, in_stock=1, price=118.0)
                alerts, _ = _process(se, [_aes_row(self.ASIN, price=118.0)])
                self.assertEqual(alerts, [])
            finally:
                se.conn.close()

    def test_crossing_respects_target_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            se = StateEngine(str(Path(tmp) / "m.db"), price_drop_percent=10)
            try:
                _seed_aes(se, self.ASIN, in_stock=1, price=125.0)
                se.conn.execute(
                    """
                    INSERT INTO alerts (asin, alert_type, source, old_price, new_price, sent_at)
                    VALUES (?, 'price_below_target', 'aes_llc', NULL, 119.0, ?)
                    """,
                    (self.ASIN, (utc_now() - timedelta(hours=2)).isoformat()),
                )
                se.conn.commit()
                alerts, _ = _process(se, [_aes_row(self.ASIN, price=118.0)])
                self.assertEqual(alerts, [])
            finally:
                se.conn.close()

    def test_price_drop_wins_over_crossing_same_cycle(self) -> None:
        # 200 -> 110: a 45% drop that also crosses the target — one alert, not two.
        with tempfile.TemporaryDirectory() as tmp:
            se = StateEngine(str(Path(tmp) / "m.db"), price_drop_percent=10)
            try:
                _seed_aes(se, self.ASIN, in_stock=1, price=200.0)
                alerts, _ = _process(se, [_aes_row(self.ASIN, price=110.0)])
                self.assertEqual([a["type"] for a in alerts], ["price_drop"])
            finally:
                se.conn.close()


if __name__ == "__main__":
    unittest.main()
