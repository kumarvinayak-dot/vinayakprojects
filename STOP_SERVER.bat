@echo off
setlocal EnableDelayedExpansion
title Comparison Tool - Stopping...
echo.
echo ============================================================
echo          Comparison Tool - Stopping Server
echo          HTTP (and legacy HTTPS if present)
echo ============================================================
echo.

set "HTTP_PORT=5000"
set "HTTPS_PORT=5443"
if not "%~1"=="" set "HTTP_PORT=%~1"

set "FOUND=0"

echo Checking persistent scheduled task CRMCompareToolServer...
schtasks /Query /TN "CRMCompareToolServer" >nul 2>&1
if not errorlevel 1 (
    echo   Ending persistent server task...
    schtasks /End /TN "CRMCompareToolServer" >nul 2>&1
)

for %%P in (%HTTP_PORT% %HTTPS_PORT%) do (
    echo Checking port %%P...
    for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":%%P" ^| findstr "LISTENING"') do (
        set "FOUND=1"
        echo   Stopping PID %%a ^(port %%P^)
        taskkill /F /PID %%a >nul 2>&1
        if not errorlevel 1 (
            echo   [OK] Stopped PID %%a
        ) else (
            echo   [INFO] PID %%a may have already exited
        )
    )
)

if "!FOUND!"=="0" (
    echo [INFO] No listeners on ports %HTTP_PORT% or %HTTPS_PORT%
    echo.
    echo If the server still runs: Task Manager — end "Comparison Tool Server" or python.exe
)

echo.
echo Done!
timeout /t 3 /nobreak >nul
