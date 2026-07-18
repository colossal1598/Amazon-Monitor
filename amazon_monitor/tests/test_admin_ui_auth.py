import base64
import json
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import MagicMock, patch

import tools.admin_ui_server as admin_ui_server
from tools.admin_ui_server import create_server, restart_pm2_stack


class AdminUIAuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "monitor.db"
        self.server = create_server(
            host="127.0.0.1",
            port=0,
            db_path=self.db_path,
            admin_user="admin",
            admin_password="secret",
        )
        self.port = int(self.server.server_address[1])
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self._tmp.cleanup()

    def test_api_bandwidth_summary_requires_auth(self) -> None:
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/api/bandwidth/summary")
        response = conn.getresponse()
        self.assertEqual(response.status, 401)
        response.read()
        conn.close()

    def test_api_bandwidth_summary_with_auth_ok(self) -> None:
        telemetry_path = Path(self._tmp.name) / "telemetry.db"
        today_il = datetime.now(timezone(timedelta(hours=3))).date().isoformat()
        conn = sqlite3.connect(telemetry_path)
        conn.executescript(
            """
            CREATE TABLE cycle_stats (
                id INTEGER PRIMARY KEY,
                recorded_at_il TEXT,
                net_bytes_total INTEGER,
                net_bytes_pdp INTEGER,
                net_bytes_aes INTEGER,
                net_bytes_image INTEGER,
                blocked_url INTEGER,
                blocked_heavy INTEGER,
                gb_est_total REAL
            );
            CREATE TABLE daily_bandwidth (
                date_il TEXT PRIMARY KEY,
                bytes_total INTEGER NOT NULL,
                bytes_pdp INTEGER NOT NULL,
                bytes_aes INTEGER NOT NULL,
                bytes_image INTEGER NOT NULL,
                cycles INTEGER NOT NULL,
                updated_at_il TEXT NOT NULL
            );
            """
        )
        conn.execute(
            """
            INSERT INTO cycle_stats (
                recorded_at_il, net_bytes_total, net_bytes_pdp, net_bytes_aes,
                net_bytes_image, blocked_url, blocked_heavy, gb_est_total
            ) VALUES (?, 1048576, 900000, 100000, 48576, 1, 2, 0.001)
            """,
            ("2026-06-04T10:00:00+03:00",),
        )
        conn.execute(
            """
            INSERT INTO daily_bandwidth (
                date_il, bytes_total, bytes_pdp, bytes_aes, bytes_image, cycles, updated_at_il
            ) VALUES (?, 2147483648, 0, 0, 0, 3, ?)
            """,
            (today_il, "2026-06-04T12:00:00+03:00"),
        )
        conn.commit()
        conn.close()

        config_path = Path(self._tmp.name) / "config.yaml"
        config_path.write_text(
            f"db_path: {self.db_path.as_posix()}\ntelemetry_db_path: {telemetry_path.as_posix()}\n",
            encoding="utf-8",
        )
        with patch.object(admin_ui_server, "CONFIG_PATH", config_path):
            token = base64.b64encode(b"admin:secret").decode("ascii")
            http = HTTPConnection("127.0.0.1", self.port, timeout=5)
            http.request(
                "GET",
                "/api/bandwidth/summary",
                headers={"Authorization": f"Basic {token}"},
            )
            response = http.getresponse()
            self.assertEqual(response.status, 200)
            payload = json.loads(response.read().decode("utf-8"))
        self.assertTrue(payload.get("ok"))
        self.assertEqual(payload["last_cycle"]["net_bytes_total"], 1048576)
        self.assertEqual(payload["today"]["bytes_total"], 2147483648)
        self.assertGreater(payload["last_7_days_gb"], 1.9)
        self.assertEqual(len(payload["recent_cycles"]), 1)
        http.close()

    def test_api_settings_requires_auth(self) -> None:
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/api/settings")
        response = conn.getresponse()
        self.assertEqual(response.status, 401)
        self.assertIn("Basic", response.getheader("WWW-Authenticate", ""))
        response.read()
        conn.close()

    def test_index_html_without_auth_ok(self) -> None:
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/")
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        body = response.read().decode("utf-8")
        self.assertIn("login-form", body)
        conn.close()

    def test_help_html_without_auth_ok(self) -> None:
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/help.html")
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        body = response.read().decode("utf-8")
        self.assertIn("מדריך קצר", body)
        conn.close()

    def test_api_settings_with_basic_auth_ok(self) -> None:
        token = base64.b64encode(b"admin:secret").decode("ascii")
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/api/settings", headers={"Authorization": f"Basic {token}"})
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        payload = json.loads(response.read().decode("utf-8"))
        self.assertTrue(payload.get("ok"))
        self.assertIsInstance(payload.get("settings"), dict)
        conn.close()

    def test_pm2_restart_requires_auth(self) -> None:
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/api/pm2/restart", body=b"{}", headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        self.assertEqual(response.status, 401)
        response.read()
        conn.close()

    @patch("tools.admin_ui_server.subprocess.run")
    def test_pm2_restart_with_auth_ok(self, mock_run: MagicMock) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=["pm2", "restart", "all"],
            returncode=0,
            stdout="ok",
            stderr="",
        )
        token = base64.b64encode(b"admin:secret").decode("ascii")
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(
            "POST",
            "/api/pm2/restart",
            body=b"{}",
            headers={"Authorization": f"Basic {token}", "Content-Type": "application/json"},
        )
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        payload = json.loads(response.read().decode("utf-8"))
        self.assertTrue(payload.get("ok"))
        self.assertIn("הופעלה מחדש", payload.get("message", ""))
        mock_run.assert_called_once()
        args, kwargs = mock_run.call_args
        self.assertEqual(args[0], ["pm2", "restart", "all"])
        self.assertTrue(kwargs.get("capture_output"))
        conn.close()

    @patch("tools.admin_ui_server.subprocess.run")
    def test_restart_pm2_stack_unit(self, mock_run: MagicMock) -> None:
        admin_ui_server._pm2_last_restart_mono = 0.0
        mock_run.return_value = subprocess.CompletedProcess(
            args=["pm2", "restart", "all"],
            returncode=0,
            stdout="",
            stderr="",
        )
        result = restart_pm2_stack()
        self.assertTrue(result.get("ok"))
        self.assertIn("הופעלה מחדש", result.get("message", ""))

    def test_settings_put_roundtrip_new_keys(self) -> None:
        token = base64.b64encode(b"admin:secret").decode("ascii")
        body = json.dumps(
            {
                "settings": {
                    "playwright_headless": False,
                    "wa_group_id": "120363@test@g.us",
                    "wa_client_to": "972501234567@c.us",
                    "price_drop_percent": 15,
                    "max_requests_per_minute": 8,
                    "stock_alert_same_price_dedupe_minutes": 240,
                    "affiliate_tag": "test-tag",
                    # Legacy key nothing reads: accepted by the API but pruned on the
                    # next load, so it must NOT come back in the response settings.
                    "pdp_watch_max_concurrent_tabs": 3,
                }
            }
        ).encode("utf-8")
        headers = {"Authorization": f"Basic {token}", "Content-Type": "application/json"}
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("PUT", "/api/settings", body=body, headers=headers)
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        payload = json.loads(response.read().decode("utf-8"))
        settings = payload.get("settings", {})
        self.assertFalse(settings.get("playwright_headless"))
        self.assertEqual(settings.get("wa_group_id"), "120363@test@g.us")
        self.assertEqual(settings.get("wa_client_to"), "972501234567@c.us")
        self.assertEqual(settings.get("price_drop_percent"), 15)
        self.assertEqual(settings.get("max_requests_per_minute"), 8)
        self.assertEqual(settings.get("stock_alert_same_price_dedupe_minutes"), 240)
        self.assertEqual(settings.get("affiliate_tag"), "test-tag")
        self.assertNotIn("pdp_watch_max_concurrent_tabs", settings)
        conn.close()


if __name__ == "__main__":
    unittest.main()
