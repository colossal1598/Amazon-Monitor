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

$monitorProcesses = Get-CimInstance Win32_Process | Where-Object {
    $_.Name -match '^python(\.exe)?$' -and
    $_.CommandLine -like "*main.py*" -and
    $_.CommandLine -like "*amazon_monitor*"
}

if ($monitorProcesses) {
    foreach ($proc in $monitorProcesses) {
        Stop-Process -Id $proc.ProcessId -Force
    }
    Write-Host "Stopped existing monitor PID(s): $($monitorProcesses.ProcessId -join ', ')"
}

Start-Process -FilePath $pythonExe -ArgumentList "main.py" -WorkingDirectory $projectRoot | Out-Null
Write-Host "Monitor started."
