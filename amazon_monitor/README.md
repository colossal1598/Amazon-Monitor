# Amazon PDP + AES Discovery Monitor

Python monitor for Amazon product detail pages (PDP watch list) and Amazon Export Sales LLC SERP discovery. Alerts go to WhatsApp via a local `wa-server`.

## What it does

- **PDP watch** — continuously scrapes ASINs on the `watch` list (SQLite `asins` table). Seller must match `pdp_allowed_seller_substrings` (default: `amazon.com`, `amazon export`). State lives in `products`. Alerts: `new_product`, `back_in_stock`, `price_drop` (threshold: `price_drop_percent`).
- **AES LLC SERP** — scrapes page 1 of `search_urls.aes_llc` on a timer, runs the same filter pipeline, mirrors state in `aes_products` only. Same alert types; AES never writes `products`.
- **Blacklist** — ASINs with role `blacklist` are skipped during AES processing.
- **Immediate alerts** — each PDP or AES scrape diffs state and sends WhatsApp as soon as that scrape finishes. There is no end-of-cycle batch wait.
- **Settings hot-reload** — runtime config and watch list reload from SQLite every ~30 seconds. No restart needed for most setting changes.

Bootstrap [`config.yaml`](config.yaml) holds local paths only (`db_path`, `telemetry_db_path`, `log_dir`, `auth_dir`). Everything else is in SQLite (`settings` + `asins` tables) via the admin UI.

## Architecture

```
main.py  →  MonitorEngine (monitor_engine.py)
              ├── one persistent Playwright browser
              ├── RingScheduler: pick most-overdue watch ASIN → scrape → diff → alert
              ├── stream_concurrent_tabs parallel workers (1–4)
              ├── AES SERP interleaved every aes_check_minutes
              ├── browser recycled every browser_recycle_minutes (or after captcha pause)
              └── telemetry "cycle" = one full sweep over the watch list (admin UI stats)
```

`main.py` is a thin entrypoint: logging, bootstrap config, SQLite migration, heartbeat side-task (every 30 min), then `engine.run_forever()`.

## SQLite tables

| Table | Written by | Purpose |
|-------|------------|---------|
| `products` | PDP watch | Price/stock for watch-list ASINs |
| `aes_products` | AES SERP mirror | Page-1 SERP mirror after filters |
| `alerts` | Both paths | Alert history |
| `settings` / `asins` | Admin UI / migration | Config and watch/blacklist lists |

## Module map

| Module | Role |
|--------|------|
| `main.py` | Entrypoint, logging, heartbeat |
| `monitor_engine.py` | Streaming engine, scheduler, health file, browser session |
| `pdp_scraper.py` | PDP extraction |
| `search_scraper.py` / `filter_pipeline.py` | AES SERP scrape + filters |
| `state_engine.py` | `products`, `aes_products`, alert logic, cooldowns |
| `alert_decisions.py` / `alert_dedupe.py` | Shared rules and per-tick dedupe |
| `settings_store.py` | SQLite config, `DEFAULT_RUNTIME_CONFIG`, hot-reload source |
| `browser_factory.py` | Stealth browser, global rate limiter |
| `webhook_sender.py` | WhatsApp API |
| `client_alerts.py` | Operational DMs (captcha, stall, healthcheck) |
| `telemetry_store.py` | Per-sweep stats in `data/telemetry.db` |
| `tools/admin_ui_server.py` | Local admin UI (port 80) |
| `tools/healthcheck.py` | PM2 cron health probe |

## Key settings

| Setting | Default | Purpose |
|---------|---------|---------|
| `asin_check_interval_seconds` | 60 | Target freshness per watch ASIN |
| `stream_concurrent_tabs` | 2 | Parallel PDP workers (1–4) |
| `max_requests_per_minute` | 25 | Global token bucket (PDP + AES) |
| `aes_check_minutes` | 5 | AES SERP interval |
| `browser_recycle_minutes` | 60 | Planned browser relaunch |
| `captcha_recovery_pause_seconds` | 120 | Pause after captcha/network block |
| `price_drop_percent` | 10 | Price-drop alert threshold |
| `stock_alert_cooldown_minutes` | 60 | Cooldown for unconfirmed OOS→in-stock |
| `stock_alert_confirmed_cooldown_minutes` | 10 | Cooldown after confirmed OOS |
| `stock_alert_same_price_dedupe_minutes` | 720 | Same-price back_in_stock dedupe (rotation/flap churn); strong sellouts exempt |
| `stock_alert_same_price_tolerance_pct` | 3.0 | Relative band for "same price" (rotation price drift); 0 = exact |
| `aes_max_pages` | 2 | Storefront SERP pages scanned per AES cycle |
| `aes_target_prices` | {} | AES-side per-ASIN price gates: listed ASINs alert only at/below target |
| `aes_oos_confirm_cycles` | 3 | AES mirror OOS debounce (consecutive cycles) |
| `cross_source_alert_dedupe_minutes` | 15 | Suppress duplicate alerts across PDP/AES |

WhatsApp credentials: `WA_API_URL` / `WA_API_KEY` in `.env`. Group and templates: `wa_group_id`, `wa_message_templates` in SQLite.

## Scaling

Detection latency for a full watch-list pass is bounded by:

```
max(watch_count × per_scrape_seconds ÷ stream_concurrent_tabs,
    watch_count ÷ max_requests_per_minute × 60)
```

Typical per-scrape time is 6–12 seconds.

**Example:** 25–35 ASINs at ~60 s target freshness — set `stream_concurrent_tabs=3` and `max_requests_per_minute=35`, then confirm `sweep_sec` in `logs/monitor.log` or `data/health.json` stays near or below `asin_check_interval_seconds × watch_count`.

If sweeps run long, raise `max_requests_per_minute` and/or `stream_concurrent_tabs` before lowering `asin_check_interval_seconds`.

## PM2 stack

Four processes in [`ecosystem.config.cjs`](ecosystem.config.cjs):

| Process | Role |
|---------|------|
| `amazon-monitor` | `main.py` streaming engine |
| `admin-ui` | Settings / ASIN management |
| `wa-server` | Local WhatsApp sender (sibling repo) |
| `monitor-healthcheck` | Cron every 10 min; WhatsApp ping on staleness |

Start: `.\start-pm2-stack.bat` or `pm2 start ecosystem.config.cjs`.

## Health and logs

- `logs/monitor.log` — lifecycle lines (sweeps, alerts, captcha pauses)
- `logs/debug.log` — verbose scrape detail
- `data/health.json` — live engine and per-ASIN status
- `data/telemetry.db` — per-sweep timing and bandwidth

Product alerts → `wa_group_id`. Operational alerts (captcha, stall, healthcheck failure) → `wa_client_to` when set.

## Docs

- [QUICKSTART.md](QUICKSTART.md) — fresh install to first alert
- [RUNBOOK.md](RUNBOOK.md) — day-2 operations
- [DEPLOYMENT.md](DEPLOYMENT.md) — remote deploy and updates
