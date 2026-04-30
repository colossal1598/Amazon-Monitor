$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonExe = Join-Path $projectRoot ".venv\Scripts\python.exe"
$requirementsFile = Join-Path $projectRoot "requirements.txt"
$stopScript = Join-Path $PSScriptRoot "stop_monitor.ps1"
$startScript = Join-Path $PSScriptRoot "start_monitor.ps1"

if (-not (Test-Path $pythonExe)) {
    throw "Python virtual environment not found. Expected: $pythonExe"
}

Write-Host "Stopping monitor (if running)..."
& $stopScript

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

Write-Host "Starting monitor..."
& $startScript

Write-Host "Update + restart completed successfully."
