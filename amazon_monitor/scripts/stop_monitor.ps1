$ErrorActionPreference = "Stop"

$monitorProcesses = Get-CimInstance Win32_Process | Where-Object {
    $_.Name -match '^python(\.exe)?$' -and
    $_.CommandLine -like "*main.py*" -and
    $_.CommandLine -like "*amazon_monitor*"
}

if (-not $monitorProcesses) {
    Write-Host "Monitor is not running."
    exit 0
}

foreach ($proc in $monitorProcesses) {
    Stop-Process -Id $proc.ProcessId -Force
}

Write-Host "Monitor stopped. PID(s): $($monitorProcesses.ProcessId -join ', ')"
