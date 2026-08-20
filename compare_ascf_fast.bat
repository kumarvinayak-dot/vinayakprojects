@echo off
REM ============================================================================
REM Fast ASCF Comparison with Performance Optimization
REM ============================================================================

echo ============================================================================
echo ASCF FOLDER COMPARISON - OPTIMIZED FOR SPEED
echo ============================================================================
echo.
echo This script uses smart mode: deep archive analysis only for changed files
echo Expected time: 10-20 minutes (vs 60+ minutes in full mode)
echo.
echo Comparing:
echo   Folder 1: \\INPNQASCFLB02\rel\10241\uif\ASCF_HF_10240pb01hf06
echo   Folder 2: \\INPNQASCFLB02\rel\10241\uif\ASCF_HF_10240pb01hf07
echo.
echo Mode: Smart (--deep-compare-if-different)
echo Progress: Enabled (shows which files are being processed)
echo.
pause

py main.py --custom-folders ^
  --folder1 "\\INPNQASCFLB02\rel\10241\uif\ASCF_HF_10240pb01hf06" ^
  --folder2 "\\INPNQASCFLB02\rel\10241\uif\ASCF_HF_10240pb01hf07" ^
  --deep-compare-if-different ^
  --show-all-differences

if errorlevel 1 (
    echo.
    echo ============================================================================
    echo COMPARISON FAILED
    echo ============================================================================
    echo Check the error messages above for details.
    echo.
    pause
    exit /b 1
)

echo.
echo ============================================================================
echo COMPARISON COMPLETE
echo ============================================================================
echo.
echo Check the generated reports for results.
echo.
pause

