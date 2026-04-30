# Deployment (Simple Version)

This is the easiest way to update the bot on the client machine.

## How updates work

You edit code on your machine and push to GitHub.
Client machine pulls latest code and restarts bot.

No manual file copy is needed.

## One command to update client machine

Run this inside `amazon_monitor` on the client machine:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\update_and_restart.ps1
```

That one command does everything:

1. Stops the bot
2. Pulls latest code from GitHub
3. Installs updated dependencies
4. Starts the bot again

## Optional: auto-update every day

Run once on client machine:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install_update_task.ps1 -DailyAt "02:00"
```

This creates a scheduled task called `AmazonMonitorUpdate`.
It will run the update script every day at 02:00.

## Scripts included

- `scripts\update_and_restart.ps1` -> stop + pull + install + restart
- `scripts\start_monitor.ps1` -> start bot
- `scripts\stop_monitor.ps1` -> stop bot
- `scripts\install_update_task.ps1` -> install daily auto-update task

## If update fails

1. Open PowerShell in `amazon_monitor`
2. Run:

```powershell
git pull
```

3. If Git shows conflict/error, fix it first before restarting bot.

## Important

- Keep `.env` only on client machine.
- Do not commit client secrets to GitHub.

