# Amazon PDP Monitor - Quickstart

## 1. Install

Open PowerShell in `amazon_monitor`:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chrome
```

If activation is blocked:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

## 2. Configure

Edit `config.yaml`:

- Add ASINs under `pdp_watch_asins`.
- Keep `pdp_allowed_seller_substrings` as `amazon.com` and `amazon export` unless you intentionally allow another seller.
- Set `wa_api_url`, `wa_api_key`, `wa_group_id`, and `affiliate_tag`.
- Optional: set `PROXY_URL` in `.env`.

## 3. Test WhatsApp

```powershell
python first_time_setup.py
```

Confirm the setup message arrives before running the monitor.

## 4. Start

```powershell
python main.py
```

You should see `Monitor started.` and a PDP cycle should run immediately.

## 5. Check Health

```powershell
python tools/healthcheck.py
```

Expected result: `PASS: all monitored jobs are healthy`.

## 6. Stop

Press `Ctrl+C` in the PowerShell window running `main.py`.
