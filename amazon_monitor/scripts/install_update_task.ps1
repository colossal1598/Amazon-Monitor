param(
    [string]$TaskName = "AmazonMonitorUpdate",
    [string]$DailyAt = "02:00"
)

$ErrorActionPreference = "Stop"

$scriptPath = Join-Path $PSScriptRoot "update_and_restart.ps1"
if (-not (Test-Path $scriptPath)) {
    throw "update_and_restart.ps1 not found at: $scriptPath"
}

$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$scriptPath`""

$trigger = New-ScheduledTaskTrigger -Daily -At $DailyAt

# Run whether user is logged on or not is omitted here to keep setup simple.
# This registers task for the current user context.
Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Description "Pull latest monitor code, install deps, and restart monitor." `
    -Force | Out-Null

Write-Host "Scheduled task '$TaskName' installed to run daily at $DailyAt."
