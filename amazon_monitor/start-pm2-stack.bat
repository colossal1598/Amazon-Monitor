@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo === PM2 stack: amazon-monitor, wa-server, monitor-healthcheck ===

where pm2 >nul 2>nul
if errorlevel 1 (
  echo PM2 not in PATH. Install: npm install -g pm2
  echo Then re-open this .bat.
  pause
  exit /b 1
)

REM Start/reload apps from ecosystem (idempotent: delete old names then start fresh)
call pm2 delete amazon-monitor >nul 2>&1
call pm2 delete wa-server >nul 2>&1
call pm2 delete monitor-healthcheck >nul 2>&1

call pm2 start ecosystem.config.cjs
if errorlevel 1 (
  echo PM2 start failed. If wa-server path is wrong, set WA_SERVER_ROOT and retry.
  echo Example: set WA_SERVER_ROOT=C:\Users\Eyal\dev\wa-server
  pause
  exit /b 1
)

call pm2 save
echo.
echo === pm2 list ===
call pm2 list
echo.
echo IMPORTANT: Run "pm2 startup" once as Administrator to enable boot startup.
echo Then run "pm2 save" after any process-list changes.
echo.
pause
endlocal
