# Generate CRM build verification runbook PNGs from docs/crm_build_verification_capture/*.html
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$ProjectRoot = Split-Path $PSScriptRoot -Parent
$CaptureDir = Join-Path $ProjectRoot "docs\crm_build_verification_capture"
$OutDir = Join-Path $ProjectRoot "docs\crm_build_verification_screenshots"
$CrmDir = Join-Path $ProjectRoot "docs\crm_runbook_screenshots"

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

function Save-HtmlScreenshot {
    param([string]$HtmlPath, [string]$PngPath, [int]$Width = 1100, [int]$Height = 720)
    $form = New-Object System.Windows.Forms.Form
    $form.Width = $Width + 16
    $form.Height = $Height + 40
    $form.ShowInTaskbar = $false
    $form.StartPosition = "Manual"
    $form.Location = New-Object System.Drawing.Point(-2000, -2000)
    $browser = New-Object System.Windows.Forms.WebBrowser
    $browser.Width = $Width
    $browser.Height = $Height
    $browser.ScriptErrorsSuppressed = $true
    $form.Controls.Add($browser) | Out-Null
    $uri = [Uri]("file:///" + ($HtmlPath -replace '\\', '/'))
    $done = $false
    $browser.Add_DocumentCompleted({
        param($sender, $e)
        if ($e.Url.AbsoluteUri -eq $uri.AbsoluteUri) { $script:done = $true }
    })
    $form.Show() | Out-Null
    $browser.Navigate($uri)
    $sw = [Diagnostics.Stopwatch]::StartNew()
    while (-not $done -and $sw.ElapsedMilliseconds -lt 15000) {
        [System.Windows.Forms.Application]::DoEvents()
        Start-Sleep -Milliseconds 80
    }
    Start-Sleep -Milliseconds 400
    $bmp = New-Object System.Drawing.Bitmap $Width, $Height
    $browser.DrawToBitmap($bmp, (New-Object System.Drawing.Rectangle 0, 0, $Width, $Height))
    $bmp.Save($PngPath, [System.Drawing.Imaging.ImageFormat]::Png)
    $bmp.Dispose()
    $form.Close()
    $form.Dispose()
}

$captures = Get-ChildItem $CaptureDir -Filter "*.html" | Sort-Object Name
foreach ($html in $captures) {
    $out = Join-Path $OutDir ($html.BaseName + ".png")
    Write-Host "Capture $($html.Name) -> $out"
    Save-HtmlScreenshot -HtmlPath $html.FullName -PngPath $out
}

$reuse = @{
    "step-02-enable-build-comparison" = "step-05-enable-build-comparison.png"
    "step-03-select-crm-application" = "step-06-select-crm-application.png"
    "step-07-full-content-mode" = "step-10-crm-comparison-mode.png"
    "step-08-run-comparison" = "step-11-run-comparison.png"
}
foreach ($pair in $reuse.GetEnumerator()) {
    $src = Join-Path $CrmDir $pair.Value
    $dst = Join-Path $OutDir ($pair.Key + ".png")
    if (Test-Path $src) {
        Copy-Item $src $dst -Force
        Write-Host "Reuse $($pair.Value) -> $dst"
    }
}

Write-Host "Done. Screenshots in $OutDir"
