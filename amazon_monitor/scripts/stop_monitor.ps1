$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "pm2-stack.ps1")

Write-Host "Stopping amazon-monitor via PM2..."
Stop-Monitor
Save-Pm2State

Write-Host "amazon-monitor stopped through PM2."
