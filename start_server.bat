@echo off
setlocal EnableDelayedExpansion
title Comparison Tool - Starting...
echo.
echo ============================================================
echo          Comparison Tool - Starting Server
echo          Localhost HTTP only (this PC only)
echo ============================================================
echo.

REM Resolve project path from this script location
set "PROJECT_DIR=%~dp0"
set "PROJECT_DIR=%PROJECT_DIR:~0,-1%"

set "HTTP_PORT=5000"
if not "%~1"=="" set "HTTP_PORT=%~1"
set "HOST=127.0.0.1"

REM Use local venv Python for predictable startup
set "PYTHON_EXE=%PROJECT_DIR%\.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" (
    echo [ERROR] Python not found at: %PYTHON_EXE%
    echo Run INSTALL.cmd first, then launch_comparison_tool.bat
    pause
    exit /b 1
)

cd /d "%PROJECT_DIR%"

echo Stopping any listener on HTTP port %HTTP_PORT%...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":%HTTP_PORT%" ^| findstr "LISTENING"') do (
    echo   Killing PID %%a ^(port %HTTP_PORT%^)
    taskkill /F /PID %%a >nul 2>&1
)

REM Legacy HTTPS port cleanup (older installs may still listen on 5443)
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":5443" ^| findstr "LISTENING"') do (
    echo   Killing legacy HTTPS PID %%a ^(port 5443^)
    taskkill /F /PID %%a >nul 2>&1
)

ping localhost -n 2 >nul 2>&1

set "CFR_JAR=C:\cfr-0.152.jar"

echo.
echo ============================================================
echo   Server will start in BACKGROUND mode
echo.
echo   URL:  http://localhost:%HTTP_PORT%/crm
echo.
echo   Access is limited to this PC only (127.0.0.1).
echo   To STOP the server, run: STOP_SERVER.bat
echo.
echo   For secured LAN access: start_server_lan.bat + .env token
echo ============================================================
echo.

start "Comparison Tool Server" /min /D "%PROJECT_DIR%" cmd /c "set PYTHONDONTWRITEBYTECODE=1&& set CRM_REQUIRE_SECURE=0&& set CRM_OPEN_FIREWALL=0&& set CRM_USE_HTTPS=0&& "%PYTHON_EXE%" "%PROJECT_DIR%\builtin_server.py" --host %HOST% --port %HTTP_PORT% --skip-demos"

echo Server start command issued (HTTP %HTTP_PORT% only^).
echo.

set "SERVER_READY=0"
for /l %%i in (1,1,12) do (
    netstat -ano | findstr ":%HTTP_PORT%" | findstr "LISTENING" >nul
    if not errorlevel 1 (
        set "SERVER_READY=1"
        goto :ready
    )
    ping localhost -n 2 >nul
)

:ready
if "!SERVER_READY!"=="1" (
    echo [SUCCESS] Server is listening on http://localhost:%HTTP_PORT%/crm
    start "" "http://localhost:%HTTP_PORT%/crm"
) else (
    echo [WARNING] Server may still be starting... run CHECK_STATUS.bat
)

echo.
echo This window will close in 5 seconds...
ping localhost -n 6 >nul 2>&1
