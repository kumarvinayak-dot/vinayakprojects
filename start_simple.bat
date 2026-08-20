@echo off
echo ========================================================================
echo Starting ASCF Server (Simple Mode - No Auto-Cleanup)
echo ========================================================================
echo.
echo Starting on 10.19.164.180:8080...
echo.
py builtin_server.py --host 10.19.164.180 --port 8080 --skip-demos
pause

