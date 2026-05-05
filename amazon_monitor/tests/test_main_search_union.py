import unittest

from search_union import merge_search_candidates_by_asin, should_reconcile_missing_asins


class TestMainSearchUnion(unittest.TestCase):
    def test_merge_normalizes_asin_and_presence_stock(self) -> None:
        rows = merge_search_candidates_by_asin(
            [
                {
                    "asin": "b012345678",
                    "title": "Pokemon TCG Item",
                    "price": 19.99,
                    "in_stock": False,
                    "shipping_text": "FREE delivery",
                }
            ],
            [{"asin": "not-an-asin", "title": "Invalid", "in_stock": True}],
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["asin"], "B012345678")
        self.assertTrue(rows[0]["in_stock"])
        self.assertEqual(rows[0]["title"], "Pokemon TCG Item")

    def test_merge_prefers_truthy_stock_duplicate_metadata(self) -> None:
        rows = merge_search_candidates_by_asin(
            [
                {
                    "asin": "B012345678",
                    "title": "Ambiguous SERP Row",
                    "price": 24.99,
                    "in_stock": False,
                }
            ],
            [
                {
                    "asin": "B012345678",
                    "title": "Explicit In Stock SERP Row",
                    "price": 21.99,
                    "in_stock": True,
                }
            ],
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "Explicit In Stock SERP Row")
        self.assertEqual(rows[0]["price"], 21.99)
        self.assertTrue(rows[0]["in_stock"])

    def test_reconcile_decision_uses_candidate_count_threshold(self) -> None:
        config = {"enable_missing_asin_oos": True, "min_results_for_absence_reconcile": 2}

        should_skip, reason = should_reconcile_missing_asins(config, 1)
        self.assertFalse(should_skip)
        self.assertEqual(reason, "filtered_count_below_min:1<2")

        should_reconcile, reason = should_reconcile_missing_asins(config, 2)
        self.assertTrue(should_reconcile)
        self.assertIsNone(reason)


if __name__ == "__main__":
    unittest.main()
