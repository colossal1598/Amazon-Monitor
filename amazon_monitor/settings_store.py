"""SQLite-backed runtime settings and ASIN list storage."""

from __future__ import annotations

import copy
import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import yaml

from pdp_helpers import valid_asin

_ROLES = {"watch", "blacklist"}

DEFAULT_RUNTIME_CONFIG: dict[str, Any] = {
    "pdp_poll_minutes": 4,
    "playwright_headless": True,
    "max_cycle_seconds": 170,
    "max_requests_per_minute": 10,
    "captcha_recovery_pause_seconds": 120,
    "price_drop_percent": 10,
    "search_urls": {
        "aes_llc": "https://www.amazon.com/s?i=merchant-items&me=A2YBZOEOYPROUE",
    },
    "required_keywords": ["pokemon", "tcg"],
    "title_blacklist_phrases": [],
    "pdp_allowed_seller_substrings": ["amazon.com", "amazon export"],
    "pdp_watch_max_concurrent_tabs": 2,
    "pdp_watch_max_attempts": 3,
    "pdp_watch_tab_jitter_seconds": [0.15, 0.55],
    "pdp_watch_scroll_delay_seconds": [0.25, 0.65],
    "fx_enabled": True,
    "fx_refresh_every_runs": 10,
    "fx_cache_path": "data/fx_usd_ils.json",
    "image_cache_dir": "data/product_images",
    "fx_fallback_usd_ils": 3,
    "fx_request_timeout_seconds": 5,
    "affiliate_tag": "yourclient-20",
    "wa_api_url": "http://localhost:3001/send",
    "wa_group_id": "120363408155154756@g.us",
    "wa_client_to": "",
    "wa_send_heartbeat": False,
    "wa_message_templates": {
        "default": (
            "New product detected!\n"
            "Title: {title}\n"
            "Price: {price_text}\n"
            "{shipping}\n"
            "Link: {affiliate_link}"
        ),
        "new_product": (
            "New product detected!\n"
            "Title: {title}\n"
            "Price: {price_text}\n"
            "{shipping}\n"
            "Link: {affiliate_link}"
        ),
        "price_drop": (
            "Price drop detected!\n"
            "Title: {title}\n"
            "Price: {price_text}\n"
            "{shipping}\n"
            "Link: {affiliate_link}"
        ),
        "back_in_stock": (
            "Back in stock!\n"
            "Title: {title}\n"
            "Price: {price_text}\n"
            "{shipping}\n"
            "Link: {affiliate_link}"
        ),
    },
    "db_path": "data/monitor.db",
    "log_dir": "logs",
    "auth_dir": "auth",
}


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect(db_path: str) -> sqlite3.Connection:
    db_file = Path(db_path)
    db_file.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS asins (
            asin TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('watch', 'blacklist')),
            enabled INTEGER NOT NULL DEFAULT 1,
            notes TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (asin, role)
        );
        CREATE INDEX IF NOT EXISTS idx_asins_role ON asins(role, enabled);
        """
    )


def _decode_value(raw_value: str) -> Any:
    try:
        return json.loads(raw_value)
    except json.JSONDecodeError:
        return raw_value


def _encode_value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _valid_role(role: str) -> str:
    role_norm = str(role).strip().lower()
    if role_norm not in _ROLES:
        raise ValueError(f"Unsupported role: {role}")
    return role_norm


def _normalize_asins(values: Iterable[Any]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        asin = str(value).strip().upper()
        if not valid_asin(asin) or asin in seen:
            continue
        seen.add(asin)
        out.append(asin)
    return out


def get_setting(db_path: str, key: str, default: Any = None) -> Any:
    with closing(_connect(db_path)) as conn:
        _ensure_tables(conn)
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return default
    return _decode_value(str(row["value"]))


def set_setting(db_path: str, key: str, value: Any) -> None:
    now = _utc_iso()
    encoded = _encode_value(value)
    with closing(_connect(db_path)) as conn:
        _ensure_tables(conn)
        conn.execute(
            """
            INSERT INTO settings(key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (key, encoded, now),
        )
        conn.commit()


def list_asins(db_path: str, role: str) -> list[str]:
    role_norm = _valid_role(role)
    with closing(_connect(db_path)) as conn:
        _ensure_tables(conn)
        rows = conn.execute(
            """
            SELECT asin
            FROM asins
            WHERE role = ? AND enabled = 1
            ORDER BY created_at ASC, asin ASC
            """,
            (role_norm,),
        ).fetchall()
    return [str(row["asin"]).upper() for row in rows]


def list_asin_entries(db_path: str, role: str) -> list[dict[str, Any]]:
    role_norm = _valid_role(role)
    with closing(_connect(db_path)) as conn:
        _ensure_tables(conn)
        rows = conn.execute(
            """
            SELECT asin, notes, created_at, updated_at
            FROM asins
            WHERE role = ? AND enabled = 1
            ORDER BY created_at ASC, asin ASC
            """,
            (role_norm,),
        ).fetchall()
    return [
        {
            "asin": str(row["asin"]).upper(),
            "notes": str(row["notes"] or ""),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }
        for row in rows
    ]


