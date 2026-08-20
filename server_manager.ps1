# ============================================================================
# ASCF Comparison Tool - Server Manager (Interactive Menu)
# ============================================================================
# Usage: Right-click -> "Run with PowerShell" OR run from terminal
# ============================================================================

$ProjectPath = Split-Path -Parent $MyInvocation.MyCommand.Path
$Port = 5001
$PythonExe = "C:\Users\SVINAYAK\AppData\Local\Programs\Python\Python314\python.exe"

function Show-Menu {
    Clear-Host
    Write-Host ""
    Write-Host "============================================================" -ForegroundColor Cyan
    Write-Host "     ASCF Comparison Tool - Server Manager                  " -ForegroundColor Cyan
    Write-Host "============================================================" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "  Project: $ProjectPath" -ForegroundColor Gray
    Write-Host "  Port:    $Port" -ForegroundColor Gray
    Write-Host ""
    
    # Quick status check
    $isRunning = netstat -ano 2>$null | Select-String ":$Port" | Select-String "LISTENING"
    if ($isRunning) {
        Write-Host "  Status:  " -NoNewline -ForegroundColor Gray
        Write-Host "[RUNNING]" -ForegroundColor Green
    } else {
        Write-Host "  Status:  " -NoNewline -ForegroundColor Gray
        Write-Host "[STOPPED]" -ForegroundColor Red
    }
    
    Write-Host ""
    Write-Host "------------------------------------------------------------" -ForegroundColor Gray
    Write-Host ""
    Write-Host "  [1] Start Server" -ForegroundColor Green
    Write-Host "  [2] Stop Server (port $Port only)" -ForegroundColor Red
    Write-Host "  [3] Stop ALL Python processes" -ForegroundColor Red
    Write-Host "  [4] Restart Server" -ForegroundColor Yellow
    Write-Host "  [5] Check Status (detailed)" -ForegroundColor Cyan
    Write-Host "  [6] Open in Browser" -ForegroundColor Magenta
    Write-Host "  [7] Show Network IPs" -ForegroundColor Blue
    Write-Host ""
    Write-Host "  [Q] Quit" -ForegroundColor Gray
    Write-Host ""
    Write-Host "------------------------------------------------------------" -ForegroundColor Gray
    Write-Host ""
}

function Start-Server {
    Write-Host ""
    Write-Host "Starting server..." -ForegroundColor Yellow
    
    # Kill existing processes on port
    $existing = netstat -ano | Select-String ":$Port" | Select-String "LISTENING"
    if ($existing) {
        Write-Host "Stopping existing processes on port $Port..." -ForegroundColor Yellow
        foreach ($line in $existing) {
            $parts = ($line.ToString().Trim()) -split '\s+'
            $pid = $parts[-1]
            if ($pid -match '^\d+$' -and $pid -ne "0") {
                taskkill /F /PID $pid 2>$null | Out-Null
            }
        }
        Start-Sleep -Seconds 2
    }
    
    # Set environment
    $env:CFR_JAR = "C:\cfr-0.152.jar"
    Set-Location $ProjectPath
    
    # Get network IP
    $networkIP = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notmatch '^127\.' -and $_.IPAddress -notmatch '^169\.' } | Select-Object -First 1).IPAddress
    
    Write-Host ""
    Write-Host "============================================================" -ForegroundColor Green
    Write-Host "  Server starting on port $Port" -ForegroundColor Green
    Write-Host "  Local:   http://localhost:$Port" -ForegroundColor Cyan
    Write-Host "  Network: http://${networkIP}:$Port" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "  Press Ctrl+C to stop the server and return to menu" -ForegroundColor Yellow
    Write-Host "============================================================" -ForegroundColor Green
    Write-Host ""
    
    # Start server
    & $PythonExe builtin_server.py --host 0.0.0.0 --port $Port --skip-demos
    
    Write-Host ""
    Write-Host "Server stopped." -ForegroundColor Yellow
    Write-Host "Press any key to return to menu..."
    $null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
}

function Stop-ServerByPort {
    Write-Host ""
    Write-Host "Stopping server on port $Port..." -ForegroundColor Yellow
    
    $existing = netstat -ano | Select-String ":$Port" | Select-String "LISTENING"
    if ($existing) {
        $killed = 0
        foreach ($line in $existing) {
            $parts = ($line.ToString().Trim()) -split '\s+'
            $pid = $parts[-1]
            if ($pid -match '^\d+$' -and $pid -ne "0") {
                taskkill /F /PID $pid 2>$null | Out-Null
                Write-Host "  Killed PID $pid" -ForegroundColor Green
                $killed++
            }
        }
        Write-Host ""
        Write-Host "Stopped $killed process(es)." -ForegroundColor Green
    } else {
        Write-Host "No processes found on port $Port." -ForegroundColor Yellow
    }
    
    Write-Host ""
    Write-Host "Press any key to continue..."
    $null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
}

