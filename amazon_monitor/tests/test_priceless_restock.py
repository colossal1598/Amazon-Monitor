"""C3: a purchasable buybox that renders no price should confirm over a couple of
checks and fire a priceless back_in_stock alert, respecting preorder suppression and
the normal stock-alert cooldown. The alert carries no price; the webhook template must
render it gracefully."""

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from state_engine import StateEngine, utc_now
from webhook_sender import _format_message

_CONFIG = {
    "cross_source_alert_dedupe_minutes": 15,
    "stock_alert_cooldown_minutes": 60,
    "stock_alert_confirmed_cooldown_minutes": 10,
    "pdp_priceless_restock_alert": True,
    "pdp_priceless_confirm_checks": 2,
}


def _priceless_row(asin: str, *, is_preorder: bool = False) -> dict:
    return {
        "asin": asin,
        "title": "Pokemon Card",
        "price": None,
        "in_stock": False,
        "stock_confidence": "unknown",
        "stock_reason": "priceless_purchasable",
        "buybox_purchasable": True,
        "is_preorder": is_preorder,
        "shipping_text": "FREE delivery",
        "image_url": None,
        "seller": "pdp_watch",
        "seller_text": "Sold by Amazon.com",
        "source": "pdp_watch",
    }


def _in_stock_row(asin: str, price: float = 19.99) -> dict:
    return {
        "asin": asin,
        "title": "Pokemon Card",
        "price": price,
        "in_stock": True,
        "stock_confidence": "confirmed_in",
        "shipping_text": "FREE delivery",
        "image_url": None,
        "seller": "pdp_watch",
        "source": "pdp_watch",
    }


def _seed(
    se: StateEngine,
    asin: str,
    *,
    in_stock: int = 0,
    is_preorder: int = 0,
    last_oos_confirmed: int = 0,
    priceless_streak: int = 0,
) -> None:
    now = "2020-01-01T00:00:00+00:00"
    se.conn.execute(
        """
        INSERT INTO products
        (asin, title, seller, price, in_stock, first_seen, last_seen,
         is_preorder, last_oos_confirmed, priceless_streak)
        VALUES (?, 'Pokemon Card', 'pdp_watch', NULL, ?, ?, ?, ?, ?, ?)
        """,
        (asin, in_stock, now, now, is_preorder, last_oos_confirmed, priceless_streak),
    )
    se.conn.commit()


def _row(se: StateEngine, asin: str):
    return se.conn.execute(
        "SELECT in_stock, price, priceless_streak, last_stock_alert FROM products WHERE asin = ?",
        (asin,),
    ).fetchone()


