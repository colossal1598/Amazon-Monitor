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
    # Streaming engine: target freshness per watch ASIN. Actual pace is bounded by
    # max_requests_per_minute and stream_concurrent_tabs; with 25-35 ASINs raise
    # max_requests_per_minute accordingly (engine logs sweep_sec so you can verify).
    "asin_check_interval_seconds": 60,
    "stream_concurrent_tabs": 2,
    "aes_check_minutes": 5,
    "browser_recycle_minutes": 60,
    # Headed is the production mode of record: headless sessions get served
    # offer-less (nav-shell) PDPs. The engine falls back to headless (with a
    # client alert) when no desktop session exists.
    "playwright_headless": False,
    "max_requests_per_minute": 25,
    "captcha_recovery_pause_seconds": 120,
    "price_drop_percent": 10,
    "search_urls": {
        "aes_llc": "https://www.amazon.com/s?i=merchant-items&me=A2YBZOEOYPROUE",
    },
    "required_keywords": ["pokemon", "tcg"],
    "title_blacklist_phrases": [],
    "pdp_allowed_seller_substrings": ["amazon.com", "amazon export"],
    "pdp_watch_max_attempts": 2,
    "pdp_watch_tab_jitter_seconds": [0.15, 0.55],
    "pdp_watch_scroll_delay_seconds": [0.25, 0.65],
    "pdp_settle_seconds": 8.0,
    "pdp_settle_poll_interval_seconds": 1.0,
    "pdp_unknown_retry_seconds": 2.5,
    "pdp_continue_shopping_max_clicks": 3,
    "cross_source_alert_dedupe_minutes": 15,
    # Minimum gap between two identical stock alerts (same ASIN + type) from ANY
    # source. Stops flapping state (page-1 churn, transient scrape misses) from
    # spamming repeated back_in_stock/new_product messages. 0 disables.
    "stock_alert_cooldown_minutes": 60,
    # Shorter cooldown used instead when the item's previous out-of-stock was backed
    # by explicit evidence (PDP/card said "currently unavailable" etc.). Real
    # sell-out -> restock cycles are common for Pokemon drops and must re-alert fast.
    "stock_alert_confirmed_cooldown_minutes": 10,
    # Suppress a back_in_stock at the SAME price as the last one within this window,
    # unless the preceding OOS was a strong page-text sellout (last_oos_reason).
    # Kills seller-rotation / SERP-flap re-alert churn; price changes always alert.
    # 0 disables.
    "stock_alert_same_price_dedupe_minutes": 360,
    # Consecutive AES/SERP cycles an item must look out-of-stock (absent from the
    # filtered page or non-qualifying row) before the mirror flips to OOS. 1 = old
    # immediate behavior.
    "aes_oos_confirm_cycles": 3,
    # Consecutive PDP scrapes with *weak* OOS evidence (buybox churn, missing
    # price, inferred signals) before a watched product flips to out-of-stock.
    # Strong page text ("currently unavailable" etc.) still flips immediately.
    # 1 = old immediate behavior.
    "pdp_oos_confirm_cycles": 2,
    # Price-less purchasable buybox (price never rendered) restock path: alert after
    # this many consecutive priceless checks, on its own cooldown.
    "pdp_priceless_restock_alert": True,
    "pdp_priceless_confirm_checks": 2,
    "pdp_priceless_alert_cooldown_minutes": 30,
    # Legacy preorder->preorder re-alert gate, default OFF (kept as rollback lever
    # for flapping spam; suppressing consumes the 0->1 edge — see state_engine).
    "pdp_preorder_realert_suppression": False,
    # C2 fast-recheck for unknown/priceless rows: override interval and max repeats.
    "pdp_unknown_fast_retry_seconds": 15,
    "pdp_unknown_fast_retry_max": 3,
    # Degraded-page (skeleton/nav-shell) burst -> proactive session recycle.
    "degraded_recycle_threshold": 4,
    "degraded_recycle_window_seconds": 180,
    # F1 AOD side-fetch gating: min gap per OOS ASIN (previously-in-stock ASINs
    # always re-check), and engine-wide disable after consecutive fetch failures.
    "pdp_aod_min_interval_seconds": 240,
    "aod_fail_disable_threshold": 5,
    "aod_fail_disable_minutes": 30,
    # F2 mass-flip circuit breaker: >= min_flips DISTINCT in-stock ASINs flipping
    # OOS inside the window under a degraded context = poisoned session; hold flips
    # as degraded skips for hold_seconds and request a recycle.
    "mass_flip_min_flips": 2,
    "mass_flip_window_seconds": 120,
    "mass_flip_hold_seconds": 120,
    # Consecutive AES soft-fail cycles before the engine recycles the session.
    "aes_recycle_fail_streak": 2,
    # Dump the PDP HTML to data/debug_no_price on no-price extractions (keep-20).
    "pdp_dump_no_price_html": True,
    # Off-screen browser window experiment — OFF: it fingerprints (2026-07-16).
    "browser_window_offscreen": False,
    "metrics_enabled": True,
    # Watchdog: engine restarted if no progress for this long.
    "watchdog_stall_seconds": 600,
    # WhatsApp webhook delivery retries (webhook_sender).
    "wa_send_attempts": 3,
    "wa_send_retry_backoff_seconds": 2.0,
    # AES/SERP result-card wait per attempt. Kept short: this is a lightweight 1-page
    # check and normally resolves in a few seconds; a long timeout here just delays
    # detection of dead/error pages and risks overlapping the next scheduled cycle.
    "aes_selector_timeout_ms": 12_000,
    # Navigation retries *within* one AES attempt when Amazon serves its own
    # "Sorry! Something went wrong" soft-error interstitial or the result cards
    # don't show up in time. Confirmed from production logs: this page recurs on
    # many consecutive scheduled AES cycles in a row, so more headroom here (with
    # increasing backoff) meaningfully reduces "skipped this whole cycle" outcomes.
    "aes_serp_inner_retries": 2,
    # Extra whole-attempt retries around the AES scrape call (new browser tab,
    # fresh navigation) when every inner retry above was exhausted. 1 = try twice
    # total before giving up on this aes_check_minutes interval.
    "aes_outer_retries": 1,
    "fx_enabled": True,
    "fx_refresh_every_runs": 10,
    "fx_cache_path": "data/fx_usd_ils.json",
    "image_cache_dir": "data/product_images",
    "telemetry_enabled": True,
    "telemetry_db_path": "data/telemetry.db",
    "telemetry_stats_keep_days": 90,
    "telemetry_events_keep_days": 7,
    # Operator decision 2026-07-23: operational error messages to the client's WhatsApp
    # teach nothing and add pressure — everything still lands in logs/telemetry.
    "client_alerts_enabled": False,
    "client_alert_cooldown_minutes": 30,
    "client_alert_max_per_window": 3,
    "client_alert_window_hours": 6,
    "client_alert_scrape_fail_min": 2,
    "client_alert_scrape_fail_ratio": 0.5,
    "client_alert_chronic_stall_consecutive": 5,
    "client_alert_chronic_stall_cooldown_hours": 24,
    # A single cycle that runs a bit long (e.g. one AES/SERP hiccup that already
    # self-recovered) shouldn't page anyone — require this many *consecutive*
    # over-poll-interval cycles before sending the "stalled" client alert.
    "client_alert_stall_min_consecutive": 2,
    # Same idea for AES/SERP scrape degradation: require this many consecutive bad
    # AES cycles in a row before sending "scrape_degraded" for AES-only causes.
    # PDP-side degradation (client_alert_scrape_fail_min/ratio above) is unaffected.
    "client_alert_aes_fail_consecutive": 3,
    # The "all clear, back to normal" follow-up ping sent after a stalled/degraded
    # alert recovers. Considered noise on its own — off by default.
    "client_alert_recovery_enabled": False,
    # Minimum gap between two price_below_target alerts for the same ASIN. The
    # alert has its own arm/disarm hysteresis (fires once per below-target
    # crossing); this cooldown additionally guards against a fast re-cross
    # (price bounces back above target and dips below again) within the window.
    "target_price_alert_cooldown_hours": 6,
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
    # Empty list uses browser_factory.DEFAULT_BANDWIDTH_BLOCK_URL_SUBSTRINGS at runtime.
    "bandwidth_block_url_substrings": [],
    "bandwidth_block_stylesheets": False,
    "bandwidth_block_non_amazon_script": False,
    "bandwidth_meter_max_body_reads_per_cycle": 200,
}


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect(db_path: str) -> sqlite3.Connection:
    db_file = Path(db_path)
    db_file.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    return conn


