$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$serverScript = Join-Path $projectRoot "tools\admin_ui_server.py"
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $serverScript)) {
    throw "admin_ui_server.py not found at: $serverScript"
}

$pythonExe = if (Test-Path $venvPython) { $venvPython } else { "py" }
$pythonArgs = if ($pythonExe -eq "py") { @("-3", $serverScript) } else { @($serverScript) }

Write-Host "Starting admin UI server..."
Start-Process -FilePath $pythonExe -ArgumentList $pythonArgs -WorkingDirectory $projectRoot -WindowStyle Hidden | Out-Null

Start-Sleep -Seconds 1
Start-Process "http://127.0.0.1:8765" | Out-Null

Write-Host "Admin UI opened at http://127.0.0.1:8765"
