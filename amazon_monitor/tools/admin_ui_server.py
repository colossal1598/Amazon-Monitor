"""Local admin UI server with Basic Auth and sqlite-web controls."""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import logging
import math
import mimetypes
import os
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

# PM2 runs this file from tools/ — project root must be on sys.path for imports.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import yaml
try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - fallback for minimal test environments
    def load_dotenv(*_args: Any, **_kwargs: Any) -> bool:
        return False

from pdp_helpers import valid_asin
from settings_store import (
    add_asin,
    list_asin_entries,
    load_runtime_config,
    remove_asin,
    set_asin_target_price,
    set_setting,
)

try:
    import sqlite_web  # noqa: F401

    _HAVE_SQLITE_WEB = True
except ImportError:
    _HAVE_SQLITE_WEB = False


ROOT = _ROOT
CONFIG_PATH = ROOT / "config.yaml"
STATIC_DIR = ROOT / "tools" / "admin_ui"

# Bind host/port. Overridable via .env (ADMIN_UI_HOST / ADMIN_UI_PORT) so the
# UI can sit on a Tailscale-funnel-compatible port (443/8443/10000) without
# code changes. Funnel proxies to localhost, so 127.0.0.1 stays the right bind.
HOST = "127.0.0.1"
PORT = 8443

SQLITE_WEB_HOST = "127.0.0.1"
SQLITE_WEB_PORT = 8768
SQLITE_WEB_TTL_SEC = 600

_SQLITE_LOCK = threading.RLock()
_sqlite_proc: subprocess.Popen | None = None
_sqlite_timer: threading.Timer | None = None
_sqlite_deadline_mono: float | None = None
_sqlite_read_only: bool | None = None

_PM2_LOCK = threading.RLock()
_pm2_last_restart_mono: float = 0.0
_PM2_RESTART_COOLDOWN_SEC = 30

LOG = logging.getLogger("admin-ui")


def _load_bootstrap_config() -> dict[str, Any]:
    if not CONFIG_PATH.is_file():
        return {}
    loaded = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    return loaded if isinstance(loaded, dict) else {}


def _resolve_db_path(cfg: dict[str, Any]) -> Path:
    raw = str(cfg.get("db_path") or "data/monitor.db").strip()
    db_path = Path(raw)
    if not db_path.is_absolute():
        db_path = ROOT / db_path
    return db_path.resolve()


def _subprocess_extra_kwargs() -> dict[str, Any]:
    kwargs: dict[str, Any] = {"cwd": str(ROOT)}
    if sys.platform == "win32" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
    return kwargs


def _popen_kwargs() -> dict[str, Any]:
    return {
        **_subprocess_extra_kwargs(),
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }


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
    with _SQLITE_LOCK:
        _stop_sqlite_web_unlocked()


def _on_sqlite_timer() -> None:
    with _SQLITE_LOCK:
        _stop_sqlite_web_unlocked()


def sqlite_web_status() -> dict[str, Any]:
    with _SQLITE_LOCK:
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
            "seconds_remaining": rem,
            "url": f"http://{SQLITE_WEB_HOST}:{SQLITE_WEB_PORT}/",
            "read_only": _sqlite_read_only,
        }


def start_sqlite_web(*, db_path: str, read_only: bool) -> dict[str, Any]:
    global _sqlite_proc, _sqlite_timer, _sqlite_deadline_mono, _sqlite_read_only
    if not _HAVE_SQLITE_WEB:
        return {
            "ok": False,
            "error": "sqlite_web_missing",
            "message": "sqlite-web is not installed. Install with: pip install sqlite-web",
        }

    db = Path(db_path)
    if not db.is_absolute():
        db = ROOT / db
    db = db.resolve()
    if not db.is_file():
        return {"ok": False, "error": "db_not_found", "message": f"Database not found: {db}"}

    with _SQLITE_LOCK:
        _stop_sqlite_web_unlocked()
        args = [sys.executable, "-m", "sqlite_web", "-x", "-H", SQLITE_WEB_HOST, "-p", str(SQLITE_WEB_PORT)]
        if read_only:
            args.append("-r")
        args.append(str(db))
        try:
            _sqlite_proc = subprocess.Popen(args, **_popen_kwargs())
        except OSError as exc:
            return {"ok": False, "error": "spawn_failed", "message": str(exc)}

        time.sleep(0.45)
        if _sqlite_proc.poll() is not None:
            _stop_sqlite_web_unlocked()
            return {
                "ok": False,
                "error": "sqlite_web_exited",
                "message": "sqlite-web exited immediately. Check db_path and database validity.",
            }

        _sqlite_deadline_mono = time.monotonic() + SQLITE_WEB_TTL_SEC
        _sqlite_read_only = read_only
        timer = threading.Timer(float(SQLITE_WEB_TTL_SEC), _on_sqlite_timer)
        timer.daemon = True
        _sqlite_timer = timer
        timer.start()

    return {
        "ok": True,
        "running": True,
        "seconds_remaining": SQLITE_WEB_TTL_SEC,
        "url": f"http://{SQLITE_WEB_HOST}:{SQLITE_WEB_PORT}/",
        "read_only": read_only,
    }


