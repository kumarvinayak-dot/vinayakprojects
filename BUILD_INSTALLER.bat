@echo off
title Build Comparison Tool.zip
echo.
echo Building Comparison Tool.zip (ZIP-only distribution) ...
echo Developed by Vinayak Srivastava
echo Output: dist\Comparison Tool.zip
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\build_installer.ps1"
echo.
pause
