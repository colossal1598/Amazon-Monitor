"""title_blacklist_phrases in run_search_filter_pipeline."""

import unittest

from filter_pipeline import run_search_filter_pipeline


class TitleBlacklistPhrasesTests(unittest.TestCase):
    def test_drops_magic_the_gathering_in_title(self) -> None:
        raw = [
            {
                "asin": "B011111111",
                "title": "Pokemon TCG Booster Magic The Gathering crossover tin",
                "price": 24.99,
                "price_text": "$24.99",
                "in_stock": True,
                "shipping_text": "FREE delivery",
                "seller": "x",
                "seller_text": "Sold by Amazon.com",
                "image_url": None,
                "product_url": "https://www.amazon.com/dp/B011111111",
                "source": "t",
                "search_url": "https://www.amazon.com/s?k=test",
            }
        ]
        cfg = {
            "required_keywords": ["pokemon", "tcg"],
            "blacklist": [],
            "title_blacklist_phrases": ["magic the gathering", "mtg"],
        }
        filtered, meta = run_search_filter_pipeline(raw, cfg)
        self.assertEqual(len(filtered), 0)
        drops = meta.get("blacklist_kw_drops") or []
        self.assertTrue(any(d.get("reason") == "title_blacklist_phrase" for d in drops))

    def test_yaml_blacklist_drops_asin(self) -> None:
        raw = [
            {
                "asin": "B022222222",
                "title": "Pokemon TCG Booster Bundle",
                "price": 39.99,
                "price_text": "$39.99",
                "in_stock": True,
                "shipping_text": "FREE delivery",
                "seller": "x",
                "seller_text": "Sold by Amazon.com",
                "image_url": None,
                "product_url": "https://www.amazon.com/dp/B022222222",
                "source": "t",
                "search_url": "https://www.amazon.com/s?k=test",
            }
        ]
        cfg = {
            "required_keywords": ["pokemon", "tcg"],
            "blacklist": ["B022222222"],
            "title_blacklist_phrases": [],
        }
        filtered, meta = run_search_filter_pipeline(raw, cfg)
        self.assertEqual(len(filtered), 0)
        drops = meta.get("blacklist_kw_drops") or []
        self.assertTrue(any(d.get("reason") == "yaml_blacklist_asin" for d in drops))
