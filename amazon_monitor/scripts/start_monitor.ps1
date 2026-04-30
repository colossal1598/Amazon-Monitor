$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonExe = Join-Path $projectRoot ".venv\Scripts\python.exe"
$mainFile = Join-Path $projectRoot "main.py"

if (-not (Test-Path $pythonExe)) {
    throw "Python virtual environment not found. Expected: $pythonExe"
}
if (-not (Test-Path $mainFile)) {
    throw "main.py not found. Expected: $mainFile"
}

$running = Get-CimInstance Win32_Process | Where-Object {
    $_.Name -match '^python(\.exe)?$' -and
    $_.CommandLine -like "*main.py*" -and
    $_.CommandLine -like "*amazon_monitor*"
}

if ($running) {
    Write-Host "Monitor is already running. PID(s): $($running.ProcessId -join ', ')"
    exit 0
}

Start-Process -FilePath $pythonExe -ArgumentList "main.py" -WorkingDirectory $projectRoot | Out-Null
Write-Host "Monitor started."
