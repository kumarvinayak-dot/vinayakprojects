@echo off
call "%~dp0scripts\install_packages.cmd"
if errorlevel 1 exit /b 1
echo.
pause
exit /b 0
