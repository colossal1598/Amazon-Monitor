import os
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from settings_store import (
    add_asin,
    get_setting,
    list_asins,
    load_runtime_config,
    migrate_yaml_to_db,
    remove_asin,
    replace_asins,
    set_setting,
)


class TestSettingsStore(unittest.TestCase):
    def test_migrate_yaml_to_db_imports_settings_and_asins_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            yaml_path = tmp_path / "config.yaml"
            db_path = tmp_path / "monitor.db"
            yaml_path.write_text(
                textwrap.dedent(
                    """
                    pdp_poll_minutes: 7
                    max_cycle_seconds: 190
                    search_urls:
                      aes_llc: "https://example.com/aes"
                    required_keywords:
                      - pokemon
                    title_blacklist_phrases:
                      - magic the gathering
                    pdp_watch_asins:
                      - B012345678
                      - invalid
                      - b012345678
                    blacklist:
                      - b099999999
                    wa_api_url: "http://yaml-wa.local/send"
                    wa_api_key: "from-yaml-should-not-import"
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )

            self.assertTrue(migrate_yaml_to_db(str(yaml_path), str(db_path)))
            self.assertFalse(migrate_yaml_to_db(str(yaml_path), str(db_path)))

            self.assertEqual(get_setting(str(db_path), "pdp_poll_minutes"), 7)
            self.assertEqual(
                get_setting(str(db_path), "search_urls"),
                {"aes_llc": "https://example.com/aes"},
            )
            self.assertIsNone(get_setting(str(db_path), "wa_api_key"))

            self.assertEqual(list_asins(str(db_path), "watch"), ["B012345678"])
            self.assertEqual(list_asins(str(db_path), "blacklist"), ["B099999999"])

    def test_asin_role_helpers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "monitor.db")
            add_asin(db_path, "b011111111", "watch")
            add_asin(db_path, "B022222222", "watch")
            add_asin(db_path, "B033333333", "blacklist")

            self.assertEqual(list_asins(db_path, "watch"), ["B011111111", "B022222222"])
            self.assertEqual(list_asins(db_path, "blacklist"), ["B033333333"])

            remove_asin(db_path, "B022222222", "watch")
            self.assertEqual(list_asins(db_path, "watch"), ["B011111111"])

            replace_asins(db_path, "watch", ["B044444444", "invalid", "b044444444", "B055555555"])
            self.assertEqual(list_asins(db_path, "watch"), ["B044444444", "B055555555"])

    def test_load_runtime_config_merges_env_and_asins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "monitor.db")
            set_setting(db_path, "wa_api_url", "http://yaml-wa.local/send")
            set_setting(db_path, "price_drop_percent", 15)
            add_asin(db_path, "B066666666", "watch")
            add_asin(db_path, "B077777777", "blacklist")

            with patch.dict(
                os.environ,
                {"WA_API_URL": "http://env-wa.local/send", "WA_API_KEY": "from-env-key"},
                clear=False,
            ):
                cfg = load_runtime_config(db_path)

            self.assertEqual(cfg.get("wa_api_url"), "http://env-wa.local/send")
            self.assertEqual(cfg.get("wa_api_key"), "from-env-key")
            self.assertEqual(cfg.get("price_drop_percent"), 15)
            self.assertEqual(cfg.get("pdp_watch_asins"), ["B066666666"])
            self.assertEqual(cfg.get("blacklist"), ["B077777777"])


if __name__ == "__main__":
    unittest.main()
