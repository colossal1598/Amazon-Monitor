# Quickstart

Fresh install on Windows through first WhatsApp alert and PM2 stack.

## 1. Install

Open PowerShell in `amazon_monitor`:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium
```

If activation is blocked:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

## 2. Bootstrap config

Copy secrets and edit paths if needed:

```powershell
copy .env.example .env
```

Edit [`.env`](.env.example):

- `WA_API_URL` — default `http://localhost:3001/send`
- `WA_API_KEY` — must match `wa-server` `API_KEY`
- `ADMIN_UI_USER` / `ADMIN_UI_PASSWORD` — admin UI login

[`config.yaml`](config.yaml) is bootstrap-only (DB, log, auth paths). Runtime settings live in SQLite after first run.

## 3. WhatsApp server

Start `wa-server` (sibling of the repo, or set `WA_SERVER_ROOT` before PM2). Confirm it listens on the URL in `.env`.

## 4. Initialize SQLite and settings

Start the admin UI (or run the monitor once to create the DB):

```powershell
pm2 start ecosystem.config.cjs --only admin-ui
```

Open http://127.0.0.1 (Basic Auth from `.env`). Set at minimum:

- `wa_group_id` — WhatsApp group for product alerts
- `affiliate_tag`
- Watch ASINs (`watch` role)
- `search_urls.aes_llc` if different from default

Alternatively, run `python main.py` once and press Ctrl+C after `Monitor started (streaming engine).` — this runs `migrate_yaml_to_db` and seeds defaults.

## 5. Test WhatsApp

```powershell
python first_time_setup.py
```

Confirm the setup message arrives. If nothing sends: check `wa-server`, `.env` credentials, and `wa_group_id` in admin UI. The setup script reads `config.yaml`; the running monitor merges `.env` via `load_runtime_config`. Set `wa_api_url` in admin UI to match `.env` if the test cannot reach the API.

## 6. Run locally

```powershell
python main.py
```

Expected lifecycle log:

```
Monitor started (streaming engine).
Streaming engine starting.
```

PDP checks begin immediately; AES runs after `aes_check_minutes`. Alerts log as `ALERT <type> <ASIN> ... (dispatched immediately)`.

## 7. Check health

In another terminal:

```powershell
python tools/healthcheck.py
```

Expected: `PASS: all monitored jobs are healthy` (after at least one sweep completes).

Inspect `data/health.json` for `engine.sweep_seconds` and per-ASIN `last_checked`.

## 8. PM2 production stack

```powershell
npm install -g pm2
.\start-pm2-stack.bat
pm2 save
```

Administrator shell, once:

```powershell
pm2 startup
```

Run the command PM2 prints, then `pm2 save`.

Stack: `amazon-monitor`, `admin-ui`, `wa-server`, `monitor-healthcheck`.

## 9. Stop

- Local run: Ctrl+C in the `main.py` window
- PM2: `pm2 stop amazon-monitor` or `pm2 stop all`
