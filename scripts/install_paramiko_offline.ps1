# Offline install paramiko + dependencies from C:\paramiko_wheels
# Usage: .\.venv\Scripts\Activate.ps1; .\scripts\install_paramiko_offline.ps1

$ErrorActionPreference = "Stop"
$WheelDir = if ($env:PARAMIKO_WHEELS) { $env:PARAMIKO_WHEELS } else { "C:\paramiko_wheels" }
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $Python)) {
    Write-Error "venv Python not found at $Python"
}
if (-not (Test-Path $WheelDir)) {
    Write-Error "Wheel folder not found: $WheelDir"
}

Write-Host "Python:" (& $Python --version)
Write-Host "Installing from: $WheelDir"

function Install-Wheel($pattern, [switch]$NoDeps) {
    $wheel = Get-ChildItem $WheelDir -Filter $pattern | Select-Object -First 1
    if (-not $wheel) {
        Write-Warning "Missing wheel: $pattern"
        return $false
    }
    $args = @("-m", "pip", "install", "--no-index", "--force-reinstall")
    if ($NoDeps) { $args += "--no-deps" }
    $args += $wheel.FullName
    & $Python @args
    if ($LASTEXITCODE -ne 0) { throw "Failed: $($wheel.Name)" }
    Write-Host "OK: $($wheel.Name)"
    return $true
}

Install-Wheel "pycparser-*.whl" | Out-Null

# cffi must match Python version (cp314 on Python 3.14, cp313 on Python 3.13)
$pyVer = (& $Python -c "import sys; print(f'{sys.version_info.major}{sys.version_info.minor}')").Trim()
$cffiWheel = Get-ChildItem $WheelDir -Filter "cffi-*-cp$pyVer-*-win_amd64.whl" | Select-Object -First 1
if (-not $cffiWheel) {
    Write-Error @"
No cffi wheel for Python $pyVer in $WheelDir.
Download on a PC with internet:
  py -3.$($pyVer.Substring(1)) -m pip download cffi -d C:\paramiko_wheels
Or copy plink.exe (from MobaXterm) to $WheelDir\plink.exe for SSH without paramiko.
"@
}
Install-Wheel $cffiWheel.Name -NoDeps | Out-Null

Install-Wheel "cryptography-*.whl" -NoDeps | Out-Null
Install-Wheel "bcrypt-*.whl" -NoDeps | Out-Null
Install-Wheel "pynacl-*.whl" -NoDeps | Out-Null
Install-Wheel "invoke-*.whl" -NoDeps | Out-Null
Install-Wheel "paramiko-*.whl" -NoDeps | Out-Null

Write-Host "Verifying paramiko..."
& $Python -c "import paramiko; print('paramiko', paramiko.__version__, 'ready')"
if ($LASTEXITCODE -ne 0) {
    Write-Warning "paramiko import failed - copy plink.exe to $WheelDir for SSH fallback."
    exit 1
}
Write-Host "Done."
