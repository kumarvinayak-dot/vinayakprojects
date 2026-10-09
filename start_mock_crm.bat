@echo off
title Mock CRM (port 5050)
REM Starts the mock CRM order-management app used as the Playwright test target.
REM Login: http://127.0.0.1:5050/login   UI map for the test generator: http://127.0.0.1:5050/ui-map

set "PROJECT_DIR=%~dp0"
set "PYTHON_EXE=C:\Users\USER\AppData\Local\Programs\Python\Python312\python.exe"
set "PORT=5050"

if not exist "%PYTHON_EXE%" (
    echo [ERROR] Python 3.12 not found at: %PYTHON_EXE%
    pause
    exit /b 1
)

echo Stopping any existing server on port %PORT%...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":%PORT% " ^| findstr "LISTENING"') do (
    taskkill /F /PID %%a >nul 2>&1
)

cd /d "%PROJECT_DIR%"
start "" cmd /c "timeout /t 3 /nobreak >nul & start http://127.0.0.1:%PORT%/login"

echo Starting Mock CRM... (press Ctrl+C to stop)
"%PYTHON_EXE%" mock_crm_app.py --port %PORT%

pause