def _table_has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(str(row[1]) == column for row in rows)


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
    if not _table_has_column(conn, "asins", "target_price"):
        conn.execute("ALTER TABLE asins ADD COLUMN target_price REAL")


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
            SELECT asin, notes, created_at, updated_at, target_price
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
            "target_price": float(row["target_price"]) if row["target_price"] is not None else None,
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
    target_price: float | None = None,
) -> None:
    role_norm = _valid_role(role)
    normalized = _normalize_asins([asin])
    if not normalized:
        return
    asin_norm = normalized[0]
    now = _utc_iso()
    target_price_val = float(target_price) if target_price is not None else None
    with closing(_connect(db_path)) as conn:
        _ensure_tables(conn)
        conn.execute(
            """
            INSERT INTO asins(asin, role, enabled, notes, created_at, updated_at, target_price)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(asin, role) DO UPDATE SET
                enabled = excluded.enabled,
                notes = excluded.notes,
                updated_at = excluded.updated_at,
                target_price = excluded.target_price
            """,
            (asin_norm, role_norm, 1 if enabled else 0, notes, now, now, target_price_val),
        )
        conn.commit()


def set_asin_target_price(db_path: str, asin: str, target_price: float | None) -> bool:
    """Set (or clear, with None) the target price for a watched ASIN.

    Returns False when there is no ``watch`` row for this ASIN (nothing to update).
    """
    normalized = _normalize_asins([asin])
    if not normalized:
        return False
    asin_norm = normalized[0]
    now = _utc_iso()
    target_price_val = float(target_price) if target_price is not None else None
    with closing(_connect(db_path)) as conn:
        _ensure_tables(conn)
        cursor = conn.execute(
            """
            UPDATE asins
            SET target_price = ?, updated_at = ?
            WHERE asin = ? AND role = 'watch'
            """,
            (target_price_val, now, asin_norm),
        )
        conn.commit()
        return cursor.rowcount > 0


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


