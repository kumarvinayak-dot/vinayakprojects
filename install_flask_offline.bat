@echo off
echo ================================================================================
echo Flask Offline Installation Script
echo ================================================================================
echo.
echo This script will install Flask from wheel files.
echo.
echo BEFORE running this script:
echo   1. Download Flask wheels on a machine with internet using:
echo      pip download flask
echo.
echo   2. Transfer ALL .whl files to this directory:
echo      %~dp0flask_wheels\
echo.
echo   3. Run this script
echo.
echo ================================================================================
echo.

set WHEEL_DIR=%~dp0flask_wheels

if not exist "%WHEEL_DIR%" (
    echo ERROR: Wheel directory not found: %WHEEL_DIR%
    echo.
    echo Please create the directory and copy all Flask wheel files there.
    echo.
    pause
    exit /b 1
)

echo Checking for wheel files in: %WHEEL_DIR%
echo.

dir /b "%WHEEL_DIR%\*.whl" 2>nul | findstr "." >nul
if errorlevel 1 (
    echo ERROR: No .whl files found in %WHEEL_DIR%
    echo.
    echo Please copy Flask wheel files to that directory.
    echo.
    pause
    exit /b 1
)

echo Found wheel files:
dir /b "%WHEEL_DIR%\*.whl"
echo.
echo Installing Flask and dependencies...
echo ================================================================================
echo.

py -m pip install --no-index --find-links "%WHEEL_DIR%" flask

if errorlevel 1 (
    echo.
    echo ================================================================================
    echo ERROR: Installation failed!
    echo ================================================================================
    pause
    exit /b 1
)

echo.
echo ================================================================================
echo SUCCESS: Flask installed successfully!
echo ================================================================================
echo.
echo You can now start the web server with:
echo   start_server.bat
echo.
pause











