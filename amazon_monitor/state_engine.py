import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

"""SQLite-backed state management for search monitoring."""


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
        if old_price and new_price and old_price > 0:
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

    def process_search_candidates(self, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Process scraped search candidates and emit new/stock/price alerts."""
        alerts: list[dict[str, Any]] = []
        with self.lock:
            for item in candidates:
                asin = (item.get("asin") or "").upper()
                if not asin:
                    continue
                title = item.get("title")
                seller = item.get("seller")
                amazon_sold = bool(item.get("amazon_sold", False))
                if seller == "amazon_com" and not amazon_sold:
                    continue
                image_url = item.get("image_url")
                new_price = item.get("price")
                new_stock = 1 if item.get("in_stock") else 0
                now = utc_iso()

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
                    alert = self._build_alert("new_product", "search", asin, title, new_price, image_url=image_url)
                    alerts.append(alert)
                    self._record_alert(alert)
                    continue

                old_price = row["price"]
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

                if old_stock == 0 and new_stock == 1:
                    alert = self._build_alert("back_in_stock", "search", asin, title, new_price, image_url=image_url)
                    alerts.append(alert)
                    self._record_alert(alert)
                    self.conn.execute("UPDATE products SET last_stock_alert = ? WHERE asin = ?", (now, asin))

                last_price_alert = parse_dt(row["last_price_alert"])
                if old_price and new_price and new_price < old_price:
                    pct_drop = ((old_price - new_price) / old_price) * 100
                    enough_drop = pct_drop >= self.price_drop_percent
                    cooldown_ok = not last_price_alert or utc_now() - last_price_alert > timedelta(hours=24)
                    if enough_drop and cooldown_ok:
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
            self.conn.commit()
        return alerts


