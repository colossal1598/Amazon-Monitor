import os
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from settings_store import (
    add_asin,
    get_setting,
    list_asin_entries,
    list_asins,
    load_runtime_config,
    migrate_yaml_to_db,
    remove_asin,
    replace_asins,
    set_asin_target_price,
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

    def test_target_price_defaults_to_none_and_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "monitor.db")
            add_asin(db_path, "B088888888", "watch")

            entries = list_asin_entries(db_path, "watch")
            self.assertEqual(len(entries), 1)
            self.assertIn("target_price", entries[0])
            self.assertIsNone(entries[0]["target_price"])

            add_asin(db_path, "B099999999", "watch", target_price=49.99)
            entries = list_asin_entries(db_path, "watch")
            by_asin = {e["asin"]: e["target_price"] for e in entries}
            self.assertEqual(by_asin["B099999999"], 49.99)
            self.assertIsNone(by_asin["B088888888"])

    def test_set_asin_target_price_updates_existing_watch_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "monitor.db")
            add_asin(db_path, "B011112222", "watch")

            self.assertTrue(set_asin_target_price(db_path, "b011112222", 15.5))
            entries = list_asin_entries(db_path, "watch")
            self.assertEqual(entries[0]["target_price"], 15.5)

            # Clearing with None disables the alert again.
            self.assertTrue(set_asin_target_price(db_path, "B011112222", None))
            entries = list_asin_entries(db_path, "watch")
            self.assertIsNone(entries[0]["target_price"])

    def test_set_asin_target_price_returns_false_for_missing_watch_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "monitor.db")
            self.assertFalse(set_asin_target_price(db_path, "B033334444", 10.0))

            # Present only as blacklist, not watch -> still False.
            add_asin(db_path, "B033334444", "blacklist")
            self.assertFalse(set_asin_target_price(db_path, "B033334444", 10.0))

    def test_load_runtime_config_exposes_pdp_watch_target_prices(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "monitor.db")
            add_asin(db_path, "B055556666", "watch", target_price=29.5)
            add_asin(db_path, "B066667777", "watch")

            cfg = load_runtime_config(db_path)
            self.assertEqual(cfg.get("pdp_watch_target_prices"), {"B055556666": 29.5})

    def test_load_runtime_config_prunes_removed_legacy_keys(self) -> None:
        # Production carried dead rows (max_cycle_seconds / pdp_poll_minutes from the
        # pre-streaming era); they must vanish from both the config and the db.
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "monitor.db")
            set_setting(db_path, "max_cycle_seconds", 300)
            set_setting(db_path, "pdp_title_wait_ms", 6000)
            set_setting(db_path, "aes_check_minutes", 7)

            cfg = load_runtime_config(db_path)
            self.assertNotIn("max_cycle_seconds", cfg)
            self.assertNotIn("pdp_title_wait_ms", cfg)
            self.assertEqual(cfg.get("aes_check_minutes"), 7)
            self.assertIsNone(get_setting(db_path, "max_cycle_seconds"))
            self.assertIsNone(get_setting(db_path, "pdp_title_wait_ms"))

    def test_defaults_expose_hidden_knobs(self) -> None:
        # Keys the engine reads must exist in the defaults so /api/settings lists
        # them (previously invisible until explicitly set).
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_runtime_config(str(Path(tmp) / "monitor.db"))
            self.assertEqual(cfg.get("stock_alert_same_price_dedupe_minutes"), 360)
            self.assertEqual(cfg.get("pdp_aod_min_interval_seconds"), 240)
            self.assertEqual(cfg.get("mass_flip_min_flips"), 2)
            self.assertEqual(cfg.get("watchdog_stall_seconds"), 600)
            self.assertFalse(cfg.get("pdp_preorder_realert_suppression"))
            # Headed is the production mode of record.
            self.assertIs(cfg.get("playwright_headless"), False)


if __name__ == "__main__":
    unittest.main()
