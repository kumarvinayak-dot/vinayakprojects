@echo off
echo ================================================================================
echo Flask Installation Status Check
echo ================================================================================
echo.

echo [1] Checking Python...
py --version
if %ERRORLEVEL% NEQ 0 (
    echo ERROR: Python not found!
    pause
    exit /b 1
)
echo.

echo [2] Checking pip...
py -m pip --version
if %ERRORLEVEL% NEQ 0 (
    echo ERROR: pip not found!
    pause
    exit /b 1
)
echo.

echo [3] Checking Flask installation...
py -m flask --version 2>nul
if %ERRORLEVEL% EQU 0 (
    echo SUCCESS: Flask is installed!
    py -m flask --version
    echo.
    echo You can use the web interface by running: start_server.bat
) else (
    echo STATUS: Flask is NOT installed
    echo.
    echo [4] Checking for wheel files...
    if exist "flask_wheels\*.whl" (
        echo Found wheel files in flask_wheels folder:
        echo.
        dir /B flask_wheels\*.whl
        echo.
        echo You can install Flask by running: install_flask_from_wheels.bat
    ) else (
        echo No wheel files found in flask_wheels folder.
        echo.
        echo NEXT STEPS:
        echo 1. Download Flask wheel files on a computer with internet
        echo 2. See instructions: flask_wheels\DOWNLOAD_INSTRUCTIONS.txt
        echo 3. Copy wheel files to: flask_wheels\
        echo 4. Run: install_flask_from_wheels.bat
    )
)

echo.
echo [5] Alternative: Use Standalone GUI (no Flask needed)
echo    Run: start_gui.bat
echo.

echo ================================================================================
pause











