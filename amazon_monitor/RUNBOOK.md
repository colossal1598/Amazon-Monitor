# Runbook: Remote Machine Setup and Operations

This runbook is the current, functional guide for running the **search-only** monitor on the client remote machine.

## 1) Remote Machine Requirements

Set up the remote machine with:

- Windows 10/11
- Python 3.10+ (`python --version`)
- Google Chrome installed
- Git installed (`git --version`)
- Internet access stable enough for Amazon browsing
- Local n8n running and reachable from this machine

## 2) One-Time Install

From `amazon_monitor/`:

1. `python -m venv .venv`
2. `.\\.venv\\Scripts\\Activate.ps1`
3. `pip install -r requirements.txt`
4. `playwright install chrome`

If PowerShell blocks activation:

1. `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`
2. `.\\.venv\\Scripts\\Activate.ps1`

## 3) Required Configuration

### `.env`

Set:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `PROXY_URL`

### `config.yaml`

Set and verify:

- `webhook_alert`
- `webhook_heartbeat`
- `affiliate_tag`
- `modem_reconnect_command`
- `modem_auto_refresh_hours`

Note: search URLs are already configured for your use case, including free-shipping filter on the `amazon_com` URL.

## 4) First-Time Webhook Test

Run:

- `python first_time_setup.py`

Complete:

1. Run the setup script.
2. Confirm setup test alert reaches n8n/WhatsApp.

## 5) Start / Stop

Start:

1. `.\\.venv\\Scripts\\Activate.ps1`
2. `python main.py`

Stop:

- `Ctrl + C`

## 6) What Must Be Running

For production operation:

- `main.py` process running
- n8n service running
- WhatsApp API service (your n8n downstream) running

If any of these are down, alerts stop.

## 7) Daily Operations Check (1 minute)

1. Confirm `main.py` is running.
2. Run `python tools/healthcheck.py` and confirm `PASS`.
3. Check `logs/monitor.log` has recent entries.
4. Confirm n8n heartbeat webhook receives events.

## 8) Manual Validation Commands

- Syntax check:
  - `python -m compileall .`
- Public IP:
  - `python tools/check_ip.py`
- Health:
  - `python tools/healthcheck.py`

## 9) Recovery Playbooks

### Captcha or anti-bot block

Expected:

- scraper pauses
- modem refresh logic runs
- scraper resumes after wait

Action:

1. Check latest errors in `logs/monitor.log`.
2. Verify modem reconnect command works manually.
3. Reduce aggressive scheduling only if repeated blocks continue.

### Modem IP did not change

Expected:

- error in log
- healthcheck may fail

Action:

1. Verify `modem_reconnect_command` in `config.yaml`.
2. Compare IP before/after reconnect:
   - `python tools/check_ip.py`
3. Restart monitor.

## 10) Updating Client Machine from Your Commits (No Manual Copy)

One-command update + restart:

1. `powershell -ExecutionPolicy Bypass -File .\scripts\update_and_restart.ps1`

Manual equivalent on client machine in project folder:

1. `git pull`
2. `.\\.venv\\Scripts\\Activate.ps1`
3. `pip install -r requirements.txt`
4. Restart bot (`python main.py`)

This is enough for most updates.

## 11) Optional Auto-Start on Reboot

Use Task Scheduler to run at startup:

Program/script:

- `powershell.exe`

Arguments:

- `-ExecutionPolicy Bypass -File "<full-path>\\start_monitor.ps1"`

Where `start_monitor.ps1` activates venv and starts `python main.py`.

## 12) Optional Scheduled Auto-Update

Install daily update task:

1. `powershell -ExecutionPolicy Bypass -File .\scripts\install_update_task.ps1 -DailyAt "02:00"`

This creates Task Scheduler job `AmazonMonitorUpdate` that runs:

- `scripts\update_and_restart.ps1`

## 13) Production Readiness Checklist

- [ ] `first_time_setup.py` completed successfully
- [ ] n8n alert webhook receives messages
- [ ] n8n heartbeat webhook receives heartbeats
- [ ] `python tools/healthcheck.py` returns PASS
- [ ] `logs/monitor.log` updates continuously
