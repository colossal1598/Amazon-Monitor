import base64
import json
import sqlite3
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path

from tools.admin_ui_server import create_server

_AUTH = base64.b64encode(b"admin:secret").decode("ascii")


def _init_db(db_path: Path) -> None:
    """The missed_reports table is created lazily by the endpoint; we only need
    the database file to exist for the connection to open."""
    conn = sqlite3.connect(db_path)
    conn.close()


class AdminUIMissedTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "monitor.db"
        _init_db(self.db_path)
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

    def _request(self, method: str, path: str, *, auth: bool = True, body=None):
        headers = {}
        if auth:
            headers["Authorization"] = f"Basic {_AUTH}"
        raw = None
        if body is not None:
            raw = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(method, path, body=raw, headers=headers)
        response = conn.getresponse()
        status = response.status
        data = response.read().decode("utf-8")
        conn.close()
        payload = json.loads(data) if data else {}
        return status, payload

    # ----- auth -----

    def test_missed_get_requires_auth(self) -> None:
        status, _ = self._request("GET", "/api/alerts/missed", auth=False)
        self.assertEqual(status, 401)

    def test_missed_post_requires_auth(self) -> None:
        status, _ = self._request(
            "POST",
            "/api/alerts/missed",
            auth=False,
            body={"asin": "B0PRODUCT1", "seen_at": "2026-07-14T11:30"},
        )
        self.assertEqual(status, 401)

    # ----- POST roundtrip -----

    def test_missed_post_roundtrip(self) -> None:
        status, payload = self._request(
            "POST",
            "/api/alerts/missed",
            body={"asin": "b0product1", "seen_at": "2026-07-14T11:30", "note": "  saw it  "},
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        report = payload["report"]
        # asin uppercased, note trimmed
        self.assertEqual(report["asin"], "B0PRODUCT1")
        self.assertEqual(report["seen_at"], "2026-07-14T11:30")
        self.assertEqual(report["note"], "saw it")
        self.assertIn("id", report)
        self.assertIn("created_at", report)

        # GET returns the row
        status, payload = self._request("GET", "/api/alerts/missed")
        self.assertEqual(status, 200)
        self.assertEqual(payload["days"], 7)
        reports = payload["reports"]
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]["asin"], "B0PRODUCT1")
        self.assertEqual(reports[0]["note"], "saw it")

    def test_missed_note_optional(self) -> None:
        status, payload = self._request(
            "POST",
            "/api/alerts/missed",
            body={"asin": "B0PRODUCT2", "seen_at": "2026-07-14T09:00"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["report"]["note"], "")

    def test_missed_newest_first(self) -> None:
        for i in range(3):
            status, _ = self._request(
                "POST",
                "/api/alerts/missed",
                body={"asin": "B0PRODUCT1", "seen_at": f"2026-07-1{i}T10:00"},
            )
            self.assertEqual(status, 200)
        status, payload = self._request("GET", "/api/alerts/missed")
        ids = [r["id"] for r in payload["reports"]]
        self.assertEqual(ids, sorted(ids, reverse=True))

    # ----- POST validation -----

    def test_missed_bad_asin_400(self) -> None:
        status, payload = self._request(
            "POST",
            "/api/alerts/missed",
            body={"asin": "NOTANASIN", "seen_at": "2026-07-14T11:30"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "invalid_asin")

    def test_missed_missing_asin_400(self) -> None:
        status, payload = self._request(
            "POST", "/api/alerts/missed", body={"seen_at": "2026-07-14T11:30"}
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "invalid_asin")

    def test_missed_bad_seen_at_400(self) -> None:
        status, payload = self._request(
            "POST",
            "/api/alerts/missed",
            body={"asin": "B0PRODUCT1", "seen_at": "14/07/2026 11:30"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "invalid_seen_at")

    def test_missed_missing_seen_at_400(self) -> None:
        status, payload = self._request(
            "POST", "/api/alerts/missed", body={"asin": "B0PRODUCT1"}
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "invalid_seen_at")

    # ----- GET window filter -----

    def test_missed_window_filter(self) -> None:
        # Insert a report, then backdate its created_at beyond a 1-day window.
        status, payload = self._request(
            "POST",
            "/api/alerts/missed",
            body={"asin": "B0PRODUCT1", "seen_at": "2026-07-14T11:30"},
        )
        old_id = payload["report"]["id"]
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE missed_reports SET created_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
            (old_id,),
        )
        conn.commit()
        conn.close()

        # Fresh report inside the window
        self._request(
            "POST",
            "/api/alerts/missed",
            body={"asin": "B0PRODUCT2", "seen_at": "2026-07-15T08:00"},
        )

        status, payload = self._request("GET", "/api/alerts/missed?days=1")
        self.assertEqual(status, 200)
        self.assertEqual(payload["days"], 1)
        ids = [r["id"] for r in payload["reports"]]
        self.assertNotIn(old_id, ids)
        self.assertEqual(len(ids), 1)

    def test_missed_days_clamped(self) -> None:
        status, payload = self._request("GET", "/api/alerts/missed?days=999")
        self.assertEqual(status, 200)
        self.assertEqual(payload["days"], 30)


if __name__ == "__main__":
    unittest.main()
