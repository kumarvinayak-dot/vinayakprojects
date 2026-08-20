# ============================================================================
# ASCF Comparison Tool - Start Server Script (Background Mode)
# ============================================================================
# Usage: Right-click -> "Run with PowerShell" OR run from terminal
# Server runs in background - you can close this window!
# ============================================================================

param(
    [string]$ServerHost = "0.0.0.0",
    [int]$Port = 5001,
    [switch]$SkipDemos = $true
)

# Full path to Python executable (to avoid PATH issues)
$PythonExe = "C:\Users\SVINAYAK\AppData\Local\Programs\Python\Python314\python.exe"
# Fixed project path where builtin_server.py is located
$ProjectPath = "C:\Users\SVINAYAK\python_project"

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "       ASCF Comparison Tool - Server Startup Script         " -ForegroundColor Cyan
Write-Host "              (Background Mode - Window Safe)               " -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""

# Step 1: Check for existing processes on the port
Write-Host "[1/5] Checking for existing processes on port $Port..." -ForegroundColor Yellow
$existingProcesses = netstat -ano | Select-String ":$Port" | Select-String "LISTENING"

if ($existingProcesses) {
    Write-Host "      Found existing process(es) on port $Port" -ForegroundColor Red
    
    # Extract PIDs
    $processPids = @()
    foreach ($line in $existingProcesses) {
        $parts = $line -split '\s+'
        $procId = $parts[-1]
        if ($procId -match '^\d+$' -and $procId -ne "0") {
            $processPids += $procId
        }
    }
    $processPids = $processPids | Select-Object -Unique
    
    if ($processPids.Count -gt 0) {
        Write-Host "      Killing PIDs: $($processPids -join ', ')" -ForegroundColor Yellow
        foreach ($procId in $processPids) {
            try {
                taskkill /F /PID $procId 2>$null | Out-Null
                Write-Host "      -> Killed PID $procId" -ForegroundColor Green
            } catch {
                Write-Host "      -> Failed to kill PID $procId" -ForegroundColor Red
            }
        }
        Write-Host "      Waiting for processes to terminate..." -ForegroundColor Yellow
        Start-Sleep -Seconds 3
    }
} else {
    Write-Host "      No existing processes found on port $Port" -ForegroundColor Green
}

# Step 2: Set environment variables
Write-Host "[2/5] Setting environment variables..." -ForegroundColor Yellow
$env:CFR_JAR = "C:\cfr-0.152.jar"
Write-Host "      CFR_JAR = $env:CFR_JAR" -ForegroundColor Green

# Step 3: Get network IP for display
Write-Host "[3/5] Detecting network configuration..." -ForegroundColor Yellow
$networkIP = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notmatch '^127\.' -and $_.IPAddress -notmatch '^169\.' } | Select-Object -First 1).IPAddress
if (-not $networkIP) {
    $networkIP = "Unable to detect"
}

# Step 4: Build the command arguments
Write-Host "[4/5] Preparing server launch..." -ForegroundColor Yellow
$scriptArgs = "builtin_server.py --host $ServerHost --port $Port"
if ($SkipDemos) {
    $scriptArgs += " --skip-demos"
}

# Step 5: Start the server in BACKGROUND
Write-Host "[5/5] Starting server in background mode..." -ForegroundColor Yellow
Write-Host "      Using Python: $PythonExe" -ForegroundColor Gray
Write-Host ""

# Start server as a background process that survives window close
$processInfo = Start-Process -FilePath $PythonExe `
    -ArgumentList $scriptArgs `
    -WorkingDirectory $ProjectPath `
    -WindowStyle Hidden `
    -PassThru

# Wait a moment for the server to start
Start-Sleep -Seconds 3

# Check if server started successfully
$serverRunning = netstat -ano | Select-String ":$Port" | Select-String "LISTENING"

Write-Host "============================================================" -ForegroundColor Green
Write-Host "  Server Configuration:" -ForegroundColor Green
Write-Host "    - Host: $ServerHost" -ForegroundColor White
Write-Host "    - Port: $Port" -ForegroundColor White
Write-Host "    - Process ID: $($processInfo.Id)" -ForegroundColor White
Write-Host "    - Mode: BACKGROUND (window-safe)" -ForegroundColor Cyan
Write-Host "" -ForegroundColor White
Write-Host "  Access URLs:" -ForegroundColor Green
Write-Host "    - Local:   http://localhost:$Port" -ForegroundColor Cyan
Write-Host "    - Network: http://${networkIP}:$Port" -ForegroundColor Cyan
Write-Host "" -ForegroundColor White

if ($serverRunning) {
    Write-Host "  [SUCCESS] Server is running!" -ForegroundColor Green
} else {
    Write-Host "  [INFO] Server is starting... wait a few seconds" -ForegroundColor Yellow
}

Write-Host "" -ForegroundColor White
Write-Host "  You can CLOSE this window - server will keep running!" -ForegroundColor Cyan
Write-Host "" -ForegroundColor White
Write-Host "  To STOP the server:" -ForegroundColor Yellow
Write-Host "    - Run stop_server.ps1 (or .bat)" -ForegroundColor White
Write-Host "    - Or: taskkill /F /PID $($processInfo.Id)" -ForegroundColor White
Write-Host "    - Or: Use Task Manager to end Python process" -ForegroundColor White
Write-Host "============================================================" -ForegroundColor Green
Write-Host ""

Write-Host "This window will close in 10 seconds..." -ForegroundColor Gray
Start-Sleep -Seconds 10
