@echo off
echo ========================================================================
echo Testing Server Startup
echo ========================================================================
echo.
echo Step 1: Testing on localhost first...
echo.
py builtin_server.py --host 127.0.0.1 --port 8080 --skip-demos
pause

