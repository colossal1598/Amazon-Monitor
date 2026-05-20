import base64
import json
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path

from tools.admin_ui_server import create_server


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


if __name__ == "__main__":
    unittest.main()
