import json
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

try:
    import sqlite_web  # noqa: F401
    _HAVE_SQLITE_WEB = True
except ImportError:
    _HAVE_SQLITE_WEB = False

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config.yaml"
HTML_PATH = ROOT / "tools" / "config_editor_he.html"
SCRIPTS_DIR = ROOT / "scripts"

# Only these template keys are edited in the web UI; operational keys are code-only in webhook_sender.
EDITOR_TEMPLATE_KEYS = frozenset({"default", "new_product", "price_drop", "back_in_stock"})
# Remove from saved YAML so old custom operational text does not confuse (runtime ignores them anyway).
STRIP_FROM_SAVED_TEMPLATES = frozenset(
    {"setup_test", "heartbeat_ok", "heartbeat_error", "search_error", "modem_error", "modem_trigger"}
)

SQLITE_WEB_HOST = "127.0.0.1"
SQLITE_WEB_PORT = 8768
SQLITE_WEB_TTL_SEC = 600

_sqlite_lock = threading.RLock()
_sqlite_proc: subprocess.Popen | None = None
_sqlite_timer: threading.Timer | None = None
_sqlite_deadline_mono: float | None = None
_sqlite_read_only: bool | None = None


def _resolve_db_path(cfg: dict) -> Path:
    raw = cfg.get("db_path") or "data/monitor.db"
    p = Path(str(raw).strip())
    if not p.is_absolute():
        p = ROOT / p
    return p.resolve()


def _popen_kwargs() -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "cwd": str(ROOT),
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if sys.platform == "win32" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
    return kwargs


def _stop_sqlite_web_unlocked() -> None:
    global _sqlite_proc, _sqlite_timer, _sqlite_deadline_mono, _sqlite_read_only
    if _sqlite_timer is not None:
        _sqlite_timer.cancel()
        _sqlite_timer = None
    if _sqlite_proc is not None:
        proc = _sqlite_proc
        _sqlite_proc = None
        try:
            proc.terminate()
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=4)
            except Exception:
                pass
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    _sqlite_deadline_mono = None
    _sqlite_read_only = None


def stop_sqlite_web() -> None:
    with _sqlite_lock:
        _stop_sqlite_web_unlocked()


def _on_sqlite_timer() -> None:
    with _sqlite_lock:
        _stop_sqlite_web_unlocked()


def sqlite_web_status() -> dict[str, Any]:
    with _sqlite_lock:
        if _sqlite_proc is None or _sqlite_deadline_mono is None:
            return {
                "ok": True,
                "running": False,
                "seconds_remaining": 0,
                "url": None,
                "read_only": None,
            }
        if _sqlite_proc.poll() is not None:
            _stop_sqlite_web_unlocked()
            return {
                "ok": True,
                "running": False,
                "seconds_remaining": 0,
                "url": None,
                "read_only": None,
            }
        rem = max(0, int(_sqlite_deadline_mono - time.monotonic()))
        return {
            "ok": True,
            "running": True,
            "read_only": _sqlite_read_only,
            "seconds_remaining": rem,
            "url": f"http://{SQLITE_WEB_HOST}:{SQLITE_WEB_PORT}/",
        }


