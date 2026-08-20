@echo off
REM ============================================================================
REM Fastest ASCF Comparison - File Level Only
REM ============================================================================

echo ============================================================================
echo ASCF FOLDER COMPARISON - FASTEST MODE (FILE-LEVEL ONLY)
echo ============================================================================
echo.
echo This script skips deep archive analysis for maximum speed
echo Expected time: 2-5 minutes
echo.
echo Comparing:
echo   Folder 1: \\INPNQASCFLB02\rel\10241\uif\ASCF_HF_10240pb01hf06
echo   Folder 2: \\INPNQASCFLB02\rel\10241\uif\ASCF_HF_10240pb01hf07
echo.
echo Mode: Fast (--no-deep-archive-compare)
echo Note: Will show which files differ, but not what's inside archives
echo.
pause

py main.py --custom-folders ^
  --folder1 "\\INPNQASCFLB02\rel\10241\uif\ASCF_HF_10240pb01hf06" ^
  --folder2 "\\INPNQASCFLB02\rel\10241\uif\ASCF_HF_10240pb01hf07" ^
  --no-deep-archive-compare ^
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
echo This was a file-level comparison only.
echo For deep archive analysis, use compare_ascf_fast.bat instead.
echo.
pause

