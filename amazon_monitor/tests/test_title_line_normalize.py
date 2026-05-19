"""Single-line title normalization for PDP alerts / DB."""

import unittest

from pdp_helpers import normalize_title_line


class NormalizeTitleLineTests(unittest.TestCase):
    def test_brand_then_title_newline(self) -> None:
        raw = "Pokémon\nPokémon TCG: Example Booster"
        self.assertEqual(normalize_title_line(raw), "Pokémon Pokémon TCG: Example Booster")

    def test_crlf_and_tabs(self) -> None:
        self.assertEqual(
            normalize_title_line("A\r\nB\t\tC"),
            "A B C",
        )

    def test_none_and_empty(self) -> None:
        self.assertIsNone(normalize_title_line(None))
        self.assertIsNone(normalize_title_line(""))
        self.assertIsNone(normalize_title_line("   \n  "))


if __name__ == "__main__":
    unittest.main()
