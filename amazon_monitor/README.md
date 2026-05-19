# Amazon PDP Monitor

Python monitor for watched Amazon product-detail pages. It visits configured ASINs on `amazon.com`, accepts offers sold by Amazon.com or Amazon Export LLC, stores state in SQLite, and sends WhatsApp alerts for new, back-in-stock, and price-drop events.

## What It Does

- Watches only `pdp_watch_asins` from `config.yaml`.
- Extracts title, buy-box price, seller/shipper text, product image, and delivery details.
- Accepts paid delivery as long as the seller matches the allowlist and Amazon does not say the item cannot ship to the selected location.
- Skips DB updates on per-page failures/timeouts so one bad PDP does not falsely mark a product out of stock.
- Pauses and resumes the PDP job when Amazon shows CAPTCHA or a global network block.

## Key Files

- `main.py` — APScheduler runtime: PDP scrape, state update, alert send, heartbeat.
- `pdp_scraper.py` — Playwright PDP extraction, seller matching, retry/CAPTCHA handling.
- `pdp_helpers.py` — ASIN validation, title cleanup, WhatsApp delivery-line formatting.
- `state_engine.py` — SQLite products/alerts and PDP alert decisions.
- `webhook_sender.py` — WhatsApp API payloads.
- `browser_factory.py` — user agents, rate limiter, Amazon locale/currency cookie helpers.

## Configuration

Edit `config.yaml`:

- `pdp_watch_asins` — ASINs to visit every cycle.
- `pdp_allowed_seller_substrings` — default: `amazon.com`, `amazon export`.
- `pdp_poll_minutes`, `max_cycle_seconds`, `max_requests_per_minute`.
- `pdp_watch_max_concurrent_tabs`, `pdp_watch_max_attempts`, jitter/delay ranges.
- WhatsApp fields: `wa_api_url`, `wa_api_key`, `wa_group_id`, optional `wa_client_to`.
- `affiliate_tag`, `db_path`, `log_dir`, optional FX settings.

Delivery text in alerts is produced by `pdp_helpers.shipping_display_hebrew`:

- Free delivery: `משלוח חינם`
- Paid delivery: `משלוח: <delivery price or line>`

## Run

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chrome
python main.py
```

Optional proxy:

```powershell
$env:PROXY_URL = "http://user:pass@host:port"
python main.py
```

## Health

- Logs: `logs/monitor.log`, `logs/monitor.debug.log`
- Health snapshot: `data/health.json`
- Check command: `python tools/healthcheck.py`

## Troubleshooting

- CAPTCHA: the PDP job pauses for `captcha_recovery_pause_seconds`, sends an operational WhatsApp error, then resumes.
- Timeouts: each PDP navigation gets bounded retries. If all attempts fail, the existing DB row is left unchanged.
- No alerts: confirm `pdp_watch_asins`, seller allowlist, WhatsApp settings, and `logs/monitor.debug.log`.
