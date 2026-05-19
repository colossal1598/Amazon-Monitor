$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "pm2-stack.ps1")

Write-Host "Restarting amazon-monitor via PM2..."
Restart-Monitor
Save-Pm2State

Write-Host "amazon-monitor restarted through PM2."
