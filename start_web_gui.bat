@echo off
setlocal EnableDelayedExpansion
echo ========================================================================
echo Starting ASCF Patch Comparison Web GUI
echo ========================================================================
echo.
echo Note: Any existing server instances will be automatically stopped.
echo Server binds to 0.0.0.0 so other machines on your network can connect.
echo.

set "PROJECT_DIR=%~dp0"
set "PROJECT_DIR=%PROJECT_DIR:~0,-1%"
set "HTTP_PORT=5000"

for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /c:"IPv4"') do (
    set "IP=%%a"
    goto :found_ip
)
:found_ip

echo Local:   http://127.0.0.1:%HTTP_PORT%/crm
if defined IP echo Network: http://%IP:~1%:%HTTP_PORT%/crm
echo.
set CRM_USE_HTTPS=0
py builtin_server.py --host 0.0.0.0 --port %HTTP_PORT% --skip-demos
pause
