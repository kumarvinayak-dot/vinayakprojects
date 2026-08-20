# Build Comparison Tool.zip (ZIP-only distribution — no self-extracting EXE).
$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path $PSScriptRoot -Parent
$PackageName = "Comparison Tool"
$BuildDir = Join-Path $ProjectDir "build_installer"
$PackageDir = Join-Path $BuildDir $PackageName
$DistDir = Join-Path $ProjectDir "dist"
$DistZip = Join-Path $DistDir "$PackageName.zip"
$LegacyExe = Join-Path $DistDir "$PackageName.exe"

$ExcludeDirs = @(
    ".venv", ".git", "__pycache__", "build", "dist", "logs", "temp_compare",
    "agent-transcripts", "mcps", ".cursor", "release_staging", "build_iexpress",
    "build_installer", "tmp_cfr_diff", "flask_wheels", "_setup_test", ".ssl"
)

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  Build Comparison Tool.zip (ZIP-only)" -ForegroundColor Cyan
Write-Host "  Developed by Vinayak Srivastava" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""

Write-Host "[1/5] Staging application payload..."
if (Test-Path $BuildDir) { Remove-Item -Recurse -Force $BuildDir }
New-Item -ItemType Directory -Path $PackageDir -Force | Out-Null

Get-ChildItem -Path $ProjectDir -Force | ForEach-Object {
    if ($ExcludeDirs -contains $_.Name) { return }
    if ($_.Name -eq "Comparison Tool.exe") { return }
    if ($_.Name -eq "Comparison Tool.zip") { return }
    if ($_.Name -like "*.pyc") { return }
    if ($_.Name -eq ".env") { return }
    $dest = Join-Path $PackageDir $_.Name
    if ($_.PSIsContainer) {
        Copy-Item -Path $_.FullName -Destination $dest -Recurse -Force
    } else {
        Copy-Item -Path $_.FullName -Destination $dest -Force
    }
}

foreach ($rootBat in @("INSTALL.cmd", "setup.cmd", "launch_comparison_tool.bat")) {
    $srcBat = Join-Path $ProjectDir $rootBat
    if (-not (Test-Path $srcBat)) {
        throw "Build failed - missing $rootBat at project root."
    }
    Copy-Item $srcBat (Join-Path $PackageDir $rootBat) -Force
}
Copy-Item (Join-Path $ProjectDir "INSTALL_INSTRUCTIONS.md") (Join-Path $PackageDir "INSTALL_INSTRUCTIONS.md") -Force
Copy-Item (Join-Path $ProjectDir "INSTALL_INSTRUCTIONS.html") (Join-Path $PackageDir "INSTALL_INSTRUCTIONS.html") -Force

foreach ($runtimeDir in @("logs", "temp_compare")) {
    $runtimePath = Join-Path $PackageDir $runtimeDir
    New-Item -ItemType Directory -Path $runtimePath -Force | Out-Null
}

Write-Host "[2/5] Applying read-only protection to application source..."
$readonlyScript = Join-Path $ProjectDir "scripts\apply_distribution_readonly.ps1"
if (-not (Test-Path $readonlyScript)) {
    throw "Build failed - missing scripts\apply_distribution_readonly.ps1"
}
& $readonlyScript -InstallDir $PackageDir

Write-Host "[3/5] Creating distribution ZIP..."
New-Item -ItemType Directory -Path $DistDir -Force | Out-Null
if (Test-Path $DistZip) { Remove-Item -Force $DistZip }
if (Test-Path $LegacyExe) {
    Remove-Item -Force $LegacyExe
    Write-Host "       Removed legacy EXE installer from dist\" -ForegroundColor Yellow
}

$zipParent = Split-Path $DistZip -Parent
$zipLeaf = Split-Path $DistZip -Leaf
Push-Location $zipParent
try {
    Compress-Archive -Path (Join-Path $BuildDir $PackageName) -DestinationPath $zipLeaf -Force
} finally {
    Pop-Location
}

if (-not (Test-Path $DistZip)) {
    throw "Build failed - $DistZip not found."
}

Write-Host "[4/5] Verifying package contents..."
$required = @(
    "INSTALL.cmd", "setup.cmd", "launch_comparison_tool.bat",
    "builtin_server.py", "requirements.txt", "start_server.bat",
    "INSTALL_INSTRUCTIONS.md", "INSTALL_INSTRUCTIONS.html",
    "scripts\install_packages.cmd",
    "scripts\apply_distribution_readonly.ps1"
)
foreach ($name in $required) {
    $path = Join-Path $PackageDir $name
    if (-not (Test-Path $path)) {
        throw "Package verification failed - missing $name"
    }
}

Write-Host "[5/5] Done." -ForegroundColor Green
$sizeMb = [math]::Round((Get-Item $DistZip).Length / 1MB, 1)
Write-Host ""
Write-Host "  Output ZIP: $DistZip ($sizeMb MB)" -ForegroundColor Cyan
Write-Host ""
Write-Host "  User steps:"
Write-Host "    1. Extract Comparison Tool.zip"
Write-Host "    2. Double-click INSTALL.cmd (one time)"
Write-Host "    3. Double-click launch_comparison_tool.bat (daily)"
Write-Host ""
Write-Host "  Distribute: Comparison Tool.zip (includes INSTALL_INSTRUCTIONS.md + .html)"
Write-Host ""
