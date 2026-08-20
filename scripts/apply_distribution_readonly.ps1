# Mark application source as read-only so users can run the tool but not edit shipped code.
# Writable at runtime: .venv, logs, temp_compare, __pycache__, .ssl, install manifest.
param(
    [Parameter(Mandatory = $true)]
    [string]$InstallDir
)

$ErrorActionPreference = "Stop"

$root = (Resolve-Path -LiteralPath $InstallDir).Path
$writableDirNames = @(
    ".venv", "logs", "temp_compare", "__pycache__", ".ssl",
    "build_installer", "dist", "build", "agent-transcripts", "mcps", ".cursor"
)
$writableLeafNames = @(
    "INSTALL_MANIFEST.txt"
)

function Test-WritableRelativePath {
    param([string]$RelativePath)
    $rel = ($RelativePath -replace "\\", "/").TrimStart("./")
    if (-not $rel) { return $true }
    $parts = $rel -split "/"
    foreach ($name in $writableDirNames) {
        if ($parts[0] -eq $name) { return $true }
        if ($rel -like "$name/*") { return $true }
    }
    $leaf = Split-Path -Leaf $rel
    foreach ($allow in $writableLeafNames) {
        if ($leaf -ieq $allow) { return $true }
    }
    if ($leaf -like "Uninstall *.bat") { return $true }
    return $false
}

$locked = 0
$skipped = 0
Get-ChildItem -LiteralPath $root -Recurse -Force -File | ForEach-Object {
    $rel = $_.FullName.Substring($root.Length).TrimStart("\")
    if (Test-WritableRelativePath $rel) {
        $_.IsReadOnly = $false
        $skipped += 1
        return
    }
    if (-not $_.IsReadOnly) {
        $_.IsReadOnly = $true
    }
    $locked += 1
}

Write-Host "       Read-only lock: $locked file(s) protected; $skipped runtime file(s) left writable." -ForegroundColor Green
