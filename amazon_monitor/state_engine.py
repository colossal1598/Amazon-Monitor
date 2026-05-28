"""SQLite-backed state management for the PDP monitor."""

import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import image_cache

from alert_decisions import decide_back_in_stock, decide_new_product, decide_price_drop
from pdp_helpers import normalize_title_line, shipping_display_hebrew

LOGGER = logging.getLogger(__name__)


# Get “right now” in UTC so all timestamps in the database and alerts line up.
def utc_now() -> datetime:
    return datetime.now(timezone.utc)


# Turn a datetime into a standard text timestamp (or use the current time if none was given).
def utc_iso(dt: datetime | None = None) -> str:
    return (dt or utc_now()).isoformat()


# Safely turn something into a number we can compare as a price, returning None when it’s missing or invalid.
def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# Decide if a first-time-seen product is “good enough to message about” by requiring it to look in-stock and have a real price.
def _should_emit_new_product_alert(new_stock: int, new_price: float | None) -> bool:
    """First DB row for an ASIN only triggers WhatsApp when the offer is plausibly buyable."""
    if new_stock != 1:
        return False
    if new_price is None or new_price <= 0:
        return False
    return True


class StateEngine:
    """Tracks products, detects changes, and records generated alerts."""

    # Create the database connection and configure how big a price drop must be to alert.
    def __init__(self, db_path: str, price_drop_percent: float) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.price_drop_percent = float(price_drop_percent)
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.init_db()

    # Create the tables we need (products + alerts) so the monitor can remember what it saw between runs.
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
                    last_stock_alert TEXT,
                    image_url TEXT
                );

                CREATE TABLE IF NOT EXISTS aes_products (
                    asin TEXT PRIMARY KEY,
                    title TEXT,
                    price REAL,
                    in_stock INTEGER,
                    first_seen TEXT,
                    last_seen TEXT,
                    last_price_alert TEXT,
                    last_stock_alert TEXT,
                    image_url TEXT
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
            self._migrate_image_url_columns()
            self.conn.commit()

    def _table_has_column(self, table: str, column: str) -> bool:
        rows = self.conn.execute(f"PRAGMA table_info({table})").fetchall()
        return any(str(row[1]) == column for row in rows)

    def _migrate_image_url_columns(self) -> None:
        for table in ("products", "aes_products"):
            if not self._table_has_column(table, "image_url"):
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN image_url TEXT")

    def _maybe_cache_image(
        self,
        asin: str,
        image_url: str | None,
        config: dict[str, Any] | None,
    ) -> None:
        if not config or not image_url:
            return
        image_cache.ensure_cached_image(asin, image_url, config)

    # Look up the saved product row for an ASIN so we can compare “before” vs “now”.
    def _fetch_product(self, asin: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM products WHERE asin = ?", (asin,)).fetchone()

    # List all ASINs we’re already tracking.
    def list_known_asins(self) -> set[str]:
        """All ASINs currently in the products table (uppercase)."""
        with self.lock:
            rows = self.conn.execute("SELECT asin FROM products").fetchall()
        return {str(r[0]).upper() for r in rows if r and r[0]}

    # Save an alert record into the database so you have a history of what was sent and when.
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

    # Create the standard alert dictionary that the WhatsApp sender expects, including percent drop when relevant.
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
        shipping: str = "",
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
            "shipping": shipping,
        }

    # Update the database from watched product-page checks and generate alerts, while skipping updates for pages that failed to scrape.
    def process_pdp_watch_candidates(
        self,
        candidates: list[dict[str, Any]],
        watch_asins: set[str],
        *,
        source: str = "pdp_watch",
        config: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Apply PDP watch scrape rows with no global absence reconcile.

        Per-ASIN PDP failures are surfaced as ``_skip_update=True`` markers and intentionally
        leave the existing DB row untouched. Watched ASINs missing entirely from ``candidates``
        are also left untouched so a single broken page never falsely flips a product OOS.
        Only ASINs in ``watch_asins`` are processed.
        """
        alerts: list[dict[str, Any]] = []
        new_count = 0
        back_in_stock_count = 0
        price_drop_count = 0
        skipped_update_count = 0
        by_asin: dict[str, dict[str, Any]] = {}
        for item in candidates:
            asin_key = (item.get("asin") or "").upper()
            if not asin_key:
                continue
            by_asin[asin_key] = item
        watch_upper = {a.upper() for a in watch_asins}
        with self.lock:
            for asin in sorted(watch_upper):
                item = by_asin.get(asin)
                if item is None:
                    LOGGER.info("pdp_watch_no_row asin=%s (DB unchanged)", asin)
                    skipped_update_count += 1
                    continue
                if item.get("_skip_update"):
                    LOGGER.info(
                        "pdp_watch_skip_update asin=%s reason=%s (DB unchanged)",
                        asin,
                        item.get("skip_reason") or "unknown",
                    )
                    skipped_update_count += 1
                    continue
                row_source = str(item.get("source") or source)
                title = normalize_title_line(item.get("title"))
                seller = item.get("seller") or row_source
                image_url = item.get("image_url")
                new_price = _as_float(item.get("price"))
                ship_line = shipping_display_hebrew(item.get("shipping_text"))
                new_stock = 1 if bool(item.get("in_stock")) else 0
                confidence = str(item.get("stock_confidence") or "").strip().lower()
                reason = str(item.get("stock_reason") or "").strip() or None
                now = utc_iso()

                row = self._fetch_product(asin)
                if row is None:
                    self.conn.execute(
                        """
                        INSERT INTO products
                        (asin, title, seller, price, in_stock, first_seen, last_seen, image_url)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (asin, title, seller, new_price, new_stock, now, now, image_url),
                    )
                    self._maybe_cache_image(asin, image_url, config)
                    if _should_emit_new_product_alert(new_stock, new_price):
                        nd = decide_new_product(is_first_observation=True)
                        assert nd.emit and nd.alert_type is not None
                        alert = self._build_alert(
                            nd.alert_type,
                            row_source,
                            asin,
                            title,
                            new_price,
                            image_url=image_url,
                            shipping=ship_line,
                        )
                        alerts.append(alert)
                        self._record_alert(alert)
                        new_count += 1
                    else:
                        LOGGER.info(
                            "pdp_watch_seeded_no_new_alert asin=%s in_stock=%s price=%s",
                            asin,
                            new_stock,
                            new_price,
                            extra={"channel": "debug"},
                        )
                    continue

                old_price = _as_float(row["price"])
                old_stock = int(row["in_stock"] or 0)
                if confidence == "unknown":
                    LOGGER.info(
                        "pdp_watch_unknown_stock asin=%s reason=%s (DB unchanged)",
                        asin,
                        reason or "unknown",
                        extra={"channel": "debug"},
                    )
                    skipped_update_count += 1
                    continue
                if new_stock == 0:
                    # Non-qualifying PDP row explicitly marks the item out of stock.
                    if confidence and confidence != "confirmed_out":
                        # Newer scrapers distinguish "unknown" vs explicit out-of-stock.
                        # Only explicit OOS signals may flip the DB to out-of-stock.
                        LOGGER.info(
                            "pdp_watch_nonqualifying_not_oos asin=%s confidence=%s reason=%s (DB unchanged)",
                            asin,
                            confidence,
                            reason or "unknown",
                            extra={"channel": "debug"},
                        )
                        skipped_update_count += 1
                        continue
                    self.conn.execute(
                        """
                        UPDATE products
                        SET in_stock = 0,
                            image_url = COALESCE(?, image_url)
                        WHERE asin = ?
                        """,
                        (image_url, asin),
                    )
                    self._maybe_cache_image(asin, image_url, config)
                    continue

                if old_stock == 0:
                    self.conn.execute(
                        """
                        UPDATE products
                        SET price = COALESCE(?, price),
                            in_stock = 1,
                            last_seen = ?,
                            image_url = COALESCE(?, image_url)
                        WHERE asin = ?
                        """,
                        (new_price, now, image_url, asin),
                    )
                else:
                    self.conn.execute(
                        """
                        UPDATE products
                        SET price = COALESCE(?, price),
                            last_seen = ?,
                            image_url = COALESCE(?, image_url)
                        WHERE asin = ?
                        """,
                        (new_price, now, image_url, asin),
                    )
                self._maybe_cache_image(asin, image_url, config)

                stock_decision = decide_back_in_stock(old_stock, 1)
                emitted_back_in_stock = False
                if stock_decision.emit:
                    alert = self._build_alert(
                        "back_in_stock",
                        row_source,
                        asin,
                        title,
                        new_price,
                        image_url=image_url,
                        shipping=ship_line,
                    )
                    alerts.append(alert)
                    self._record_alert(alert)
                    self.conn.execute("UPDATE products SET last_stock_alert = ? WHERE asin = ?", (now, asin))
                    back_in_stock_count += 1
                    emitted_back_in_stock = True
                if not emitted_back_in_stock:
                    price_decision = decide_price_drop(
                        old_price,
                        new_price,
                        self.price_drop_percent,
                    )
                    if (
                        not price_decision.emit
                        and old_price is not None
                        and new_price is not None
                        and old_price > 0
                        and new_price < old_price
                    ):
                        pct = ((old_price - new_price) / old_price) * 100
                        LOGGER.info(
                            "price_drop_skipped where=%s asin=%s old_price=%s new_price=%s "
                            "pct_drop=%.2f threshold_pct=%s skip=%s last_price_alert=%s",
                            row_source,
                            asin,
                            old_price,
                            new_price,
                            pct,
                            self.price_drop_percent,
                            price_decision.skip_reason,
                            row["last_price_alert"],
                            extra={"channel": "debug"},
                        )
                    if price_decision.emit:
                        alert = self._build_alert(
                            "price_drop",
                            row_source,
                            asin,
                            title,
                            new_price,
                            old_price=old_price,
                            new_price=new_price,
                            image_url=image_url,
                            shipping=ship_line,
                        )
                        alerts.append(alert)
                        self._record_alert(alert)
                        self.conn.execute("UPDATE products SET last_price_alert = ? WHERE asin = ?", (now, asin))
                        price_drop_count += 1

            missing_candidates = len(watch_upper - set(by_asin.keys()))
            LOGGER.info(
                "pdp_watch candidate_rows=%s watch=%s new_count=%s back_in_stock_count=%s price_drop_count=%s "
                "skipped_update_count=%s asins_without_scrape_row=%s",
                len(by_asin),
                len(watch_upper),
                new_count,
                back_in_stock_count,
                price_drop_count,
                skipped_update_count,
                missing_candidates,
                extra={"channel": "debug"},
            )
            self.conn.commit()
        return alerts

    def process_aes_serp_mirror(
        self,
        candidates: list[dict[str, Any]],
        *,
        source: str = "aes_llc",
        reconcile_absence: bool = True,
        config: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Apply AES SERP rows into dedicated mirror state.

        Rows are persisted in ``aes_products`` only; ``products`` remains PDP-owned.
        Alerts mirror PDP semantics: ``new_product`` on first in-stock sighting,
        ``back_in_stock`` on 0->1 transition, and ``price_drop`` when still in stock.
        """
        alerts: list[dict[str, Any]] = []
        inserted_count = 0
        new_count = 0
        back_in_stock_count = 0
        price_drop_count = 0
        reconciled_oos_count = 0
        seen_asins: set[str] = set()

        with self.lock:
            for item in candidates:
                asin = (item.get("asin") or "").strip().upper()
                if not asin:
                    continue
                seen_asins.add(asin)

                title = normalize_title_line(item.get("title"))
                image_url = item.get("image_url")
                new_price = _as_float(item.get("price"))
                ship_line = shipping_display_hebrew(item.get("shipping_text"))
                new_stock = 1 if bool(item.get("in_stock")) else 0
                now = utc_iso()

                row = self.conn.execute("SELECT * FROM aes_products WHERE asin = ?", (asin,)).fetchone()
                if row is None:
                    self.conn.execute(
                        """
                        INSERT INTO aes_products
                        (asin, title, price, in_stock, first_seen, last_seen, image_url)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (asin, title, new_price, new_stock, now, now, image_url),
                    )
                    self._maybe_cache_image(asin, image_url, config)
                    inserted_count += 1
                    if _should_emit_new_product_alert(new_stock, new_price):
                        nd = decide_new_product(is_first_observation=True)
                        assert nd.emit and nd.alert_type is not None
                        alert = self._build_alert(
                            nd.alert_type,
                            source,
                            asin,
                            title,
                            new_price,
                            image_url=image_url,
                            shipping=ship_line,
                        )
                        alerts.append(alert)
                        self._record_alert(alert)
                        new_count += 1
                    continue

                old_price = _as_float(row["price"])
                old_stock = int(row["in_stock"] or 0)
                if new_stock == 0:
                    self.conn.execute(
                        """
                        UPDATE aes_products
                        SET title = COALESCE(?, title),
                            in_stock = 0,
                            last_seen = ?,
                            image_url = COALESCE(?, image_url)
                        WHERE asin = ?
                        """,
                        (title, now, image_url, asin),
                    )
                    self._maybe_cache_image(asin, image_url, config)
                    continue

                if old_stock == 0:
                    self.conn.execute(
                        """
                        UPDATE aes_products
                        SET title = COALESCE(?, title),
                            price = COALESCE(?, price),
                            in_stock = 1,
                            last_seen = ?,
                            image_url = COALESCE(?, image_url)
                        WHERE asin = ?
                        """,
                        (title, new_price, now, image_url, asin),
                    )
                else:
                    self.conn.execute(
                        """
                        UPDATE aes_products
                        SET title = COALESCE(?, title),
                            price = COALESCE(?, price),
                            in_stock = 1,
                            last_seen = ?,
                            image_url = COALESCE(?, image_url)
                        WHERE asin = ?
                        """,
                        (title, new_price, now, image_url, asin),
                    )
                self._maybe_cache_image(asin, image_url, config)

                stock_decision = decide_back_in_stock(old_stock, 1)
                emitted_back_in_stock = False
                if stock_decision.emit:
                    alert = self._build_alert(
                        "back_in_stock",
                        source,
                        asin,
                        title,
                        new_price,
                        image_url=image_url,
                        shipping=ship_line,
                    )
                    alerts.append(alert)
                    self._record_alert(alert)
                    self.conn.execute("UPDATE aes_products SET last_stock_alert = ? WHERE asin = ?", (now, asin))
                    back_in_stock_count += 1
                    emitted_back_in_stock = True
                if not emitted_back_in_stock:
                    price_decision = decide_price_drop(
                        old_price,
                        new_price,
                        self.price_drop_percent,
                    )
                    if (
                        not price_decision.emit
                        and old_price is not None
                        and new_price is not None
                        and old_price > 0
                        and new_price < old_price
                    ):
                        pct = ((old_price - new_price) / old_price) * 100
                        LOGGER.info(
                            "price_drop_skipped where=%s asin=%s old_price=%s new_price=%s "
                            "pct_drop=%.2f threshold_pct=%s skip=%s last_price_alert=%s",
                            source,
                            asin,
                            old_price,
                            new_price,
                            pct,
                            self.price_drop_percent,
                            price_decision.skip_reason,
                            row["last_price_alert"],
                            extra={"channel": "debug"},
                        )
                    if price_decision.emit:
                        alert = self._build_alert(
                            "price_drop",
                            source,
                            asin,
                            title,
                            new_price,
                            old_price=old_price,
                            new_price=new_price,
                            image_url=image_url,
                            shipping=ship_line,
                        )
                        alerts.append(alert)
                        self._record_alert(alert)
                        self.conn.execute("UPDATE aes_products SET last_price_alert = ? WHERE asin = ?", (now, asin))
                        price_drop_count += 1

            if reconcile_absence:
                if seen_asins:
                    placeholders = ",".join("?" for _ in seen_asins)
                    cursor = self.conn.execute(
                        f"UPDATE aes_products SET in_stock = 0 WHERE in_stock != 0 AND asin NOT IN ({placeholders})",
                        tuple(sorted(seen_asins)),
                    )
                else:
                    cursor = self.conn.execute("UPDATE aes_products SET in_stock = 0 WHERE in_stock != 0")
                reconciled_oos_count = int(cursor.rowcount or 0)

            LOGGER.info(
                "aes_serp_mirror candidate_rows=%s inserted_count=%s new_count=%s back_in_stock_count=%s "
                "price_drop_count=%s reconciled_oos_count=%s reconcile_absence=%s",
                len(candidates),
                inserted_count,
                new_count,
                back_in_stock_count,
                price_drop_count,
                reconciled_oos_count,
                reconcile_absence,
                extra={"channel": "debug"},
            )
            self.conn.commit()

        return alerts
