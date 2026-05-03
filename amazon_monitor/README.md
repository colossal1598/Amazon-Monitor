# Pokemon TCG Amazon Monitor

Local Python monitor for Pokemon TCG listings on Amazon.com: Playwright search scraping (no product-detail pages), dual search URLs, SQLite state, title/keyword filters plus optional YAML ASIN whitelist/blacklist, and alerts via a local WhatsApp API.

## What This Project Does

- **`search_urls.amazon_com`** — scrapes the full Amazon.com–scoped result set (dynamic pagination from Amazon `s-metadata`, capped by `max_search_pages` and `max_cycle_seconds`). Drives catalog updates, price drops, back-in-stock, and optional missing-ASIN reconciliation.
- **`search_urls.aes_llc`** — Amazon Export Sales LLC (`me=` scope), **page 1 only**. After the same filters as the Amazon.com pass, only ASINs **not already in the database** are processed, with **no** missing-ASIN reconcile (avoids false OOS from a partial page).
- Sends alerts to your local WhatsApp API server.

## Configuration

Edit `config.yaml`:

- `search_urls.amazon_com` — Amazon.com slot SERP URL (filters/sort embedded in the link).
- `search_urls.aes_llc` — Amazon Export Sales LLC seller-scoped URL.
- **Do not rely on combining** free-shipping refines (`p_n_is_free_shipping` in `rh`) **with** seller refines (`p_6` / `emi`) in one Amazon search URL—Amazon often does not honor both. Stage1 only needs a **visible shipping/delivery line** on the card: non-empty **`shipping_text`** (from `div[data-cy="delivery-block"]` / `.udm-primary-delivery-message` when present), or **`delivery` / `shipping`** in the scraped blob, or **free** phrasing (`free delivery` / `free shipping`, or **חינם** on the line). No currency parsing.
- **WhatsApp templates** — `{shipping}` is built in code (`filter_pipeline.shipping_display_hebrew`): **`משלוח חינם`** when free, else **`משלוח: 54₪`**-style (amount + ₪ when the line has **₪** or **ILS** + digits; otherwise **`משלוח:`** + the full scraped line). Edit that function to change wording or `$` handling.
- `pagination_mode`: `auto` (derive page count from `totalResultCount` / `asinOnPageCount`) or `fixed` (use `search_pages`).
- `max_search_pages`, `max_cycle_seconds`, `search_pages`, `required_keywords`, `whitelist` / `blacklist` (ASIN lists in YAML), WhatsApp fields, `db_path`, etc.

Legacy keys `search_urls.featured` / `search_urls.newest_arrivals` / `search_urls.main_search` and top-level `search_url` are still read as fallbacks for the Amazon.com URL only; prefer `amazon_com` and `aes_llc`.

## Price and stock logic

- SQLite `products`, `alert_decisions` for new / back-in-stock / price-drop, optional `enable_missing_asin_oos` on the **Amazon.com** pass only.

## Architecture

- `search_scraper.py` — Playwright search only; metadata pagination.
- `filter_pipeline.py` — stage1 (Pokemon TCG + **shipping/delivery line present on card** + price), optional YAML **whitelist** / **blacklist** ASINs, `required_keywords` for non-whitelist rows → rows for the state engine.
- `state_engine.py` — SQLite + alerts; `list_known_asins()` for the AES LLC pass.
- `webhook_sender.py` — WhatsApp API posts.
- `main.py` — APScheduler: Amazon.com scrape → process → AES LLC scrape → new ASINs only → process.

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
- **No alerts** — verify WA API settings, `search_urls`, and filter lists (`required_keywords`, YAML `whitelist` / `blacklist`). If hits exist on Amazon but not in logs, check stage1: the SERP should populate **`shipping_text`** (delivery block) or otherwise contain **delivery/shipping** text on the card scrape.
