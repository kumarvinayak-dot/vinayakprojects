@echo off
title ASCF Server Manager
cd /d "%~dp0"
powershell -ExecutionPolicy Bypass -File "server_manager.ps1"


