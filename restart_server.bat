@echo off
echo ========================================================================
echo Restarting ASCF Server on 10.19.164.180:8080
echo ========================================================================
echo.
echo Note: Server will automatically stop any existing instances.
echo.
py builtin_server.py --host 10.19.164.180 --port 8080 --skip-demos
pause

