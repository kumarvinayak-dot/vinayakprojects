@echo off
echo ================================================================================
echo Starting Folder Comparison Tool (TK GUI - No Flask Required)
echo ================================================================================
echo.
echo This tool uses tkinter (built into Python) - no dependencies needed!
echo.
echo A window will open shortly...
echo ================================================================================
echo.

cd /d %~dp0
py compare_folders_gui.py

if errorlevel 1 (
    echo.
    echo ERROR: Failed to start GUI
    echo Please check if Python is installed correctly
    pause
)











