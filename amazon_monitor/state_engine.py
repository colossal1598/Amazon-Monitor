"""SQLite-backed state management for the PDP monitor."""

import logging
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import image_cache

from alert_decisions import (
    decide_back_in_stock,
    decide_new_product,
    decide_price_below_target,
    decide_price_drop,
)
from pdp_helpers import normalize_title_line, shipping_display_for

LOGGER = logging.getLogger(__name__)

_STOCK_ALERT_TYPES = frozenset({"new_product", "back_in_stock"})

# PDP stock_reason values that mean the page *explicitly* said out-of-stock, as opposed
# to inferred/heuristic OOS (missing price, "see all buying options", no buy button).
_STRONG_OOS_REASONS = frozenset(
    {
        "explicit_oos",
        "explicit_oos_text",
        "outofstock_container",
        "explicit_oos_buybox_text",
    }
)


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
                    image_url TEXT,
                    oos_miss_streak INTEGER NOT NULL DEFAULT 0,
                    last_oos_confirmed INTEGER NOT NULL DEFAULT 0,
                    is_preorder INTEGER NOT NULL DEFAULT 0,
                    target_alert_armed INTEGER NOT NULL DEFAULT 1,
                    target_alert_last_sent TEXT,
                    priceless_streak INTEGER NOT NULL DEFAULT 0
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
                    image_url TEXT,
                    oos_miss_streak INTEGER NOT NULL DEFAULT 0,
                    last_oos_confirmed INTEGER NOT NULL DEFAULT 0
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
        for table in ("products", "aes_products"):
            if not self._table_has_column(table, "oos_miss_streak"):
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN oos_miss_streak INTEGER NOT NULL DEFAULT 0")
        for table in ("products", "aes_products"):
            if not self._table_has_column(table, "last_oos_confirmed"):
                self.conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN last_oos_confirmed INTEGER NOT NULL DEFAULT 0"
                )
        if not self._table_has_column("products", "is_preorder"):
            self.conn.execute("ALTER TABLE products ADD COLUMN is_preorder INTEGER NOT NULL DEFAULT 0")
        if not self._table_has_column("products", "target_alert_armed"):
            self.conn.execute("ALTER TABLE products ADD COLUMN target_alert_armed INTEGER NOT NULL DEFAULT 1")
        if not self._table_has_column("products", "target_alert_last_sent"):
            self.conn.execute("ALTER TABLE products ADD COLUMN target_alert_last_sent TEXT")
        if not self._table_has_column("products", "priceless_streak"):
            self.conn.execute(
                "ALTER TABLE products ADD COLUMN priceless_streak INTEGER NOT NULL DEFAULT 0"
            )

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

    @staticmethod
    def _config_minutes(config: dict[str, Any] | None, key: str, default: int) -> int:
        if not config:
            return default
        try:
            return max(0, int(config.get(key, default)))
        except (TypeError, ValueError):
            return default

    def _recent_stock_alert(
        self,
        asin: str,
        alert_type: str,
        window_minutes: int,
        *,
        exclude_source: str | None = None,
    ) -> sqlite3.Row | None:
        """Most recent matching alert within the window; optionally ignoring one source.

        With ``exclude_source`` set this is the cross-source dedupe check; without it,
        it is the global per-ASIN cooldown that stops repeat alerts from any source.
        """
        if alert_type not in _STOCK_ALERT_TYPES or window_minutes <= 0:
            return None
        if exclude_source is not None:
            row = self.conn.execute(
                """
                SELECT source, sent_at FROM alerts
                WHERE asin = ? AND alert_type = ? AND source != ?
                ORDER BY sent_at DESC LIMIT 1
                """,
                (asin, alert_type, exclude_source),
            ).fetchone()
        else:
            row = self.conn.execute(
                """
                SELECT source, sent_at FROM alerts
                WHERE asin = ? AND alert_type = ?
                ORDER BY sent_at DESC LIMIT 1
                """,
                (asin, alert_type),
            ).fetchone()
        if row is None:
            return None
        try:
            sent_at = datetime.fromisoformat(str(row["sent_at"]))
            if sent_at.tzinfo is None:
                sent_at = sent_at.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
        age = utc_now() - sent_at
        if age < timedelta(minutes=window_minutes):
            return row
        return None

    def _emit_stock_alert_if_allowed(
        self,
        *,
        alert_type: str,
        source: str,
        asin: str,
        title: str | None,
        price: float | None,
        image_url: str | None,
        shipping: str,
        config: dict[str, Any] | None,
        telemetry: Any,
        cycle_id: int,
        _tel: Any,
        confirmed_transition: bool = False,
    ) -> dict[str, Any] | None:
        """Build and return a stock alert, or None when suppressed by dedupe/cooldown.

        ``confirmed_transition`` marks a back-in-stock whose preceding OOS was backed by
        strong evidence (explicit "unavailable"/OOS on the page or card). Those are real
        sell-out -> restock events, so they only respect the short confirmed cooldown;
        weak-evidence transitions (SERP absence, missing price, inferred OOS) use the
        long cooldown because they are usually scrape noise.
        """
        if confirmed_transition:
            cooldown = self._config_minutes(config, "stock_alert_confirmed_cooldown_minutes", 10)
        else:
            cooldown = self._config_minutes(config, "stock_alert_cooldown_minutes", 60)
        recent = self._recent_stock_alert(asin, alert_type, cooldown)
        if recent is not None:
            _tel(
                "stock_alert_cooldown_suppressed",
                asin=asin,
                alert_type=alert_type,
                source=source,
                prior_source=recent["source"],
                prior_sent_at=recent["sent_at"],
                cooldown_minutes=cooldown,
                confirmed_transition=confirmed_transition,
            )
            return None
        window = self._config_minutes(config, "cross_source_alert_dedupe_minutes", 15)
        prior = self._recent_stock_alert(asin, alert_type, window, exclude_source=source)
        if prior is not None:
            _tel(
                "cross_source_alert_suppressed",
                asin=asin,
                alert_type=alert_type,
                source=source,
                other_source=prior["source"],
                other_sent_at=prior["sent_at"],
                window_minutes=window,
            )
            return None
        alert = self._build_alert(
            alert_type,
            source,
            asin,
            title,
            price,
            image_url=image_url,
            shipping=shipping,
        )
        self._record_alert(alert)
        return alert

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

    def _target_price_alert_decision(
        self,
        *,
        price: float | None,
        qualifies: bool,
        target: float,
        armed: bool,
        last_sent: str | None,
        cooldown_hours: float,
    ) -> tuple[bool, bool]:
        """Hysteresis + cooldown gate for the ``price_below_target`` alert.

        Returns ``(should_fire, new_armed_state)``. The item is re-armed any time it
        is not currently below target (price back at/above target, OOS, or price
        missing) so the next crossing can alert again. While it stays below target,
        firing only happens once (armed -> disarmed), and re-firing after a re-arm is
        additionally blocked until ``cooldown_hours`` has passed since the last
        ``price_below_target`` alert for this ASIN, to survive a scan loop bouncing the
        price back and forth across the target.
        """
        below_target = qualifies and price is not None and price < target
        if not below_target:
            return False, True
        decision = decide_price_below_target(price, qualifies, target, armed)
        if not decision.emit:
            return False, armed
        if last_sent and cooldown_hours > 0:
            try:
                sent_at = datetime.fromisoformat(str(last_sent))
                if sent_at.tzinfo is None:
                    sent_at = sent_at.replace(tzinfo=timezone.utc)
                if utc_now() - sent_at < timedelta(hours=cooldown_hours):
                    # Still armed: once the cooldown elapses this can fire without
                    # needing another above-target re-arm first.
                    return False, armed
            except ValueError:
                pass
        return True, False

    # Update the database from watched product-page checks and generate alerts, while skipping updates for pages that failed to scrape.
    def process_pdp_watch_candidates(
        self,
        candidates: list[dict[str, Any]],
        watch_asins: set[str],
        *,
        source: str = "pdp_watch",
        config: dict[str, Any] | None = None,
        telemetry: Any = None,
        cycle_id: int = 0,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
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
        target_alert_count = 0
        skipped_update_count = 0
        by_asin: dict[str, dict[str, Any]] = {}
        for item in candidates:
            asin_key = (item.get("asin") or "").upper()
            if not asin_key:
                continue
            by_asin[asin_key] = item
        watch_upper = {a.upper() for a in watch_asins}
        _SCRAPE_DEBUG_REASONS = frozenset({"navigation_failed", "parse_failed", "goto_failed"})
        target_prices = (config or {}).get("pdp_watch_target_prices") or {}
        if not isinstance(target_prices, dict):
            target_prices = {}
        try:
            target_cooldown_hours = float((config or {}).get("target_price_alert_cooldown_hours", 6))
        except (TypeError, ValueError):
            target_cooldown_hours = 6.0
        priceless_alert_enabled = bool((config or {}).get("pdp_priceless_restock_alert", True))
        try:
            priceless_confirm_checks = max(1, int((config or {}).get("pdp_priceless_confirm_checks", 2)))
        except (TypeError, ValueError):
            priceless_confirm_checks = 2

        def _tel(event: str, *, asin: str | None = None, **fields: Any) -> None:
            if telemetry and cycle_id:
                telemetry.debug(cycle_id, event, asin=asin, **fields)

        with self.lock:
            for asin in sorted(watch_upper):
                item = by_asin.get(asin)
                if item is None:
                    _tel("pdp_watch_no_row", asin=asin)
                    skipped_update_count += 1
                    continue
                if item.get("_skip_update"):
                    reason = str(item.get("skip_reason") or "unknown")
                    if reason in _SCRAPE_DEBUG_REASONS:
                        _tel("pdp_watch_skip_update", asin=asin, reason=reason)
                    skipped_update_count += 1
                    continue
                row_source = str(item.get("source") or source)
                title = normalize_title_line(item.get("title"))
                seller = item.get("seller") or row_source
                image_url = item.get("image_url")
                new_price = _as_float(item.get("price"))
                ship_line = shipping_display_for(
                    item.get("shipping_text"), seller_text=item.get("seller_text"), source=row_source
                )
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
                        alert = self._emit_stock_alert_if_allowed(
                            alert_type=nd.alert_type,
                            source=row_source,
                            asin=asin,
                            title=title,
                            price=new_price,
                            image_url=image_url,
                            shipping=ship_line,
                            config=config,
                            telemetry=telemetry,
                            cycle_id=cycle_id,
                            _tel=_tel,
                        )
                        if alert is not None:
                            alerts.append(alert)
                            new_count += 1
                    target = target_prices.get(asin)
                    if target is not None:
                        should_fire, _new_armed = self._target_price_alert_decision(
                            price=new_price,
                            qualifies=bool(new_stock == 1),
                            target=float(target),
                            armed=True,
                            last_sent=None,
                            cooldown_hours=target_cooldown_hours,
                        )
                        if should_fire:
                            target_alert = self._build_alert(
                                "price_below_target",
                                row_source,
                                asin,
                                title,
                                new_price,
                                image_url=image_url,
                                shipping=ship_line,
                            )
                            target_alert["target_price"] = float(target)
                            self._record_alert(target_alert)
                            self.conn.execute(
                                "UPDATE products SET target_alert_armed = 0, target_alert_last_sent = ? WHERE asin = ?",
                                (now, asin),
                            )
                            alerts.append(target_alert)
                            target_alert_count += 1
                    continue

                # PDP image extraction often comes back empty (explicit-OOS pages skip it,
                # "continue shopping" interstitials can hide it); fall back to the last
                # known-good image already stored for this ASIN so alerts still carry one.
                if not image_url:
                    image_url = row["image_url"]

                old_price = _as_float(row["price"])
                old_stock = int(row["in_stock"] or 0)
                seller_snippet = " ".join(str(item.get("seller_text") or "").split())[:200] or None
                if confidence == "unknown":
                    if reason == "priceless_purchasable" and priceless_alert_enabled:
                        # A purchasable allowed-seller buybox rendered no price. This is
                        # exactly the restock-transition state that price extraction
                        # keeps discarding, so confirm it over a couple of checks and
                        # alert instead of silently skipping. The alert carries no price
                        # (webhook template renders "not available"); a later check with
                        # a real price updates the row via the normal in-stock path.
                        old_streak = int(row["priceless_streak"] or 0)
                        new_streak = old_streak + 1
                        self.conn.execute(
                            "UPDATE products SET priceless_streak = ? WHERE asin = ?",
                            (new_streak, asin),
                        )
                        new_preorder = 1 if bool(item.get("is_preorder")) else 0
                        old_preorder = int(row["is_preorder"] or 0)
                        # Same preorder-suppression gate as the normal restock path:
                        # preorder->preorder flaps on weak evidence are noise unless the
                        # preceding OOS was strong-text confirmed.
                        preorder_suppressed = bool(
                            new_preorder and old_preorder and not bool(row["last_oos_confirmed"])
                        )
                        if old_stock == 0 and new_streak >= priceless_confirm_checks and not preorder_suppressed:
                            alert = self._emit_stock_alert_if_allowed(
                                alert_type="back_in_stock",
                                source=row_source,
                                asin=asin,
                                title=title,
                                price=None,
                                image_url=image_url,
                                shipping=ship_line,
                                config=config,
                                telemetry=telemetry,
                                cycle_id=cycle_id,
                                _tel=_tel,
                                confirmed_transition=bool(row["last_oos_confirmed"]) and not new_preorder,
                            )
                            if alert is not None:
                                alerts.append(alert)
                                back_in_stock_count += 1
                                self.conn.execute(
                                    """
                                    UPDATE products
                                    SET in_stock = 1,
                                        price = NULL,
                                        priceless_streak = 0,
                                        oos_miss_streak = 0,
                                        is_preorder = ?,
                                        last_seen = ?,
                                        last_stock_alert = ?
                                    WHERE asin = ?
                                    """,
                                    (new_preorder, now, now, asin),
                                )
                        elif old_stock == 0 and new_streak >= priceless_confirm_checks and preorder_suppressed:
                            _tel("pdp_preorder_realert_suppressed", asin=asin, price=None)
                        _tel(
                            "pdp_watch_priceless_streak",
                            asin=asin,
                            streak=new_streak,
                            confirm_checks=priceless_confirm_checks,
                            old_stock=old_stock,
                        )
                    else:
                        # Any other unknown result resolves the ASIN away from priceless;
                        # drop a stale streak so a future restock starts counting fresh.
                        if int(row["priceless_streak"] or 0):
                            self.conn.execute(
                                "UPDATE products SET priceless_streak = 0 WHERE asin = ?", (asin,)
                            )
                        _tel(
                            "pdp_watch_unknown_stock",
                            asin=asin,
                            reason=reason or "unknown",
                            price=new_price,
                            seller_snippet=seller_snippet,
                        )
                    skipped_update_count += 1
                    continue
                if new_stock == 0:
                    # Non-qualifying PDP row explicitly marks the item out of stock.
                    if confidence and confidence != "confirmed_out":
                        # Newer scrapers distinguish "unknown" vs explicit out-of-stock.
                        # Only explicit OOS signals may flip the DB to out-of-stock.
                        _tel(
                            "pdp_watch_nonqualifying_not_oos",
                            asin=asin,
                            confidence=confidence,
                            reason=reason or "unknown",
                            price=new_price,
                            seller_snippet=seller_snippet,
                        )
                        skipped_update_count += 1
                        continue
                    oos_strong = (reason or "") in _STRONG_OOS_REASONS
                    old_streak = int(row["oos_miss_streak"] or 0)
                    miss_streak = old_streak + 1
                    # Strong page-text evidence flips OOS immediately (genuine
                    # sell-outs must re-alert fast on restock). Weak evidence
                    # (buybox churn, missing price, inferred signals) needs
                    # consecutive confirmations, mirroring aes_oos_confirm_cycles:
                    # production alert history showed single-scrape weak flips
                    # producing dozens of fake back_in_stock alerts per day for
                    # flapping/preorder pages.
                    try:
                        confirm_cycles = max(1, int((config or {}).get("pdp_oos_confirm_cycles", 2)))
                    except (TypeError, ValueError):
                        confirm_cycles = 2
                    flip_oos = oos_strong or old_stock == 0 or miss_streak >= confirm_cycles
                    if not flip_oos:
                        _tel(
                            "pdp_oos_debounced",
                            asin=asin,
                            reason=reason or "unknown",
                            miss_streak=miss_streak,
                            confirm_cycles=confirm_cycles,
                        )
                    # Arm the confirmed cooldown for the next allocation wave when the
                    # OOS is either strong page text OR a debounced seller_mismatch that
                    # has now been observed on `confirm_cycles` consecutive checks. Two
                    # consecutive parsed-3P-buybox observations ~= a real "Amazon offer
                    # gone", so treating them as a confirmed transition lets the next
                    # Amazon wave re-alert on the short confirmed cooldown. Previously
                    # waves that passed through 3P-buybox interludes never flipped the DB
                    # (stayed unknown), so no 0->1 edge fired and repeat waves never
                    # re-alerted. Otherwise keep the prior confirmed flag while already
                    # OOS, else clear it.
                    oos_confirmed = (
                        1
                        if oos_strong
                        or (reason == "seller_mismatch" and miss_streak >= confirm_cycles)
                        else (int(row["last_oos_confirmed"] or 0) if old_stock == 0 else 0)
                    )
                    self.conn.execute(
                        """
                        UPDATE products
                        SET in_stock = ?,
                            oos_miss_streak = ?,
                            last_oos_confirmed = ?,
                            priceless_streak = 0,
                            image_url = COALESCE(?, image_url),
                            target_alert_armed = 1
                        WHERE asin = ?
                        """,
                        (0 if flip_oos else 1, miss_streak, oos_confirmed, image_url, asin),
                    )
                    self._maybe_cache_image(asin, image_url, config)
                    continue

                new_preorder = 1 if bool(item.get("is_preorder")) else 0
                old_preorder = int(row["is_preorder"] or 0)
                if old_stock == 0:
                    self.conn.execute(
                        """
                        UPDATE products
                        SET price = COALESCE(?, price),
                            in_stock = 1,
                            oos_miss_streak = 0,
                            priceless_streak = 0,
                            is_preorder = ?,
                            last_seen = ?,
                            image_url = COALESCE(?, image_url)
                        WHERE asin = ?
                        """,
                        (new_price, new_preorder, now, image_url, asin),
                    )
                else:
                    self.conn.execute(
                        """
                        UPDATE products
                        SET price = COALESCE(?, price),
                            oos_miss_streak = 0,
                            priceless_streak = 0,
                            is_preorder = ?,
                            last_seen = ?,
                            image_url = COALESCE(?, image_url)
                        WHERE asin = ?
                        """,
                        (new_price, new_preorder, now, image_url, asin),
                    )
                self._maybe_cache_image(asin, image_url, config)

                target = target_prices.get(asin)
                if target is not None:
                    prior_armed = bool(row["target_alert_armed"]) if row["target_alert_armed"] is not None else True
                    prior_last_sent = row["target_alert_last_sent"]
                    should_fire, new_armed = self._target_price_alert_decision(
                        price=new_price,
                        qualifies=True,
                        target=float(target),
                        armed=prior_armed,
                        last_sent=prior_last_sent,
                        cooldown_hours=target_cooldown_hours,
                    )
                    if should_fire:
                        target_alert = self._build_alert(
                            "price_below_target",
                            row_source,
                            asin,
                            title,
                            new_price,
                            image_url=image_url,
                            shipping=ship_line,
                        )
                        target_alert["target_price"] = float(target)
                        self._record_alert(target_alert)
                        self.conn.execute(
                            "UPDATE products SET target_alert_armed = 0, target_alert_last_sent = ? WHERE asin = ?",
                            (now, asin),
                        )
                        alerts.append(target_alert)
                        target_alert_count += 1
                    elif new_armed != prior_armed:
                        self.conn.execute(
                            "UPDATE products SET target_alert_armed = ? WHERE asin = ?",
                            (1 if new_armed else 0, asin),
                        )

                stock_decision = decide_back_in_stock(old_stock, 1)
                emitted_back_in_stock = False
                if (
                    stock_decision.emit
                    and new_preorder
                    and old_preorder
                    and not bool(row["last_oos_confirmed"])
                ):
                    # Preorder buyboxes flap on weak evidence (missing price, buybox
                    # rotation) constantly; those 0->1 flips are scrape noise, not a
                    # new allocation wave, so they stay suppressed. But when the
                    # preceding OOS was CONFIRMED by strong page text (a real
                    # sell-out), a preorder reopening is a genuine allocation wave
                    # the client wants immediately — let it through to the normal
                    # emit path, which still applies the confirmed/normal cooldowns.
                    # (A live Amazon.com preorder wave was missed on 2026-07-13
                    # because this suppressed every preorder->preorder re-alert.)
                    _tel(
                        "pdp_preorder_realert_suppressed",
                        asin=asin,
                        price=new_price,
                    )
                elif stock_decision.emit:
                    alert = self._emit_stock_alert_if_allowed(
                        alert_type="back_in_stock",
                        source=row_source,
                        asin=asin,
                        title=title,
                        price=new_price,
                        image_url=image_url,
                        shipping=ship_line,
                        config=config,
                        telemetry=telemetry,
                        cycle_id=cycle_id,
                        _tel=_tel,
                        confirmed_transition=bool(row["last_oos_confirmed"]) and not new_preorder,
                    )
                    if alert is not None:
                        alerts.append(alert)
                        self.conn.execute(
                            "UPDATE products SET last_stock_alert = ? WHERE asin = ?",
                            (now, asin),
                        )
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
                        _tel(
                            "price_drop_skipped",
                            asin=asin,
                            where=row_source,
                            old_price=old_price,
                            new_price=new_price,
                            pct_drop=round(pct, 2),
                            threshold_pct=self.price_drop_percent,
                            skip=price_decision.skip_reason,
                            last_price_alert=row["last_price_alert"],
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
            summary = {
                "candidate_rows": len(by_asin),
                "watch": len(watch_upper),
                "new_count": new_count,
                "back_in_stock_count": back_in_stock_count,
                "price_drop_count": price_drop_count,
                "target_alert_count": target_alert_count,
                "skipped_update_count": skipped_update_count,
                "asins_without_scrape_row": missing_candidates,
            }
            self.conn.commit()
        return alerts, summary

    def process_aes_serp_mirror(
        self,
        candidates: list[dict[str, Any]],
        *,
        source: str = "aes_llc",
        reconcile_absence: bool = True,
        config: dict[str, Any] | None = None,
        telemetry: Any = None,
        cycle_id: int = 0,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
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
        debounced_oos_count = 0
        seen_asins: set[str] = set()

        # SERP absence/non-qualifying rows are noisy (page-1 churn, transient missing
        # price). Require this many consecutive OOS observations before flipping the
        # mirror to out-of-stock, so a one-cycle blip can't produce a fake
        # 0->1 "back_in_stock" on the next cycle.
        try:
            oos_confirm_cycles = max(1, int((config or {}).get("aes_oos_confirm_cycles", 3)))
        except (TypeError, ValueError):
            oos_confirm_cycles = 3

        def _tel(event: str, *, asin: str | None = None, **fields: Any) -> None:
            if telemetry and cycle_id:
                telemetry.debug(cycle_id, event, asin=asin, **fields)

        with self.lock:
            for item in candidates:
                asin = (item.get("asin") or "").strip().upper()
                if not asin:
                    continue
                seen_asins.add(asin)

                title = normalize_title_line(item.get("title"))
                image_url = item.get("image_url")
                new_price = _as_float(item.get("price"))
                # source here is the AES storefront (Amazon Export) — always free shipping.
                ship_line = shipping_display_for(
                    item.get("shipping_text"), seller_text=item.get("seller_text"), source=source
                )
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
                        alert = self._emit_stock_alert_if_allowed(
                            alert_type=nd.alert_type,
                            source=source,
                            asin=asin,
                            title=title,
                            price=new_price,
                            image_url=image_url,
                            shipping=ship_line,
                            config=config,
                            telemetry=telemetry,
                            cycle_id=cycle_id,
                            _tel=_tel,
                        )
                        if alert is not None:
                            alerts.append(alert)
                            new_count += 1
                    continue

                # Fall back to the last known-good stored image_url when this scrape's
                # row didn't yield one (mirrors the PDP watch path).
                if not image_url:
                    image_url = row["image_url"]

                old_price = _as_float(row["price"])
                old_stock = int(row["in_stock"] or 0)
                old_streak = int(row["oos_miss_streak"] or 0)
                if new_stock == 0:
                    explicit_oos = bool(item.get("explicit_oos"))
                    miss_streak = old_streak + 1
                    # Explicit "unavailable"/OOS text on the card is trustworthy: flip
                    # immediately and remember it was confirmed, so a fast genuine
                    # restock can re-alert on the short confirmed cooldown. Weak
                    # signals (missing price etc.) keep the consecutive-cycle debounce.
                    flip_oos = explicit_oos or old_stock == 0 or miss_streak >= oos_confirm_cycles
                    if not flip_oos:
                        debounced_oos_count += 1
                        _tel(
                            "aes_oos_debounced",
                            asin=asin,
                            miss_streak=miss_streak,
                            confirm_cycles=oos_confirm_cycles,
                        )
                    oos_confirmed = 1 if explicit_oos else (int(row["last_oos_confirmed"] or 0) if old_stock == 0 else 0)
                    self.conn.execute(
                        """
                        UPDATE aes_products
                        SET title = COALESCE(?, title),
                            in_stock = ?,
                            oos_miss_streak = ?,
                            last_oos_confirmed = ?,
                            last_seen = ?,
                            image_url = COALESCE(?, image_url)
                        WHERE asin = ?
                        """,
                        (title, 0 if flip_oos else 1, miss_streak, oos_confirmed, now, image_url, asin),
                    )
                    self._maybe_cache_image(asin, image_url, config)
                    continue

                self.conn.execute(
                    """
                    UPDATE aes_products
                    SET title = COALESCE(?, title),
                        price = COALESCE(?, price),
                        in_stock = 1,
                        oos_miss_streak = 0,
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
                    alert = self._emit_stock_alert_if_allowed(
                        alert_type="back_in_stock",
                        source=source,
                        asin=asin,
                        title=title,
                        price=new_price,
                        image_url=image_url,
                        shipping=ship_line,
                        config=config,
                        telemetry=telemetry,
                        cycle_id=cycle_id,
                        _tel=_tel,
                        confirmed_transition=bool(row["last_oos_confirmed"]),
                    )
                    if alert is not None:
                        alerts.append(alert)
                        self.conn.execute(
                            "UPDATE aes_products SET last_stock_alert = ? WHERE asin = ?",
                            (now, asin),
                        )
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
                        _tel(
                            "price_drop_skipped",
                            asin=asin,
                            where=source,
                            old_price=old_price,
                            new_price=new_price,
                            pct_drop=round(pct, 2),
                            threshold_pct=self.price_drop_percent,
                            skip=price_decision.skip_reason,
                            last_price_alert=row["last_price_alert"],
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
                # Absence from filtered page-1 is a weak OOS signal (page churn, filter
                # blips), so count consecutive misses and only flip after the streak
                # reaches aes_oos_confirm_cycles.
                if seen_asins:
                    placeholders = ",".join("?" for _ in seen_asins)
                    seen_params = tuple(sorted(seen_asins))
                    self.conn.execute(
                        f"UPDATE aes_products SET oos_miss_streak = oos_miss_streak + 1 "
                        f"WHERE in_stock != 0 AND asin NOT IN ({placeholders})",
                        seen_params,
                    )
                    cursor = self.conn.execute(
                        f"UPDATE aes_products SET in_stock = 0, last_oos_confirmed = 0 "
                        f"WHERE in_stock != 0 AND oos_miss_streak >= ? AND asin NOT IN ({placeholders})",
                        (oos_confirm_cycles, *seen_params),
                    )
                else:
                    self.conn.execute(
                        "UPDATE aes_products SET oos_miss_streak = oos_miss_streak + 1 WHERE in_stock != 0"
                    )
                    cursor = self.conn.execute(
                        "UPDATE aes_products SET in_stock = 0, last_oos_confirmed = 0 WHERE in_stock != 0 AND oos_miss_streak >= ?",
                        (oos_confirm_cycles,),
                    )
                reconciled_oos_count = int(cursor.rowcount or 0)

            summary = {
                "candidate_rows": len(candidates),
                "inserted_count": inserted_count,
                "new_count": new_count,
                "back_in_stock_count": back_in_stock_count,
                "price_drop_count": price_drop_count,
                "reconciled_oos_count": reconciled_oos_count,
                "debounced_oos_count": debounced_oos_count,
                "reconcile_absence": reconcile_absence,
            }
            self.conn.commit()

        return alerts, summary
