@echo off
title AI TestGen Server
REM Starts web_app.py on http://127.0.0.1:5000/ai-testgen using Python 3.12
REM (Python 3.12 has the AI packages: sentence-transformers, faiss, langgraph, openai)

set "PROJECT_DIR=%~dp0"
set "PYTHON_EXE=C:\Users\USER\AppData\Local\Programs\Python\Python312\python.exe"
set "PORT=5000"

if not exist "%PYTHON_EXE%" (
    echo [ERROR] Python 3.12 not found at: %PYTHON_EXE%
    pause
    exit /b 1
)

if "%GROQ_API_KEY%"=="" (
    echo [WARNING] GROQ_API_KEY is not set - test generation will fail.
    echo.
)

echo Stopping any existing server on port %PORT%...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":%PORT% " ^| findstr "LISTENING"') do (
    taskkill /F /PID %%a >nul 2>&1
)

cd /d "%PROJECT_DIR%"

REM Open the browser a few seconds after startup
start "" cmd /c "timeout /t 8 /nobreak >nul & start http://127.0.0.1:%PORT%/ai-testgen"

echo Starting server... (press Ctrl+C to stop)
"%PYTHON_EXE%" web_app.py --port %PORT%

pause
