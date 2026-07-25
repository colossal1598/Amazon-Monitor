"""_title_signals_pokemon_tcg_scope: which titles satisfy the "tcg" required keyword.

Japanese-import products are branded "Pokemon Card Game" with no literal "TCG" token;
one such SERP hit (B0G1XB2STM, 2026-07-24) was silently dropped at AES discovery and
never reached aes_products or an alert.
"""

import unittest

from filter_pipeline import _title_signals_pokemon_tcg_scope, run_search_filter_pipeline


def _serp_item(asin: str, title: str) -> dict:
    return {
        "asin": asin,
        "title": title,
        "price": 59.99,
        "price_text": "$59.99",
        "in_stock": True,
        "shipping_text": "FREE delivery",
        "seller": "x",
        "seller_text": "Sold by Amazon.com",
        "image_url": None,
        "product_url": f"https://www.amazon.com/dp/{asin}",
        "source": "t",
        "search_url": "https://www.amazon.com/s?k=test",
    }


class TestTcgScopeTitles(unittest.TestCase):
    def test_japanese_brand_pokemon_card_game_passes(self) -> None:
        self.assertTrue(
            _title_signals_pokemon_tcg_scope(
                "Pokemon Card Game MEGA Expansion Pack Ninja Spinner Box"
            )
        )

    def test_explicit_tcg_still_passes(self) -> None:
        self.assertTrue(_title_signals_pokemon_tcg_scope("Pokemon TCG Prismatic Evolutions"))

    def test_non_card_pokemon_product_still_fails(self) -> None:
        self.assertFalse(_title_signals_pokemon_tcg_scope("Pokemon Pikachu Plush Toy 12 inch"))
        self.assertFalse(_title_signals_pokemon_tcg_scope("MEGA Pokemon Building Kit Charizard"))

    def test_pipeline_keeps_japanese_brand_title(self) -> None:
        raw = [_serp_item("B0G1XB2STM", "Pokemon Card Game MEGA Expansion Pack Ninja Spinner Box")]
        cfg = {
            "required_keywords": ["pokemon", "tcg"],
            "blacklist": [],
            "title_blacklist_phrases": [],
        }
        filtered, meta = run_search_filter_pipeline(raw, cfg)
        self.assertEqual([p["asin"] for p in filtered], ["B0G1XB2STM"])
        self.assertEqual(meta.get("blacklist_kw_drops") or [], [])


if __name__ == "__main__":
    unittest.main()
