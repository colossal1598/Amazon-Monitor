"""SQLite-backed state management for search monitoring."""

import logging
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from alert_decisions import decide_back_in_stock, decide_new_product, decide_price_drop
from filter_pipeline import normalize_title_line, shipping_display_hebrew

LOGGER = logging.getLogger(__name__)


# Get “right now” in UTC so all timestamps in the database and alerts line up.
def utc_now() -> datetime:
    return datetime.now(timezone.utc)


# Turn a datetime into a standard text timestamp (or use the current time if none was given).
def utc_iso(dt: datetime | None = None) -> str:
    return (dt or utc_now()).isoformat()


# Convert a stored timestamp string back into a datetime so we can compare times like cooldown windows.
def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


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
        self._price_alert_cooldown = timedelta(hours=24)

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

    # Look up the saved product row for an ASIN so we can compare “before” vs “now”.
    def _fetch_product(self, asin: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM products WHERE asin = ?", (asin,)).fetchone()

    # List all ASINs we’re already tracking so other parts of the pipeline can avoid re-seeding duplicates.
    def list_known_asins(self) -> set[str]:
        """All ASINs currently in the products table (uppercase)."""
        with self.lock:
            rows = self.conn.execute("SELECT asin FROM products").fetchall()
        return {str(r[0]).upper() for r in rows if r and r[0]}

    _SERP_PRESENCE_CHUNK = 400

    # Refresh last_seen / in_stock for ASINs that passed the search filter pipeline (no alerts, no INSERT).
    def touch_tracked_serp_pipeline_presence(
        self,
        asins: set[str],
        *,
        exclude_asins: set[str] | None = None,
    ) -> int:
        """Set in_stock=1 and last_seen for existing rows whose ASINs appear in the pipeline merge.

        Used after ``process_search_candidates`` so tracked listings that pass Pokémon/stage1/keywords
        but not the free-shipping alert path still show fresh presence. PDP-watch ASINs should be
        passed in ``exclude_asins`` so SERP does not overwrite PDP-owned stock.

        SQLite ``UPDATE`` only affects rows that exist; unknown ASINs are ignored. Chunked IN lists
        stay under typical SQLite variable limits.
        """
        normalized = {str(a).strip().upper() for a in asins if a and str(a).strip()}
        excl = {str(a).strip().upper() for a in (exclude_asins or ()) if a}
        targets = sorted(normalized - excl)
        if not targets:
            return 0
        now = utc_iso()
        total = 0
        chunk_size = self._SERP_PRESENCE_CHUNK
        with self.lock:
            for i in range(0, len(targets), chunk_size):
                chunk = targets[i : i + chunk_size]
                placeholders = ",".join("?" for _ in chunk)
                cur = self.conn.execute(
                    f"""
                    UPDATE products
                    SET in_stock = 1,
                        last_seen = ?
                    WHERE asin IN ({placeholders})
                    """,
                    [now, *chunk],
                )
                total += cur.rowcount or 0
            self.conn.commit()
        LOGGER.info(
            "serp_pipeline_presence_touch rows=%s asins_requested=%s excluded=%s",
            total,
            len(targets),
            len(excl),
        )
        return total

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

    # Mark tracked products as out of stock when they don’t show up in a healthy run, unless they’re excluded (like PDP-watch items).
    def _mark_missing_asins_out_of_stock(
        self,
        seen_asins: set[str],
        _source: str,
        now: str,
        exclude_asins: set[str] | None = None,
    ) -> int:
        """Mark tracked ASINs absent from this healthy run as out-of-stock.

        Single-tenant DB: scope by ASIN only (seller column holds display names like
        'Amazon.com', not a shared bucket string).

        ``exclude_asins`` (e.g. PDP watch list): never marked OOS here—stock for those rows
        comes from the PDP pass, not SERP presence.
        """
        if not seen_asins:
            return 0
        excl = {str(a).upper() for a in (exclude_asins or ()) if a}
        seen_sorted = sorted(seen_asins)
        ph_seen = ",".join("?" for _ in seen_sorted)
        params: list[Any] = [now, *seen_sorted]
        extra_not_in = ""
        if excl:
            ex_sorted = sorted(excl)
            ph_ex = ",".join("?" for _ in ex_sorted)
            extra_not_in = f" AND asin NOT IN ({ph_ex})"
            params.extend(ex_sorted)
        cursor = self.conn.execute(
            f"""
            UPDATE products
            SET in_stock = 0,
                last_seen = ?
            WHERE in_stock != 0
              AND asin NOT IN ({ph_seen})
              {extra_not_in}
            """,
            params,
        )
        return cursor.rowcount

    # Update the database from search-page results and generate alerts for new items, back-in-stock, and price drops.
    def process_search_candidates(
        self,
        candidates: list[dict[str, Any]],
        reconcile_missing: bool = False,
        source: str = "amazon_export",
        reconcile_exclude_asins: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Process scraped search candidates and emit new/stock/price alerts.

        When `reconcile_missing` is true, any tracked ASIN for the same source
        that is absent from this successful run is marked out-of-stock.

        ``reconcile_exclude_asins``: ASINs not subject to that absence rule (e.g. PDP-only watches).
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
                title = normalize_title_line(item.get("title"))
                seller = item.get("seller") or source
                image_url = item.get("image_url")
                new_price = _as_float(item.get("price"))
                ship_line = shipping_display_hebrew(item.get("shipping_text"))
                # SERP card `in_stock` from scraper; reconcile_missing still marks DB ASINs absent from this run as OOS.
                new_stock = 1 if bool(item.get("in_stock")) else 0
                now = utc_iso()
                now_dt = utc_now()

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
                    if _should_emit_new_product_alert(new_stock, new_price):
                        nd = decide_new_product(is_first_observation=True)
                        assert nd.emit and nd.alert_type is not None
                        alert = self._build_alert(
                            nd.alert_type,
                            "search",
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
                            "product_seeded_no_new_alert asin=%s in_stock=%s price=%s",
                            asin,
                            new_stock,
                            new_price,
                        )
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
                emitted_back_in_stock = False
                if stock_decision.emit:
                    alert = self._build_alert(
                        "back_in_stock",
                        "search",
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
                            shipping=ship_line,
                        )
                        alerts.append(alert)
                        self._record_alert(alert)
                        self.conn.execute("UPDATE products SET last_price_alert = ? WHERE asin = ?", (now, asin))
                        price_drop_count += 1
            marked_oos_count = 0
            if reconcile_missing:
                marked_oos_count = self._mark_missing_asins_out_of_stock(
                    seen_asins, source, utc_iso(), reconcile_exclude_asins
                )
            LOGGER.info(
                "search_reconcile seen_count=%s new_count=%s marked_oos_count=%s "
                "back_in_stock_count=%s price_drop_count=%s reconcile_missing=%s reconcile_excluded=%s",
                len(seen_asins),
                new_count,
                marked_oos_count,
                back_in_stock_count,
                price_drop_count,
                reconcile_missing,
                len(reconcile_exclude_asins or ()),
            )
            self.conn.commit()
        return alerts

    # Update the database from watched product-page checks and generate alerts, while skipping updates for pages that failed to scrape.
    def process_pdp_watch_candidates(
        self,
        candidates: list[dict[str, Any]],
        watch_asins: set[str],
        *,
        source: str = "pdp_watch",
    ) -> list[dict[str, Any]]:
        """Apply PDP watch scrape rows: same alert rules as search, no global SERP reconcile.

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
                title = normalize_title_line(item.get("title"))
                seller = item.get("seller") or source
                image_url = item.get("image_url")
                new_price = _as_float(item.get("price"))
                ship_line = shipping_display_hebrew(item.get("shipping_text"))
                new_stock = 1 if bool(item.get("in_stock")) else 0
                now = utc_iso()
                now_dt = utc_now()

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
                    else:
                        LOGGER.info(
                            "pdp_watch_seeded_no_new_alert asin=%s in_stock=%s price=%s",
                            asin,
                            new_stock,
                            new_price,
                        )
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
                    self.conn.execute("UPDATE products SET last_stock_alert = ? WHERE asin = ?", (now, asin))
                    back_in_stock_count += 1
                    emitted_back_in_stock = True
                if not emitted_back_in_stock:
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
            )
            self.conn.commit()
        return alerts
