#!/usr/bin/env python3
"""Capture live CRM runbook screenshots from http://127.0.0.1:5000/crm."""
from __future__ import annotations

import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = PROJECT_ROOT / "docs" / "crm_runbook_screenshots"
CAPTURE_DIR = PROJECT_ROOT / "docs" / "crm_runbook_capture"

OLD_PATH = (
    r"\\dvciisilon02\cm_share2\v10.24.1\v10.24.1_67\SDK\Output"
    r"\SDK_CRM_10.24.1.0_linux64_20260508_1421.jar"
)
NEW_PATH = (
    r"\\dvciisilon02\cm_share2\v10.24.1\v10.24.1_68\SDK\Output"
    r"\SDK_CRM_10.24.1.0_linux64_20260608_0939.jar"
)
BASE_URL = "http://127.0.0.1:5000/crm"
SERVER_ROOT = "http://127.0.0.1:5000"


def _shot(driver, name: str) -> None:
    path = OUT_DIR / name
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    driver.set_window_size(1400, 1000)
    time.sleep(0.6)
    driver.save_screenshot(str(path))
    print(f"Saved {path}")


def _click_id(driver, element_id: str) -> None:
    el = driver.find_element("id", element_id)
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
    time.sleep(0.2)
    el.click()


def _fill_name(driver, name: str, value: str) -> None:
    el = driver.find_element("css selector", f'input[name="{name}"]')
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
    el.clear()
    el.send_keys(value)


def _dismiss_intro(driver) -> None:
    for _ in range(3):
        try:
            btns = driver.find_elements(
                "xpath",
                "//button[contains(.,'OK') or contains(.,'Got it') or contains(.,'Continue')]",
            )
            for btn in btns:
                if btn.is_displayed():
                    btn.click()
                    time.sleep(0.4)
                    return
        except Exception:
            pass
        time.sleep(0.3)


def _show_progress_demo(driver) -> None:
    driver.execute_script(
        """
        var c = document.getElementById('progress-container');
        if (c) {
          c.style.display = 'block';
          var bar = document.getElementById('progress-bar');
          var txt = document.getElementById('progress-text');
          var st = document.getElementById('progress-status');
          var files = document.getElementById('progress-files');
          var cur = document.getElementById('progress-current');
          if (bar) bar.style.width = '42%';
          if (txt) txt.textContent = '42%';
          if (st) st.textContent = 'Comparing archive contents...';
          if (files) files.textContent = '184 / 438';
          if (cur) cur.textContent = 'SDK_CRM_10.24.1.0_linux64_20260608_0939.jar::crm/CRM_10.24/...';
        }
        """
    )


def main() -> int:
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait
    except ImportError:
        print("Install selenium: pip install selenium")
        return 1

    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--window-size=1400,1000")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--force-device-scale-factor=1")

    try:
        driver = webdriver.Chrome(options=opts)
    except Exception as exc:
        print(f"Chrome WebDriver failed: {exc}")
        return 1

    wait = WebDriverWait(driver, 20)
    captured_live_report = False
    try:
        driver.get(BASE_URL)
        wait.until(EC.presence_of_element_located((By.ID, "build_compare_toggle")))
        _dismiss_intro(driver)
        _shot(driver, "step-04-open-tool-url.png")

        _click_id(driver, "build_compare_toggle")
        time.sleep(0.5)
        _shot(driver, "step-05-enable-build-comparison.png")

        _click_id(driver, "app_crm")
        time.sleep(0.5)
        _shot(driver, "step-06-select-crm-application.png")

        _fill_name(driver, "folder1_path", OLD_PATH)
        _shot(driver, "step-07-enter-old-build-path.png")

        _fill_name(driver, "folder2_path", NEW_PATH)
        _shot(driver, "step-08-enter-new-build-path.png")

        _click_id(driver, "btn-crm-base-paths")
        time.sleep(1.5)
        _shot(driver, "step-09-load-crm-base-paths.png")

        driver.execute_script("window.scrollTo(0, document.body.scrollHeight * 0.55);")
        time.sleep(0.4)
        _shot(driver, "step-10-crm-comparison-mode.png")

        try:
            run_btn = driver.find_element(
                By.XPATH, "//button[contains(.,'Run comparison')]"
            )
            driver.execute_script(
                "arguments[0].scrollIntoView({block:'center'});", run_btn
            )
            time.sleep(0.4)
        except Exception:
            pass
        _shot(driver, "step-11-run-comparison.png")

        _show_progress_demo(driver)
        time.sleep(0.4)
        _shot(driver, "step-12-console-output.png")

        driver.get(f"{SERVER_ROOT}/reports")
        time.sleep(1.0)
        _shot(driver, "step-14-save-report.png")

        driver.get(BASE_URL)
        wait.until(EC.presence_of_element_located((By.ID, "env_path_toggle")))
        _dismiss_intro(driver)
        _click_id(driver, "env_path_toggle")
        time.sleep(0.6)
        _shot(driver, "step-15-environment-validation.png")

        driver.get(f"{SERVER_ROOT}/reports")
        time.sleep(1.0)
        links = driver.find_elements(By.CSS_SELECTOR, "a[href*='.html'], a[href*='/report']")
        for link in links:
            href = link.get_attribute("href") or ""
            if "report" in href.lower() or href.endswith(".html"):
                driver.get(href)
                time.sleep(1.2)
                _shot(driver, "step-13-review-report.png")
                captured_live_report = True
                break
        if not captured_live_report:
            print("No live report found — using CRM report capture HTML for step 13")

    finally:
        driver.quit()

    static = ["step-01-explorer-access", "step-02-baseline-paths", "step-03-start-server", "step-16-stop-server"]
    if not captured_live_report:
        static.append("step-13-review-report")
    _capture_static_html(static)
    return 0


