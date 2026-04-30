# Deployment and Client Update Workflow

This guide explains how your client machine can receive your code updates without manual file copying.

## Recommended Model

Use a shared Git remote (GitHub/GitLab/private Git server):

1. You push changes from your development machine.
2. Client machine runs `git pull`.
3. Client machine updates dependencies (if needed).
4. Client machine restarts the bot.

## One-Time Setup on Client Machine

1. Install Git.
2. Clone the repository once:
   - `git clone <repo_url>`
3. Configure `.env` on client machine.
4. Create and install Python environment:
   - `python -m venv .venv`
   - `.\\.venv\\Scripts\\Activate.ps1`
   - `pip install -r requirements.txt`
   - `playwright install chrome`

## Update Procedure (Each Release)

One-command update from project folder on client machine:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\update_and_restart.ps1
```

Manual equivalent:

```powershell
git pull
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Then restart bot:

```powershell
python main.py
```

## Optional: Simple Auto-Update Script

Already included in repository:

- `scripts\update_and_restart.ps1`
- `scripts\start_monitor.ps1`
- `scripts\stop_monitor.ps1`
- `scripts\install_update_task.ps1`

Install scheduled daily auto-update:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install_update_task.ps1 -DailyAt "02:00"
```

## Optional: Safer Production Pattern

- Keep `.env` only on client machine.
- Keep client-specific secrets out of Git.
- Before major releases, back up:
  - `data/monitor.db`
  - `.env`
  - `config.yaml` (if client-customized)

## Practical Notes

- If `requirements.txt` did not change, `pip install -r requirements.txt` is usually fast and safe.
- If `git pull` reports conflicts, stop and resolve before restart.
- If bot fails after update, roll back to previous commit:
  - `git log --oneline`
  - `git checkout <previous_commit>`

