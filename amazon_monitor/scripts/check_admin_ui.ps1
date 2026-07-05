# Diagnose admin UI (port 80) and PM2 status.
$ErrorActionPreference = "Continue"

$AdminUiPort = 80
$projectRoot = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $projectRoot ".env"
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
$serverScript = Join-Path $projectRoot "tools\admin_ui_server.py"

Write-Host "=== Admin UI check ===" -ForegroundColor Cyan
Write-Host "Project: $projectRoot"

if (-not (Test-Path $venvPython)) {
    Write-Host "FAIL: venv missing: $venvPython" -ForegroundColor Red
    Write-Host "Run: py -3 -m venv .venv ; .\.venv\Scripts\pip install -r requirements.txt"
} else {
    Write-Host "OK: venv python exists"
}

if (-not (Test-Path $serverScript)) {
    Write-Host "FAIL: $serverScript not found (pull latest code)" -ForegroundColor Red
}

$hasUser = $false
$hasPass = $false
if (Test-Path $envFile) {
    foreach ($line in Get-Content $envFile) {
        if ($line -match '^\s*ADMIN_UI_USER\s*=\s*\S+') { $hasUser = $true }
        if ($line -match '^\s*ADMIN_UI_PASSWORD\s*=\s*\S+') { $hasPass = $true }
    }
    if ($hasUser -and $hasPass) {
        Write-Host "OK: ADMIN_UI_USER and ADMIN_UI_PASSWORD set in .env"
    } else {
        Write-Host "FAIL: Add to .env (required or server exits immediately):" -ForegroundColor Red
        Write-Host "  ADMIN_UI_USER=your_username"
        Write-Host "  ADMIN_UI_PASSWORD=long_random_password"
    }
} else {
    Write-Host "FAIL: .env missing at $envFile" -ForegroundColor Red
}

Write-Host ""
Write-Host "=== Port $AdminUiPort ===" -ForegroundColor Cyan
$listeners = Get-NetTCPConnection -LocalPort $AdminUiPort -State Listen -ErrorAction SilentlyContinue
if ($listeners) {
    Write-Host "OK: Something is listening on 127.0.0.1:$AdminUiPort"
} else {
    Write-Host "FAIL: Nothing listening on port $AdminUiPort (browser = connection refused)" -ForegroundColor Red
}

Write-Host ""
Write-Host "=== PM2 admin-ui ===" -ForegroundColor Cyan
if (Get-Command pm2 -ErrorAction SilentlyContinue) {
    pm2 describe admin-ui 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "admin-ui is NOT registered in PM2. Run from amazon_monitor:" -ForegroundColor Yellow
        Write-Host "  .\start-pm2-stack.bat"
        Write-Host "  # or: pm2 start ecosystem.config.cjs"
    } else {
        Write-Host ""
        Write-Host "Last admin-ui logs:" -ForegroundColor Cyan
        pm2 logs admin-ui --lines 15 --nostream
    }
} else {
    Write-Host "PM2 not in PATH"
}

Write-Host ""
Write-Host "=== Fix (run in amazon_monitor folder) ===" -ForegroundColor Cyan
Write-Host @"
1. Add ADMIN_UI_USER and ADMIN_UI_PASSWORD to .env
2. pip install -r requirements.txt
3. pm2 delete admin-ui 2>`$null; pm2 start ecosystem.config.cjs --only admin-ui
4. pm2 save
5. Open http://127.0.0.1 (browser will ask for basic auth; port 80 requires admin/elevated PM2 on Windows)
"@
