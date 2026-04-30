# Runbook: Install, Run, Test, Recover

This is the operator guide for running the monitor on your local PC with n8n already working.

## 1) Install (One Time)

From `amazon_monitor/`:

1. `python -m venv .venv`
2. `.\\.venv\\Scripts\\Activate.ps1`
3. `pip install -r requirements.txt`
4. `playwright install chrome`

## 2) Configure

Edit `.env`:

- `AMAZON_EMAIL`
- `AMAZON_PASSWORD`
- optional Telegram fallback (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`)

Edit `config.yaml`:

- webhook URLs
- affiliate tag
- modem reconnect command

## 3) First-Time Session Setup

Run:

- `python first_time_setup.py`

Checklist:

- complete login + 2FA in opened browser
- confirm Amazon session is valid
- confirm dummy alert reaches your n8n -> WhatsApp flow

## 4) Start and Stop

Start:

- `python main.py`

Stop:

- `Ctrl + C`

## 5) Daily Checks

- verify `logs/monitor.log` is updating
- verify `data/health.json` timestamp changes
- run `python tools/healthcheck.py`

## 6) Manual Test Commands

- Compile/syntax:
  - `python -m compileall .`
- Public IP check:
  - `python tools/check_ip.py`
- Health status:
  - `python tools/healthcheck.py`

Optional quick webhook smoke test:

- `python first_time_setup.py` (includes test alert)

## 7) Recovery Playbooks

### Captcha blocked

Expected behavior:

- monitor pauses scraping jobs
- modem refresh step runs
- monitor waits then resumes jobs

Action:

- check `logs/monitor.log`
- if repeated blocks continue, lower scrape frequency and verify network/IP quality

### Amazon session expired

Expected behavior:

- shipping job pauses
- Telegram urgent message if configured

Action:

1. Run `python first_time_setup.py`
2. Confirm Amazon account session is valid in persistent context
3. Restart `python main.py`

### Modem IP unchanged

Expected behavior:

- modem job logs failure
- `tools/healthcheck.py` may report modem errors

Action:

1. Validate command in `config.yaml` (`modem_reconnect_command`)
2. Run `python tools/check_ip.py` before and after manual reconnect
3. Restart monitor

## 8) Production Readiness Checklist

- [ ] n8n alert webhook receives messages
- [ ] n8n heartbeat webhook receives heartbeats
- [ ] `first_time_setup.py` completed successfully
- [ ] `python tools/healthcheck.py` returns PASS
- [ ] `logs/monitor.log` has no repeating critical exceptions
