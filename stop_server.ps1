# ============================================================================
# ASCF Comparison Tool - Stop Server Script
# ============================================================================

param(
    [int]$Port = 5001
)

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "       ASCF Comparison Tool - Stop Server Script            " -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""

Write-Host "Checking for server process on port $Port..." -ForegroundColor Yellow
$existingProcesses = netstat -ano | Select-String ":$Port" | Select-String "LISTENING"

if ($existingProcesses) {
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
        foreach ($procId in $processPids) {
            Write-Host "Found server running with PID: $procId" -ForegroundColor White
            Write-Host "Stopping..." -ForegroundColor Yellow
            try {
                taskkill /F /PID $procId 2>$null | Out-Null
                Write-Host "[SUCCESS] Server stopped successfully!" -ForegroundColor Green
            } catch {
                Write-Host "[INFO] Process may have already stopped" -ForegroundColor Yellow
            }
        }
    }
} else {
    Write-Host "[INFO] No server found running on port $Port" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Done!" -ForegroundColor Green
Write-Host ""
Start-Sleep -Seconds 3
