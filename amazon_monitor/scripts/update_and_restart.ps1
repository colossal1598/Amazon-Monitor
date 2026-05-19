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

Write-Host "Starting amazon-monitor via PM2..."
Start-Monitor
Save-Pm2State

Write-Host "Update + PM2 restart completed successfully."
