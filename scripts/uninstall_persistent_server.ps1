# Remove persistent CRM Compare scheduled task.
#Requires -RunAsAdministrator
param(
    [int]$HttpPort = 5000,
    [int]$HttpsPort = 5443
)

$ErrorActionPreference = "Stop"
$TaskName = "CRMCompareToolServer"

Write-Host "Stopping scheduled task '$TaskName' ..."
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($task) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "[OK] Removed scheduled task '$TaskName'." -ForegroundColor Green
} else {
    Write-Host "[INFO] Scheduled task '$TaskName' was not installed." -ForegroundColor Yellow
}

foreach ($p in @($HttpPort, $HttpsPort)) {
    Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue |
        ForEach-Object {
            Write-Host "Stopping PID $($_.OwningProcess) on port $p ..."
            Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue
        }
}

Write-Host "Done."
