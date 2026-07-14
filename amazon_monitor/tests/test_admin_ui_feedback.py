import base64
import json
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.client import HTTPConnection
from pathlib import Path

from tools.admin_ui_server import create_server

_AUTH = base64.b64encode(b"admin:secret").decode("ascii")


def _seed_monitor_db(db_path: Path) -> None:
    """Create the alerts/products tables the admin feedback endpoints read."""
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asin TEXT,
            alert_type TEXT,
            source TEXT,
            old_price REAL,
            new_price REAL,
            sent_at TEXT
        );
        CREATE TABLE products (
            asin TEXT PRIMARY KEY,
            title TEXT,
            price REAL
        );
        """
    )
    now = datetime.now(timezone.utc)

    def iso(days_ago: float) -> str:
        return (now - timedelta(days=days_ago)).isoformat()

    conn.execute("INSERT INTO products (asin, title) VALUES ('B0PRODUCT1', 'Pikachu Box')")
    # id 1: recent product alert (back_in_stock) with price on new_price
    conn.execute(
        "INSERT INTO alerts (asin, alert_type, source, old_price, new_price, sent_at)"
        " VALUES ('B0PRODUCT1', 'back_in_stock', 'pdp_watch', NULL, 24.99, ?)",
        (iso(1),),
    )
    # id 2: recent price_drop, price only on old_price (new_price null)
    conn.execute(
        "INSERT INTO alerts (asin, alert_type, source, old_price, new_price, sent_at)"
        " VALUES ('B0PRODUCT2', 'price_drop', 'aes_llc', 30.0, NULL, ?)",
        (iso(2),),
    )
    # id 3: operational alert type -> must be filtered out
    conn.execute(
        "INSERT INTO alerts (asin, alert_type, source, old_price, new_price, sent_at)"
        " VALUES ('B0PRODUCT1', 'captcha', 'system', NULL, NULL, ?)",
        (iso(1),),
    )
    # id 4: product alert but outside the default 7-day window
    conn.execute(
        "INSERT INTO alerts (asin, alert_type, source, old_price, new_price, sent_at)"
        " VALUES ('B0PRODUCT1', 'new_product', 'aes_llc', NULL, 12.5, ?)",
        (iso(20),),
    )
    conn.commit()
    conn.close()


class AdminUIFeedbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "monitor.db"
        _seed_monitor_db(self.db_path)
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

    def test_recent_requires_auth(self) -> None:
        status, _ = self._request("GET", "/api/alerts/recent", auth=False)
        self.assertEqual(status, 401)

    def test_feedback_requires_auth(self) -> None:
        status, _ = self._request(
            "POST", "/api/alerts/feedback", auth=False, body={"alert_id": 1}
        )
        self.assertEqual(status, 401)

    # ----- GET recent -----

    def test_recent_filters_types_and_window(self) -> None:
        status, payload = self._request("GET", "/api/alerts/recent")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["days"], 7)
        alerts = payload["alerts"]
        ids = [a["alert_id"] for a in alerts]
        # id 1 and id 2 only: captcha (id3) filtered by type, id4 outside window
        self.assertEqual(ids, [1, 2])
        # newest first
        first = alerts[0]
        self.assertEqual(first["alert_type"], "back_in_stock")
        self.assertEqual(first["title"], "Pikachu Box")
        self.assertEqual(first["price"], 24.99)
        self.assertIsNone(first["feedback"])
        # price falls back to old_price when new_price is null
        self.assertEqual(alerts[1]["price"], 30.0)
        self.assertIsNone(alerts[1]["title"])

    def test_recent_price_falls_back_to_product(self) -> None:
        # Legacy alerts recorded before old/new price were persisted carry NULL
        # prices; the endpoint must surface products.price instead, and only
        # render nothing when the product has no price either.
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE products SET price = 19.99 WHERE asin = 'B0PRODUCT1'")
        now = datetime.now(timezone.utc).isoformat()
        cur = conn.execute(
            "INSERT INTO alerts (asin, alert_type, source, old_price, new_price, sent_at)"
            " VALUES ('B0PRODUCT1', 'back_in_stock', 'pdp_watch', NULL, NULL, ?)",
            (now,),
        )
        priced_id = cur.lastrowid
        # No products row for this asin -> no fallback price available.
        cur = conn.execute(
            "INSERT INTO alerts (asin, alert_type, source, old_price, new_price, sent_at)"
            " VALUES ('B0NOPRICE', 'back_in_stock', 'pdp_watch', NULL, NULL, ?)",
            (now,),
        )
        null_id = cur.lastrowid
        conn.commit()
        conn.close()

        status, payload = self._request("GET", "/api/alerts/recent")
        self.assertEqual(status, 200)
        by_id = {a["alert_id"]: a for a in payload["alerts"]}
        self.assertEqual(by_id[priced_id]["price"], 19.99)
        self.assertIsNone(by_id[null_id]["price"])

    def test_recent_days_clamped(self) -> None:
        status, payload = self._request("GET", "/api/alerts/recent?days=999")
        self.assertEqual(status, 200)
        self.assertEqual(payload["days"], 30)
        # id 4 (20 days ago) now visible within a 30-day window
        ids = [a["alert_id"] for a in payload["alerts"]]
        self.assertIn(4, ids)

    def test_recent_merges_feedback(self) -> None:
        status, _ = self._request(
            "POST",
            "/api/alerts/feedback",
            body={
                "alert_id": 1,
                "verdict": "like",
                "tags": ["late"],
                "note": "  good  ",
                "seen_at": "2026-07-14T11:30",
            },
        )
        self.assertEqual(status, 200)
        status, payload = self._request("GET", "/api/alerts/recent")
        self.assertEqual(status, 200)
        row = next(a for a in payload["alerts"] if a["alert_id"] == 1)
        self.assertIsNotNone(row["feedback"])
        self.assertEqual(row["feedback"]["verdict"], "like")
        self.assertEqual(row["feedback"]["tags"], ["late"])
        self.assertEqual(row["feedback"]["note"], "good")
        self.assertEqual(row["feedback"]["seen_at"], "2026-07-14T11:30")

    # ----- POST upsert -----

    def test_feedback_upsert_roundtrip(self) -> None:
        status, payload = self._request(
            "POST",
            "/api/alerts/feedback",
            body={"alert_id": 2, "verdict": "dislike", "tags": ["wrong_price"], "note": "x"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["feedback"]["verdict"], "dislike")
        self.assertEqual(payload["feedback"]["tags"], ["wrong_price"])
        first_updated = payload["feedback"]["updated_at"]

        # update same alert_id -> still one row, values replaced
        status, payload = self._request(
            "POST",
            "/api/alerts/feedback",
            body={
                "alert_id": 2,
                "verdict": "like",
                "tags": ["duplicate", "irrelevant"],
                "note": "changed",
                "seen_at": "2026-07-13T09:15",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["feedback"]["verdict"], "like")
        self.assertEqual(payload["feedback"]["tags"], ["duplicate", "irrelevant"])
        self.assertEqual(payload["feedback"]["note"], "changed")

        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(
            "SELECT verdict, tags, note, seen_at FROM alert_feedback WHERE alert_id = 2"
        ).fetchall()
        conn.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "like")
        self.assertEqual(json.loads(rows[0][1]), ["duplicate", "irrelevant"])
        self.assertEqual(rows[0][3], "2026-07-13T09:15")
        # updated_at moved forward (or at least present)
        self.assertIsNotNone(first_updated)

    def test_feedback_null_verdict_allowed(self) -> None:
        status, payload = self._request(
            "POST",
            "/api/alerts/feedback",
            body={"alert_id": 1, "verdict": None, "tags": [], "note": ""},
        )
        self.assertEqual(status, 200)
        self.assertIsNone(payload["feedback"]["verdict"])

    # ----- POST validation -----

    def test_feedback_missing_alert_404(self) -> None:
        status, payload = self._request(
            "POST", "/api/alerts/feedback", body={"alert_id": 9999, "verdict": "like"}
        )
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"], "alert_not_found")

    def test_feedback_bad_verdict_400(self) -> None:
        status, payload = self._request(
            "POST", "/api/alerts/feedback", body={"alert_id": 1, "verdict": "meh"}
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "invalid_verdict")

    def test_feedback_bad_tag_400(self) -> None:
        status, payload = self._request(
            "POST", "/api/alerts/feedback", body={"alert_id": 1, "tags": ["nope"]}
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "invalid_tag")

    def test_feedback_bad_seen_at_400(self) -> None:
        status, payload = self._request(
            "POST",
            "/api/alerts/feedback",
            body={"alert_id": 1, "seen_at": "14/07/2026 11:30"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "invalid_seen_at")

    def test_feedback_missing_alert_id_400(self) -> None:
        status, payload = self._request(
            "POST", "/api/alerts/feedback", body={"verdict": "like"}
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "invalid_alert_id")


if __name__ == "__main__":
    unittest.main()
