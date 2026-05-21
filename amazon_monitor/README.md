# Amazon PDP + AES Discovery Monitor

Python monitor for Amazon product pages and Amazon Export Sales LLC SERP discovery.

## What it does

- **PDP watch** — visits ASINs in the `watch` list (SQLite `asins` table). Seller must match `pdp_allowed_seller_substrings` (Amazon.com / Amazon Export). State is stored in **`products`**. On each successful scrape, while the offer is in stock, the monitor compares the stored price to the new PDP price and can alert on **`price_drop`** (threshold: `price_drop_percent` in settings). Also alerts on **`new_product`** and **`back_in_stock`**.
- **AES LLC SERP** — after each PDP cycle, scrapes page 1 of the configured Amazon Export seller URL. The same Pokémon TCG filter pipeline applies. State is stored only in **`aes_products`** (never in `products`). Alerts on **`new_product`**, **`back_in_stock`**, and **`price_drop`** using the same `price_drop_percent` rule as PDP.
- **Blacklist** — ASINs in `blacklist` role are ignored during AES processing.
- **Per-tick dedupe** — PDP and AES alerts for one scheduler cycle are merged, then deduped to one WhatsApp per ASIN (`back_in_stock` > `new_product` > `price_drop`).
- Settings and ASIN lists live in **SQLite** (`settings` + `asins` tables). Bootstrap [`config.yaml`](config.yaml) only has `db_path`, `log_dir`, `auth_dir`.

## SQLite tables (who writes what)

| Table | Written by | Purpose |
|-------|------------|---------|
| `products` | PDP watch only | Price/stock for ASINs on the watch list |
| `aes_products` | AES SERP mirror only | Price/stock mirror of filtered page-1 SERP rows |
| `alerts` | Both paths | History of alerts generated (not product state) |
| `settings` / `asins` | Admin UI / migrations | Config and watch/blacklist lists |

AES never inserts or updates `products`. PDP never inserts or updates `aes_products`. An ASIN can exist in both tables independently (e.g. on watch list and on the SERP).

## AES mirror semantics

- **Mirrored** means “on current page-1 SERP after filters,” not global Amazon inventory.
- Each cycle, every filtered SERP row updates its `aes_products` row (price written every time while in stock).
- **In stock on SERP** uses the same idea as PDP: valid price on the card + shippable to your location, not Amazon “in stock” wording (SERP cards often omit that text).
- **Reconcile absence** — only when this cycle has at least one filtered candidate (`aes_candidates` non-empty). Any `aes_products` row whose ASIN is *not* on the page is marked out of stock (`in_stock = 0`). If the scrape or pipeline yields zero candidates, reconcile is skipped so a bad or empty run does not mass-mark everything OOS.
- Item leaves page 1 → OOS in mirror; item reappears in stock on the card → **`back_in_stock`**.

## Key files

- `main.py` — scheduler: PDP scrape → AES scrape → dedupe → WhatsApp
- `pdp_scraper.py` — PDP extraction
- `search_scraper.py` / `filter_pipeline.py` — AES SERP scrape + filters
- `state_engine.py` — `products`, `aes_products`, alert logic
- `alert_decisions.py` / `alert_dedupe.py` — shared rules and per-tick dedupe
- `settings_store.py` — load/save runtime config and ASIN roles
- `tools/admin_ui_server.py` + `tools/admin_ui/` — Hebrew admin UI
- `webhook_sender.py` — WhatsApp API

## Run locally

```powershell
cd amazon_monitor
pip install -r requirements.txt
playwright install chrome
python main.py
```

## Admin UI

```powershell
# Set in .env: ADMIN_UI_USER, ADMIN_UI_PASSWORD
.\scripts\open_admin_ui.ps1
# http://127.0.0.1:8765
```

Or via PM2 stack: `admin-ui` app in `ecosystem.config.cjs`.

Remote access: see [RUNBOOK.md](RUNBOOK.md) (Tailscale Funnel + basic auth).

## Migrate legacy YAML

If you have a full old `config.yaml` backup, restore it once and run:

```powershell
python tools/migrate_yaml_to_db.py
```

(Only imports when the `settings` table is empty.)

## Health

- `logs/monitor.log`, `data/health.json`
- `python tools/healthcheck.py`
