$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonExe = Join-Path $projectRoot ".venv\Scripts\python.exe"
$requirementsFile = Join-Path $projectRoot "requirements.txt"

. (Join-Path $PSScriptRoot "pm2-stack.ps1")

if (-not (Test-Path $pythonExe)) {
    throw "Python virtual environment not found. Expected: $pythonExe"
}

Write-Host "Stopping amazon-monitor via PM2..."
Stop-Monitor

Write-Host "Pulling latest code..."
git -C $projectRoot pull
if ($LASTEXITCODE -ne 0) {
    throw "git pull failed. Resolve repository issue before retrying."
}

Write-Host "Installing/updating dependencies..."
& $pythonExe -m pip install -r $requirementsFile
if ($LASTEXITCODE -ne 0) {
    throw "Dependency installation failed."
}

Write-Host "Starting PM2 stack (monitor + admin-ui + wa-server + healthcheck)..."
Start-Stack
Save-Pm2State

Write-Host ""
Write-Host "Admin UI: http://127.0.0.1 (requires ADMIN_UI_USER / ADMIN_UI_PASSWORD in .env)"
Write-Host "If connection refused, run: .\scripts\check_admin_ui.ps1"
Write-Host "Update + PM2 restart completed successfully."