def restart_pm2_stack() -> dict[str, Any]:
    """Run `pm2 restart all` on the host machine (local admin only)."""
    global _pm2_last_restart_mono
    now = time.monotonic()
    with _PM2_LOCK:
        if now - _pm2_last_restart_mono < _PM2_RESTART_COOLDOWN_SEC:
            wait = int(_PM2_RESTART_COOLDOWN_SEC - (now - _pm2_last_restart_mono))
            return {
                "ok": False,
                "error": "cooldown",
                "message": f"נא להמתין {wait} שניות לפני הפעלה מחדש נוספת",
            }
        _pm2_last_restart_mono = now

    try:
        completed = subprocess.run(
            ["pm2", "restart", "all"],
            capture_output=True,
            text=True,
            timeout=120,
            **_subprocess_extra_kwargs(),
        )
    except FileNotFoundError:
        return {
            "ok": False,
            "error": "pm2_not_found",
            "message": "PM2 לא מותקן או לא נמצא ב-PATH",
        }
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "error": "timeout",
            "message": "הפעלה מחדש נמשכה יותר מדי זמן",
        }
    except OSError:
        return {
            "ok": False,
            "error": "spawn_failed",
            "message": "לא ניתן להריץ את PM2",
        }

    stdout_tail = (completed.stdout or "")[-500:].strip()
    stderr_tail = (completed.stderr or "")[-500:].strip()
    ok = completed.returncode == 0
    payload: dict[str, Any] = {
        "ok": ok,
        "exit_code": completed.returncode,
        "message": "המערכת הופעלה מחדש" if ok else "הפעלה מחדש נכשלה",
    }
    if stdout_tail:
        payload["stdout_tail"] = stdout_tail
    if stderr_tail:
        payload["stderr_tail"] = stderr_tail
    return payload


def _settings_for_client(db_path: str) -> dict[str, Any]:
    cfg = load_runtime_config(db_path)
    cfg.pop("wa_api_key", None)
    return cfg


def _israel_tz():
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo("Asia/Jerusalem")
    except Exception:
        return timezone(timedelta(hours=3), name="Asia/Jerusalem")


def _resolve_telemetry_db_path(monitor_db_path: str) -> Path:
    bootstrap = _load_bootstrap_config()
    runtime = load_runtime_config(monitor_db_path)
    raw = str(
        bootstrap.get("telemetry_db_path")
        or runtime.get("telemetry_db_path")
        or "data/telemetry.db"
    ).strip()
    db_path = Path(raw)
    if not db_path.is_absolute():
        db_path = ROOT / db_path
    return db_path.resolve()


def _health_summary() -> dict[str, Any]:
    health_path = ROOT / "data" / "health.json"
    if not health_path.is_file():
        return {"ok": True, "health": None}
    try:
        data = json.loads(health_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"ok": True, "health": None}
    if not isinstance(data, dict):
        return {"ok": True, "health": None}
    return {"ok": True, "health": data}


