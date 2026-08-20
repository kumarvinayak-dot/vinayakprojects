@echo off
setlocal EnableDelayedExpansion
title Install Persistent CRM Compare Server
echo.
echo ============================================================
echo   Auto-start secured CRM Compare server
echo   Creates .env with access token (no Windows password)
echo ============================================================
echo.
echo Windows Server: copy project to local disk (not OneDrive).
echo Share access token from .env only via approved channels.
echo.
echo Requires Administrator approval.
echo.

net session >nul 2>&1
if not %errorlevel%==0 (
    echo Requesting Administrator privileges...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

set "PROJECT_DIR=%~dp0"
set "PROJECT_DIR=%PROJECT_DIR:~0,-1%"
set "HTTP_PORT=5000"
set "HTTPS_PORT=5443"
if not "%~1"=="" set "HTTP_PORT=%~1"
if not "%~2"=="" set "HTTPS_PORT=%~2"

powershell -NoProfile -ExecutionPolicy Bypass -File "%PROJECT_DIR%\scripts\install_persistent_server.ps1" -ProjectDir "%PROJECT_DIR%" -HttpPort %HTTP_PORT% -HttpsPort %HTTPS_PORT%

echo.
pause