def _watch_target_prices(db_path: str) -> dict[str, float]:
    with closing(_connect(db_path)) as conn:
        _ensure_tables(conn)
        rows = conn.execute(
            """
            SELECT asin, target_price
            FROM asins
            WHERE role = 'watch' AND enabled = 1 AND target_price IS NOT NULL
            """
        ).fetchall()
    return {str(row["asin"]).upper(): float(row["target_price"]) for row in rows}


# Settings rows that no code reads anymore. Pruned on every load_runtime_config so
# stale overrides stop masquerading as tunables (production carried max_cycle_seconds /
# pdp_poll_minutes from the pre-streaming scheduler era, plus superseded PDP waits).
_REMOVED_SETTINGS_KEYS: tuple[str, ...] = (
    "max_cycle_seconds",
    "pdp_poll_minutes",
    "pdp_title_wait_ms",
    "pdp_price_wait_ms",
    "pdp_watch_max_concurrent_tabs",
)


def load_runtime_config(db_path: str) -> dict[str, Any]:
    config: dict[str, Any] = copy.deepcopy(DEFAULT_RUNTIME_CONFIG)
    with closing(_connect(db_path)) as conn:
        _ensure_tables(conn)
        removed = conn.execute(
            f"DELETE FROM settings WHERE key IN ({','.join('?' * len(_REMOVED_SETTINGS_KEYS))})",
            _REMOVED_SETTINGS_KEYS,
        )
        if removed.rowcount:
            conn.commit()
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    for row in rows:
        key = str(row["key"])
        if key in {"pdp_watch_asins", "blacklist", "whitelist", "wa_api_key"}:
            continue
        config[key] = _decode_value(str(row["value"]))

    config["pdp_watch_asins"] = list_asins(db_path, "watch")
    config["blacklist"] = list_asins(db_path, "blacklist")
    config["pdp_watch_target_prices"] = _watch_target_prices(db_path)
    config.pop("wa_api_key", None)

    wa_api_url = _env("WA_API_URL")
    if wa_api_url:
        config["wa_api_url"] = wa_api_url

    wa_api_key = _env("WA_API_KEY")
    if wa_api_key:
        config["wa_api_key"] = wa_api_key

    return config
