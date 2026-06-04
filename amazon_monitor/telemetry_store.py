"""SQLite telemetry: per-cycle stats, selective debug events, client-alert state."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import usage_metrics

LOGGER = logging.getLogger(__name__)

STALL_TRACKER_KIND = "_stall_tracker"


def _israel_tz():
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo("Asia/Jerusalem")
    except Exception:
        return timezone(timedelta(hours=3), name="Asia/Jerusalem")


def israel_now_iso() -> str:
    return datetime.now(_israel_tz()).isoformat(timespec="seconds")


def _parse_il(iso: str) -> datetime:
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=_israel_tz())
    return dt


class TelemetryStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._last_prune_il: str | None = None
        self.init_db()

    def init_db(self) -> None:
        with self.lock:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS cycle_stats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cycle_started_il TEXT NOT NULL,
                    recorded_at_il TEXT NOT NULL,
                    total_sec REAL,
                    pdp_sec REAL,
                    aes_sec REAL,
                    net_kb_est REAL,
                    pdp_net_kb_est REAL,
                    aes_net_kb_est REAL,
                    image_net_kb REAL,
                    image_fetches INTEGER,
                    image_cache_hits INTEGER,
                    pdp_ok INTEGER,
                    pdp_skip INTEGER,
                    blocked_heavy INTEGER,
                    exceeds_poll_interval INTEGER,
                    pdp_poll_minutes INTEGER,
                    pdp_watch INTEGER,
                    watch_rows INTEGER,
                    in_stock INTEGER,
                    skip_update INTEGER,
                    captcha_skip INTEGER,
                    captcha_aborted INTEGER,
                    alerts_sent INTEGER,
                    aes_raw INTEGER,
                    aes_candidates INTEGER,
                    aes_alerts INTEGER,
                    captcha_aborted_flag INTEGER,
                    pdp_scrape_errors INTEGER,
                    pdp_scrape_error_reasons_json TEXT,
                    aes_scrape_outcome_json TEXT,
                    pdp_state_summary_json TEXT,
                    aes_state_summary_json TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_cycle_stats_recorded ON cycle_stats(recorded_at_il);

                CREATE TABLE IF NOT EXISTS debug_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cycle_id INTEGER NOT NULL,
                    recorded_at_il TEXT NOT NULL,
                    event TEXT NOT NULL,
                    asin TEXT,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_debug_events_cycle ON debug_events(cycle_id);
                CREATE INDEX IF NOT EXISTS idx_debug_events_event ON debug_events(event);
                CREATE INDEX IF NOT EXISTS idx_debug_events_asin ON debug_events(asin);
                CREATE INDEX IF NOT EXISTS idx_debug_events_time ON debug_events(recorded_at_il);

                CREATE TABLE IF NOT EXISTS client_alert_state (
                    kind TEXT PRIMARY KEY,
                    last_sent_at_il TEXT NOT NULL,
                    sent_count_in_window INTEGER NOT NULL DEFAULT 0,
                    window_started_il TEXT NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS daily_bandwidth (
                    date_il TEXT PRIMARY KEY,
                    bytes_total INTEGER NOT NULL DEFAULT 0,
                    bytes_pdp INTEGER NOT NULL DEFAULT 0,
                    bytes_aes INTEGER NOT NULL DEFAULT 0,
                    bytes_image INTEGER NOT NULL DEFAULT 0,
                    cycles INTEGER NOT NULL DEFAULT 0,
                    updated_at_il TEXT NOT NULL
                );
                """
            )
            for col, col_type in (
                ("net_bytes_total", "INTEGER"),
                ("net_bytes_pdp", "INTEGER"),
                ("net_bytes_aes", "INTEGER"),
                ("net_bytes_image", "INTEGER"),
                ("blocked_url", "INTEGER"),
                ("gb_est_total", "REAL"),
            ):
                try:
                    self.conn.execute(f"ALTER TABLE cycle_stats ADD COLUMN {col} {col_type}")
                except sqlite3.OperationalError:
                    pass
            self.conn.commit()

    def enabled(self, config: dict[str, Any] | None) -> bool:
        if config is None:
            return True
        if "telemetry_enabled" in config:
            return bool(config.get("telemetry_enabled"))
        return bool(config.get("metrics_enabled", True))

    def begin_cycle(self, config: dict[str, Any] | None = None) -> int:
        if not self.enabled(config):
            return 0
        started = usage_metrics.israel_now_iso()
        with self.lock:
            cur = self.conn.execute(
                """
                INSERT INTO cycle_stats (cycle_started_il, recorded_at_il)
                VALUES (?, ?)
                """,
                (started, started),
            )
            self.conn.commit()
            return int(cur.lastrowid or 0)

    def debug(
        self,
        cycle_id: int,
        event: str,
        *,
        asin: str | None = None,
        **fields: Any,
    ) -> None:
        if cycle_id <= 0:
            return
        payload = {k: v for k, v in fields.items() if k != "asin"}
        with self.lock:
            self.conn.execute(
                """
                INSERT INTO debug_events (cycle_id, recorded_at_il, event, asin, payload_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (cycle_id, israel_now_iso(), event, asin, json.dumps(payload, ensure_ascii=False)),
            )
            self.conn.commit()

    def finish_cycle(self, cycle_id: int, summary: dict[str, Any], config: dict[str, Any] | None = None) -> None:
        if cycle_id <= 0 or not self.enabled(config):
            return
        cols = [
            "recorded_at_il",
            "total_sec",
            "pdp_sec",
            "aes_sec",
            "net_kb_est",
            "pdp_net_kb_est",
            "aes_net_kb_est",
            "image_net_kb",
            "image_fetches",
            "image_cache_hits",
            "pdp_ok",
            "pdp_skip",
            "blocked_heavy",
            "blocked_url",
            "net_bytes_total",
            "net_bytes_pdp",
            "net_bytes_aes",
            "net_bytes_image",
            "gb_est_total",
            "exceeds_poll_interval",
            "pdp_poll_minutes",
            "pdp_watch",
            "watch_rows",
            "in_stock",
            "skip_update",
            "captcha_skip",
            "captcha_aborted",
            "alerts_sent",
            "aes_raw",
            "aes_candidates",
            "aes_alerts",
            "captcha_aborted_flag",
            "pdp_scrape_errors",
            "pdp_scrape_error_reasons_json",
            "aes_scrape_outcome_json",
            "pdp_state_summary_json",
            "aes_state_summary_json",
        ]

        def _val(key: str, default: Any = None) -> Any:
            v = summary.get(key, default)
            if key.endswith("_json") and isinstance(v, dict):
                return json.dumps(v, ensure_ascii=False)
            if key == "exceeds_poll_interval":
                return 1 if v else 0
            if key == "captcha_aborted_flag":
                return 1 if v else 0
            return v

        values = [israel_now_iso()] + [_val(c) for c in cols[1:]]
        set_clause = ", ".join(f"{c} = ?" for c in cols)
        with self.lock:
            self.conn.execute(
                f"UPDATE cycle_stats SET {set_clause} WHERE id = ?",
                (*values, cycle_id),
            )
            self.conn.commit()
        self.upsert_daily_bandwidth(summary)

    def upsert_daily_bandwidth(self, summary: dict[str, Any]) -> None:
        date_il = datetime.now(_israel_tz()).date().isoformat()
        bytes_pdp = int(summary.get("net_bytes_pdp") or 0)
        bytes_aes = int(summary.get("net_bytes_aes") or 0)
        bytes_image = int(summary.get("net_bytes_image") or 0)
        bytes_total = int(summary.get("net_bytes_total") or 0) + bytes_image
        now = israel_now_iso()
        with self.lock:
            self.conn.execute(
                """
                INSERT INTO daily_bandwidth (
                    date_il, bytes_total, bytes_pdp, bytes_aes, bytes_image, cycles, updated_at_il
                )
                VALUES (?, ?, ?, ?, ?, 1, ?)
                ON CONFLICT(date_il) DO UPDATE SET
                    bytes_total = bytes_total + excluded.bytes_total,
                    bytes_pdp = bytes_pdp + excluded.bytes_pdp,
                    bytes_aes = bytes_aes + excluded.bytes_aes,
                    bytes_image = bytes_image + excluded.bytes_image,
                    cycles = cycles + 1,
                    updated_at_il = excluded.updated_at_il
                """,
                (date_il, bytes_total, bytes_pdp, bytes_aes, bytes_image, now),
            )
            self.conn.commit()

    def recent_stall_ratio(self, n: int = 10) -> float:
        with self.lock:
            rows = self.conn.execute(
                """
                SELECT exceeds_poll_interval FROM cycle_stats
                WHERE exceeds_poll_interval IS NOT NULL
                ORDER BY id DESC LIMIT ?
                """,
                (n,),
            ).fetchall()
        if not rows:
            return 0.0
        stalled = sum(1 for r in rows if int(r[0] or 0))
        return stalled / len(rows)

    def get_stall_tracker(self) -> dict[str, Any]:
        with self.lock:
            row = self.conn.execute(
                "SELECT payload_json FROM client_alert_state WHERE kind = ?",
                (STALL_TRACKER_KIND,),
            ).fetchone()
        if not row:
            return {
                "consecutive_stall_count": 0,
                "suppressed_stall_count": 0,
                "last_timely_cycle_il": None,
                "pending_recovery": False,
            }
        try:
            data = json.loads(str(row[0]))
        except json.JSONDecodeError:
            data = {}
        return {
            "consecutive_stall_count": int(data.get("consecutive_stall_count") or 0),
            "suppressed_stall_count": int(data.get("suppressed_stall_count") or 0),
            "last_timely_cycle_il": data.get("last_timely_cycle_il"),
            "pending_recovery": bool(data.get("pending_recovery")),
        }

    def set_stall_tracker(self, data: dict[str, Any]) -> None:
        payload = json.dumps(data, ensure_ascii=False)
        now = israel_now_iso()
        with self.lock:
            self.conn.execute(
                """
                INSERT INTO client_alert_state (kind, last_sent_at_il, sent_count_in_window, window_started_il, payload_json)
                VALUES (?, ?, 0, ?, ?)
                ON CONFLICT(kind) DO UPDATE SET payload_json = excluded.payload_json
                """,
                (STALL_TRACKER_KIND, now, now, payload),
            )
            self.conn.commit()

    def get_alert_state(self, kind: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.conn.execute(
                "SELECT last_sent_at_il, sent_count_in_window, window_started_il FROM client_alert_state WHERE kind = ?",
                (kind,),
            ).fetchone()
        if not row:
            return None
        return {
            "last_sent_at_il": str(row[0]),
            "sent_count_in_window": int(row[1]),
            "window_started_il": str(row[2]),
        }

    def record_alert_sent(self, kind: str) -> None:
        now = israel_now_iso()
        with self.lock:
            row = self.conn.execute(
                "SELECT sent_count_in_window, window_started_il FROM client_alert_state WHERE kind = ?",
                (kind,),
            ).fetchone()
            if row:
                window_started = str(row[1])
                window_hours = 6
                try:
                    if _parse_il(now) - _parse_il(window_started) > timedelta(hours=window_hours):
                        count = 1
                        window_started = now
                    else:
                        count = int(row[0]) + 1
                except ValueError:
                    count = 1
                    window_started = now
                self.conn.execute(
                    """
                    UPDATE client_alert_state
                    SET last_sent_at_il = ?, sent_count_in_window = ?, window_started_il = ?
                    WHERE kind = ?
                    """,
                    (now, count, window_started, kind),
                )
            else:
                self.conn.execute(
                    """
                    INSERT INTO client_alert_state (kind, last_sent_at_il, sent_count_in_window, window_started_il, payload_json)
                    VALUES (?, ?, 1, ?, '{}')
                    """,
                    (kind, now, now),
                )
            stall_row = self.conn.execute(
                "SELECT payload_json FROM client_alert_state WHERE kind = ?",
                (STALL_TRACKER_KIND,),
            ).fetchone()
            if stall_row:
                try:
                    tracker = json.loads(str(stall_row[0]))
                except json.JSONDecodeError:
                    tracker = {}
            else:
                tracker = {}
            tracker["pending_recovery"] = True
            payload = json.dumps(tracker, ensure_ascii=False)
            self.conn.execute(
                """
                INSERT INTO client_alert_state (kind, last_sent_at_il, sent_count_in_window, window_started_il, payload_json)
                VALUES (?, ?, 0, ?, ?)
                ON CONFLICT(kind) DO UPDATE SET payload_json = excluded.payload_json
                """,
                (STALL_TRACKER_KIND, now, now, payload),
            )
            self.conn.commit()

    def maybe_prune(self, config: dict[str, Any]) -> None:
        if not self.enabled(config):
            return
        now = israel_now_iso()
        if self._last_prune_il:
            try:
                if _parse_il(now) - _parse_il(self._last_prune_il) < timedelta(hours=24):
                    return
            except ValueError:
                pass
        stats_days = int(config.get("telemetry_stats_keep_days", 90))
        events_days = int(config.get("telemetry_events_keep_days", 7))
        cutoff_stats = (datetime.now(_israel_tz()) - timedelta(days=stats_days)).isoformat(timespec="seconds")
        cutoff_events = (datetime.now(_israel_tz()) - timedelta(days=events_days)).isoformat(timespec="seconds")
        with self.lock:
            self.conn.execute("DELETE FROM debug_events WHERE recorded_at_il < ?", (cutoff_events,))
            self.conn.execute("DELETE FROM cycle_stats WHERE recorded_at_il < ?", (cutoff_stats,))
            self.conn.commit()
        self._last_prune_il = now
