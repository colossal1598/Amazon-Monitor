# Pokemon TCG Amazon Monitor

Local Python monitor for Pokemon TCG listings on Amazon.com: Playwright search scraping (no product-detail pages), dual search URLs, merchant-ID allowlisting, SQLite state, and alerts via a local WhatsApp API.

## What This Project Does

- **Featured URL** — scrapes the full result set (dynamic pagination from Amazon `s-metadata`, capped by `max_search_pages` and `max_cycle_seconds`). Drives catalog updates, price drops, back-in-stock, and optional missing-ASIN reconciliation.
- **Newest arrivals URL** — page 1 only. After the same filters + `allowed_merchant_ids`, only ASINs **not already in the database** are processed, with **no** missing-ASIN reconcile (avoids false OOS from a partial page).
- Sends alerts to your local WhatsApp API server.

## Configuration

Edit `config.yaml`:

- `search_urls.featured` — main SERP URL (filters/sort embedded in the link).
- `search_urls.newest_arrivals` — same query sorted by newest (e.g. `s=date-desc-rank`).
- `allowed_merchant_ids` — tokens such as `ATVPDKIKX0DER` (marketplace) and `A2XZ7JICGUQ1CX` (seller facet); matched from card HTML, URL `rh` `p_6:` facets, and page `marketplaceId` when present.
- `pagination_mode`: `auto` (derive page count from `totalResultCount` / `asinOnPageCount`) or `fixed` (use `search_pages`).
- `max_search_pages`, `max_cycle_seconds`, `search_pages`, `required_keywords`, `blacklist_file`, WhatsApp fields, `db_path`, etc.

Legacy `search_url` / `search_urls.main_search` are still accepted as the **featured** URL only; `newest_arrivals` is required.

## Price and stock logic

- Same as before: SQLite `products`, `alert_decisions` for new / back-in-stock / price-drop, optional `enable_missing_asin_oos` on the **featured** pass only.

## Architecture

- `search_scraper.py` — Playwright search only; metadata pagination; merchant token extraction per card.
- `filter_pipeline.py` — stage1 (Pokemon TCG + Israel free shipping + price), blacklist/keywords, `allowed_merchant_ids`.
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

Bootstrap (seed DB without alerts):

```powershell
python main.py --bootstrap
```

One-shot scrape + JSON dumps under `data/test_scrape/`:

```powershell
python test_scrape.py
```

## Health

- `logs/monitor.log`, `data/health.json` (search + heartbeat jobs).
- `python tools/healthcheck.py` — exit `0` healthy, `1` stale/errors, `2` missing health file.

## Troubleshooting

- **Captcha** — search job pauses ~120s and resumes; no modem rotation. Check logs and consider running less often or from a stable residential IP.
- **No alerts** — confirm `allowed_merchant_ids` and URL facets; verify WA API settings.
