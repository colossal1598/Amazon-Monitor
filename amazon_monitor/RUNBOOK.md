# PDP Monitor Runbook

## Requirements

- Windows 10/11
- Python 3.10+
- Google Chrome
- Local WhatsApp API server
- Stable internet or configured `PROXY_URL`
- PM2 installed and stack registered (`pm2 start ecosystem.config.cjs`)

## Admin UI (local)

**Connection refused?** Nothing is listening on port 8765. Usually one of:

1. `admin-ui` not running in PM2 — run `.\start-pm2-stack.bat` or `pm2 start ecosystem.config.cjs --only admin-ui`
2. Missing `.env` credentials — server **exits immediately** without `ADMIN_UI_USER` and `ADMIN_UI_PASSWORD`

Diagnose:

```powershell
cd amazon_monitor
.\scripts\check_admin_ui.ps1
pm2 logs admin-ui --lines 30
```

Add to `.env` (see `.env.example`), then:

```powershell
pm2 restart admin-ui
pm2 save
.\scripts\open_admin_ui.ps1
```

Open: http://127.0.0.1:8765 — browser prompts for the same user/password as in `.env`.

## Admin UI + Tailscale Funnel (Hebrew/English)

1. **Start admin-ui locally only**  
   הפעילו את `admin-ui` רק על `127.0.0.1:8765` (local only):
   ```powershell
   pm2 start ecosystem.config.cjs --only admin-ui
   pm2 save
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

- `logs/monitor.log` for lifecycle events and warnings.
- `data/telemetry.db` (`cycle_stats`, `debug_events`) for per-cycle timing and debug detail.
- `data/health.json` for last `pdp` and `heartbeat` success times.
- `pm2 list` for PM2 process status.

Query examples:

```sql
-- Last cycles timing
SELECT recorded_at_il, total_sec, pdp_sec, aes_sec, exceeds_poll_interval, alerts_sent
FROM cycle_stats ORDER BY id DESC LIMIT 20;
```

## Bandwidth (cellular / metered link)

The scraper records **Playwright browser** transfer only in `data/telemetry.db`. It does **not** measure OS-wide or cellular modem totals — other apps, updates, and background traffic on the PC are excluded but also mean `daily_bandwidth` can be **lower** than your ISP/cellular dashboard.

| Source | What it measures |
|--------|------------------|
| Admin UI card **שימוש ברשת** / `GET /api/bandwidth/summary` | Last cycle + `daily_bandwidth` rollups from telemetry |
| Windows / phone cellular usage | Whole device — compare to telemetry to spot drift |

**Tables and columns**

- `daily_bandwidth` — per Israel calendar day: `bytes_total`, `bytes_pdp`, `bytes_aes`, `bytes_image`, `cycles`.
- `cycle_stats` (per cycle): `net_bytes_total`, `net_bytes_pdp`, `net_bytes_aes`, `net_bytes_image`, `blocked_url`, `gb_est_total`, plus existing `blocked_heavy`.

**Planning target (500 GB / month plan)**

- Rough budget: **under ~16 GB/day** on average (`500 ÷ 31`) if this PC is the main consumer of the cap.
- If `daily_bandwidth.bytes_total` trends far below cellular usage, assume non-scraper traffic; if it is close or above scraper-only expectations, review image blocking, watch-list size, and concurrent tabs — not poll interval (see existing `pdp_poll_minutes` guidance elsewhere in this runbook).

Example queries:

```sql
SELECT date_il, bytes_total, cycles FROM daily_bandwidth ORDER BY date_il DESC LIMIT 7;

SELECT recorded_at_il, net_bytes_total, net_bytes_pdp, net_bytes_aes, blocked_url, blocked_heavy
FROM cycle_stats ORDER BY id DESC LIMIT 10;
```

## Client operational alerts (WhatsApp DM)

When `wa_client_to` is set, short Hebrew alerts go to the client on captcha, cycle failure, network block, stall, chronic stall, or widespread scrape failures. Rate-limited (default: 30 min cooldown, max 3 per 6 h per kind). Full detail stays in `telemetry.db` and `monitor.log`.

## CAPTCHA Or Network Block

Expected behavior:

- Client DM alert (captcha or network) when `wa_client_to` is configured.
- PDP job pauses for `captcha_recovery_pause_seconds`.
- Scheduler resumes automatically.

If it repeats:

- Increase `pdp_poll_minutes`.
- Lower `pdp_watch_max_concurrent_tabs`.
- Check proxy/IP quality.

## Timeout Or Slow Product Page

PDP scrape is bare-bones: one navigation per ASIN, dismiss “continue shopping” interstitials (up to `pdp_continue_shopping_max_clicks`, default 3), then `pdp_settle_seconds` (default **8**) before a single DOM extract. Default concurrency is **3** tabs (`pdp_watch_max_concurrent_tabs`).

If navigation fails or the page is empty after settle, the row is `_skip_update` and the database is unchanged. If price is missing but add-to-cart is present, stock stays **unknown** (no false OOS). Tune `pdp_settle_seconds` in settings when telemetry shows many `pdp_watch_unknown_stock` events; check `logs/debug.log` for `continue shopping` click counts per ASIN.

## Seller And Delivery Rules

An ASIN is considered in stock only when:

- A product price is parsed.
- Seller/shipper text contains an allowed substring such as `amazon.com` or `amazon export`.
- The delivery text does not say the item cannot ship to the selected location.

Paid delivery is allowed and appears in the WhatsApp `{shipping}` line when Amazon exposes it.