def _capture_static_html(stems: list[str]) -> None:
    """Screenshot selected mock HTML pages without overwriting live UI captures."""
    import subprocess

    ps1 = PROJECT_ROOT / "scripts" / "_capture_html_pages.ps1"
    ps1.write_text(
        """
param([string[]]$Stems, [string]$CaptureDir, [string]$OutDir)
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
function Save-HtmlScreenshot($HtmlPath, $PngPath, $Width, $Height) {
    $form = New-Object System.Windows.Forms.Form
    $form.Width = $Width + 16; $form.Height = $Height + 40
    $form.ShowInTaskbar = $false
    $form.StartPosition = "Manual"
    $form.Location = New-Object System.Drawing.Point(-2000, -2000)
    $browser = New-Object System.Windows.Forms.WebBrowser
    $browser.Width = $Width; $browser.Height = $Height
    $browser.ScriptErrorsSuppressed = $true
    $form.Controls.Add($browser) | Out-Null
    $uri = [Uri]("file:///" + ($HtmlPath -replace '\\\\', '/'))
    $done = $false
    $browser.Add_DocumentCompleted({ param($s,$e) if ($e.Url.AbsoluteUri -eq $uri.AbsoluteUri) { $script:done = $true } })
    $form.Show() | Out-Null
    $browser.Navigate($uri)
    $sw = [Diagnostics.Stopwatch]::StartNew()
    while (-not $done -and $sw.ElapsedMilliseconds -lt 15000) { [System.Windows.Forms.Application]::DoEvents(); Start-Sleep -Milliseconds 80 }
    Start-Sleep -Milliseconds 300
    $bmp = New-Object System.Drawing.Bitmap $Width, $Height
    $browser.DrawToBitmap($bmp, (New-Object System.Drawing.Rectangle 0, 0, $Width, $Height))
    $bmp.Save($PngPath, [System.Drawing.Imaging.ImageFormat]::Png)
    $bmp.Dispose(); $form.Close(); $form.Dispose()
}
foreach ($stem in $Stems) {
    $html = Join-Path $CaptureDir ($stem + '.html')
    $png = Join-Path $OutDir ($stem + '.png')
    if (Test-Path $html) { Save-HtmlScreenshot $html $png 1100 720; Write-Host "Static $stem" }
}
""",
        encoding="utf-8",
    )
    subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Sta",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(ps1),
            "-Stems",
            *stems,
            "-CaptureDir",
            str(CAPTURE_DIR),
            "-OutDir",
            str(OUT_DIR),
        ],
        cwd=str(PROJECT_ROOT),
        check=False,
    )


def _write_report_capture_html(path: Path) -> None:
    pass  # file created in docs/crm_runbook_capture/step-13-review-report.html


if __name__ == "__main__":
    raise SystemExit(main())