def _bandwidth_summary(monitor_db_path: str) -> dict[str, Any]:
    telemetry_path = _resolve_telemetry_db_path(monitor_db_path)
    empty: dict[str, Any] = {
        "last_cycle": None,
        "today": None,
        "last_7_days_gb": 0.0,
        "recent_cycles": [],
    }
    if not telemetry_path.is_file():
        return empty

    tz = _israel_tz()
    today_il = datetime.now(tz).date().isoformat()
    week_start_il = (datetime.now(tz).date() - timedelta(days=6)).isoformat()

    conn = sqlite3.connect(str(telemetry_path), timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        last_row = conn.execute(
            """
            SELECT recorded_at_il, net_bytes_total, net_bytes_pdp, net_bytes_aes,
                   net_bytes_image, blocked_url, blocked_heavy, gb_est_total
            FROM cycle_stats
            WHERE net_bytes_total IS NOT NULL
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        last_cycle = dict(last_row) if last_row else None

        today_row = conn.execute(
            """
            SELECT date_il, bytes_total, bytes_pdp, bytes_aes, bytes_image, cycles, updated_at_il
            FROM daily_bandwidth
            WHERE date_il = ?
            """,
            (today_il,),
        ).fetchone()
        today = dict(today_row) if today_row else None

        week_sum = conn.execute(
            """
            SELECT COALESCE(SUM(bytes_total), 0) AS bytes_total
            FROM daily_bandwidth
            WHERE date_il >= ? AND date_il <= ?
            """,
            (week_start_il, today_il),
        ).fetchone()
        last_7_bytes = int(week_sum["bytes_total"] if week_sum else 0)
        last_7_days_gb = round(last_7_bytes / (1024**3), 3)

        recent_rows = conn.execute(
            """
            SELECT recorded_at_il, net_bytes_total
            FROM cycle_stats
            WHERE net_bytes_total IS NOT NULL
            ORDER BY id DESC
            LIMIT 10
            """
        ).fetchall()
        recent_cycles = [dict(r) for r in recent_rows]
    finally:
        conn.close()

    return {
        "last_cycle": last_cycle,
        "today": today,
        "last_7_days_gb": last_7_days_gb,
        "recent_cycles": recent_cycles,
    }


def _parse_target_price(raw: Any) -> tuple[bool, float | None]:
    """Validate an optional target_price value. Returns (ok, value).

    None/absent is valid (clears the target). Otherwise must be a finite
    float > 0.
    """
    if raw is None:
        return True, None
    if isinstance(raw, bool):
        return False, None
    if not isinstance(raw, (int, float, str)):
        return False, None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return False, None
    if not math.isfinite(value) or value <= 0:
        return False, None
    return True, value


def _parse_json_body(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    content_length = int(handler.headers.get("Content-Length", "0"))
    raw = handler.rfile.read(content_length) if content_length > 0 else b"{}"
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("invalid_json") from exc
    if not isinstance(parsed, dict):
        raise ValueError("json_must_be_object")
    return parsed


class AdminUIHandler(BaseHTTPRequestHandler):
    server: ThreadingHTTPServer

    def log_message(self, format: str, *args: Any) -> None:
        LOG.info("%s - %s", self.address_string(), format % args)

    @property
    def db_path(self) -> str:
        return str(getattr(self.server, "db_path"))

    @property
    def admin_user(self) -> str:
        return str(getattr(self.server, "admin_user"))

    @property
    def admin_password(self) -> str:
        return str(getattr(self.server, "admin_password"))

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _drain_request_body(self) -> None:
        """Consume any unread request body before responding early (401/400).

        Closing the connection with unread bytes in the socket buffer makes
        Windows send a TCP RST, which can destroy the in-flight response before
        the client reads it (observed as ConnectionAbortedError/WinError 10053
        on unauthenticated POSTs). Bounded read so a huge bogus body can't
        stall the handler thread.
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            return
        remaining = min(max(0, length), 1_048_576)
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                break
            remaining -= len(chunk)

    def _send_unauthorized(self) -> None:
        self._drain_request_body()
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Amazon Monitor Admin UI", charset="UTF-8"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _is_authorized(self) -> bool:
        auth_header = self.headers.get("Authorization") or ""
        if not auth_header.startswith("Basic "):
            return False
        token = auth_header[6:].strip()
        try:
            decoded = base64.b64decode(token).decode("utf-8")
        except (ValueError, UnicodeDecodeError, binascii.Error):
            return False
        user, sep, password = decoded.partition(":")
        if sep != ":":
            return False
        return hmac.compare_digest(user, self.admin_user) and hmac.compare_digest(password, self.admin_password)

    def _require_auth(self) -> bool:
        if self._is_authorized():
            return True
        self._send_unauthorized()
        return False

    @staticmethod
    def _is_public_static(path: str) -> bool:
        """HTML/JS/CSS load without auth; only /api/* needs credentials."""
        if path in ("", "/"):
            return True
        return path in {"/index.html", "/help.html", "/app.js", "/styles.css"}

    def _serve_static(self, path: str) -> None:
        rel = "index.html" if path in ("", "/") else path.lstrip("/")
        target = (STATIC_DIR / rel).resolve()
        try:
            target.relative_to(STATIC_DIR.resolve())
        except ValueError:
            self._json(404, {"ok": False, "error": "not_found"})
            return

        if not target.is_file():
            self._json(404, {"ok": False, "error": "not_found"})
            return

        body = target.read_bytes()
        ctype, _ = mimetypes.guess_type(str(target))
        content_type = ctype or "application/octet-stream"
        if content_type.startswith("text/") or content_type in {"application/javascript", "application/json"}:
            content_type = f"{content_type}; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path

        if self._is_public_static(path):
            if path in ("", "/"):
                self._serve_static("/index.html")
            else:
                self._serve_static(path)
            return

        if not self._require_auth():
            return

        if path == "/api/settings":
            self._json(200, {"ok": True, "settings": _settings_for_client(self.db_path)})
            return
        if path == "/api/asins":
            role = (parse_qs(parsed.query).get("role", [""])[0] or "").strip().lower()
            if role not in {"watch", "blacklist"}:
                self._json(400, {"ok": False, "error": "invalid_role"})
                return
            self._json(200, {"ok": True, "items": list_asin_entries(self.db_path, role), "role": role})
            return
        if path == "/api/sqlite/status":
            self._json(200, sqlite_web_status())
            return
        if path == "/api/bandwidth/summary":
            summary = _bandwidth_summary(self.db_path)
            self._json(200, {"ok": True, **summary})
            return
        if path == "/api/health":
            self._json(200, _health_summary())
            return

        self._json(404, {"ok": False, "error": "not_found"})

    def do_PUT(self) -> None:  # noqa: N802
        if not self._require_auth():
            return
        parsed = urlparse(self.path)

        if parsed.path == "/api/asins/target":
            try:
                payload = _parse_json_body(self)
            except ValueError as exc:
                self._json(400, {"ok": False, "error": str(exc)})
                return
            asin = str(payload.get("asin") or "").strip().upper()
            if not valid_asin(asin):
                self._json(400, {"ok": False, "error": "invalid_asin"})
                return
            price_ok, target_price = _parse_target_price(payload.get("target_price"))
            if not price_ok:
                self._json(400, {"ok": False, "error": "invalid_target_price"})
                return
            found = set_asin_target_price(self.db_path, asin, target_price)
            if not found:
                self._json(404, {"ok": False, "error": "not_found"})
                return
            self._json(200, {"ok": True, "asin": asin, "target_price": target_price})
            return

        if parsed.path != "/api/settings":
            self._json(404, {"ok": False, "error": "not_found"})
            return

        try:
            payload = _parse_json_body(self)
        except ValueError as exc:
            self._json(400, {"ok": False, "error": str(exc)})
            return

        updates = payload.get("settings", payload)
        if not isinstance(updates, dict):
            self._json(400, {"ok": False, "error": "settings_must_be_object"})
            return

        skipped: list[str] = []
        for key, value in updates.items():
            setting_key = str(key).strip()
            if not setting_key:
                continue
            if setting_key in {"wa_api_key", "pdp_watch_asins", "blacklist", "whitelist"}:
                skipped.append(setting_key)
                continue
            set_setting(self.db_path, setting_key, value)

        self._json(
            200,
            {
                "ok": True,
                "updated": sorted(str(k).strip() for k in updates.keys() if str(k).strip()),
                "skipped": skipped,
                "settings": _settings_for_client(self.db_path),
            },
        )

    def do_POST(self) -> None:  # noqa: N802
        if not self._require_auth():
            return
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/asins":
            try:
                payload = _parse_json_body(self)
            except ValueError as exc:
                self._json(400, {"ok": False, "error": str(exc)})
                return
            asin = str(payload.get("asin") or "").strip().upper()
            role = str(payload.get("role") or "").strip().lower()
            notes = str(payload.get("notes") or "").strip()
            if role not in {"watch", "blacklist"}:
                self._json(400, {"ok": False, "error": "invalid_role"})
                return
            if not valid_asin(asin):
                self._json(400, {"ok": False, "error": "invalid_asin"})
                return
            price_ok, target_price = _parse_target_price(payload.get("target_price"))
            if not price_ok:
                self._json(400, {"ok": False, "error": "invalid_target_price"})
                return
            add_asin(self.db_path, asin, role, notes=notes or None, target_price=target_price)
            self._json(
                200,
                {"ok": True, "asin": asin, "role": role, "notes": notes, "target_price": target_price},
            )
            return

        if path == "/api/sqlite/start":
            try:
                payload = _parse_json_body(self)
            except ValueError as exc:
                self._json(400, {"ok": False, "error": str(exc)})
                return
            read_only = bool(payload.get("read_only", True))
            result = start_sqlite_web(db_path=self.db_path, read_only=read_only)
            self._json(200 if result.get("ok") else 400, result)
            return

        if path == "/api/sqlite/stop":
            stop_sqlite_web()
            self._json(200, {"ok": True})
            return

        if path == "/api/pm2/restart":
            result = restart_pm2_stack()
            self._json(200 if result.get("ok") else 400, result)
            return

        self._json(404, {"ok": False, "error": "not_found"})

    def do_DELETE(self) -> None:  # noqa: N802
        if not self._require_auth():
            return
        parsed = urlparse(self.path)
        parts = parsed.path.strip("/").split("/")
        if len(parts) == 4 and parts[0] == "api" and parts[1] == "asins":
            asin = unquote(parts[2]).strip().upper()
            role = unquote(parts[3]).strip().lower()
            if role not in {"watch", "blacklist"}:
                self._json(400, {"ok": False, "error": "invalid_role"})
                return
            if not valid_asin(asin):
                self._json(400, {"ok": False, "error": "invalid_asin"})
                return
            remove_asin(self.db_path, asin, role)
            self._json(200, {"ok": True, "asin": asin, "role": role})
            return
        self._json(404, {"ok": False, "error": "not_found"})


def create_server(
    *,
    host: str = HOST,
    port: int = PORT,
    db_path: str | Path | None = None,
    admin_user: str,
    admin_password: str,
) -> ThreadingHTTPServer:
    resolved_db = _resolve_db_path(_load_bootstrap_config()) if db_path is None else Path(db_path)
    server = ThreadingHTTPServer((host, int(port)), AdminUIHandler)
    server.db_path = str(resolved_db)  # type: ignore[attr-defined]
    server.admin_user = admin_user  # type: ignore[attr-defined]
    server.admin_password = admin_password  # type: ignore[attr-defined]
    return server


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    load_dotenv(ROOT / ".env")

    admin_user = os.environ.get("ADMIN_UI_USER", "").strip()
    admin_password = os.environ.get("ADMIN_UI_PASSWORD", "").strip()
    if not admin_user or not admin_password:
        msg = (
            "ADMIN_UI_USER / ADMIN_UI_PASSWORD missing. "
            "Add both to amazon_monitor/.env then: pm2 restart admin-ui"
        )
        LOG.error(msg)
        print(msg, file=sys.stderr, flush=True)
        return 1

    db_path = _resolve_db_path(_load_bootstrap_config())
    db_path.parent.mkdir(parents=True, exist_ok=True)

    host = os.environ.get("ADMIN_UI_HOST", "").strip() or HOST
    try:
        port = int(os.environ.get("ADMIN_UI_PORT", "").strip() or PORT)
    except ValueError:
        port = PORT

    server = create_server(
        host=host, port=port, db_path=db_path, admin_user=admin_user, admin_password=admin_password
    )
    admin_url = f"http://{host}" if port == 80 else f"http://{host}:{port}"
    LOG.info("Admin UI running at %s", admin_url)
    LOG.info(
        "sqlite-web is exposed only locally at http://%s:%s and auto-stops after %ss",
        SQLITE_WEB_HOST,
        SQLITE_WEB_PORT,
        SQLITE_WEB_TTL_SEC,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOG.info("Shutting down admin UI server.")
    finally:
        stop_sqlite_web()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
