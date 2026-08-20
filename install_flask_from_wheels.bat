@echo off
echo ================================================================================
echo Flask Offline Installation Script
echo ================================================================================
echo.

REM Check if wheel files exist
if not exist "flask_wheels\flask_wheels\*.whl" (
    echo ERROR: No wheel files found in flask_wheels\flask_wheels folder!
    echo.
    echo Please download Flask wheel files first.
    echo See: flask_wheels\DOWNLOAD_INSTRUCTIONS.txt
    echo.
    pause
    exit /b 1
)

echo Checking wheel files...
echo.
dir /B flask_wheels\flask_wheels\*.whl
echo.
echo Found the above wheel files.
echo.

set /p CONFIRM="Do you want to proceed with installation? (Y/N): "
if /i not "%CONFIRM%"=="Y" (
    echo Installation cancelled.
    pause
    exit /b 0
)

echo.
echo Installing Flask from local wheel files...
echo.

py -m pip install --no-index --find-links=flask_wheels\flask_wheels flask

if %ERRORLEVEL% EQU 0 (
    echo.
    echo ================================================================================
    echo SUCCESS: Flask installed successfully!
    echo ================================================================================
    echo.
    echo Verifying installation...
    py -m flask --version
    echo.
    echo You can now use the web interface by running: start_server.bat
    echo.
) else (
    echo.
    echo ================================================================================
    echo ERROR: Flask installation failed!
    echo ================================================================================
    echo.
    echo Please check:
    echo 1. All required wheel files are in flask_wheels folder
    echo 2. Python is installed correctly
    echo 3. pip is working: py -m pip --version
    echo.
)

pause






