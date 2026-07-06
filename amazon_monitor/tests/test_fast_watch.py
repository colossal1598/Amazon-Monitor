import tempfile
import unittest
from pathlib import Path

from fast_watch import _row_for_state_engine, parse_aod_response
from state_engine import StateEngine

_ALLOWED = ["amazon.com", "amazon export"]


def _aod_in_stock_html(price: str = "39.99", seller: str = "Amazon.com") -> str:
    return f"""
    <div id="all-offers-display">
      <span id="aod-asin-title-text">Pokemon TCG Scarlet &amp; Violet Booster Bundle</span>
      <input id="aod-total-offer-count" value="3" type="hidden">
      <div id="aod-pinned-offer" class="aod-offer">
        <span class="a-price"><span class="a-offscreen">${price}</span></span>
        <div id="aod-offer-shipsFrom"><span>Ships from</span> <span>Amazon.com</span></div>
        <div id="aod-offer-soldBy"><span>Sold by</span> <a href="#">{seller}</a></div>
        <div class="aod-delivery"><span>FREE delivery July 12 - 30</span></div>
      </div>
      <div id="aod-offer-list"><div class="aod-offer">other offer</div></div>
    </div>
    """


def _aod_no_offers_html() -> str:
    return """
    <div id="all-offers-display">
      <span id="aod-asin-title-text">Pokemon TCG Elite Trainer Box</span>
      <input id="aod-total-offer-count" value="0" type="hidden">
      <div id="aod-no-offer">There are currently no listings for this product.</div>
    </div>
    """


def _captcha_html() -> str:
    return """
    <html><body>
      <h4>Enter the characters you see below</h4>
      <form action="/errors/validateCaptcha"></form>
    </body></html>
    """


class TestParseAodResponse(unittest.TestCase):
    def test_in_stock_amazon_seller(self) -> None:
        parsed = parse_aod_response(_aod_in_stock_html(), _ALLOWED)
        self.assertEqual(parsed["status"], "in")
        self.assertEqual(parsed["price"], 39.99)
        self.assertIn("Booster Bundle", parsed["title"])
        self.assertIn("FREE delivery", parsed["shipping_text"])

    def test_price_with_thousands_separator(self) -> None:
        parsed = parse_aod_response(_aod_in_stock_html(price="1,299.00"), _ALLOWED)
        self.assertEqual(parsed["status"], "in")
        self.assertEqual(parsed["price"], 1299.00)

    def test_third_party_seller_is_unknown_not_oos(self) -> None:
        parsed = parse_aod_response(_aod_in_stock_html(seller="SomeCardShop LLC"), _ALLOWED)
        self.assertEqual(parsed["status"], "unknown")
        self.assertEqual(parsed["reason"], "seller_mismatch")

    def test_no_offers_is_confirmed_out(self) -> None:
        parsed = parse_aod_response(_aod_no_offers_html(), _ALLOWED)
        self.assertEqual(parsed["status"], "out")
        self.assertEqual(parsed["reason"], "no_offers")

    def test_captcha_detected(self) -> None:
        parsed = parse_aod_response(_captcha_html(), _ALLOWED)
        self.assertEqual(parsed["status"], "captcha")

    def test_empty_body_is_unknown(self) -> None:
        parsed = parse_aod_response("", _ALLOWED)
        self.assertEqual(parsed["status"], "unknown")

    def test_missing_pinned_offer_is_unknown(self) -> None:
        html = '<div id="all-offers-display"><input id="aod-total-offer-count" value="2"></div>'
        parsed = parse_aod_response(html, _ALLOWED)
        self.assertEqual(parsed["status"], "unknown")
        self.assertEqual(parsed["reason"], "no_pinned_offer")


class TestRowMapping(unittest.TestCase):
    def test_in_row_shape(self) -> None:
        parsed = parse_aod_response(_aod_in_stock_html(), _ALLOWED)
        row = _row_for_state_engine("B011111111", parsed)
        self.assertIsNotNone(row)
        self.assertTrue(row["in_stock"])
        self.assertEqual(row["stock_confidence"], "confirmed_in")
        self.assertEqual(row["source"], "fast_watch")

    def test_out_row_is_strong_oos(self) -> None:
        parsed = parse_aod_response(_aod_no_offers_html(), _ALLOWED)
        row = _row_for_state_engine("B011111111", parsed)
        self.assertIsNotNone(row)
        self.assertFalse(row["in_stock"])
        self.assertEqual(row["stock_confidence"], "confirmed_out")
        self.assertEqual(row["stock_reason"], "explicit_oos")

    def test_unknown_maps_to_none(self) -> None:
        parsed = parse_aod_response("", _ALLOWED)
        self.assertIsNone(_row_for_state_engine("B011111111", parsed))


class TestFastWatchStateIntegration(unittest.TestCase):
    def test_fast_lane_sellout_then_restock_alerts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            se = StateEngine(str(Path(tmp) / "m.db"), price_drop_percent=10)
            config = {
                "cross_source_alert_dedupe_minutes": 15,
                "stock_alert_cooldown_minutes": 60,
                "stock_alert_confirmed_cooldown_minutes": 10,
            }
            try:
                asin = "B011111111"
                # Seed as in stock (e.g. from the browser lane).
                now = "2020-01-01T00:00:00+00:00"
                se.conn.execute(
                    """
                    INSERT INTO products (asin, title, seller, price, in_stock, first_seen, last_seen)
                    VALUES (?, 'Pokemon Card', 'pdp_watch', 19.99, 1, ?, ?)
                    """,
                    (asin, now, now),
                )
                se.conn.commit()

                # Fast lane sees zero offers -> confirmed OOS.
                oos_row = _row_for_state_engine(asin, parse_aod_response(_aod_no_offers_html(), _ALLOWED))
                alerts, _ = se.process_pdp_watch_candidates([oos_row], {asin}, source="fast_watch", config=config)
                self.assertEqual(alerts, [])
                db = se.conn.execute(
                    "SELECT in_stock, last_oos_confirmed FROM products WHERE asin = ?", (asin,)
                ).fetchone()
                self.assertEqual((int(db[0]), int(db[1])), (0, 1))

                # Fast lane sees the pinned Amazon offer again -> back_in_stock.
                in_row = _row_for_state_engine(asin, parse_aod_response(_aod_in_stock_html(), _ALLOWED))
                alerts, _ = se.process_pdp_watch_candidates([in_row], {asin}, source="fast_watch", config=config)
                self.assertEqual([a["type"] for a in alerts], ["back_in_stock"])
                self.assertEqual(alerts[0]["source"], "fast_watch")

                # Browser lane confirms in-stock a minute later: state already 1 -> no duplicate.
                pdp_row = {
                    "asin": asin,
                    "title": "Pokemon Card",
                    "price": 39.99,
                    "in_stock": True,
                    "stock_confidence": "confirmed_in",
                    "shipping_text": "FREE delivery",
                    "image_url": None,
                    "seller": "pdp_watch",
                    "source": "pdp_watch",
                }
                alerts, _ = se.process_pdp_watch_candidates([pdp_row], {asin}, config=config)
                self.assertEqual(alerts, [])
            finally:
                se.conn.close()


if __name__ == "__main__":
    unittest.main()
