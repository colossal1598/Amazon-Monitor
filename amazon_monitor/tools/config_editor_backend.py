import json
import subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse

import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config.yaml"
HTML_PATH = ROOT / "tools" / "config_editor_he.html"
SCRIPTS_DIR = ROOT / "scripts"


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data


def save_config(updated: dict) -> None:
    with CONFIG_PATH.open("w", encoding="utf-8") as f:
        yaml.safe_dump(updated, f, allow_unicode=True, sort_keys=False)


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
    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
            self._json(200, {"ok": True, "config": cfg})
            return
        self._json(404, {"ok": False, "error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path != "/api/config":
            self._json(404, {"ok": False, "error": "not_found"})
            return

        content_length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(content_length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            self._json(400, {"ok": False, "error": "invalid_json"})
            return

        cfg = load_config()
        basic = payload.get("basic", {})
        templates = payload.get("templates", {})
        advanced = payload.get("advanced", {})
        restart_requested = bool(payload.get("restart_services"))

        if basic.get("wa_api_url") is not None:
            cfg["wa_api_url"] = str(basic.get("wa_api_url", "")).strip()
        if basic.get("wa_api_key") is not None:
            cfg["wa_api_key"] = str(basic.get("wa_api_key", "")).strip()
        if basic.get("wa_group_id") is not None:
            cfg["wa_group_id"] = str(basic.get("wa_group_id", "")).strip()
        if basic.get("wa_client_to") is not None:
            cfg["wa_client_to"] = str(basic.get("wa_client_to", "")).strip()
        if basic.get("affiliate_tag") is not None:
            cfg["affiliate_tag"] = str(basic.get("affiliate_tag", "")).strip()

        if isinstance(templates, dict):
            cleaned = {str(k): str(v) for k, v in templates.items()}
            cfg["wa_message_templates"] = cleaned

        if advanced.get("wa_send_heartbeat") is not None:
            cfg["wa_send_heartbeat"] = bool(advanced.get("wa_send_heartbeat"))
        if advanced.get("wa_send_modem_trigger") is not None:
            cfg["wa_send_modem_trigger"] = bool(advanced.get("wa_send_modem_trigger"))
        if advanced.get("search_poll_minutes") is not None:
            cfg["search_poll_minutes"] = int(advanced.get("search_poll_minutes"))
        if advanced.get("search_pages") is not None:
            cfg["search_pages"] = int(advanced.get("search_pages"))
        if advanced.get("price_drop_percent") is not None:
            cfg["price_drop_percent"] = float(advanced.get("price_drop_percent"))
        if advanced.get("wa_restart_command") is not None:
            cfg["wa_restart_command"] = str(advanced.get("wa_restart_command", "")).strip()

        if not cfg.get("wa_api_url") or not cfg.get("wa_group_id"):
            self._json(400, {"ok": False, "error": "missing_required_fields"})
            return

        save_config(cfg)
        if restart_requested:
            restart_services(cfg)
        self._json(200, {"ok": True, "restarted": restart_requested})


def main() -> None:
    server = HTTPServer(("127.0.0.1", 8765), Handler)
    print("Hebrew config editor running at http://127.0.0.1:8765")
    server.serve_forever()


if __name__ == "__main__":
    main()
