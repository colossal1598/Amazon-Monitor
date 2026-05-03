"""Pokémon TCG title scope (stage1)."""

import unittest

from filter_pipeline import _has_pokemon_tcg_title


class TitleScopeTests(unittest.TestCase):
    def test_scarlet_violet_without_tcg_token(self) -> None:
        self.assertTrue(
            _has_pokemon_tcg_title(
                "Pokemon Scarlet & Violet 9 Journey Together Three Booster Blister, Random Draw"
            )
        )

    def test_explicit_pokemon_tcg(self) -> None:
        self.assertTrue(_has_pokemon_tcg_title("Pokemon TCG: Q4 Poke Ball Tins"))

    def test_booster_bundle_token(self) -> None:
        self.assertTrue(
            _has_pokemon_tcg_title("Pokémon TCG: Mega Evolution—Perfect Order Booster Bundle")
        )

    def test_non_tcg_pokemon_toy_rejected(self) -> None:
        self.assertFalse(
            _has_pokemon_tcg_title("Pokemon Trainer Expert - Electronic Guessing Board Game")
        )


if __name__ == "__main__":
    unittest.main()