function Stop-AllPython {
    Write-Host ""
    Write-Host "Stopping ALL Python processes..." -ForegroundColor Yellow
    
    $procs = Get-Process python -ErrorAction SilentlyContinue
    if ($procs) {
        $count = $procs.Count
        $procs | Stop-Process -Force
        Write-Host "Stopped $count Python process(es)." -ForegroundColor Green
    } else {
        Write-Host "No Python processes found." -ForegroundColor Yellow
    }
    
    Write-Host ""
    Write-Host "Press any key to continue..."
    $null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
}

function Show-DetailedStatus {
    Write-Host ""
    Write-Host "Checking detailed status..." -ForegroundColor Yellow
    Write-Host ""
    
    # Port status
    Write-Host "Port $Port Status:" -ForegroundColor Cyan
    Write-Host "------------------------------------------------------------" -ForegroundColor Gray
    $netstatOutput = netstat -ano | Select-String ":$Port" | Select-String "LISTENING"
    
    if ($netstatOutput) {
        Write-Host "  [RUNNING] Server is active on port $Port" -ForegroundColor Green
        foreach ($line in $netstatOutput) {
            $parts = ($line.ToString().Trim()) -split '\s+'
            $pid = $parts[-1]
            if ($pid -match '^\d+$') {
                $proc = Get-Process -Id $pid -ErrorAction SilentlyContinue
                if ($proc) {
                    $memMB = [math]::Round($proc.WorkingSet64 / 1MB, 2)
                    Write-Host "    PID: $pid, Memory: $memMB MB" -ForegroundColor Gray
                }
            }
        }
    } else {
        Write-Host "  [STOPPED] No server on port $Port" -ForegroundColor Red
    }
    
    Write-Host ""
    Write-Host "All Python Processes:" -ForegroundColor Cyan
    Write-Host "------------------------------------------------------------" -ForegroundColor Gray
    $pythonProcs = Get-Process python -ErrorAction SilentlyContinue
    if ($pythonProcs) {
        foreach ($p in $pythonProcs) {
            $memMB = [math]::Round($p.WorkingSet64 / 1MB, 2)
            Write-Host "  PID: $($p.Id), Memory: $memMB MB" -ForegroundColor Gray
        }
    } else {
        Write-Host "  None" -ForegroundColor Gray
    }
    
    Write-Host ""
    Write-Host "Press any key to continue..."
    $null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
}

function Open-InBrowser {
    Write-Host ""
    Write-Host "Opening browser..." -ForegroundColor Yellow
    Start-Process "http://localhost:$Port"
    Write-Host "Opened http://localhost:$Port in default browser." -ForegroundColor Green
    Start-Sleep -Seconds 1
}

function Show-NetworkIPs {
    Write-Host ""
    Write-Host "Network IP Addresses:" -ForegroundColor Cyan
    Write-Host "------------------------------------------------------------" -ForegroundColor Gray
    
    $adapters = Get-NetIPAddress -AddressFamily IPv4 | Where-Object { 
        $_.IPAddress -notmatch '^127\.' -and 
        $_.IPAddress -notmatch '^169\.'
    }
    
    if ($adapters) {
        foreach ($adapter in $adapters) {
            $ifName = (Get-NetAdapter -InterfaceIndex $adapter.InterfaceIndex -ErrorAction SilentlyContinue).Name
            Write-Host ""
            Write-Host "  Interface: $ifName" -ForegroundColor White
            Write-Host "  IP Address: $($adapter.IPAddress)" -ForegroundColor Cyan
            Write-Host "  Share this URL: http://$($adapter.IPAddress):$Port" -ForegroundColor Green
        }
    }
    
    Write-Host ""
    Write-Host "Localhost URLs (for your use):" -ForegroundColor Yellow
    Write-Host "  http://localhost:$Port" -ForegroundColor Cyan
    Write-Host "  http://127.0.0.1:$Port" -ForegroundColor Cyan
    
    Write-Host ""
    Write-Host "Press any key to continue..."
    $null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
}

# Main loop
do {
    Show-Menu
    $choice = Read-Host "Enter your choice"
    
    switch ($choice.ToUpper()) {
        "1" { Start-Server }
        "2" { Stop-ServerByPort }
        "3" { Stop-AllPython }
        "4" { 
            Stop-ServerByPort
            Start-Sleep -Seconds 1
            Start-Server 
        }
        "5" { Show-DetailedStatus }
        "6" { Open-InBrowser }
        "7" { Show-NetworkIPs }
        "Q" { break }
        default { 
            Write-Host "Invalid choice. Press any key to continue..." -ForegroundColor Red
            $null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
        }
    }
} while ($choice.ToUpper() -ne "Q")

Write-Host ""
Write-Host "Goodbye!" -ForegroundColor Cyan