def start_sqlite_web(*, read_only: bool) -> dict[str, Any]:
    global _sqlite_proc, _sqlite_timer, _sqlite_deadline_mono, _sqlite_read_only
    if not _HAVE_SQLITE_WEB:
        return {
            "ok": False,
            "error": "sqlite_web_missing",
            "message_he": "חבילת sqlite-web לא מותקנת. הריצו: pip install sqlite-web",
        }

    cfg = load_config()
    db = _resolve_db_path(cfg)
    if not db.is_file():
        return {
            "ok": False,
            "error": "db_not_found",
            "message_he": "קובץ מסד הנתונים לא נמצא בנתיב המוגדר. ודאו ש־db_path נכון או שהמוניטור כבר יצר את הקובץ.",
        }

    with _sqlite_lock:
        _stop_sqlite_web_unlocked()
        if read_only:
            args = [
                sys.executable,
                "-m",
                "sqlite_web",
                "-x",
                "-H",
                SQLITE_WEB_HOST,
                "-p",
                str(SQLITE_WEB_PORT),
                "-r",
                str(db),
            ]
        else:
            args = [
                sys.executable,
                "-m",
                "sqlite_web",
                "-x",
                "-H",
                SQLITE_WEB_HOST,
                "-p",
                str(SQLITE_WEB_PORT),
                str(db),
            ]
        try:
            _sqlite_proc = subprocess.Popen(args, **_popen_kwargs())
        except OSError as exc:
            return {"ok": False, "error": "spawn_failed", "message_he": str(exc)}

        time.sleep(0.45)
        if _sqlite_proc.poll() is not None:
            _stop_sqlite_web_unlocked()
            return {
                "ok": False,
                "error": "sqlite_web_exited",
                "message_he": "sqlite-web נסגר מיד — ייתכן שהקובץ אינו מסד SQLite תקין או שהנתיב שגוי.",
            }

        _sqlite_deadline_mono = time.monotonic() + SQLITE_WEB_TTL_SEC
        _sqlite_read_only = read_only
        t = threading.Timer(float(SQLITE_WEB_TTL_SEC), _on_sqlite_timer)
        t.daemon = True
        _sqlite_timer = t
        t.start()

    url = f"http://{SQLITE_WEB_HOST}:{SQLITE_WEB_PORT}/"
    return {
        "ok": True,
        "url": url,
        "seconds_remaining": SQLITE_WEB_TTL_SEC,
        "read_only": read_only,
    }


# Read the current YAML config from disk (or return an empty config if it doesn’t exist yet).
def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data


# Save an updated config back to YAML so the monitor will use it on the next run.
def save_config(updated: dict) -> None:
    with CONFIG_PATH.open("w", encoding="utf-8") as f:
        yaml.safe_dump(updated, f, allow_unicode=True, sort_keys=False)


# Prepare a browser-safe copy of the config by stripping secrets so you can edit settings without exposing keys.
def config_for_client(cfg: dict) -> dict:
    """Copy safe for browser — omits secrets."""
    out = dict(cfg)
    out.pop("wa_api_key", None)
    return out


# Turn a “min/max” style input into a clean pair of numbers, falling back to defaults when needed.
def _coerce_float_pair(raw: Any, default: tuple[float, float]) -> tuple[float, float]:
    if isinstance(raw, (list, tuple)) and len(raw) >= 2:
        return float(raw[0]), float(raw[1])
    return default


# Clean up a list of strings from the editor so it becomes a neat list without empty items.
def _normalize_string_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(x).strip() for x in raw if str(x).strip()]


