"""SQLite-backed state management for search monitoring."""

import logging
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from alert_decisions import decide_back_in_stock, decide_new_product, decide_price_drop

LOGGER = logging.getLogger(__name__)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(dt: datetime | None = None) -> str:
    return (dt or utc_now()).isoformat()


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class StateEngine:
    """Tracks products, detects changes, and records generated alerts."""

    def __init__(self, db_path: str, price_drop_percent: float) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.price_drop_percent = float(price_drop_percent)
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.init_db()
        self._price_alert_cooldown = timedelta(hours=24)

    def init_db(self) -> None:
        with self.lock:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS products (
                    asin TEXT PRIMARY KEY,
                    title TEXT,
                    seller TEXT,
                    price REAL,
                    in_stock INTEGER,
                    first_seen TEXT,
                    last_seen TEXT,
                    last_price_alert TEXT,
                    last_stock_alert TEXT
                );

                CREATE TABLE IF NOT EXISTS alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    asin TEXT,
                    alert_type TEXT,
                    source TEXT,
                    old_price REAL,
                    new_price REAL,
                    sent_at TEXT
                );
                """
            )
            self.conn.commit()

    def _fetch_product(self, asin: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM products WHERE asin = ?", (asin,)).fetchone()

    def list_known_asins(self) -> set[str]:
        """All ASINs currently in the products table (uppercase)."""
        with self.lock:
            rows = self.conn.execute("SELECT asin FROM products").fetchall()
        return {str(r[0]).upper() for r in rows if r and r[0]}

    def _record_alert(self, alert: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO alerts (asin, alert_type, source, old_price, new_price, sent_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                alert.get("asin"),
                alert.get("type"),
                alert.get("source"),
                alert.get("old_price"),
                alert.get("new_price"),
                alert.get("timestamp"),
            ),
        )

    def _build_alert(
        self,
        alert_type: str,
        source: str,
        asin: str,
        title: str | None,
        price: float | None,
        old_price: float | None = None,
        new_price: float | None = None,
        image_url: str | None = None,
    ) -> dict[str, Any]:
        pct = None
        if old_price is not None and new_price is not None and old_price > 0:
            pct = round(((old_price - new_price) / old_price) * 100, 2)
        return {
            "type": alert_type,
            "asin": asin,
            "title": title,
            "price": price,
            "old_price": old_price,
            "new_price": new_price,
            "percentage": pct,
            "source": source,
            "image_url": image_url,
            "timestamp": utc_iso(),
        }

    def _mark_missing_asins_out_of_stock(self, seen_asins: set[str], source: str, now: str) -> int:
        """Mark tracked ASINs absent from this healthy run as out-of-stock."""
        if seen_asins:
            placeholders = ",".join("?" for _ in seen_asins)
            params: list[Any] = [now, source, *sorted(seen_asins)]
            cursor = self.conn.execute(
                f"""
                UPDATE products
                SET in_stock = 0,
                    last_seen = ?
                WHERE seller = ?
                  AND in_stock != 0
                  AND asin NOT IN ({placeholders})
                """,
                params,
            )
        else:
            cursor = self.conn.execute(
                """
                UPDATE products
                SET in_stock = 0,
                    last_seen = ?
                WHERE seller = ?
                  AND in_stock != 0
                """,
                (now, source),
            )
        return cursor.rowcount

    def process_search_candidates(
        self,
        candidates: list[dict[str, Any]],
        reconcile_missing: bool = False,
        source: str = "amazon_export",
    ) -> list[dict[str, Any]]:
        """Process scraped search candidates and emit new/stock/price alerts.

        When `reconcile_missing` is true, any tracked ASIN for the same source
        that is absent from this successful run is marked out-of-stock.
        """
        alerts: list[dict[str, Any]] = []
        seen_asins: set[str] = set()
        new_count = 0
        back_in_stock_count = 0
        price_drop_count = 0
        with self.lock:
            for item in candidates:
                asin = (item.get("asin") or "").upper()
                if not asin:
                    continue
                seen_asins.add(asin)
                title = item.get("title")
                seller = item.get("seller") or source
                image_url = item.get("image_url")
                new_price = _as_float(item.get("price"))
                # Presence in a healthy, filtered run is the stock signal.
                # Missing-ASIN reconciliation marks absent items out-of-stock.
                new_stock = 1
                now = utc_iso()
                now_dt = utc_now()

                row = self._fetch_product(asin)
                if row is None:
                    nd = decide_new_product(is_first_observation=True)
                    assert nd.emit and nd.alert_type is not None
                    self.conn.execute(
                        """
                        INSERT INTO products
                        (asin, title, seller, price, in_stock, first_seen, last_seen)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (asin, title, seller, new_price, new_stock, now, now),
                    )
                    alert = self._build_alert(nd.alert_type, "search", asin, title, new_price, image_url=image_url)
                    alerts.append(alert)
                    self._record_alert(alert)
                    new_count += 1
                    continue

                old_price = _as_float(row["price"])
                old_stock = int(row["in_stock"] or 0)
                self.conn.execute(
                    """
                    UPDATE products
                    SET title = COALESCE(?, title),
                        seller = COALESCE(?, seller),
                        price = ?,
                        in_stock = ?,
                        last_seen = ?
                    WHERE asin = ?
                    """,
                    (title, seller, new_price, new_stock, now, asin),
                )

                stock_decision = decide_back_in_stock(old_stock, new_stock)
                if stock_decision.emit:
                    alert = self._build_alert("back_in_stock", "search", asin, title, new_price, image_url=image_url)
                    alerts.append(alert)
                    self._record_alert(alert)
                    self.conn.execute("UPDATE products SET last_stock_alert = ? WHERE asin = ?", (now, asin))
                    back_in_stock_count += 1
                elif stock_decision.skip_reason:
                    LOGGER.info("alert_skip asin=%s alert=back_in_stock reason=%s", asin, stock_decision.skip_reason)

                last_price_alert = parse_dt(row["last_price_alert"])
                price_decision = decide_price_drop(
                    old_price,
                    new_price,
                    last_price_alert,
                    now_dt,
                    self.price_drop_percent,
                    self._price_alert_cooldown,
                )
                if price_decision.emit:
                    alert = self._build_alert(
                        "price_drop",
                        "search",
                        asin,
                        title,
                        new_price,
                        old_price=old_price,
                        new_price=new_price,
                        image_url=image_url,
                    )
                    alerts.append(alert)
                    self._record_alert(alert)
                    self.conn.execute("UPDATE products SET last_price_alert = ? WHERE asin = ?", (now, asin))
                    price_drop_count += 1
                elif price_decision.skip_reason:
                    LOGGER.info("alert_skip asin=%s alert=price_drop reason=%s", asin, price_decision.skip_reason)
            marked_oos_count = 0
            if reconcile_missing:
                marked_oos_count = self._mark_missing_asins_out_of_stock(seen_asins, source, utc_iso())
            LOGGER.info(
                "search_reconcile seen_count=%s new_count=%s marked_oos_count=%s "
                "back_in_stock_count=%s price_drop_count=%s reconcile_missing=%s",
                len(seen_asins),
                new_count,
                marked_oos_count,
                back_in_stock_count,
                price_drop_count,
                reconcile_missing,
            )
            self.conn.commit()
        return alerts

    def seed_candidates_without_alerts(
        self,
        candidates: list[dict[str, Any]],
        source: str = "main_search",
    ) -> int:
        """Upsert candidates into products without creating alerts (bootstrap mode)."""
        seeded = 0
        with self.lock:
            now = utc_iso()
            for item in candidates:
                asin = (item.get("asin") or "").upper()
                if not asin:
                    continue
                title = item.get("title")
                seller = item.get("seller") or source
                new_price = _as_float(item.get("price"))
                new_stock = 1
                row = self._fetch_product(asin)
                if row is None:
                    self.conn.execute(
                        """
                        INSERT INTO products
                        (asin, title, seller, price, in_stock, first_seen, last_seen)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (asin, title, seller, new_price, new_stock, now, now),
                    )
                    seeded += 1
                    continue
                self.conn.execute(
                    """
                    UPDATE products
                    SET title = COALESCE(?, title),
                        seller = COALESCE(?, seller),
                        price = ?,
                        in_stock = ?,
                        last_seen = ?
                    WHERE asin = ?
                    """,
                    (title, seller, new_price, new_stock, now, asin),
                )
                seeded += 1
            self.conn.commit()
        return seeded
