@echo off
title ASCF Server - Status Check
echo.
echo ============================================================
echo          ASCF Comparison Tool - Status Check
echo ============================================================
echo.

echo Checking port 5000...
echo.

schtasks /Query /TN "CRMCompareToolServer" >nul 2>&1
if not errorlevel 1 (
    echo [PERSISTENT] Scheduled task CRMCompareToolServer is installed
    schtasks /Query /TN "CRMCompareToolServer" /FO LIST /V | findstr /I "Status: Last Run Time: Next Run Time:"
    echo.
) else (
    echo [AUTO-START] Not installed - run INSTALL_PERSISTENT_SERVER.bat for login auto-start
    echo.
)

netstat -ano | findstr ":5000" | findstr "LISTENING"

if %ERRORLEVEL%==0 (
    echo.
    echo [RUNNING] Server is running on port 5000
) else (
    echo [STOPPED] No server on port 5000
)

echo.
echo ============================================================
echo.
echo Network IP Addresses:
echo.
ipconfig | findstr /c:"IPv4"

echo.
echo Share your network IP with colleagues for them to access.
echo Example: http://YOUR_IP:5000
echo.
echo ============================================================
echo.
pause


