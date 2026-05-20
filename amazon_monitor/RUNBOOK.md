# PDP Monitor Runbook

## Requirements

- Windows 10/11
- Python 3.10+
- Google Chrome
- Local WhatsApp API server
- Stable internet or configured `PROXY_URL`
- PM2 installed and stack registered (`pm2 start ecosystem.config.cjs`)

## Admin UI + Tailscale Funnel (Hebrew/English)

1. **Start admin-ui locally only**  
   הפעילו את `admin-ui` רק על `127.0.0.1:8765` (local only):
   ```powershell
   pm2 start admin-ui
   ```
2. **Expose through Funnel path**  
   פרסמו דרך Funnel לנתיב `/`:
   ```powershell
   tailscale funnel --bg --set-path / http://127.0.0.1:8765
   ```
3. **Basic Auth stays mandatory**  
   ודאו שקיימים `.env` values: `ADMIN_UI_USER`, `ADMIN_UI_PASSWORD`. כל הראוטים מוגנים.
4. **Never expose sqlite-web**  
   לעולם לא לעשות Funnel לפורט `8768`. sqlite-web נשאר מקומי בלבד.
5. **Disable Funnel when done**  
   בסיום עבודה בטלו חשיפה:
   ```powershell
   tailscale funnel reset
   ```
6. **Client workflow**  
   הלקוח נכנס ל־Funnel URL, מזדהה ב־Basic Auth, מעדכן הגדרות/רשימות, סוגר Funnel בסיום.

## Start

```powershell
pm2 start amazon-monitor
```

The scheduler runs one PDP cycle immediately, then every `pdp_poll_minutes`.

## Stop

```powershell
pm2 stop amazon-monitor
```

For the whole stack:

```powershell
pm2 stop all
```

## Restart

```powershell
pm2 restart amazon-monitor
```

For the whole stack:

```powershell
pm2 restart all
```

## Daily Check

```powershell
python tools/healthcheck.py
```

Also check:

- `logs/monitor.log` for lifecycle events.
- `logs/monitor.debug.log` for per-cycle counts and skipped PDP rows.
- `data/health.json` for last `pdp` and `heartbeat` success times.
- `pm2 list` for PM2 process status.

## CAPTCHA Or Network Block

Expected behavior:

- Operational WhatsApp error is sent with `pdp_error`.
- PDP job pauses for `captcha_recovery_pause_seconds`.
- Scheduler resumes automatically.

If it repeats:

- Increase `pdp_poll_minutes`.
- Lower `pdp_watch_max_concurrent_tabs`.
- Check proxy/IP quality.

## Timeout Or Slow Product Page

Each ASIN gets `pdp_watch_max_attempts` navigation attempts. If all attempts fail, the row is emitted as `_skip_update`, and the database is left unchanged for that ASIN.

## Seller And Delivery Rules

An ASIN is considered in stock only when:

- A product price is parsed.
- Seller/shipper text contains an allowed substring such as `amazon.com` or `amazon export`.
- The delivery text does not say the item cannot ship to the selected location.

Paid delivery is allowed and appears in the WhatsApp `{shipping}` line when Amazon exposes it.
