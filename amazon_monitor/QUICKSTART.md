# Pokemon Amazon Monitor - Beginner Quickstart

This guide is written for non-technical users.
Follow it exactly in order.

---

## 1) What this bot does

- Watches Amazon search results for Pokemon TCG products.
- Sends alerts through your local n8n workflow.
- Keeps health status in a file so you can verify it is running.

---

## 2) Before you start (one-time checklist)

Make sure these are already installed:

- Windows PC (always on)
- Python 3.10 or newer
- Google Chrome
- n8n running and working

Make sure you have these files in your project folder:

- `config.yaml`
- `.env`
- `requirements.txt`
- `first_time_setup.py`
- `main.py`

---

## 3) First-time install (simple version)

Open **PowerShell** in the `amazon_monitor` folder and run these commands one by one:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chrome
```

If PowerShell says script execution is blocked:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

---

## 4) Fill your settings

### Edit `.env`

Set:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `PROXY_URL` (optional)

### Edit `config.yaml`

Check:

- `webhook_alert`
- `webhook_heartbeat`
- `affiliate_tag`
- `modem_reconnect_command`

Do not change the search URLs unless you were told to.

---

## 5) First-time webhook test

Run:

```powershell
python first_time_setup.py
```

What you must do:

1. Run setup script.
2. Confirm test alert is received in your n8n/WhatsApp flow.

If test alert is not received, stop and fix n8n first.

---

## 6) Start the bot (daily use)

In PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
python main.py
```

You should see:

- `Monitor started`

Leave this window open while bot is running.

---

## 7) Stop the bot safely

In the same PowerShell window:

- Press `Ctrl + C`

---

## 8) How to check if it is healthy

Open a new PowerShell window in project folder and run:

```powershell
.\.venv\Scripts\Activate.ps1
python tools/healthcheck.py
```

Expected:

- `PASS: all monitored jobs are healthy`

If you see `FAIL`, open:

- `logs/monitor.log`

and share the latest errors with support/developer.

---

## 9) Useful files (for support)

- Main logs: `logs/monitor.log`
- Health status: `data/health.json`
- Last public IP: `data/last_ip.txt`
- Database: `data/monitor.db`

---

## 10) Re-installation process (clean reset)

Use this if setup is broken or dependencies are corrupted.

### Step A - Stop everything

- Stop bot (`Ctrl + C` if running).
- Close browser windows used by setup.

### Step B - Delete old virtual environment

In PowerShell from `amazon_monitor`:

```powershell
Remove-Item -Recurse -Force .venv
```

### Step C - Recreate environment

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install --upgrade pip
pip install -r requirements.txt
playwright install chrome
```

### Step D - Re-run setup

```powershell
python first_time_setup.py
```

### Step E - Start bot again

```powershell
python main.py
```

### Optional hard reset (only if asked by developer)

If you need a full state reset, you can back up then remove:

- `data/monitor.db`
- `data/health.json`
- `data/last_ip.txt`

Do **not** delete these unless you understand it will remove stored product history.

---

## 11) Common problems and fixes

### Problem: `ModuleNotFoundError` or import errors

Fix:

1. Activate `.venv`
2. Run `pip install -r requirements.txt` again

### Problem: Healthcheck FAIL with stale jobs

Fix:

1. Confirm `python main.py` is still running
2. Check `logs/monitor.log` for repeated errors
3. Restart bot

### Problem: No WhatsApp alerts

Fix:

1. Confirm n8n is running
2. Confirm webhook URLs in `config.yaml`
3. Run `python first_time_setup.py` test again

## 12) Daily 30-second checklist for client

1. Start bot: `python main.py`
2. Run healthcheck: `python tools/healthcheck.py`
3. Confirm result is `PASS`
4. If `FAIL`, check `logs/monitor.log`

