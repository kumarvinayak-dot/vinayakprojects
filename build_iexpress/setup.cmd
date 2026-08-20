@echo off

setlocal EnableDelayedExpansion

title Comparison Tool - Setup

echo.

echo ============================================================

echo   Comparison Tool - Setup (localhost only)

echo   Developed by Vinayak Srivastava

echo ============================================================

echo.



set "SCRIPT_DIR=%~dp0"

set "DEFAULT_DIR=%USERPROFILE%\Desktop\Comparison_Tool"

set "INSTALL_DIR=%DEFAULT_DIR%"



echo Default install folder:

echo   %DEFAULT_DIR%

echo.

set /p CUSTOM_DIR=Press Enter to accept, or type another folder path: 

if not "%CUSTOM_DIR%"=="" set "INSTALL_DIR=%CUSTOM_DIR%"



echo.

echo [1/5] Locating Python 3.10+...

set "PY_EXE="

for %%C in (py -3 py -3.11 py -3.10 python python3) do (

    if not defined PY_EXE (

        for /f "delims=" %%P in ('%%C -c "import sys; print(sys.executable)" 2^>nul') do set "PY_EXE=%%P"

    )

)

if not defined PY_EXE (

    echo [ERROR] Python 3.10+ not found. Install from python.org and check Add Python to PATH.

    pause

    exit /b 1

)

echo        Using: %PY_EXE%



echo [2/5] Preparing application files...

set "TEMP_PAYLOAD=%TEMP%\Comparison_Tool_Payload_%RANDOM%"

if exist "%SCRIPT_DIR%payload.zip" (

    if exist "%TEMP_PAYLOAD%" rmdir /S /Q "%TEMP_PAYLOAD%"

    mkdir "%TEMP_PAYLOAD%"

    powershell -NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -LiteralPath '%SCRIPT_DIR%payload.zip' -DestinationPath '%TEMP_PAYLOAD%' -Force"

    if errorlevel 1 (

        echo [ERROR] Failed to extract payload.zip

        pause

        exit /b 1

    )

    set "SOURCE_DIR=%TEMP_PAYLOAD%"

) else (

    set "SOURCE_DIR=%SCRIPT_DIR%"

)



echo [3/5] Copying to %INSTALL_DIR% ...

if not exist "%INSTALL_DIR%" mkdir "%INSTALL_DIR%"

robocopy "%SOURCE_DIR%" "%INSTALL_DIR%" /E /XD .venv __pycache__ .git /XF *.pyc payload.zip iexpress_setup.cmd /NFL /NDL /NJH /NJS /nc /ns /np

if errorlevel 8 (

    echo [ERROR] File copy failed.

    if exist "%TEMP_PAYLOAD%" rmdir /S /Q "%TEMP_PAYLOAD%" 2>nul

    pause

    exit /b 1

)

if exist "%TEMP_PAYLOAD%" rmdir /S /Q "%TEMP_PAYLOAD%" 2>nul



echo [4/5] Creating virtual environment and installing packages...

if not exist "%INSTALL_DIR%\.venv\Scripts\python.exe" (

    "%PY_EXE%" -m venv "%INSTALL_DIR%\.venv"

    if errorlevel 1 (

        echo [ERROR] venv creation failed.

        pause

        exit /b 1

    )

)

"%INSTALL_DIR%\.venv\Scripts\python.exe" -m pip install --upgrade pip -q

if exist "%INSTALL_DIR%\requirements.txt" (

    "%INSTALL_DIR%\.venv\Scripts\python.exe" -m pip install -r "%INSTALL_DIR%\requirements.txt"

    if errorlevel 1 (

        echo [WARN] pip install had errors - check network/proxy.

    )

)



echo [5/5] Creating uninstaller...

if exist "%INSTALL_DIR%\scripts\write_uninstall_bat.ps1" (

    powershell -NoProfile -ExecutionPolicy Bypass -File "%INSTALL_DIR%\scripts\write_uninstall_bat.ps1" -InstallDir "%INSTALL_DIR%"

) else (

    echo [WARN] scripts\write_uninstall_bat.ps1 not found — Uninstall Comparison Tool.bat was not created.

)



echo Comparison Tool - install manifest > "%INSTALL_DIR%\INSTALL_MANIFEST.txt"

echo Developed by: Vinayak Srivastava >> "%INSTALL_DIR%\INSTALL_MANIFEST.txt"

echo Install folder: %INSTALL_DIR% >> "%INSTALL_DIR%\INSTALL_MANIFEST.txt"

echo Installed: %DATE% %TIME% >> "%INSTALL_DIR%\INSTALL_MANIFEST.txt"

echo Mode: localhost only >> "%INSTALL_DIR%\INSTALL_MANIFEST.txt"

echo Start: run start_server.bat in this folder >> "%INSTALL_DIR%\INSTALL_MANIFEST.txt"

echo URL: http://localhost:5000/crm >> "%INSTALL_DIR%\INSTALL_MANIFEST.txt"



echo.

echo Installation complete.

echo   Developed by Vinayak Srivastava

echo   Folder:    %INSTALL_DIR%

echo   Start:     %INSTALL_DIR%\start_server.bat

echo   Stop:      %INSTALL_DIR%\STOP_SERVER.bat

echo   Uninstall: %INSTALL_DIR%\Uninstall Comparison Tool.bat

echo   URL:       http://localhost:5000/crm

echo.

echo The server runs on this PC only (localhost). No network URLs.

echo.

pause

