"""Fast-lane HTTP stock checker for watch-list ASINs (no browser).

Why this exists: the Playwright lane renders full product pages, which costs
10-20+ seconds per ASIN, so the whole watch list is only re-checked every
couple of minutes. Restock bots beat that by polling Amazon's lightweight AOD
("All Offers Display" / See All Buying Options) ajax endpoint with plain HTTP
requests: ~50-150KB per check, a few hundred milliseconds, and it contains the
buy-box facts we need (price, Sold by, delivery line).

This process polls each watch ASIN every ``fast_watch_interval_seconds``
(default 40s) and writes results into the SAME ``products`` table via the same
``StateEngine`` used by main.py, so all the alert cooldown / dedupe rules
apply and the browser lane keeps working as the slower, more thorough layer.

It reuses the Playwright session cookies exported by main.py each cycle
(``data/session_cookies.json``), so requests carry the same session and the
configured delivery address.

Runs as its own PM2 process (see ecosystem.config.cjs, app "fast-watch").
"""

from __future__ import annotations

import html as html_lib
import json
import logging
import random
import re
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import requests
import yaml
from dotenv import load_dotenv

from pdp_helpers import is_not_shippable_text
from settings_store import load_runtime_config
from state_engine import StateEngine
from webhook_sender import send_alert

LOGGER = logging.getLogger("fast_watch")

AOD_URL = "https://www.amazon.com/gp/product/ajax/ref=aod_f_new?asin={asin}&pc=dp&experienceId=aodAjaxMain"
AOD_URL_FALLBACK = "https://www.amazon.com/gp/aod/ajax?asin={asin}&pc=dp"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.amazon.com/",
    "x-requested-with": "XMLHttpRequest",
}

_CAPTCHA_MARKERS = (
    "Enter the characters you see below",
    "api-services-support@amazon.com",
    "Robot Check",
    "/errors/validateCaptcha",
)
_NO_OFFER_MARKERS = (
    "no offers currently available",
    "There are currently no listings",
    "aod-no-offer",
)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_PRICE_RE = re.compile(r'class="[^"]*a-offscreen[^"]*"[^>]*>\s*\$([0-9][0-9,]*(?:\.[0-9]{2})?)')
_OFFER_COUNT_RE = re.compile(r'id="aod-total-offer-count"[^>]*value="(\d+)"')
_TITLE_RE = re.compile(r'id="aod-asin-title-text"[^>]*>\s*([^<]+?)\s*<')
_SOLD_BY_RE = re.compile(r"Sold by:?\s*(.{0,120})", re.IGNORECASE)
_DELIVERY_RE = re.compile(r"((?:FREE\s+)?delivery[^.<]{0,80})", re.IGNORECASE)


def _strip_tags(fragment: str) -> str:
    text = _TAG_RE.sub(" ", fragment)
    return _WS_RE.sub(" ", html_lib.unescape(text)).strip()


def _pinned_offer_fragment(html: str) -> str | None:
    start = html.find('id="aod-pinned-offer"')
    if start < 0:
        return None
    end = html.find('id="aod-offer-list"', start)
    if end < 0:
        end = min(len(html), start + 20_000)
    return html[start:end]