class _FakeTelemetry:
    """Captures debug() events so tests can assert on emitted telemetry."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def debug(self, cycle_id: int, event: str, *, asin=None, **fields) -> None:
        self.events.append((event, {"asin": asin, **fields}))


class TestPricelessRestock(unittest.TestCase):
    def test_streak_confirms_and_alerts_with_null_price(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            se = StateEngine(str(Path(tmp) / "m.db"), price_drop_percent=10)
            try:
                asin = "B011111111"
                _seed(se, asin, in_stock=0)
                alerts1, _ = se.process_pdp_watch_candidates(
                    [_priceless_row(asin)], {asin}, config=_CONFIG
                )
                self.assertEqual(alerts1, [])
                self.assertEqual(int(_row(se, asin)["priceless_streak"]), 1)

                alerts2, _ = se.process_pdp_watch_candidates(
                    [_priceless_row(asin)], {asin}, config=_CONFIG
                )
                self.assertEqual([a["type"] for a in alerts2], ["back_in_stock"])
                self.assertIsNone(alerts2[0]["price"])
                row = _row(se, asin)
                self.assertEqual(int(row["in_stock"]), 1)
                self.assertIsNone(row["price"])
                self.assertEqual(int(row["priceless_streak"]), 0)
                self.assertIsNotNone(row["last_stock_alert"])
            finally:
                se.conn.close()

    def test_no_alert_when_already_in_stock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            se = StateEngine(str(Path(tmp) / "m.db"), price_drop_percent=10)
            try:
                asin = "B011111111"
                _seed(se, asin, in_stock=1, priceless_streak=5)
                alerts, _ = se.process_pdp_watch_candidates(
                    [_priceless_row(asin)], {asin}, config=_CONFIG
                )
                self.assertEqual(alerts, [])
            finally:
                se.conn.close()

    def test_config_flag_disables_priceless_alert(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            se = StateEngine(str(Path(tmp) / "m.db"), price_drop_percent=10)
            config = dict(_CONFIG, pdp_priceless_restock_alert=False)
            try:
                asin = "B011111111"
                _seed(se, asin, in_stock=0)
                se.process_pdp_watch_candidates([_priceless_row(asin)], {asin}, config=config)
                alerts, _ = se.process_pdp_watch_candidates(
                    [_priceless_row(asin)], {asin}, config=config
                )
                self.assertEqual(alerts, [])
                # Streak never accrues while the feature is off.
                self.assertEqual(int(_row(se, asin)["priceless_streak"]), 0)
            finally:
                se.conn.close()

    def test_preorder_reopen_alerts_by_default_when_prior_oos_unconfirmed(self) -> None:
        # Default config (suppression OFF): a preorder->preorder priceless restock after
        # an UNCONFIRMED OOS now alerts like any other restock. The pdp_oos_confirm_cycles
        # debounce already blocks the weak flapping this gate was built against.
        with tempfile.TemporaryDirectory() as tmp:
            se = StateEngine(str(Path(tmp) / "m.db"), price_drop_percent=10)
            try:
                asin = "B011111111"
                _seed(se, asin, in_stock=0, is_preorder=1, last_oos_confirmed=0)
                se.process_pdp_watch_candidates(
                    [_priceless_row(asin, is_preorder=True)], {asin}, config=_CONFIG
                )
                alerts, _ = se.process_pdp_watch_candidates(
                    [_priceless_row(asin, is_preorder=True)], {asin}, config=_CONFIG
                )
                self.assertEqual([a["type"] for a in alerts], ["back_in_stock"])
            finally:
                se.conn.close()

    def test_preorder_reopen_suppressed_when_flag_enabled(self) -> None:
        # Opt-in rollback: pdp_preorder_realert_suppression=True restores the old gate,
        # suppressing preorder->preorder restock after an unconfirmed OOS and emitting the
        # pdp_preorder_realert_suppressed telemetry event instead of an alert.
        with tempfile.TemporaryDirectory() as tmp:
            se = StateEngine(str(Path(tmp) / "m.db"), price_drop_percent=10)
            config = dict(_CONFIG, pdp_preorder_realert_suppression=True)
            tel = _FakeTelemetry()
            try:
                asin = "B011111111"
                _seed(se, asin, in_stock=0, is_preorder=1, last_oos_confirmed=0)
                se.process_pdp_watch_candidates(
                    [_priceless_row(asin, is_preorder=True)], {asin}, config=config,
                    telemetry=tel, cycle_id=1,
                )
                alerts, _ = se.process_pdp_watch_candidates(
                    [_priceless_row(asin, is_preorder=True)], {asin}, config=config,
                    telemetry=tel, cycle_id=1,
                )
                self.assertEqual(alerts, [])
                self.assertIn(
                    "pdp_preorder_realert_suppressed", [e[0] for e in tel.events]
                )
            finally:
                se.conn.close()

    def test_preorder_reopen_alerts_when_prior_oos_confirmed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            se = StateEngine(str(Path(tmp) / "m.db"), price_drop_percent=10)
            try:
                asin = "B011111111"
                _seed(se, asin, in_stock=0, is_preorder=1, last_oos_confirmed=1)
                se.process_pdp_watch_candidates(
                    [_priceless_row(asin, is_preorder=True)], {asin}, config=_CONFIG
                )
                alerts, _ = se.process_pdp_watch_candidates(
                    [_priceless_row(asin, is_preorder=True)], {asin}, config=_CONFIG
                )
                self.assertEqual([a["type"] for a in alerts], ["back_in_stock"])
            finally:
                se.conn.close()

    def test_priceless_alert_respects_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            se = StateEngine(str(Path(tmp) / "m.db"), price_drop_percent=10)
            try:
                asin = "B011111111"
                _seed(se, asin, in_stock=0, last_oos_confirmed=0)
                se.conn.execute(
                    """
                    INSERT INTO alerts (asin, alert_type, source, old_price, new_price, sent_at)
                    VALUES (?, 'back_in_stock', 'pdp_watch', NULL, 19.99, ?)
                    """,
                    (asin, (utc_now() - timedelta(minutes=5)).isoformat()),
                )
                se.conn.commit()
                se.process_pdp_watch_candidates([_priceless_row(asin)], {asin}, config=_CONFIG)
                alerts, _ = se.process_pdp_watch_candidates(
                    [_priceless_row(asin)], {asin}, config=_CONFIG
                )
                self.assertEqual(alerts, [])
                # Suppressed, not consumed: row stays OOS so it can fire once cooldown clears.
                self.assertEqual(int(_row(se, asin)["in_stock"]), 0)
            finally:
                se.conn.close()

    def test_streak_resets_on_non_priceless_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            se = StateEngine(str(Path(tmp) / "m.db"), price_drop_percent=10)
            try:
                asin = "B011111111"
                _seed(se, asin, in_stock=0)
                se.process_pdp_watch_candidates([_priceless_row(asin)], {asin}, config=_CONFIG)
                self.assertEqual(int(_row(se, asin)["priceless_streak"]), 1)
                # A confirmed in-stock scrape resolves the ASIN; streak must reset.
                se.process_pdp_watch_candidates([_in_stock_row(asin)], {asin}, config=_CONFIG)
                self.assertEqual(int(_row(se, asin)["priceless_streak"]), 0)
            finally:
                se.conn.close()


class TestNullPriceWebhookRender(unittest.TestCase):
    def test_back_in_stock_with_null_price_renders_gracefully(self) -> None:
        # A priceless restock alert has price=None; the template must not crash and
        # must render the existing "not available" wording instead of a blank/None.
        message = _format_message(
            {"type": "back_in_stock", "asin": "B011111111", "title": "Pokemon Card", "price": None},
            {},
        )
        self.assertIn("Back in stock!", message)
        # Price line falls back to the existing "not available" wording, not "None".
        self.assertIn("Price: לא זמין", message)


if __name__ == "__main__":
    unittest.main()
