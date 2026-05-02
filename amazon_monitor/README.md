# Pokemon TCG Amazon Monitor

Local Python monitor for Pokemon TCG listings on Amazon.com: Playwright search scraping (no product-detail pages), dual search URLs, SQLite state, title/blacklist/keyword filters, and alerts via a local WhatsApp API.

## What This Project Does

- **Featured URL** — scrapes the full result set (dynamic pagination from Amazon `s-metadata`, capped by `max_search_pages` and `max_cycle_seconds`). Drives catalog updates, price drops, back-in-stock, and optional missing-ASIN reconciliation.
- **Newest arrivals URL** — page 1 only. After the same filters as featured, only ASINs **not already in the database** are processed, with **no** missing-ASIN reconcile (avoids false OOS from a partial page).
- Sends alerts to your local WhatsApp API server.

## Configuration

Edit `config.yaml`:

- `search_urls.featured` — main SERP URL (filters/sort embedded in the link).
- `search_urls.newest_arrivals` — same query sorted by newest (e.g. `s=date-desc-rank`).
- **Do not rely on combining** free-shipping refines (`p_n_is_free_shipping` in `rh`) **with** seller refines (`p_6` / `emi`) in one Amazon search URL—Amazon often does not honor both; stage1 requires the substring **`free delivery`** in each result card’s scraped text (`shipping_text` / `seller_text` / `availability_text`).
- `pagination_mode`: `auto` (derive page count from `totalResultCount` / `asinOnPageCount`) or `fixed` (use `search_pages`).
- `max_search_pages`, `max_cycle_seconds`, `search_pages`, `required_keywords`, `required_any_keywords`, `blacklist_file`, WhatsApp fields, `db_path`, etc.

Legacy `search_url` / `search_urls.main_search` are still accepted as the **featured** URL only; `newest_arrivals` is required.

## Price and stock logic

- SQLite `products`, `alert_decisions` for new / back-in-stock / price-drop, optional `enable_missing_asin_oos` on the **featured** pass only.

## Architecture

- `search_scraper.py` — Playwright search only; metadata pagination.
- `filter_pipeline.py` — stage1 (Pokemon TCG + **free delivery** on card + price), blacklist, optional keyword lists → rows for the state engine.
- `state_engine.py` — SQLite + alerts; `list_known_asins()` for the newest pass.
- `webhook_sender.py` — WhatsApp API posts.
- `main.py` — APScheduler: featured scrape → process → newest scrape → new ASINs only → process.

## Prerequisites

- Windows (typical), Python 3.10+, Google Chrome, `pip install -r requirements.txt`, `playwright install chrome`.

## Run

```powershell
cd amazon_monitor
python main.py
```

## Health

- `logs/monitor.log`, `data/health.json` (search + heartbeat jobs).
- `python tools/healthcheck.py` — exit `0` healthy, `1` stale/errors, `2` missing health file.

## Troubleshooting

- **Captcha** — search job pauses ~120s and resumes; no modem rotation. Check logs and consider running less often or from a stable residential IP.
- **No alerts** — verify WA API settings, `search_urls`, and filter lists (`required_keywords`, blacklist).
