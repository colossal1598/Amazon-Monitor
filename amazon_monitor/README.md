# Amazon PDP + AES Discovery Monitor

Python monitor for Amazon product pages and Amazon Export Sales LLC SERP discovery.

## What it does

- **PDP watch** — visits ASINs in the `watch` list (SQLite `asins` table). Alerts on new, back-in-stock, and price-drop. Seller must match `pdp_allowed_seller_substrings` (Amazon.com / Amazon Export).
- **AES LLC SERP** — scrapes page 1 of the configured Amazon Export seller URL after each PDP cycle. Pokémon TCG filter pipeline applies. Alerts **only** when a new ASIN is inserted into the database (`new_product`).
- **Blacklist** — ASINs in `blacklist` role are ignored during AES discovery.
- Settings and ASIN lists live in **SQLite** (`settings` + `asins` tables). Bootstrap [`config.yaml`](config.yaml) only has `db_path`, `log_dir`, `auth_dir`.

## Key files

- `main.py` — scheduler: PDP scrape → AES discovery → alerts
- `pdp_scraper.py` — PDP extraction (unchanged behavior)
- `search_scraper.py` / `filter_pipeline.py` — AES SERP scrape + filters
- `state_engine.py` — SQLite products/alerts
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
