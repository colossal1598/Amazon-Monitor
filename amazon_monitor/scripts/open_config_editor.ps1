$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonExe = Join-Path $projectRoot ".venv\Scripts\python.exe"
$backendFile = Join-Path $projectRoot "tools\config_editor_backend.py"
$url = "http://127.0.0.1:8765"

if (-not (Test-Path $pythonExe)) {
    throw "Python virtual environment not found. Expected: $pythonExe"
}
if (-not (Test-Path $backendFile)) {
    throw "Backend file not found. Expected: $backendFile"
}

$existing = Get-CimInstance Win32_Process | Where-Object {
    $_.Name -match '^python(\.exe)?$' -and
    $_.CommandLine -like "*config_editor_backend.py*"
}

if (-not $existing) {
    Start-Process -FilePath $pythonExe -ArgumentList "tools\config_editor_backend.py" -WorkingDirectory $projectRoot | Out-Null
    Start-Sleep -Seconds 1
}

Start-Process $url | Out-Null
Write-Host "Config editor opened at $url"