# Apply the web editor’s changes onto the config while keeping sensitive fields (like the API key) untouched.
def apply_config_payload(cfg: dict, payload: dict) -> None:
    """Merge editor payload into cfg. Never touches wa_api_key."""
    search = payload.get("search")
    if isinstance(search, dict):
        urls = cfg.get("search_urls")
        if not isinstance(urls, dict):
            urls = {}
            cfg["search_urls"] = urls
        if search.get("amazon_com") is not None:
            urls["amazon_com"] = str(search.get("amazon_com", "")).strip()
        if search.get("aes_llc") is not None:
            urls["aes_llc"] = str(search.get("aes_llc", "")).strip()

    delays = payload.get("delays")
    if isinstance(delays, dict):
        scroll_def = _coerce_float_pair(cfg.get("search_scroll_delay_seconds"), (0.25, 0.65))
        page_def = _coerce_float_pair(cfg.get("search_pagination_delay_seconds"), (2.0, 4.5))
        sm = delays.get("scroll_min")
        sx = delays.get("scroll_max")
        if sm is not None and sx is not None:
            cfg["search_scroll_delay_seconds"] = [float(sm), float(sx)]
        elif delays.get("scroll") is not None:
            a, b = _coerce_float_pair(delays.get("scroll"), scroll_def)
            cfg["search_scroll_delay_seconds"] = [a, b]
        pm = delays.get("pagination_min")
        px = delays.get("pagination_max")
        if pm is not None and px is not None:
            cfg["search_pagination_delay_seconds"] = [float(pm), float(px)]
        elif delays.get("pagination") is not None:
            a, b = _coerce_float_pair(delays.get("pagination"), page_def)
            cfg["search_pagination_delay_seconds"] = [a, b]

    keywords = payload.get("keywords")
    if isinstance(keywords, dict):
        if keywords.get("required") is not None:
            cfg["required_keywords"] = _normalize_string_list(keywords.get("required"))
        if keywords.get("whitelist") is not None:
            cfg["whitelist"] = _normalize_string_list(keywords.get("whitelist"))
        if keywords.get("blacklist") is not None:
            cfg["blacklist"] = _normalize_string_list(keywords.get("blacklist"))
        if keywords.get("title_blacklist") is not None:
            cfg["title_blacklist_phrases"] = _normalize_string_list(keywords.get("title_blacklist"))

    pdp = payload.get("pdp")
    if isinstance(pdp, dict):
        if pdp.get("watch_asins") is not None:
            cfg["pdp_watch_asins"] = _normalize_string_list(pdp.get("watch_asins"))
        if pdp.get("allowed_seller_substrings") is not None:
            cfg["pdp_allowed_seller_substrings"] = _normalize_string_list(pdp.get("allowed_seller_substrings"))

    scraping = payload.get("scraping")
    if isinstance(scraping, dict):
        if scraping.get("search_poll_minutes") is not None:
            cfg["search_poll_minutes"] = int(scraping["search_poll_minutes"])
        if scraping.get("search_pages") is not None:
            cfg["search_pages"] = int(scraping["search_pages"])
        if scraping.get("max_search_pages") is not None:
            cfg["max_search_pages"] = int(scraping["max_search_pages"])
        if scraping.get("max_requests_per_minute") is not None:
            cfg["max_requests_per_minute"] = int(scraping["max_requests_per_minute"])
        if scraping.get("max_cycle_seconds") is not None:
            cfg["max_cycle_seconds"] = int(scraping["max_cycle_seconds"])
        if scraping.get("pagination_mode") is not None:
            mode = str(scraping["pagination_mode"]).lower().strip()
            cfg["pagination_mode"] = mode if mode in ("auto", "fixed") else "auto"
        if scraping.get("search_serp_inner_retries") is not None:
            cfg["search_serp_inner_retries"] = max(0, int(scraping["search_serp_inner_retries"]))
        if scraping.get("captcha_recovery_pause_seconds") is not None:
            cfg["captcha_recovery_pause_seconds"] = max(0, int(scraping["captcha_recovery_pause_seconds"]))

    fx = payload.get("fx")
    if isinstance(fx, dict):
        if fx.get("fx_enabled") is not None:
            cfg["fx_enabled"] = bool(fx["fx_enabled"])
        if fx.get("fx_refresh_every_runs") is not None:
            cfg["fx_refresh_every_runs"] = max(1, int(fx["fx_refresh_every_runs"]))
        if fx.get("fx_fallback_usd_ils") is not None:
            cfg["fx_fallback_usd_ils"] = float(fx["fx_fallback_usd_ils"])
        if fx.get("fx_request_timeout_seconds") is not None:
            cfg["fx_request_timeout_seconds"] = max(0.0, float(fx["fx_request_timeout_seconds"]))
        if fx.get("fx_cache_path") is not None:
            cfg["fx_cache_path"] = str(fx.get("fx_cache_path", "")).strip()

    alerts = payload.get("alerts")
    if isinstance(alerts, dict):
        if alerts.get("price_drop_percent") is not None:
            cfg["price_drop_percent"] = float(alerts["price_drop_percent"])
        if alerts.get("enable_missing_asin_oos") is not None:
            cfg["enable_missing_asin_oos"] = bool(alerts["enable_missing_asin_oos"])
        if alerts.get("min_results_for_absence_reconcile") is not None:
            cfg["min_results_for_absence_reconcile"] = int(alerts["min_results_for_absence_reconcile"])

    paths = payload.get("paths")
    if isinstance(paths, dict):
        if paths.get("db_path") is not None:
            cfg["db_path"] = str(paths.get("db_path", "")).strip()
        if paths.get("log_dir") is not None:
            cfg["log_dir"] = str(paths.get("log_dir", "")).strip()
        if paths.get("auth_dir") is not None:
            cfg["auth_dir"] = str(paths.get("auth_dir", "")).strip()

    whatsapp = payload.get("whatsapp")
    if isinstance(whatsapp, dict):
        if whatsapp.get("wa_api_url") is not None:
            cfg["wa_api_url"] = str(whatsapp.get("wa_api_url", "")).strip()
        if whatsapp.get("wa_group_id") is not None:
            cfg["wa_group_id"] = str(whatsapp.get("wa_group_id", "")).strip()
        if whatsapp.get("wa_client_to") is not None:
            cfg["wa_client_to"] = str(whatsapp.get("wa_client_to", "")).strip()
        if whatsapp.get("affiliate_tag") is not None:
            cfg["affiliate_tag"] = str(whatsapp.get("affiliate_tag", "")).strip()
        if whatsapp.get("wa_send_heartbeat") is not None:
            cfg["wa_send_heartbeat"] = bool(whatsapp.get("wa_send_heartbeat"))

    templates = payload.get("templates")
    if isinstance(templates, dict):
        existing = cfg.get("wa_message_templates")
        if not isinstance(existing, dict):
            existing = {}
        merged = dict(existing)
        for k in EDITOR_TEMPLATE_KEYS:
            if k in templates:
                merged[k] = str(templates[k])
        for k in STRIP_FROM_SAVED_TEMPLATES:
            merged.pop(k, None)
        cfg["wa_message_templates"] = merged

    advanced = payload.get("advanced")
    if isinstance(advanced, dict) and advanced.get("wa_restart_command") is not None:
        cfg["wa_restart_command"] = str(advanced.get("wa_restart_command", "")).strip()

    cfg.pop("required_any_keywords", None)
    cfg.pop("blacklist_file", None)


