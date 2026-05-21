import unittest

from alert_dedupe import dedupe_alerts_by_asin


class TestDedupeAlertsByAsin(unittest.TestCase):
    def test_prefers_back_in_stock_over_price_drop(self) -> None:
        alerts = [
            {"type": "price_drop", "asin": "B0999999999"},
            {"type": "back_in_stock", "asin": "B0999999999"},
        ]
        out = dedupe_alerts_by_asin(alerts)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["type"], "back_in_stock")

    def test_stable_sort_by_asin(self) -> None:
        alerts = [
            {"type": "new_product", "asin": "B0888888888"},
            {"type": "new_product", "asin": "B0777777777"},
        ]
        out = dedupe_alerts_by_asin(alerts)
        self.assertEqual([a["asin"] for a in out], ["B0777777777", "B0888888888"])

    def test_cross_source_same_asin_dedupes_to_single_back_in_stock(self) -> None:
        alerts = [
            {"type": "back_in_stock", "asin": "B066666666", "source": "pdp_watch"},
            {"type": "back_in_stock", "asin": "B066666666", "source": "aes_llc"},
            {"type": "price_drop", "asin": "B066666666", "source": "aes_llc"},
        ]
        out = dedupe_alerts_by_asin(alerts)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["type"], "back_in_stock")
