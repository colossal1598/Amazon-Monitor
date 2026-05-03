import json
import subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
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


def config_for_client(cfg: dict) -> dict:
    """Copy safe for browser — omits secrets."""
    out = dict(cfg)
    out.pop("wa_api_key", None)
    return out


def _coerce_float_pair(raw: Any, default: tuple[float, float]) -> tuple[float, float]:
    if isinstance(raw, (list, tuple)) and len(raw) >= 2:
        return float(raw[0]), float(raw[1])
    return default


def _normalize_string_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(x).strip() for x in raw if str(x).strip()]


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
        if keywords.get("required_any") is not None:
            cfg["required_any_keywords"] = _normalize_string_list(keywords.get("required_any"))

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
        if paths.get("blacklist_file") is not None:
            cfg["blacklist_file"] = str(paths.get("blacklist_file", "")).strip()
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
        merged.update({str(k): str(v) for k, v in templates.items()})
        cfg["wa_message_templates"] = merged

    advanced = payload.get("advanced")
    if isinstance(advanced, dict) and advanced.get("wa_restart_command") is not None:
        cfg["wa_restart_command"] = str(advanced.get("wa_restart_command", "")).strip()


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
            self._json(200, {"ok": True, "config": config_for_client(cfg)})
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
        apply_config_payload(cfg, payload)
        restart_requested = bool(payload.get("restart_services"))

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
