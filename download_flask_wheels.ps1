# ================================================================================
# Flask Wheel Downloader Script
# Run this script on a computer WITH internet access
# Then copy the flask_wheels folder to your offline machine
# ================================================================================

Write-Host "Flask Wheel Downloader" -ForegroundColor Cyan
Write-Host "======================" -ForegroundColor Cyan
Write-Host ""

# Create output directory
$outputDir = ".\flask_wheels_download"
if (Test-Path $outputDir) {
    Remove-Item $outputDir -Recurse -Force
}
New-Item -ItemType Directory -Path $outputDir | Out-Null

Write-Host "Downloading Flask and dependencies..." -ForegroundColor Yellow
Write-Host ""

# Download Flask and all dependencies
py -m pip download flask -d $outputDir

if ($LASTEXITCODE -eq 0) {
    Write-Host ""
    Write-Host "SUCCESS! Downloaded files:" -ForegroundColor Green
    Get-ChildItem $outputDir -Filter "*.whl" | ForEach-Object { Write-Host "  - $($_.Name)" }
    Write-Host ""
    Write-Host "Total files: $((Get-ChildItem $outputDir -Filter '*.whl').Count)" -ForegroundColor Green
    Write-Host ""
    Write-Host "NEXT STEPS:" -ForegroundColor Cyan
    Write-Host "1. Copy the 'flask_wheels_download' folder to a USB drive"
    Write-Host "2. On the offline machine, copy the .whl files to:"
    Write-Host "   C:\Users\SVINAYAK\python_project\flask_wheels\"
    Write-Host "3. Run: install_flask_from_wheels.bat"
} else {
    Write-Host "ERROR: Download failed!" -ForegroundColor Red
}

Write-Host ""
Read-Host "Press Enter to exit"

