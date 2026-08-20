param(
    [Parameter(Mandatory = $true)]
    [string]$InstallDir
)

$ErrorActionPreference = "Stop"
$AppName = "Comparison Tool"
$install = $InstallDir.TrimEnd('\')
if (-not (Test-Path -LiteralPath $install)) {
    New-Item -ItemType Directory -Path $install -Force | Out-Null
}
$out = Join-Path $install "Uninstall $AppName.bat"

$content = @"
@echo off
title Uninstall $AppName
echo.
echo This removes the install folder:
echo   $install
echo.
echo Stop the server first (run STOP_SERVER.bat).
echo.
set /p CONFIRM=Type YES to uninstall:
if /I not "%CONFIRM%"=="YES" exit /b 1
call "%~dp0STOP_SERVER.bat" >nul 2>&1
cd /d "%TEMP%"
rmdir /S /Q "$install"
echo Done.
pause
"@

Set-Content -LiteralPath $out -Value $content -Encoding ASCII
Write-Host "Created: $out"
