# ============================================================================
# ASCF Comparison Tool - Server Status Script
# ============================================================================
# Usage: Right-click -> "Run with PowerShell" OR run from terminal
# ============================================================================

param(
    [int]$Port = 5000
)

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "       ASCF Comparison Tool - Server Status Check           " -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""

# Check port status
Write-Host "Port $Port Status:" -ForegroundColor Yellow
Write-Host "------------------------------------------------------------" -ForegroundColor Gray
$netstatOutput = netstat -ano | Select-String ":$Port" | Select-String "LISTENING"

if ($netstatOutput) {
    Write-Host "  [ACTIVE] Server is RUNNING on port $Port" -ForegroundColor Green
    Write-Host ""
    
    # Extract PIDs and show details
    $pids = @()
    foreach ($line in $netstatOutput) {
        $parts = ($line.ToString().Trim()) -split '\s+'
        $pid = $parts[-1]
        if ($pid -match '^\d+$' -and $pid -ne "0") {
            $pids += [int]$pid
        }
    }
    $pids = $pids | Select-Object -Unique
    
    Write-Host "  Process Details:" -ForegroundColor Yellow
    foreach ($pid in $pids) {
        try {
            $proc = Get-Process -Id $pid -ErrorAction SilentlyContinue
            if ($proc) {
                $memMB = [math]::Round($proc.WorkingSet64 / 1MB, 2)
                Write-Host "    - PID: $pid" -ForegroundColor White
                Write-Host "      Name: $($proc.ProcessName)" -ForegroundColor Gray
                Write-Host "      Memory: $memMB MB" -ForegroundColor Gray
                Write-Host "      Started: $($proc.StartTime)" -ForegroundColor Gray
            }
        } catch {
            Write-Host "    - PID: $pid (details unavailable)" -ForegroundColor Gray
        }
    }
    
    if ($pids.Count -gt 1) {
        Write-Host ""
        Write-Host "  [WARNING] Multiple processes on same port!" -ForegroundColor Red
        Write-Host "  Consider running stop_server.ps1 and restarting." -ForegroundColor Yellow
    }
} else {
    Write-Host "  [STOPPED] No server running on port $Port" -ForegroundColor Red
}

Write-Host ""
Write-Host "------------------------------------------------------------" -ForegroundColor Gray

# Network Information
Write-Host ""
Write-Host "Network Configuration:" -ForegroundColor Yellow
Write-Host "------------------------------------------------------------" -ForegroundColor Gray

$networkAdapters = Get-NetIPAddress -AddressFamily IPv4 | Where-Object { 
    $_.IPAddress -notmatch '^127\.' -and 
    $_.IPAddress -notmatch '^169\.' -and
    $_.PrefixOrigin -ne 'WellKnown'
}

if ($networkAdapters) {
    foreach ($adapter in $networkAdapters) {
        $ifAlias = (Get-NetIPInterface -InterfaceIndex $adapter.InterfaceIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue).InterfaceAlias
        Write-Host "  Interface: $ifAlias" -ForegroundColor White
        Write-Host "    IP Address: $($adapter.IPAddress)" -ForegroundColor Cyan
        Write-Host "    Access URL: http://$($adapter.IPAddress):$Port" -ForegroundColor Green
        Write-Host ""
    }
} else {
    Write-Host "  No network adapters found with valid IP addresses." -ForegroundColor Red
}

Write-Host "  Localhost URLs:" -ForegroundColor White
Write-Host "    http://localhost:$Port" -ForegroundColor Cyan
Write-Host "    http://127.0.0.1:$Port" -ForegroundColor Cyan

Write-Host ""
Write-Host "------------------------------------------------------------" -ForegroundColor Gray

# Python processes
Write-Host ""
Write-Host "All Python Processes:" -ForegroundColor Yellow
Write-Host "------------------------------------------------------------" -ForegroundColor Gray
$pythonProcesses = Get-Process python -ErrorAction SilentlyContinue
if ($pythonProcesses) {
    Write-Host "  Found $($pythonProcesses.Count) Python process(es):" -ForegroundColor White
    foreach ($proc in $pythonProcesses) {
        $memMB = [math]::Round($proc.WorkingSet64 / 1MB, 2)
        Write-Host "    - PID: $($proc.Id), Memory: $memMB MB" -ForegroundColor Gray
    }
} else {
    Write-Host "  No Python processes running." -ForegroundColor Gray
}

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Press any key to exit..."
$null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")