def parse_aod_response(html: str, allowed_seller_substrings: list[str]) -> dict[str, Any]:
    """Classify an AOD ajax response.

    Returns dict with:
      status: "in" | "out" | "unknown" | "captcha"
      price: float | None
      title: str | None
      shipping_text: str | None
      reason: short machine string for logs/telemetry
    """
    result: dict[str, Any] = {
        "status": "unknown",
        "price": None,
        "title": None,
        "shipping_text": None,
        "reason": None,
    }
    if not html or len(html) < 50:
        result["reason"] = "empty_response"
        return result
    if any(marker in html for marker in _CAPTCHA_MARKERS):
        result["status"] = "captcha"
        result["reason"] = "captcha_page"
        return result

    title_m = _TITLE_RE.search(html)
    if title_m:
        result["title"] = _WS_RE.sub(" ", html_lib.unescape(title_m.group(1))).strip() or None

    count_m = _OFFER_COUNT_RE.search(html)
    offer_count = int(count_m.group(1)) if count_m else None
    lowered = html.lower()
    if offer_count == 0 or any(m.lower() in lowered for m in _NO_OFFER_MARKERS):
        result["status"] = "out"
        result["reason"] = "no_offers"
        return result

    pinned = _pinned_offer_fragment(html)
    if pinned is None:
        # Offers may exist but we can't see the pinned (buy-box) offer -- do not
        # guess; the browser lane will resolve it.
        result["reason"] = "no_pinned_offer"
        return result

    pinned_text = _strip_tags(pinned)

    price = None
    price_m = _PRICE_RE.search(pinned)
    if price_m:
        try:
            price = float(price_m.group(1).replace(",", ""))
        except ValueError:
            price = None
    result["price"] = price

    sold_by = None
    sold_m = _SOLD_BY_RE.search(pinned_text)
    if sold_m:
        sold_by = sold_m.group(1).strip()
    seller_ok = bool(sold_by) and any(
        sub.lower() in sold_by.lower() for sub in allowed_seller_substrings if str(sub).strip()
    )

    delivery_m = _DELIVERY_RE.search(pinned_text)
    if delivery_m:
        result["shipping_text"] = delivery_m.group(1).strip()

    if price is None:
        result["reason"] = "pinned_no_price"
        return result
    if not seller_ok:
        # Same semantics as the PDP lane: wrong/unknown seller is "unknown", never OOS.
        result["reason"] = "seller_mismatch"
        return result
    if is_not_shippable_text(pinned_text):
        result["reason"] = "not_shippable"
        return result

    result["status"] = "in"
    result["reason"] = "pinned_offer_qualifies"
    return result


def _row_for_state_engine(asin: str, parsed: dict[str, Any]) -> dict[str, Any] | None:
    """Map a parsed AOD result to the PDP-row shape the state engine consumes."""
    status = parsed.get("status")
    if status == "in":
        return {
            "asin": asin,
            "title": parsed.get("title"),
            "price": parsed.get("price"),
            "in_stock": True,
            "stock_confidence": "confirmed_in",
            "stock_reason": None,
            "shipping_text": parsed.get("shipping_text") or "",
            "image_url": None,
            "seller": "fast_watch",
            "source": "fast_watch",
        }
    if status == "out":
        # AOD affirmatively said there are zero offers: strong OOS evidence, which
        # also arms the short "confirmed restock" cooldown in the state engine.
        return {
            "asin": asin,
            "title": parsed.get("title"),
            "price": None,
            "in_stock": False,
            "stock_confidence": "confirmed_out",
            "stock_reason": "explicit_oos",
            "shipping_text": "",
            "image_url": None,
            "seller": "fast_watch",
            "source": "fast_watch",
        }
    return None


