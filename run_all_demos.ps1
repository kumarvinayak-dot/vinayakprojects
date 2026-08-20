# ============================================================================
# Run All CRM JAR Comparison Tool Demos and Tests (PowerShell)
# ============================================================================

Write-Host "============================================================================" -ForegroundColor Cyan
Write-Host "CRM JAR COMPARISON TOOL - COMPLETE DEMO" -ForegroundColor Cyan
Write-Host "============================================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "This script will run all the demo and test scripts in sequence."
Write-Host "Press Ctrl+C at any time to cancel."
Write-Host ""
Read-Host "Press Enter to continue"

# Test 1: Normalization Tests
Write-Host ""
Write-Host "============================================================================" -ForegroundColor Yellow
Write-Host "1. TESTING NORMALIZATION PATTERNS" -ForegroundColor Yellow
Write-Host "============================================================================" -ForegroundColor Yellow
Write-Host ""

py test_normalization.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Normalization tests failed!" -ForegroundColor Red
    Read-Host "Press Enter to exit"
    exit 1
}

# Test 2: Nested Path Demo
Write-Host ""
Write-Host "============================================================================" -ForegroundColor Yellow
Write-Host "2. DEMONSTRATING NESTED PATH NORMALIZATION" -ForegroundColor Yellow
Write-Host "============================================================================" -ForegroundColor Yellow
Write-Host ""

py demo_nested_normalization.py

# Test 3: Actual Comparison
Write-Host ""
Write-Host "============================================================================" -ForegroundColor Yellow
Write-Host "3. COMPARING CRM SDK JAR FILES (HFB22 vs HFB21)" -ForegroundColor Yellow
Write-Host "============================================================================" -ForegroundColor Yellow
Write-Host ""
Write-Host "This will compare the actual JAR files from your network share."
Write-Host "Make sure you have network access to \\DVCIISILON01\cm_share3"
Write-Host ""

$continue = Read-Host "Continue with JAR comparison? (Y/N)"
if ($continue -eq "Y" -or $continue -eq "y") {
    py compare_crm_jars.py
    if ($LASTEXITCODE -ne 0) {
        Write-Host "WARNING: Comparison may have encountered issues." -ForegroundColor Yellow
        Write-Host "Check the error messages above." -ForegroundColor Yellow
    }
} else {
    Write-Host "Skipping JAR comparison." -ForegroundColor Gray
}

# Summary
Write-Host ""
Write-Host "============================================================================" -ForegroundColor Green
Write-Host "DEMO COMPLETE" -ForegroundColor Green
Write-Host "============================================================================" -ForegroundColor Green
Write-Host ""
Write-Host "All demos have been executed. Check the output above for results."
Write-Host ""
Write-Host "Generated reports (if comparison ran):" -ForegroundColor Cyan
Get-ChildItem -Path "crm_sdk_comparison_*.txt" -ErrorAction SilentlyContinue | ForEach-Object { Write-Host "  - $($_.Name)" }
Get-ChildItem -Path "crm_sdk_comparison_*.html" -ErrorAction SilentlyContinue | ForEach-Object { Write-Host "  - $($_.Name)" }
Write-Host ""
Write-Host "For more options, see QUICK_START_COMMANDS.txt" -ForegroundColor Cyan
Write-Host ""
Read-Host "Press Enter to exit"

