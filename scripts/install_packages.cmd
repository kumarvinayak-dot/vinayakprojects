@echo off
setlocal EnableDelayedExpansion

title Comparison Tool - Install

echo.
echo ============================================================
echo   Comparison Tool - Install (localhost HTTP only)
echo   Developed by Vinayak Srivastava
echo ============================================================
echo.

REM Install in this folder only. Strip trailing \ so quoted paths work.
set "INSTALL_DIR=%~dp0"
if "%INSTALL_DIR:~-1%"=="\" set "INSTALL_DIR=%INSTALL_DIR:~0,-1%"
if /I "%INSTALL_DIR:~-7%"=="scripts" set "INSTALL_DIR=%INSTALL_DIR%\.."
for %%I in ("%INSTALL_DIR%") do set "INSTALL_DIR=%%~fI"

echo Install folder:
echo   %INSTALL_DIR%
echo.
echo This installs Python packages into .venv in this folder only.
echo No files are copied elsewhere. The server uses HTTP on localhost.
echo.

if not exist "%INSTALL_DIR%\builtin_server.py" (
    echo [ERROR] builtin_server.py not found.
    echo Run INSTALL.cmd from inside the extracted Comparison Tool folder.
    pause
    exit /b 1
)
if not exist "%INSTALL_DIR%\requirements.txt" (
    echo [ERROR] requirements.txt not found.
    pause
    exit /b 1
)

echo [1/5] Locating Python 3.10+...
set "PY_EXE="
for %%C in (py -3 py -3.14 py -3.13 py -3.12 py -3.11 py -3.10 python python3) do (
    if not defined PY_EXE (
        for /f "delims=" %%P in ('%%C -c "import sys; v=sys.version_info; print(sys.executable) if v>=(3,10) else ''" 2^>nul') do (
            if not "%%P"=="" set "PY_EXE=%%P"
        )
    )
)
if not defined PY_EXE (
    echo [ERROR] Python 3.10+ not found.
    echo Install from https://www.python.org/downloads/ and enable Add Python to PATH.
    pause
    exit /b 1
)
echo        Using: %PY_EXE%

echo [2/5] Creating virtual environment...
if not exist "%INSTALL_DIR%\.venv\Scripts\python.exe" (
    "%PY_EXE%" -m venv "%INSTALL_DIR%\.venv"
    if errorlevel 1 (
        echo [ERROR] venv creation failed.
        pause
        exit /b 1
    )
) else (
    echo        Reusing existing .venv
)

set "VENV_PY=%INSTALL_DIR%\.venv\Scripts\python.exe"

echo [3/5] Installing packages (clean install, no pip cache)...
REM Avoid confusing "Cache entry deserialization failed" warnings from a corrupt pip cache.
set "PIP_NO_CACHE_DIR=1"
set "PIP_DISABLE_PIP_VERSION_CHECK=1"
"%VENV_PY%" -m pip install --no-cache-dir --upgrade pip setuptools wheel -q
if errorlevel 1 (
    echo [WARN] pip upgrade had issues - continuing with package install...
)
"%VENV_PY%" -m pip install --no-cache-dir -r "%INSTALL_DIR%\requirements.txt"
if errorlevel 1 (
    echo [ERROR] Package install failed. Check network/proxy or ask your team lead for offline wheels.
    pause
    exit /b 1
)

echo [4/5] Creating uninstaller...
if exist "%INSTALL_DIR%\scripts\write_uninstall_bat.ps1" (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%INSTALL_DIR%\scripts\write_uninstall_bat.ps1" -InstallDir "%INSTALL_DIR%"
) else (
    echo [WARN] scripts\write_uninstall_bat.ps1 not found.
)

echo Comparison Tool - install manifest > "%INSTALL_DIR%\INSTALL_MANIFEST.txt"
echo Developed by: Vinayak Srivastava >> "%INSTALL_DIR%\INSTALL_MANIFEST.txt"
echo Install folder: %INSTALL_DIR% >> "%INSTALL_DIR%\INSTALL_MANIFEST.txt"
echo Installed: %DATE% %TIME% >> "%INSTALL_DIR%\INSTALL_MANIFEST.txt"
echo Mode: localhost HTTP only >> "%INSTALL_DIR%\INSTALL_MANIFEST.txt"
echo Launch: launch_comparison_tool.bat >> "%INSTALL_DIR%\INSTALL_MANIFEST.txt"
echo URL: http://localhost:5000/crm >> "%INSTALL_DIR%\INSTALL_MANIFEST.txt"

echo [5/5] Locking application files (read-only)...
if not exist "%INSTALL_DIR%\logs" mkdir "%INSTALL_DIR%\logs"
if not exist "%INSTALL_DIR%\temp_compare" mkdir "%INSTALL_DIR%\temp_compare"
if exist "%INSTALL_DIR%\scripts\apply_distribution_readonly.ps1" (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%INSTALL_DIR%\scripts\apply_distribution_readonly.ps1" -InstallDir "%INSTALL_DIR%"
) else (
    echo [WARN] scripts\apply_distribution_readonly.ps1 not found - skipping read-only lock.
)

echo.
echo Installation complete.
echo   Launch:    %INSTALL_DIR%\launch_comparison_tool.bat
echo   URL:       http://localhost:5000/crm
echo   Stop:      %INSTALL_DIR%\STOP_SERVER.bat
echo.
exit /b 0
