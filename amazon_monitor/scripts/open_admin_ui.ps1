$ErrorActionPreference = "Stop"

$AdminUiPort = 80
$AdminUiUrl = "http://127.0.0.1"

$projectRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot "pm2-stack.ps1")

Write-Host "Ensuring admin-ui is running under PM2..."
try {
    Ensure-Pm2Available
    $names = Get-Pm2ProcessNames
    if ($names -contains "admin-ui") {
        Invoke-Pm2 -Args @("restart", "admin-ui") -FailureMessage "Could not restart admin-ui."
    } else {
        Write-Host "admin-ui not in PM2 — starting from ecosystem.config.cjs..."
        Invoke-Pm2 -Args @("start", (Join-Path $projectRoot "ecosystem.config.cjs"), "--only", "admin-ui") `
            -FailureMessage "Could not start admin-ui."
    }
    Save-Pm2State
} catch {
    Write-Host "PM2 failed ($($_.Exception.Message)). Trying direct python start..." -ForegroundColor Yellow
    $venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
    $serverScript = Join-Path $projectRoot "tools\admin_ui_server.py"
    if (-not (Test-Path $venvPython)) {
        throw "No venv at $venvPython"
    }
    Start-Process -FilePath $venvPython -ArgumentList @($serverScript) -WorkingDirectory $projectRoot -WindowStyle Hidden | Out-Null
}

Start-Sleep -Seconds 2

$listening = Get-NetTCPConnection -LocalPort $AdminUiPort -State Listen -ErrorAction SilentlyContinue
if (-not $listening) {
    Write-Host ""
    Write-Host "ERROR: Port $AdminUiPort is not listening." -ForegroundColor Red
    Write-Host "Run: .\scripts\check_admin_ui.ps1"
    Write-Host "Common fix: add ADMIN_UI_USER and ADMIN_UI_PASSWORD to .env, then pm2 restart admin-ui"
    if (Get-Command pm2 -ErrorAction SilentlyContinue) {
        pm2 logs admin-ui --lines 20 --nostream
    }
    exit 1
}

Start-Process $AdminUiUrl | Out-Null
Write-Host "Admin UI: $AdminUiUrl (use .env credentials when prompted)"
