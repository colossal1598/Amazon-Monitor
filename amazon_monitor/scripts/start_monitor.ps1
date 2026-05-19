$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "pm2-stack.ps1")

Write-Host "Starting amazon-monitor via PM2..."
Start-Monitor
Save-Pm2State

Write-Host "amazon-monitor started through PM2."
