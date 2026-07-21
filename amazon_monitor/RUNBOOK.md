# Runbook

Day-2 operations for the streaming monitor engine.

## Requirements

- Windows 10/11
- Python 3.10+
- Playwright Chromium (`playwright install chromium`)
- Local `wa-server` on the machine
- PM2 stack registered (`.\start-pm2-stack.bat` or `pm2 start ecosystem.config.cjs`)

## PM2 processes

| Name | Script | Notes |
|------|--------|-------|
| `amazon-monitor` | `main.py` | Streaming engine; daily cron restart 05:00 |
| `admin-ui` | `tools/admin_ui_server.py` | http://127.0.0.1:80 |
| `wa-server` | sibling `wa-server/server.js` | Set `WA_SERVER_ROOT` if path differs |
| `monitor-healthcheck` | `tools/healthcheck.py` | Cron every 10 min, no autorestart |

```powershell
pm2 list
pm2 logs amazon-monitor --lines 50
pm2 restart amazon-monitor
pm2 stop all
```

## Reading logs

**`logs/monitor.log`** — lifecycle channel only (warnings always included).

Key lines:

```
Monitor started (streaming engine).
Streaming engine starting.
2026-07-06 14:32:01 Sweep done. checked=28 ok=26 skip=2 alerts=0 sweep_sec=312.4 watch=28
2026-07-06 14:32:15 ALERT back_in_stock B0XXXXXX price=49.99 (dispatched immediately)
Recycling browser after 60 min.
Captcha: ...: pausing stream 120s, browser will be recycled.
```

| Field | Meaning |
|-------|---------|
| `checked` | PDP scrapes this sweep |
| `ok` | Successful state updates |
| `skip` | Captcha, parse miss, seller mismatch, etc. |
| `sweep_sec` | Wall time for one full pass over the watch list |
| `watch` | Watch-list size |

Compare `sweep_sec` to `asin_check_interval_seconds × watch_count`. If sweeps consistently exceed that product, raise `stream_concurrent_tabs` and/or `max_requests_per_minute`.

**`logs/debug.log`** — per-ASIN scrape detail, AES errors, stack traces.

## health.json

Path: `data/health.json` (updated every ~5 s while running).

```json
{
  "updated_at": "2026-07-06T11:32:01+00:00",
  "jobs": {
    "stream": { "last_success_at": "...", "last_error_at": null, "last_error_message": null },
    "aes": { "last_success_at": "...", ... },
    "heartbeat": { "last_success_at": "...", ... }
  },
  "engine": {
    "status": "running",
    "watch_count": 28,
    "target_interval_seconds": 60,
    "sweep_seconds": 312.4
  },
  "asins": {
    "B0XXXXXX": { "last_checked": "...", "result": "ok", "in_stock": true, "price": 49.99 }
  }
}
```

`engine.status` values: `running`, `starting`, `idle_no_watch`, `captcha_pause`.

## Healthcheck

```powershell
python tools/healthcheck.py
```

PM2 runs this every 10 minutes as `monitor-healthcheck`. On failure it prints `FAIL` lines and sends a WhatsApp operational alert via `client_alerts` (rate-limited, needs `wa_client_to` or falls back to `wa_group_id`).

Staleness thresholds:

| Job | Max age |
|-----|---------|
| `stream` | 10 min since last success |
| `heartbeat` | 40 min |
| Per-ASIN | `max(300 s, target_interval_seconds × 5)` while `engine.status == running` |

Captcha pause sets `stream` error message; healthcheck reports it without double-counting staleness if success is recent.

## Tuning for more ASINs

Latency estimate:

```
max(watch_count × per_scrape_seconds ÷ stream_concurrent_tabs,
    watch_count ÷ max_requests_per_minute × 60)
```

Typical `per_scrape_seconds`: 6–12.

**25–35 ASINs, ~60 s freshness:** `stream_concurrent_tabs=3`, `max_requests_per_minute=35`. Verify `sweep_sec` in logs and `engine.sweep_seconds` in health.json.

Settings reload every ~30 s from SQLite — no restart for these changes.

Other knobs:

| Setting | When to change |
|---------|----------------|
| `asin_check_interval_seconds` | Lower only after sweep time fits budget |
| `browser_recycle_minutes` | Lower if memory grows; default 60 |
| `aes_check_minutes` | AES frequency; shares browser and rate limit |
| `captcha_recovery_pause_seconds` | Longer pause after repeated captchas |

## Alert cooldowns

