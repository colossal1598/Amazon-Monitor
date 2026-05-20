"""Local admin UI server with Basic Auth and sqlite-web controls."""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import logging
import mimetypes
import os
import subprocess
import sys
import threading
import time
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
from settings_store import add_asin, list_asin_entries, load_runtime_config, remove_asin, set_setting

try:
    import sqlite_web  # noqa: F401

    _HAVE_SQLITE_WEB = True
except ImportError:
    _HAVE_SQLITE_WEB = False


ROOT = _ROOT
CONFIG_PATH = ROOT / "config.yaml"
STATIC_DIR = ROOT / "tools" / "admin_ui"

HOST = "127.0.0.1"
PORT = 8765

SQLITE_WEB_HOST = "127.0.0.1"
SQLITE_WEB_PORT = 8768
SQLITE_WEB_TTL_SEC = 600

_SQLITE_LOCK = threading.RLock()
_sqlite_proc: subprocess.Popen | None = None
_sqlite_timer: threading.Timer | None = None
_sqlite_deadline_mono: float | None = None
_sqlite_read_only: bool | None = None

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


def _settings_for_client(db_path: str) -> dict[str, Any]:
    cfg = load_runtime_config(db_path)
    cfg.pop("wa_api_key", None)
    return cfg


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

    def _send_unauthorized(self) -> None:
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
        return path in {"/index.html", "/app.js", "/styles.css"}

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

        self._json(404, {"ok": False, "error": "not_found"})

    def do_PUT(self) -> None:  # noqa: N802
        if not self._require_auth():
            return
        parsed = urlparse(self.path)
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
            add_asin(self.db_path, asin, role, notes=notes or None)
            self._json(200, {"ok": True, "asin": asin, "role": role, "notes": notes})
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

    server = create_server(db_path=db_path, admin_user=admin_user, admin_password=admin_password)
    LOG.info("Admin UI running at http://%s:%s", HOST, PORT)
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
