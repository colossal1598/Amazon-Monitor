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
- **Do not rely on combining** free-shipping refines (`p_n_is_free_shipping` in `rh`) **with** seller refines (`p_6` / `emi`) in one Amazon search URL—Amazon often does not honor both; discovery vs seller truth are split: stage1 requires the substring **`free delivery`** in each result card’s scraped text (`shipping_text` / `seller_text` / `availability_text`), sellers via `allowed_merchant_ids` and per-card merchant tokens (plus optional URL facets—see `config.yaml` comments).
- `allowed_merchant_ids` — e.g. `A2XZ7JICGUQ1CX` (Amazon Export), `ATVPDKIKX0DER` (Amazon.com slot); matched from card/primary-offer data, optional `emi` / `rh` `p_6` when `apply_search_url_merchant_facets` is on, and optional `amazon_com_serp_merchant_ids`.
- `pagination_mode`: `auto` (derive page count from `totalResultCount` / `asinOnPageCount`) or `fixed` (use `search_pages`).
- `max_search_pages`, `max_cycle_seconds`, `search_pages`, `required_keywords`, `blacklist_file`, WhatsApp fields, `db_path`, etc.

Legacy `search_url` / `search_urls.main_search` are still accepted as the **featured** URL only; `newest_arrivals` is required.

## Price and stock logic

- Same as before: SQLite `products`, `alert_decisions` for new / back-in-stock / price-drop, optional `enable_missing_asin_oos` on the **featured** pass only.

## Architecture

- `search_scraper.py` — Playwright search only; metadata pagination; merchant token extraction per card.
- `filter_pipeline.py` — stage1 (Pokemon TCG + **free delivery** on card + price), blacklist/keywords, `allowed_merchant_ids`.
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
