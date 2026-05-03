"""YAML whitelist/blacklist ASIN behavior in run_search_filter_pipeline."""

import unittest

from filter_pipeline import run_search_filter_pipeline


def _serp_row(
    asin: str,
    *,
    title: str,
    price: float = 19.99,
    shipping_text: str = "FREE delivery",
) -> dict:
    return {
        "asin": asin,
        "title": title,
        "price": price,
        "price_text": f"${price}",
        "in_stock": True,
        "shipping_text": shipping_text,
        "seller_text": "",
        "availability_text": "",
        "search_url": "https://www.amazon.com/s?k=test",
    }


class YamlAsinListPipelineTests(unittest.TestCase):
    def test_whitelist_adds_raw_row_that_failed_stage1(self) -> None:
        raw = [
            _serp_row(
                "B222222222",
                title="Magic The Gathering Starter Deck",
            )
        ]
        cfg = {"required_keywords": ["pokemon", "tcg"], "whitelist": ["B222222222"], "blacklist": []}
        rows, meta = run_search_filter_pipeline(raw, cfg)
        self.assertEqual(meta["stage1_core_count"], 0)
        self.assertEqual(meta["stage1_after_whitelist_merge"], 1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["asin"], "B222222222")

    def test_required_keywords_apply_to_non_whitelist(self) -> None:
        raw = [
            _serp_row(
                "B111111111",
                title="Pokemon Scarlet Violet Booster Pack",
            )
        ]
        cfg = {
            "required_keywords": ["pokemon", "musthavexyz"],
            "whitelist": [],
            "blacklist": [],
        }
        rows, meta = run_search_filter_pipeline(raw, cfg)
        self.assertGreaterEqual(meta["stage1_core_count"], 1)
        self.assertEqual(len(rows), 0)
        drops = meta.get("blacklist_kw_drops") or []
        self.assertTrue(any(d.get("reason") == "required_keyword_missing" for d in drops))

    def test_yaml_blacklist_drops_after_merge(self) -> None:
        raw = [
            _serp_row(
                "B444444444",
                title="Pokemon Scarlet Violet Booster Pack",
            )
        ]
        cfg = {
            "required_keywords": ["pokemon", "tcg"],
            "whitelist": [],
            "blacklist": ["B444444444"],
        }
        rows, meta = run_search_filter_pipeline(raw, cfg)
        self.assertEqual(len(rows), 0)
        drops = meta.get("blacklist_kw_drops") or []
        self.assertTrue(any(d.get("reason") == "yaml_blacklist_asin" for d in drops))

    def test_blacklist_wins_over_whitelist(self) -> None:
        raw = [
            _serp_row(
                "B555555555",
                title="Unrelated Product Title",
            )
        ]
        cfg = {
            "required_keywords": ["pokemon", "tcg"],
            "whitelist": ["B555555555"],
            "blacklist": ["B555555555"],
        }
        rows, meta = run_search_filter_pipeline(raw, cfg)
        self.assertEqual(len(rows), 0)
        drops = meta.get("blacklist_kw_drops") or []
        self.assertTrue(any(d.get("reason") == "yaml_blacklist_asin" for d in drops))


if __name__ == "__main__":
    unittest.main()