# Restart the monitor scripts (and optionally the WhatsApp service) so config changes take effect without manual steps.
def restart_services(cfg: dict) -> None:
    stop_script = SCRIPTS_DIR / "stop_monitor.ps1"
    start_script = SCRIPTS_DIR / "start_monitor.ps1"

    subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(stop_script)],
        cwd=str(ROOT),
        check=False,
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(start_script)],
        cwd=str(ROOT),
        check=False,
    )

    wa_restart_command = str(cfg.get("wa_restart_command") or "").strip()
    if wa_restart_command:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", wa_restart_command],
            cwd=str(ROOT),
        )


class Handler(BaseHTTPRequestHandler):
    # Send a JSON response in a consistent way so the frontend can reliably parse success and error replies.
    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # Serve the HTML editor page and provide a read-only config endpoint for the browser.
    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            html = HTML_PATH.read_text(encoding="utf-8")
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/config":
            cfg = load_config()
            self._json(200, {"ok": True, "config": config_for_client(cfg)})
            return
        if path == "/api/sqlite-web/status":
            self._json(200, sqlite_web_status())
            return
        self._json(404, {"ok": False, "error": "not_found"})

    # Accept config changes from the browser, validate required fields, save them, and optionally restart services.
    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        content_length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(content_length) if content_length > 0 else b""

        if path == "/api/sqlite-web/start":
            try:
                body = json.loads(raw.decode("utf-8")) if raw else {}
            except json.JSONDecodeError:
                self._json(400, {"ok": False, "error": "invalid_json"})
                return
            mode = str(body.get("mode") or "").strip().lower()
            read_only = mode in ("read_only", "ro", "r")
            result = start_sqlite_web(read_only=read_only)
            status = 200 if result.get("ok") else 400
            self._json(status, result)
            return

        if path == "/api/sqlite-web/stop":
            stop_sqlite_web()
            self._json(200, {"ok": True})
            return

        if path != "/api/config":
            self._json(404, {"ok": False, "error": "not_found"})
            return

        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            self._json(400, {"ok": False, "error": "invalid_json"})
            return

        cfg = load_config()
        apply_config_payload(cfg, payload)
        restart_requested = bool(payload.get("restart_services"))

        if not cfg.get("wa_api_url") or not cfg.get("wa_group_id"):
            self._json(400, {"ok": False, "error": "missing_required_fields"})
            return

        save_config(cfg)
        if restart_requested:
            restart_services(cfg)
        self._json(200, {"ok": True, "restarted": restart_requested})


# Run a tiny local web server so you can edit `config.yaml` in a friendly Hebrew UI.
def main() -> None:
    server = HTTPServer(("127.0.0.1", 8765), Handler)
    print("Hebrew config editor running at http://127.0.0.1:8765")
    print(
        f"sqlite-web (when started from the UI) uses http://{SQLITE_WEB_HOST}:{SQLITE_WEB_PORT}/ "
        f"and stops after {SQLITE_WEB_TTL_SEC // 60} minutes."
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
