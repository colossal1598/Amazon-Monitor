# Pokemon TCG Amazon Monitor

Local Python monitor for Pokemon TCG listings on Amazon, with anti-detection scraping, modem IP rotation, and direct alert delivery to a local WhatsApp API server.

## What This Project Does

- Monitors **one** Amazon search URL (export-seller flow) for new/changed Pokemon TCG items.
- Detects:
  - New products
  - Back in stock
  - Price drops
- Sends alerts directly to your local WhatsApp API server.

## Search URL

Configure either `search_urls.amazon_export` or a single top-level `search_url` in `config.yaml`. The monitor resolves that URL in `main.resolve_export_search_url`.

Example export-seller search:

- `https://www.amazon.com/s?k=pokemon+tcg&me=A2XZ7JICGUQ1CX`

## Price Tracking Logic

- Every ASIN found in search results is saved in SQLite.
- On each cycle, new prices are compared against previous prices.
- Price-drop alerts trigger only when drop percentage passes `price_drop_percent`.
- Cooldown logic prevents duplicate price-drop alerts too frequently.
- When an alert rule does not fire, the engine may log `alert_skip` lines (with a stable `reason=` token) so you can see why there was no WhatsApp message.

## Stock Tracking Logic

- Stock is based on ASIN presence in a successful filtered run.
- If a known ASIN is present in the filtered results, it is treated as `in_stock=1`.
- If `enable_missing_asin_oos` is true and the run has at least `min_results_for_absence_reconcile` filtered results (default: 1), any previously tracked export ASIN missing from that run is marked `in_stock=0`.
- When a missing-marked ASIN appears again later, the `0 -> 1` transition emits one `back_in_stock` alert.
- If a run has zero filtered results, the monitor skips missing-ASIN reconciliation for that cycle to avoid mass out-of-stock flips from empty scrape results.

## Architecture

- `search_scraper.py`: ephemeral search scraping context
- `filter_pipeline.py`: keyword + blacklist filtering
- `alert_decisions.py`: pure rules for new / stock / price-drop alerts (with skip reasons)
- `state_engine.py`: SQLite state, applies `alert_decisions`, records alerts
- `modem_rotator.py`: modem reconnect + public IP verification
- `webhook_sender.py`: alert/heartbeat posting to local WhatsApp API server
- `main.py`: APScheduler orchestration + error handling + runtime health file
- `first_time_setup.py`: WhatsApp API smoke-test utility

## Prerequisites

- Windows machine (always-on recommended)
- Python 3.10+
- Google Chrome installed
- WhatsApp API server running locally (for example `http://localhost:3001/send`)

## Installation

From `amazon_monitor/`:

1. Create virtual environment:
   - `python -m venv .venv`
2. Activate it (PowerShell):
   - `.\\.venv\\Scripts\\Activate.ps1`
3. Install dependencies:
   - `pip install -r requirements.txt`
4. Install Playwright browser channel support:
   - `playwright install chrome`

## Configuration

1. Edit `.env`:
   - optional Telegram fallback values
   - optional proxy URL
2. Edit `config.yaml`:
   - `affiliate_tag`
   - WhatsApp API settings (`wa_api_url`, `wa_api_key`, `wa_group_id`, optional `wa_client_to`)
   - message templates (`wa_message_templates`)
   - stock reconciliation (`enable_missing_asin_oos`, `min_results_for_absence_reconcile`)
   - modem reconnect command

## First-Time Setup

Run:

- `python first_time_setup.py`

It will send a dummy WhatsApp alert as a smoke test.

## Hebrew Config Editor (Client Friendly)

Run:

- `python tools/config_editor_backend.py`

Then open:

- `http://127.0.0.1:8765`

Page sections:

- Message templates
- Basic settings
- Advanced settings (collapsed)

The page writes changes directly to `config.yaml`.

## Run the Monitor

- `python main.py`

You should see `Monitor started` and logs in:

- `logs/monitor.log`

## Monitoring Runtime Health

Basic monitoring is available through:

- `logs/monitor.log`: detailed runtime logs
- `data/health.json`: machine-readable scheduler health state
- `data/last_ip.txt`: latest modem/public IP

Check health quickly:

- `python tools/healthcheck.py`

Exit codes:

- `0` = healthy
- `1` = stale/error state
- `2` = missing health file

## Alert Payload Fields

Alert objects include:

- `type`, `asin`, `title`, `price`, `old_price`, `new_price`, `pct_drop`, `source`, `image_url`, `timestamp`, `affiliate_link`

Affiliate link format:

- `https://www.amazon.com/dp/{ASIN}?tag={affiliate_tag}`

## Troubleshooting

- Captcha/Robot Check:
  - monitor pauses jobs, triggers modem step, then resumes.
- Session expired:
  - not applicable in search-only anonymous mode.
- No alerts:
  - verify WhatsApp API URL/key/group in `config.yaml` and confirm WA server is running.
- Healthcheck failing:
  - inspect `data/health.json` and `logs/monitor.log`.
