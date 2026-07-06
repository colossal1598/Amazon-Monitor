# Deployment

PM2-managed stack on a Windows client machine. Do not start or kill monitor Python processes outside PM2.

## Stack

| Process | Role |
|---------|------|
| `amazon-monitor` | Streaming engine (`main.py`) |
| `admin-ui` | Settings UI on port 80 |
| `wa-server` | WhatsApp sender (external repo) |
| `monitor-healthcheck` | Health probe, cron every 10 min |

Defined in [`ecosystem.config.cjs`](ecosystem.config.cjs). There is no `fast-watch` process.

## One-time setup

From `amazon_monitor`:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium
copy .env.example .env
```

Edit `.env` (WhatsApp API key, admin UI password). Configure watch list and settings via admin UI after first start.

```powershell
npm install -g pm2
.\start-pm2-stack.bat
pm2 save
```

Administrator PowerShell, once:

```powershell
pm2 startup
```

Run the command PM2 prints, then:

```powershell
pm2 save
```

If `wa-server` is not at `../../wa-server` relative to `amazon_monitor`:

```powershell
set WA_SERVER_ROOT=C:\path\to\wa-server
.\start-pm2-stack.bat
```

## Update flow

Run from `amazon_monitor`:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\update_and_restart.ps1
```

Steps performed:

1. `pm2 stop amazon-monitor`
2. `git pull`
3. `.venv\Scripts\python.exe -m pip install -r requirements.txt`
4. Restart full PM2 stack (`amazon-monitor`, `admin-ui`, `wa-server`, `monitor-healthcheck`)
5. `pm2 save`

After code changes that only touch SQLite-backed settings, a monitor restart is not required — the engine hot-reloads every ~30 s. Restart when changing `.env`, Python dependencies, or Playwright/browser behavior.

### What survives git pull

`data/` is gitignored. These persist across pulls:

- `data/monitor.db` — settings, ASIN lists, product state
- `data/telemetry.db` — sweep history
- `data/health.json` — overwritten at runtime
- `data/product_images/`, `data/fx_usd_ils.json`

Local-only (never commit):

- `.env`
- `auth/` (browser session if used)
- `logs/`

## Optional scheduled updates

Daily auto-pull at 02:00:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install_update_task.ps1 -DailyAt "02:00"
```

Creates Windows scheduled task `AmazonMonitorUpdate` running `update_and_restart.ps1`.

## Daily operations

```powershell
pm2 list
pm2 logs amazon-monitor --lines 30
python tools/healthcheck.py
```

| Action | Command |
|--------|---------|
| Stop monitor | `pm2 stop amazon-monitor` |
| Start monitor | `pm2 start amazon-monitor` |
| Restart monitor | `pm2 restart amazon-monitor` |
| Restart full stack | `pm2 restart all` |
| Open admin UI | `.\scripts\open_admin_ui.ps1` |

Monitor process also has `cron_restart: "0 5 * * *"` (daily 05:00) for long-running drift insurance. Browser recycle is separate (`browser_recycle_minutes` in settings).

## Migrate from legacy scripts

If the machine used old scheduled updaters or orphan Python processes:

```powershell
Disable-ScheduledTask -TaskName AmazonMonitorUpdate -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName AmazonMonitorUpdate -Confirm:$false -ErrorAction SilentlyContinue
pm2 stop amazon-monitor
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*main.py*amazon_monitor*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
.\start-pm2-stack.bat
pm2 save
```

Legacy full `config.yaml` can be imported once when the settings table is empty:

```powershell
python tools/migrate_yaml_to_db.py
```

## Remote admin UI (Tailscale Funnel)

1. `pm2 start admin-ui` — binds `127.0.0.1:80`.
2. `tailscale funnel --bg --set-path / http://127.0.0.1:80`
3. `.env` must have `ADMIN_UI_USER` and `ADMIN_UI_PASSWORD`.
4. Do not expose sqlite-web (port 8768).
5. `tailscale funnel reset` when finished.

## Security

- Keep `.env` only on the client machine.
- Do not commit secrets to git.
- WhatsApp API key in `.env` matches `wa-server`; admin UI credentials are separate.
