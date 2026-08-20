@echo off
setlocal EnableDelayedExpansion
title ASCF Server - LAN (secured)
echo.
echo ============================================================
echo   ASCF Server - LAN mode (requires CRM_API_TOKEN in .env)
echo ============================================================
echo.
echo Copy .env.example to .env and set CRM_API_TOKEN before use.
echo Users open: http://^<server-ip^>:5000/crm?access_token=^<token^>
echo.

set "PROJECT_DIR=%~dp0"
set "PROJECT_DIR=%PROJECT_DIR:~0,-1%"
set "HTTP_PORT=5000"
if not "%~1"=="" set "HTTP_PORT=%~1"

set "PYTHON_EXE=%PROJECT_DIR%\.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" (
    echo [ERROR] Python not found at: %PYTHON_EXE%
    pause
    exit /b 1
)

cd /d "%PROJECT_DIR%"
set "CRM_OPEN_FIREWALL=1"
start "ASCF Server" /min /D "%PROJECT_DIR%" cmd /c "set CRM_USE_HTTPS=0&& "%PYTHON_EXE%" "%PROJECT_DIR%\builtin_server.py" --host 0.0.0.0 --port %HTTP_PORT% --skip-demos"

echo Server starting on all interfaces with security token from .env ...
pause
