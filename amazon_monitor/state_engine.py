import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


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
    def __init__(self, db_path: str, price_drop_percent: float, shipping_cache_hours: int) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.price_drop_percent = float(price_drop_percent)
        self.shipping_cache_hours = int(shipping_cache_hours)
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
                    free_shipping_il INTEGER DEFAULT -1,
                    priority INTEGER DEFAULT 0,
                    first_seen TEXT,
                    last_seen TEXT,
                    last_price_alert TEXT,
                    last_stock_alert TEXT,
                    shipping_checked_at TEXT
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

    def process_cart_snapshots(self, snapshots: list[dict[str, Any]]) -> list[dict[str, Any]]:
        alerts: list[dict[str, Any]] = []
        with self.lock:
            for snap in snapshots:
                asin = snap["asin"].upper()
                new_price = snap.get("price")
                new_stock = 1 if snap.get("in_stock") else 0
                row = self._fetch_product(asin)
                now = utc_iso()
                if row is None:
                    self.conn.execute(
                        """
                        INSERT INTO products
                        (asin, title, seller, price, in_stock, priority, first_seen, last_seen)
                        VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                        """,
                        (asin, None, "cart", new_price, new_stock, now, now),
                    )
                    continue

                old_price = row["price"]
                old_stock = int(row["in_stock"] or 0)
                title = row["title"]
                self.conn.execute(
                    """
                    UPDATE products
                    SET price = ?, in_stock = ?, priority = 1, last_seen = ?
                    WHERE asin = ?
                    """,
                    (new_price, new_stock, now, asin),
                )

                if old_stock == 0 and new_stock == 1:
                    alert = self._build_alert("back_in_stock", "cart", asin, title, new_price, image_url=None)
                    alerts.append(alert)
                    self._record_alert(alert)
                    self.conn.execute("UPDATE products SET last_stock_alert = ? WHERE asin = ?", (now, asin))

                last_price_alert = parse_dt(row["last_price_alert"])
                if old_price and new_price and new_price < old_price:
                    pct_drop = ((old_price - new_price) / old_price) * 100
                    enough_drop = pct_drop >= self.price_drop_percent
                    cooldown_ok = not last_price_alert or utc_now() - last_price_alert > timedelta(minutes=5)
                    if enough_drop and cooldown_ok:
                        alert = self._build_alert(
                            "price_drop",
                            "cart",
                            asin,
                            title,
                            new_price,
                            old_price=old_price,
                            new_price=new_price,
                        )
                        alerts.append(alert)
                        self._record_alert(alert)
                        self.conn.execute("UPDATE products SET last_price_alert = ? WHERE asin = ?", (now, asin))
            self.conn.commit()
        return alerts

    def process_search_candidates(self, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        alerts: list[dict[str, Any]] = []
        with self.lock:
            for item in candidates:
                asin = (item.get("asin") or "").upper()
                if not asin:
                    continue
                title = item.get("title")
                seller = item.get("seller")
                image_url = item.get("image_url")
                new_price = item.get("price")
                new_stock = 1 if item.get("in_stock") else 0
                now = utc_iso()

                row = self._fetch_product(asin)
                if row is None:
                    free_shipping_default = -1 if seller == "amazon_com" else -1
                    self.conn.execute(
                        """
                        INSERT INTO products
                        (asin, title, seller, price, in_stock, free_shipping_il, first_seen, last_seen)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (asin, title, seller, new_price, new_stock, free_shipping_default, now, now),
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

    def mark_shipping(self, asin: str, is_free: bool) -> list[dict[str, Any]]:
        with self.lock:
            row = self._fetch_product(asin)
            if row is None:
                return []
            old_state = int(row["free_shipping_il"])
            new_state = 1 if is_free else 0
            now = utc_iso()
            self.conn.execute(
                "UPDATE products SET free_shipping_il = ?, shipping_checked_at = ? WHERE asin = ?",
                (new_state, now, asin),
            )
            alerts: list[dict[str, Any]] = []
            if old_state == 0 and new_state == 1:
                alert = self._build_alert(
                    "shipping_change",
                    "shipping",
                    asin,
                    row["title"],
                    row["price"],
                    image_url=None,
                )
                alerts.append(alert)
                self._record_alert(alert)
            self.conn.commit()
            return alerts

    def get_shipping_queue(self, limit: int) -> list[str]:
        threshold = utc_now() - timedelta(hours=self.shipping_cache_hours)
        with self.lock:
            rows = self.conn.execute(
                """
                SELECT asin FROM products
                WHERE free_shipping_il = -1
                   OR shipping_checked_at IS NULL
                   OR shipping_checked_at < ?
                ORDER BY COALESCE(shipping_checked_at, '1970-01-01T00:00:00+00:00') ASC
                LIMIT ?
                """,
                (threshold.isoformat(), limit),
            ).fetchall()
        return [row["asin"] for row in rows]

