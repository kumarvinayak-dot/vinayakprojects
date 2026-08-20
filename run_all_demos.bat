@echo off
REM ============================================================================
REM Run All CRM JAR Comparison Tool Demos and Tests
REM ============================================================================

echo ============================================================================
echo CRM JAR COMPARISON TOOL - COMPLETE DEMO
echo ============================================================================
echo.
echo This script will run all the demo and test scripts in sequence.
echo Press Ctrl+C at any time to cancel.
echo.
pause

echo.
echo ============================================================================
echo 1. TESTING NORMALIZATION PATTERNS
echo ============================================================================
echo.
py test_normalization.py
if errorlevel 1 (
    echo ERROR: Normalization tests failed!
    pause
    exit /b 1
)

echo.
echo ============================================================================
echo 2. DEMONSTRATING NESTED PATH NORMALIZATION
echo ============================================================================
echo.
py demo_nested_normalization.py

echo.
echo ============================================================================
echo 3. COMPARING CRM SDK JAR FILES (HFB22 vs HFB21)
echo ============================================================================
echo.
echo This will compare the actual JAR files from your network share.
echo Make sure you have network access to \\DVCIISILON01\cm_share3
echo.
set /p continue="Continue with JAR comparison? (Y/N): "
if /i "%continue%" NEQ "Y" goto skip_comparison

py compare_crm_jars.py
if errorlevel 1 (
    echo WARNING: Comparison may have encountered issues.
    echo Check the error messages above.
)

:skip_comparison

echo.
echo ============================================================================
echo DEMO COMPLETE
echo ============================================================================
echo.
echo All demos have been executed. Check the output above for results.
echo.
echo Generated reports (if comparison ran):
dir crm_sdk_comparison_*.txt 2>nul
dir crm_sdk_comparison_*.html 2>nul
echo.
echo For more options, see QUICK_START_COMMANDS.txt
echo.
pause