def _load_cookie_jar(path: Path) -> dict[str, str]:
    """Read Playwright-exported cookies (list of dicts) into a simple name->value map."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    jar: dict[str, str] = {}
    if isinstance(raw, list):
        for c in raw:
            if isinstance(c, dict) and c.get("name"):
                jar[str(c["name"])] = str(c.get("value") or "")
    return jar


def _fill_row_from_db(se: StateEngine, asin: str, row: dict[str, Any]) -> None:
    """Reuse stored title/image so fast-lane alerts look like browser-lane alerts."""
    db_row = se.conn.execute(
        "SELECT title, image_url FROM products WHERE asin = ?", (asin,)
    ).fetchone()
    if db_row is None:
        return
    if not row.get("title"):
        row["title"] = db_row["title"]
    if not row.get("image_url"):
        row["image_url"] = db_row["image_url"]


class FastWatcher:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self.config: dict[str, Any] = {}
        self.config_loaded_at = 0.0
        self.state_engine: StateEngine | None = None
        self.session = requests.Session()
        self.session.headers.update(_HEADERS)
        self.cookie_mtime = 0.0
        self.next_due: dict[str, float] = {}
        self.backoff_until = 0.0
        self.backoff_level = 0
        self.check_count = 0
        self.alert_count = 0
        self.last_summary = time.monotonic()
        self.last_cookie_wait_log = 0.0

    # -- config / session ---------------------------------------------------

    def _reload_config_if_stale(self) -> None:
        reload_every = float(self.config.get("fast_watch_config_reload_seconds", 60) or 60)
        if self.config and (time.monotonic() - self.config_loaded_at) < reload_every:
            return
        self.config = load_runtime_config(self.db_path)
        self.config_loaded_at = time.monotonic()
        if self.state_engine is None:
            self.state_engine = StateEngine(
                db_path=self.db_path,
                price_drop_percent=float(self.config.get("price_drop_percent", 10)),
            )
        self._refresh_cookies_if_changed()

    def _refresh_cookies_if_changed(self) -> None:
        cookie_path = Path(str(self.config.get("fast_watch_cookie_path") or "data/session_cookies.json"))
        try:
            mtime = cookie_path.stat().st_mtime
        except OSError:
            return
        if mtime <= self.cookie_mtime:
            return
        jar = _load_cookie_jar(cookie_path)
        if jar:
            self.session.cookies.clear()
            for name, value in jar.items():
                self.session.cookies.set(name, value, domain=".amazon.com")
            self.cookie_mtime = mtime
            LOGGER.info("Session cookies refreshed (%d cookies).", len(jar))

    # -- pacing --------------------------------------------------------------

    def _interval_seconds(self) -> float:
        try:
            return max(10.0, float(self.config.get("fast_watch_interval_seconds", 40)))
        except (TypeError, ValueError):
            return 40.0

    def _min_gap_seconds(self) -> float:
        try:
            rpm = max(1.0, float(self.config.get("fast_watch_max_requests_per_minute", 20)))
        except (TypeError, ValueError):
            rpm = 20.0
        return 60.0 / rpm

    def _enter_backoff(self, reason: str) -> None:
        # Captchas are rare on this setup, so keep pauses light: default 90s,
        # doubling only to ~6 min on repeated hits. Tunable via settings.
        try:
            base = max(15.0, float(self.config.get("fast_watch_backoff_seconds", 90)))
        except (TypeError, ValueError):
            base = 90.0
        self.backoff_level = min(self.backoff_level + 1, 3)
        pause = base * (2 ** (self.backoff_level - 1))
        self.backoff_until = time.monotonic() + pause
        LOGGER.warning("Backing off %.0fs (%s, level %d).", pause, reason, self.backoff_level)

    # -- checking ------------------------------------------------------------

    def _fetch_aod(self, asin: str) -> tuple[int, str]:
        last_status, last_text = 0, ""
        for url_tpl in (AOD_URL, AOD_URL_FALLBACK):
            try:
                resp = self.session.get(url_tpl.format(asin=asin), timeout=15)
            except requests.RequestException as exc:
                LOGGER.info("AOD request failed asin=%s: %s", asin, exc)
                return 0, ""
            last_status, last_text = resp.status_code, resp.text or ""
            if resp.status_code == 200 and len(last_text) > 500:
                return last_status, last_text
        return last_status, last_text

    def _check_asin(self, asin: str) -> None:
        assert self.state_engine is not None
        status_code, body = self._fetch_aod(asin)
        self.check_count += 1
        if status_code in (403, 429, 503):
            self._enter_backoff(f"http_{status_code}")
            return
        if status_code != 200:
            LOGGER.info("AOD non-200 asin=%s status=%s", asin, status_code)
            return

        allowed = [
            str(s) for s in (self.config.get("pdp_allowed_seller_substrings") or ["amazon.com", "amazon export"])
        ]
        parsed = parse_aod_response(body, allowed)
        if parsed["status"] == "captcha":
            self._enter_backoff("captcha")
            return
        self.backoff_level = 0

        row = _row_for_state_engine(asin, parsed)
        LOGGER.debug(
            "checked asin=%s status=%s reason=%s price=%s",
            asin,
            parsed["status"],
            parsed["reason"],
            parsed["price"],
        )
        if row is None:
            return
        _fill_row_from_db(self.state_engine, asin, row)
        alerts, _summary = self.state_engine.process_pdp_watch_candidates(
            [row],
            {asin},
            source="fast_watch",
            config=self.config,
        )
        for alert in alerts:
            LOGGER.info(
                "ALERT %s asin=%s price=%s (fast lane)",
                alert.get("type"),
                asin,
                alert.get("price"),
            )
            send_alert(alert, self.config)
            self.alert_count += 1

    # -- main loop -----------------------------------------------------------

    def _maybe_log_summary(self) -> None:
        if time.monotonic() - self.last_summary < 600:
            return
        LOGGER.info("Summary: %d checks, %d alerts in last 10 min.", self.check_count, self.alert_count)
        self.check_count = 0
        self.alert_count = 0
        self.last_summary = time.monotonic()

    def run_forever(self) -> None:
        LOGGER.info("Fast watch starting (db=%s).", self.db_path)
        while True:
            try:
                self._tick()
            except Exception:  # noqa: BLE001 - loop must survive anything
                LOGGER.exception("Unexpected error in fast watch tick; sleeping 30s.")
                time.sleep(30)

    def _tick(self) -> None:
        self._reload_config_if_stale()
        self._maybe_log_summary()

        if not bool(self.config.get("fast_watch_enabled", True)):
            time.sleep(30)
            return
        if self.cookie_mtime <= 0 and bool(self.config.get("fast_watch_require_cookies", True)):
            # Cookieless XHRs get 503'd almost immediately; wait for the browser
            # lane to export its session instead of burning backoff cycles.
            if time.monotonic() - self.last_cookie_wait_log > 120:
                LOGGER.info(
                    "Waiting for session cookies from the browser lane (%s not loaded yet).",
                    self.config.get("fast_watch_cookie_path") or "data/session_cookies.json",
                )
                self.last_cookie_wait_log = time.monotonic()
            self._refresh_cookies_if_changed()
            time.sleep(10)
            return
        now = time.monotonic()
        if now < self.backoff_until:
            time.sleep(min(30.0, self.backoff_until - now))
            return

        watch = [str(a).upper() for a in (self.config.get("pdp_watch_asins") or [])]
        if not watch:
            time.sleep(30)
            return

        interval = self._interval_seconds()
        # Keep schedule entries only for current watch ASINs; stagger new ones so
        # a fresh start doesn't burst-fire the whole list at once.
        self.next_due = {a: t for a, t in self.next_due.items() if a in watch}
        for idx, asin in enumerate(watch):
            if asin not in self.next_due:
                self.next_due[asin] = now + (idx * interval / max(1, len(watch)))

        due = [a for a, t in self.next_due.items() if t <= now]
        if not due:
            wake = min(self.next_due.values())
            time.sleep(max(0.05, min(wake - now, 5.0)))
            return

        asin = min(due, key=lambda a: self.next_due[a])
        self._check_asin(asin)
        jitter = random.uniform(-0.1, 0.1) * interval
        self.next_due[asin] = time.monotonic() + interval + jitter
        time.sleep(self._min_gap_seconds() + random.uniform(0.0, 1.0))


def _setup_logging(log_dir: str) -> None:
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_handler = RotatingFileHandler(
        Path(log_dir) / "fast_watch.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(stream_handler)


def main() -> None:
    load_dotenv()
    bootstrap: dict[str, Any] = {}
    cfg_file = Path("config.yaml")
    if cfg_file.is_file():
        loaded = yaml.safe_load(cfg_file.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            bootstrap = loaded
    _setup_logging(str(bootstrap.get("log_dir", "logs")))
    db_path = str(bootstrap.get("db_path", "data/monitor.db"))
    FastWatcher(db_path).run_forever()


if __name__ == "__main__":
    main()
