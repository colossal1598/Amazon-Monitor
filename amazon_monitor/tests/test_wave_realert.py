"""C9: clean seller_mismatch (a 3P seller holds the buybox) is now debounced OOS
evidence. Two consecutive of them flip the DB to out-of-stock AND arm the confirmed
transition flag, so the next Amazon allocation wave re-alerts on the SHORT confirmed
cooldown instead of being swallowed by the long weak cooldown. Previously the row
stayed unknown, the DB never flipped, and no 0->1 edge fired for repeat waves."""

import tempfile
import unittest
from pathlib import Path

from state_engine import StateEngine

# Confirmed cooldown 0 (never suppresses) vs weak cooldown 60 (would suppress a
# repeat within the hour): the distinction proves which cooldown the re-alert used.
_CONFIG = {
    "cross_source_alert_dedupe_minutes": 15,
    "stock_alert_cooldown_minutes": 60,
    "stock_alert_confirmed_cooldown_minutes": 0,
    "pdp_oos_confirm_cycles": 2,
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


def _mismatch_row(asin: str) -> dict:
    # Mirrors what _pdp_row emits for a parsed price + non-empty 3P blob: a settled
    # confirmed_out with reason seller_mismatch (weak -> state-engine debounce applies).
    return {
        "asin": asin,
        "title": "Pokemon Card",
        "price": None,
        "in_stock": False,
        "stock_confidence": "confirmed_out",
        "stock_reason": "seller_mismatch",
        "buybox_purchasable": False,
        "shipping_text": "",
        "image_url": None,
        "seller": "pdp_watch",
        "seller_text": "Sold by Third Party LLC",
        "source": "pdp_watch",
    }


def _seed(se: StateEngine, asin: str, *, in_stock: int = 0) -> None:
    now = "2020-01-01T00:00:00+00:00"
    se.conn.execute(
        """
        INSERT INTO products
        (asin, title, seller, price, in_stock, first_seen, last_seen)
        VALUES (?, 'Pokemon Card', 'pdp_watch', NULL, ?, ?, ?)
        """,
        (asin, in_stock, now, now),
    )
    se.conn.commit()


def _row(se: StateEngine, asin: str):
    return se.conn.execute(
        "SELECT in_stock, last_oos_confirmed, oos_miss_streak FROM products WHERE asin = ?",
        (asin,),
    ).fetchone()


class TestWaveRealert(unittest.TestCase):
    def test_single_mismatch_does_not_flip(self) -> None:
        # Debounce: one seller_mismatch after an in-stock row is not enough to flip.
        with tempfile.TemporaryDirectory() as tmp:
            se = StateEngine(str(Path(tmp) / "m.db"), price_drop_percent=10)
            try:
                asin = "B011111111"
                _seed(se, asin, in_stock=0)
                se.process_pdp_watch_candidates([_in_stock_row(asin)], {asin}, config=_CONFIG)
                self.assertEqual(int(_row(se, asin)["in_stock"]), 1)

                se.process_pdp_watch_candidates([_mismatch_row(asin)], {asin}, config=_CONFIG)
                row = _row(se, asin)
                self.assertEqual(int(row["in_stock"]), 1)  # no flip yet
                self.assertEqual(int(row["oos_miss_streak"]), 1)
                self.assertEqual(int(row["last_oos_confirmed"] or 0), 0)
            finally:
                se.conn.close()

    def test_wave_cycle_realerts_on_confirmed_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            se = StateEngine(str(Path(tmp) / "m.db"), price_drop_percent=10)
            try:
                asin = "B011111111"
                _seed(se, asin, in_stock=0)

                # (a) Amazon wave 1: in-stock. Fires the initial back_in_stock alert,
                # which sits inside the 60-min weak-cooldown window for the rest of the test.
                alerts_a, _ = se.process_pdp_watch_candidates(
                    [_in_stock_row(asin)], {asin}, config=_CONFIG
                )
                self.assertEqual([a["type"] for a in alerts_a], ["back_in_stock"])
                self.assertEqual(int(_row(se, asin)["in_stock"]), 1)

                # (b) Wave ends, a 3P seller takes the buybox for two consecutive checks.
                se.process_pdp_watch_candidates([_mismatch_row(asin)], {asin}, config=_CONFIG)
                se.process_pdp_watch_candidates([_mismatch_row(asin)], {asin}, config=_CONFIG)
                row_b = _row(se, asin)
                self.assertEqual(int(row_b["in_stock"]), 0)  # flipped OOS
                self.assertEqual(int(row_b["last_oos_confirmed"]), 1)  # confirmed transition armed

                # (c) Amazon wave 2: in-stock again. A prior back_in_stock alert exists
                # within the 60-min weak window, so this only fires because it used the
                # short confirmed cooldown (0 min) -> proves the confirmed path.
                alerts_c, _ = se.process_pdp_watch_candidates(
                    [_in_stock_row(asin)], {asin}, config=_CONFIG
                )
                self.assertEqual([a["type"] for a in alerts_c], ["back_in_stock"])
                self.assertEqual(int(_row(se, asin)["in_stock"]), 1)
            finally:
                se.conn.close()


if __name__ == "__main__":
    unittest.main()
