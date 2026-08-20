@echo off
setlocal EnableDelayedExpansion
title Comparison Tool

set "PROJECT_DIR=%~dp0"
if "%PROJECT_DIR:~-1%"=="\" set "PROJECT_DIR=%PROJECT_DIR:~0,-1%"

echo.
echo ============================================================
echo   Comparison Tool
echo   http://localhost:5000/crm
echo ============================================================
echo.

if not exist "%PROJECT_DIR%\builtin_server.py" (
    echo [ERROR] builtin_server.py not found in:
    echo   %PROJECT_DIR%
    pause
    exit /b 1
)

if not exist "%PROJECT_DIR%\.venv\Scripts\python.exe" (
    echo [INFO] First-time setup is required.
    echo        Run INSTALL.cmd once, then use this launcher again.
    echo.
    if exist "%PROJECT_DIR%\INSTALL.cmd" (
        set /p RUN_INSTALL="Run INSTALL.cmd now? [Y/N]: "
        if /I "!RUN_INSTALL!"=="Y" (
            call "%PROJECT_DIR%\INSTALL.cmd"
            if errorlevel 1 exit /b 1
        ) else (
            exit /b 1
        )
    ) else if exist "%PROJECT_DIR%\setup.cmd" (
        call "%PROJECT_DIR%\setup.cmd"
        if errorlevel 1 exit /b 1
    ) else (
        pause
        exit /b 1
    )
)

call "%PROJECT_DIR%\start_server.bat" %*
exit /b %ERRORLEVEL%