def add_asin(
    db_path: str,
    asin: str,
    role: str,
    *,
    enabled: bool = True,
    notes: str | None = None,
) -> None:
    role_norm = _valid_role(role)
    normalized = _normalize_asins([asin])
    if not normalized:
        return
    asin_norm = normalized[0]
    now = _utc_iso()
    with closing(_connect(db_path)) as conn:
        _ensure_tables(conn)
        conn.execute(
            """
            INSERT INTO asins(asin, role, enabled, notes, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(asin, role) DO UPDATE SET
                enabled = excluded.enabled,
                notes = excluded.notes,
                updated_at = excluded.updated_at
            """,
            (asin_norm, role_norm, 1 if enabled else 0, notes, now, now),
        )
        conn.commit()


def remove_asin(db_path: str, asin: str, role: str) -> None:
    role_norm = _valid_role(role)
    normalized = _normalize_asins([asin])
    if not normalized:
        return
    with closing(_connect(db_path)) as conn:
        _ensure_tables(conn)
        conn.execute("DELETE FROM asins WHERE asin = ? AND role = ?", (normalized[0], role_norm))
        conn.commit()


def replace_asins(db_path: str, role: str, values: Iterable[Any]) -> None:
    role_norm = _valid_role(role)
    normalized = _normalize_asins(values)
    now = _utc_iso()
    with closing(_connect(db_path)) as conn:
        _ensure_tables(conn)
        conn.execute("DELETE FROM asins WHERE role = ?", (role_norm,))
        for asin in normalized:
            conn.execute(
                """
                INSERT INTO asins(asin, role, enabled, notes, created_at, updated_at)
                VALUES (?, ?, 1, NULL, ?, ?)
                """,
                (asin, role_norm, now, now),
            )
        conn.commit()


def _settings_row_count(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS count FROM settings").fetchone()
    if row is None:
        return 0
    return int(row["count"])


def _set_setting_with_conn(conn: sqlite3.Connection, key: str, value: Any) -> None:
    now = _utc_iso()
    conn.execute(
        """
        INSERT INTO settings(key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            value = excluded.value,
            updated_at = excluded.updated_at
        """,
        (key, _encode_value(value), now),
    )


def migrate_yaml_to_db(yaml_path: str, db_path: str) -> bool:
    """Import legacy YAML config once when settings are empty."""
    with closing(_connect(db_path)) as conn:
        _ensure_tables(conn)
        if _settings_row_count(conn) > 0:
            return False

        path = Path(yaml_path)
        if path.is_file():
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
            config = loaded if isinstance(loaded, dict) else {}
        else:
            config = {}

        watch_asins = config.get("pdp_watch_asins") if isinstance(config, dict) else []
        blacklist_asins = config.get("blacklist") if isinstance(config, dict) else []

        for key, value in config.items():
            if key in {"pdp_watch_asins", "blacklist", "whitelist", "wa_api_key"}:
                continue
            _set_setting_with_conn(conn, str(key), value)

        now = _utc_iso()
        for asin in _normalize_asins(watch_asins or []):
            conn.execute(
                """
                INSERT INTO asins(asin, role, enabled, notes, created_at, updated_at)
                VALUES (?, 'watch', 1, NULL, ?, ?)
                ON CONFLICT(asin, role) DO UPDATE SET
                    enabled = 1,
                    updated_at = excluded.updated_at
                """,
                (asin, now, now),
            )
        for asin in _normalize_asins(blacklist_asins or []):
            conn.execute(
                """
                INSERT INTO asins(asin, role, enabled, notes, created_at, updated_at)
                VALUES (?, 'blacklist', 1, NULL, ?, ?)
                ON CONFLICT(asin, role) DO UPDATE SET
                    enabled = 1,
                    updated_at = excluded.updated_at
                """,
                (asin, now, now),
            )

        conn.commit()
    return True


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    if value:
        return value
    lower = os.environ.get(name.lower())
    if lower:
        return lower
    return None


def load_runtime_config(db_path: str) -> dict[str, Any]:
    config: dict[str, Any] = copy.deepcopy(DEFAULT_RUNTIME_CONFIG)
    with closing(_connect(db_path)) as conn:
        _ensure_tables(conn)
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    for row in rows:
        key = str(row["key"])
        if key in {"pdp_watch_asins", "blacklist", "whitelist", "wa_api_key"}:
            continue
        config[key] = _decode_value(str(row["value"]))

    config["pdp_watch_asins"] = list_asins(db_path, "watch")
    config["blacklist"] = list_asins(db_path, "blacklist")
    config.pop("wa_api_key", None)

    wa_api_url = _env("WA_API_URL")
    if wa_api_url:
        config["wa_api_url"] = wa_api_url

    wa_api_key = _env("WA_API_KEY")
    if wa_api_key:
        config["wa_api_key"] = wa_api_key

    return config
