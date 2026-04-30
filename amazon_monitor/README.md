# Pokemon TCG Amazon Monitor

Local Python monitor for Pokemon TCG listings on Amazon, with anti-detection scraping, shipping checks, modem IP rotation, and alert delivery through local n8n webhooks.

## What This Project Does

- Monitors two Amazon search URLs for new/changed Pokemon TCG items.
- Detects:
  - New products
  - Back in stock
  - Price drops
  - Shipping eligibility changes (free shipping to Israel)
- Sends alert payloads to n8n only (no direct WhatsApp messaging in Python).

## Exact Search URLs in Use

- Export seller:
  - `https://www.amazon.com/s?k=pokemon+tcg&me=A2XZ7JICGUQ1CX`
- Amazon.com free-shipping flow:
  - `https://www.amazon.com/s?k=pokemon+tcg&rh=p_n_is_free_shipping%3A10236242011&s=date-desc-rank&dc&ds=v1%3ATYicYkMoU8%2B3IUjmEcjbElDvodVd9crqoNxWbnoh1o8`

These are configured in `config.yaml`.

## Architecture

- `search_scraper.py`: ephemeral search scraping context
- `shipping_checker.py`: batch shipping checks using persistent auth
- `filter_pipeline.py`: keyword + blacklist filtering
- `state_engine.py`: SQLite state, cooldown rules, alert generation
- `modem_rotator.py`: modem reconnect + public IP verification
- `webhook_sender.py`: alert/heartbeat webhook posting to n8n
- `main.py`: APScheduler orchestration + error handling + runtime health file
- `first_time_setup.py`: interactive Amazon session bootstrap + webhook test

## Prerequisites

- Windows machine (always-on recommended)
- Python 3.10+
- Google Chrome installed
- n8n running locally with working webhook workflow
- Dedicated Amazon account for bot login/session

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
   - `AMAZON_EMAIL`
   - `AMAZON_PASSWORD`
   - optional Telegram fallback values
2. Edit `config.yaml`:
   - `affiliate_tag`
   - webhook URLs (`webhook_alert`, `webhook_heartbeat`)
   - modem reconnect command

## First-Time Setup

Run:

- `python first_time_setup.py`

It will:

- Open persistent Amazon context in `auth/amazon`
- Let you manually complete login/2FA
- Verify Amazon account session access
- Send a dummy alert to n8n webhook

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

## n8n Payload Expectations

Alerts include:

- `type`, `asin`, `title`, `price`, `old_price`, `new_price`, `pct_drop`, `source`, `image_url`, `timestamp`, `affiliate_link`

Affiliate link format:

- `https://www.amazon.com/dp/{ASIN}?tag={affiliate_tag}`

## Troubleshooting

- Captcha/Robot Check:
  - monitor pauses jobs, triggers modem step, then resumes.
- Session expired:
  - shipping checks may pause; rerun `first_time_setup.py`.
- No alerts:
  - verify n8n webhook URL and workflow activation.
- Healthcheck failing:
  - inspect `data/health.json` and `logs/monitor.log`.
