# Deployment (PM2 Only)

This deployment keeps PM2 as the only process manager for:

- `amazon-monitor`
- `admin-ui`
- `wa-server`
- `monitor-healthcheck`

No script should start or kill Python directly anymore.

## One-time setup on the Windows client machine

Run these commands from `amazon_monitor`:

```powershell
npm install -g pm2
.\start-pm2-stack.bat
pm2 save
```

Then open an **Administrator PowerShell** and run:

```powershell
pm2 startup
```

Run the command that `pm2 startup` prints, then run:

```powershell
pm2 save
```

## One-time migration from old scripts

If this machine previously used the old scheduled updater or direct Python process control, run:

```powershell
Disable-ScheduledTask -TaskName AmazonMonitorUpdate -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName AmazonMonitorUpdate -Confirm:$false -ErrorAction SilentlyContinue
pm2 stop amazon-monitor
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*main.py*amazon_monitor*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
pm2 start amazon-monitor
pm2 save
```

This removes orphan monitor processes and leaves PM2 as the single owner.

## Manual update command

Run this inside `amazon_monitor` on the client machine:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\update_and_restart.ps1
```

That command now does:

1. `pm2 stop amazon-monitor`
2. `git pull`
3. `pip install -r requirements.txt`
4. `pm2 start amazon-monitor`
5. `pm2 save`

## Optional auto-update every day

Run once on the client machine:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install_update_task.ps1 -DailyAt "02:00"
```

This creates `AmazonMonitorUpdate`, which runs `update_and_restart.ps1` (PM2-only flow).

## Daily operations

- Stop monitor: `pm2 stop amazon-monitor`
- Start monitor: `pm2 start amazon-monitor`
- Restart monitor: `pm2 restart amazon-monitor`
- Start admin UI: `pm2 start admin-ui`
- Stop admin UI: `pm2 stop admin-ui`
- Stop entire stack: `pm2 stop all`
- Check status: `pm2 list`

## Tailscale Funnel for Admin UI (EN + HE)

1. **Run admin-ui locally** / הפעילו מקומית בלבד:
   ```powershell
   pm2 start admin-ui
   ```
   The service listens on `127.0.0.1:8765` only.
2. **Create Funnel path** / חשפו דרך Funnel:
   ```powershell
   tailscale funnel --bg --set-path / http://127.0.0.1:8765
   ```
3. **Use in-app Basic Auth** / התחברות עם Basic Auth מתוך האפליקציה  
   `.env` must include `ADMIN_UI_USER` and `ADMIN_UI_PASSWORD`.
4. **Do not expose sqlite-web** / אין לחשוף את `8768`  
   Never run funnel to `127.0.0.1:8768`.
5. **Disable Funnel after work** / בסיום מבטלים:
   ```powershell
   tailscale funnel reset
   ```
6. **Client workflow** / תהליך לקוח  
   Open Funnel URL -> authenticate -> edit settings/ASIN lists -> close Funnel.

## Important

- Keep `.env` only on the client machine.
- Do not commit client secrets to GitHub.

