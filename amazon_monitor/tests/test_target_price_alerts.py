"""Per-ASIN target-price alerts (`price_below_target`) fired from process_pdp_watch_candidates.

Covers: crossing below target fires, staying at/above target doesn't, hysteresis
prevents repeat firing while still below, re-arm + cooldown gate re-firing after a
bounce back above target, no target configured means no alert, and paid shipping
does not suppress the alert (only in_stock/qualifies, driven by pdp_scraper's
seller_ok + shippable checks, matters).
"""

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from state_engine import StateEngine, utc_now

ASIN = "B011111111"

_BASE_CONFIG = {
    "pdp_watch_target_prices": {ASIN: 20.0},
    "target_price_alert_cooldown_hours": 6,
}


def _seed_row(se: StateEngine, asin: str, *, in_stock: int, price: float = 25.0) -> None:
    now = "2020-01-01T00:00:00+00:00"
    se.conn.execute(
        """
        INSERT INTO products (asin, title, seller, price, in_stock, first_seen, last_seen)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (asin, "Pokemon Card", "pdp_watch", price, in_stock, now, now),
    )
    se.conn.commit()


def _pdp_row(asin: str, price: float, *, shipping_text: str = "FREE delivery") -> dict:
    """A qualifying PDP row (allowed seller, shippable, has a price) as pdp_scraper._pdp_row builds it."""
    return {
        "asin": asin,
        "title": "Pokemon Card",
        "price": price,
        "in_stock": True,
        "shipping_text": shipping_text,
        "image_url": None,
        "seller": "pdp_watch",
        "stock_confidence": "confirmed_in",
        "source": "pdp_watch",
    }


def _target_alerts(alerts: list[dict]) -> list[dict]:
    return [a for a in alerts if a.get("type") == "price_below_target"]


def _target_alert_state(se: StateEngine, asin: str) -> tuple[int, str | None]:
    row = se.conn.execute(
        "SELECT target_alert_armed, target_alert_last_sent FROM products WHERE asin = ?", (asin,)
    ).fetchone()
    return int(row[0]), (None if row[1] is None else str(row[1]))


class TestTargetPriceAlerts(unittest.TestCase):
    def test_fires_when_price_crosses_below_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            se = StateEngine(str(Path(tmp) / "m.db"), price_drop_percent=10)
            try:
                _seed_row(se, ASIN, in_stock=1, price=25.0)
                alerts, _ = se.process_pdp_watch_candidates(
                    [_pdp_row(ASIN, 15.0)], {ASIN}, config=_BASE_CONFIG
                )
                target_alerts = _target_alerts(alerts)
                self.assertEqual(len(target_alerts), 1)
                alert = target_alerts[0]
                self.assertEqual(alert["price"], 15.0)
                self.assertEqual(alert["target_price"], 20.0)
                self.assertEqual(alert["asin"], ASIN)
                armed, last_sent = _target_alert_state(se, ASIN)
                self.assertEqual(armed, 0)
                self.assertIsNotNone(last_sent)
            finally:
                se.conn.close()

    def test_does_not_fire_when_price_at_or_above_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            se = StateEngine(str(Path(tmp) / "m.db"), price_drop_percent=10)
            try:
                _seed_row(se, ASIN, in_stock=1, price=25.0)
                alerts, _ = se.process_pdp_watch_candidates(
                    [_pdp_row(ASIN, 20.0)], {ASIN}, config=_BASE_CONFIG
                )
                self.assertEqual(_target_alerts(alerts), [])

                alerts, _ = se.process_pdp_watch_candidates(
                    [_pdp_row(ASIN, 22.0)], {ASIN}, config=_BASE_CONFIG
                )
                self.assertEqual(_target_alerts(alerts), [])
                armed, last_sent = _target_alert_state(se, ASIN)
                self.assertEqual(armed, 1)
                self.assertIsNone(last_sent)
            finally:
                se.conn.close()

    def test_does_not_fire_twice_while_price_stays_below(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            se = StateEngine(str(Path(tmp) / "m.db"), price_drop_percent=10)
            try:
                _seed_row(se, ASIN, in_stock=1, price=25.0)
                first, _ = se.process_pdp_watch_candidates(
                    [_pdp_row(ASIN, 15.0)], {ASIN}, config=_BASE_CONFIG
                )
                self.assertEqual(len(_target_alerts(first)), 1)

                second, _ = se.process_pdp_watch_candidates(
                    [_pdp_row(ASIN, 14.0)], {ASIN}, config=_BASE_CONFIG
                )
                self.assertEqual(_target_alerts(second), [])

                third, _ = se.process_pdp_watch_candidates(
                    [_pdp_row(ASIN, 16.0)], {ASIN}, config=_BASE_CONFIG
                )
                self.assertEqual(_target_alerts(third), [])
            finally:
                se.conn.close()

    def test_rearms_after_price_recovers_and_respects_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            se = StateEngine(str(Path(tmp) / "m.db"), price_drop_percent=10)
            try:
                _seed_row(se, ASIN, in_stock=1, price=25.0)
                first, _ = se.process_pdp_watch_candidates(
                    [_pdp_row(ASIN, 15.0)], {ASIN}, config=_BASE_CONFIG
                )
                self.assertEqual(len(_target_alerts(first)), 1)

                # Price recovers back above target: re-arms the latch.
                recovered, _ = se.process_pdp_watch_candidates(
                    [_pdp_row(ASIN, 25.0)], {ASIN}, config=_BASE_CONFIG
                )
                self.assertEqual(_target_alerts(recovered), [])
                armed, _ = _target_alert_state(se, ASIN)
                self.assertEqual(armed, 1)

                # Re-crossing below target immediately (well inside the 6h cooldown
                # since the first alert) must NOT re-fire despite being re-armed.
                recrossed, _ = se.process_pdp_watch_candidates(
                    [_pdp_row(ASIN, 15.0)], {ASIN}, config=_BASE_CONFIG
                )
                self.assertEqual(_target_alerts(recrossed), [])

                # Once the cooldown has clearly elapsed, back-dating last_sent proves
                # the gate is timestamp-driven rather than a permanent latch.
                se.conn.execute(
                    "UPDATE products SET target_alert_last_sent = ? WHERE asin = ?",
                    ((utc_now() - timedelta(hours=7)).isoformat(), ASIN),
                )
                se.conn.commit()
                later, _ = se.process_pdp_watch_candidates(
                    [_pdp_row(ASIN, 15.0)], {ASIN}, config=_BASE_CONFIG
                )
                self.assertEqual(len(_target_alerts(later)), 1)
            finally:
                se.conn.close()

    def test_no_target_configured_means_no_alert(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            se = StateEngine(str(Path(tmp) / "m.db"), price_drop_percent=10)
            try:
                _seed_row(se, ASIN, in_stock=1, price=25.0)
                config = {"pdp_watch_target_prices": {}, "target_price_alert_cooldown_hours": 6}
                alerts, _ = se.process_pdp_watch_candidates(
                    [_pdp_row(ASIN, 1.0)], {ASIN}, config=config
                )
                self.assertEqual(_target_alerts(alerts), [])
            finally:
                se.conn.close()

    def test_paid_shipping_does_not_block_alert(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            se = StateEngine(str(Path(tmp) / "m.db"), price_drop_percent=10)
            try:
                _seed_row(se, ASIN, in_stock=1, price=25.0)
                alerts, _ = se.process_pdp_watch_candidates(
                    [_pdp_row(ASIN, 15.0, shipping_text="$7.99 delivery")],
                    {ASIN},
                    config=_BASE_CONFIG,
                )
                target_alerts = _target_alerts(alerts)
                self.assertEqual(len(target_alerts), 1)
                self.assertIn("shipping", target_alerts[0])
            finally:
                se.conn.close()

    def test_first_observation_already_below_target_fires(self) -> None:
        """A brand-new watched ASIN whose very first qualifying scrape is already
        below target should alert immediately (no need to see a prior higher price)."""
        with tempfile.TemporaryDirectory() as tmp:
            se = StateEngine(str(Path(tmp) / "m.db"), price_drop_percent=10)
            try:
                alerts, _ = se.process_pdp_watch_candidates(
                    [_pdp_row(ASIN, 5.0)], {ASIN}, config=_BASE_CONFIG
                )
                target_alerts = _target_alerts(alerts)
                self.assertEqual(len(target_alerts), 1)
                self.assertEqual(target_alerts[0]["target_price"], 20.0)
            finally:
                se.conn.close()


if __name__ == "__main__":
    unittest.main()