Configured in SQLite (`state_engine.py`):

| Setting | Default | Effect |
|---------|---------|--------|
| `stock_alert_cooldown_minutes` | 60 | Gap between identical stock alerts when previous OOS was not confirmed |
| `stock_alert_confirmed_cooldown_minutes` | 10 | Shorter gap after confirmed OOS (explicit "unavailable" evidence) |
| `aes_oos_confirm_cycles` | 3 | AES mirror must see OOS this many consecutive cycles before flip |
| `cross_source_alert_dedupe_minutes` | 15 | Suppress duplicate alert for same ASIN from PDP vs AES |
| `stock_alert_same_price_dedupe_minutes` | 720 | Suppress a back_in_stock at the SAME price as the last one within this window, unless the preceding OOS was a strong page-text sellout. Kills seller-rotation / SERP-flap re-alert churn; price changes always alert. 0 disables. |
| `stock_alert_same_price_tolerance_pct` | 3.0 | "Same price" band (% of prior alert price) — rotation drifts prices by cents-to-dollars between re-fires. 0 = exact match. |
| `aes_max_pages` | 2 | Storefront SERP pages per AES cycle (page 1 alone covered 16 of 57 items). Each page adds one navigation per cycle. |

## Captcha and network block

Expected behavior:

1. Workers stop; browser closes.
2. Operational DM if `wa_client_to` set (`client_alerts`).
3. `engine.status` → `captcha_pause`; `stream` job records error.
4. Sleep `captcha_recovery_pause_seconds` (default 120).
5. Fresh browser session; streaming resumes.

If repeats: increase `asin_check_interval_seconds`, lower `stream_concurrent_tabs`, check IP/proxy quality.

## Admin UI

Local: http://127.0.0.1 (port 80). Requires `ADMIN_UI_USER` / `ADMIN_UI_PASSWORD` in `.env`.

```powershell
.\scripts\open_admin_ui.ps1
.\scripts\check_admin_ui.ps1
pm2 logs admin-ui --lines 30
```

**Connection refused:** `admin-ui` not in PM2, or missing `.env` credentials (process exits immediately).

### Tailscale Funnel (remote settings edit)

1. `pm2 start admin-ui` — listens on `127.0.0.1:80` only.
2. `tailscale funnel --bg --set-path / http://127.0.0.1:80`
3. Basic Auth still required (`.env` credentials).
4. Never funnel sqlite-web (port 8768).
5. When done: `tailscale funnel reset`

## Client operational alerts

When `wa_client_to` is set, Hebrew DMs for captcha, network block, cycle failure, stall, chronic stall, scrape degradation, healthcheck failure. Rate-limited (`client_alert_cooldown_minutes`, `client_alert_max_per_window`). Product alerts stay on `wa_group_id`.

## Telemetry

`data/telemetry.db` — per-sweep stats (admin UI bandwidth cards, cycle history).

```sql
SELECT recorded_at_il, total_sec, pdp_sec, aes_sec, exceeds_poll_interval, alerts_sent
FROM cycle_stats ORDER BY id DESC LIMIT 20;
```

Bandwidth is Playwright browser transfer only, not OS-wide cellular totals.

## Common failures

| Symptom | Likely cause | Action |
|---------|--------------|--------|
| `No pdp_watch_asins configured; engine idle` | Empty watch list | Add ASINs in admin UI |
| `health file not found` | Monitor not running | `pm2 start amazon-monitor` |
| `stream: stale success` | Captcha pause, crash, or sweep too slow | Check logs; tune tabs/rate limit |
| `asins stale` | Engine stuck or watch list grew | Check `sweep_sec`; raise limits |
| WhatsApp product alerts silent | `wa_group_id`, wa-server, `.env` key | Test with `first_time_setup.py` |
| AES errors every cycle | Bad `search_urls.aes_llc` | Fix URL in admin UI settings |
| `pm2 start` wa-server fails | Wrong path | `set WA_SERVER_ROOT=C:\path\to\wa-server` |

## Changed from old versions

The batch-cycle scheduler is gone. Removed settings and concepts:

- `pdp_poll_minutes`, `max_cycle_seconds` — replaced by `asin_check_interval_seconds` and continuous streaming
- `fast_watch_*` and the HTTP fast-lane process — deleted entirely; one Playwright browser handles all PDP checks
- Health job name `pdp` — now `stream`
- Alerts no longer wait for end of cycle; each ASIN dispatches immediately after its scrape
