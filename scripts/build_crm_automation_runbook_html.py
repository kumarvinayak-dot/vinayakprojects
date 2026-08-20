#!/usr/bin/env python3
"""Build self-contained CRM_AUTOMATION_RUNBOOK.html (embedded PNG screenshots)."""

from __future__ import annotations

import base64
import html as html_module
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DOCS = PROJECT_ROOT / "docs"
SHOT_DIR = DOCS / "crm_runbook_screenshots"
OUT_HTML = DOCS / "CRM_AUTOMATION_RUNBOOK.html"

STEPS: list[tuple[str, str, str, str]] = [
    (
        "Step 1",
        "Open the received new CRM build UNC path in Windows Explorer and confirm you have read access.",
        "step-01-explorer-access.png",
        "New build folder example: <code>\\\\dvciisilon02\\cm_share2\\v10.24.1\\v10.24.1_68\\SDK\\Output\\</code>",
    ),
    (
        "Step 2",
        "Note the previous bundle CRM build UNC path to use as the <strong>Old build</strong> baseline for comparison.",
        "step-02-baseline-paths.png",
        "Old: <code>v10.24.1_67</code> SDK JAR · New: <code>v10.24.1_68</code> SDK JAR",
    ),
    (
        "Step 3",
        "Double-click <strong><code>launch_comparison_tool.bat</code></strong> in the Comparison Tool install folder "
        "(or <code>start_server.bat</code>) to start the tool on localhost.",
        "step-03-start-server.png",
        "One-time install: run <code>INSTALL.cmd</code> after extracting <code>Comparison Tool.zip</code>.",
    ),
    (
        "Step 4",
        "Open <code>http://localhost:5000/crm</code> in Chrome or Edge when the browser does not open automatically.",
        "step-04-open-tool-url.png",
        "Dismiss the “Before you compare” dialog if shown. Use localhost only — no network URL.",
    ),
    (
        "Step 5",
        "Check the <strong>Build Comparison</strong> checkbox on the home page.",
        "step-05-enable-build-comparison.png",
        "",
    ),
    (
        "Step 6",
        "Check the <strong>CRM/AMSS</strong> application checkbox only (do not select ASCF, UXF/OMNI, or DEX).",
        "step-06-select-crm-application.png",
        "",
    ),
    (
        "Step 7",
        "Paste the previous bundle SDK JAR path into the <strong>Old build</strong> field.",
        "step-07-enter-old-build-path.png",
        "Old build: <code>\\\\dvciisilon02\\cm_share2\\v10.24.1\\v10.24.1_67\\SDK\\Output\\SDK_CRM_10.24.1.0_linux64_20260508_1421.jar</code>",
    ),
    (
        "Step 8",
        "Paste the received new CRM SDK JAR path into the <strong>New build</strong> field.",
        "step-08-enter-new-build-path.png",
        "New build: <code>\\\\dvciisilon02\\cm_share2\\v10.24.1\\v10.24.1_68\\SDK\\Output\\SDK_CRM_10.24.1.0_linux64_20260608_0939.jar</code>",
    ),
    (
        "Step 9",
        "Optionally click <strong>Load CRM base paths</strong> and use <strong>Use as Old</strong> / "
        "<strong>Use as New</strong> to pick paths from the version table.",
        "step-09-load-crm-base-paths.png",
        "For a point release you can paste the full SDK JAR paths directly (Steps 7–8).",
    ),
    (
        "Step 10",
        "Configure CRM comparison mode and optional reports: "
        "<strong>Fast CRM/AMSS full compare</strong> (recommended, on by default), "
        "optional <strong>Compare ClfyAgent</strong> for nested SDK archives, "
        "and optional <strong>JAR manifest diff</strong> / <strong>class diff</strong> / "
        "<strong>crm-playbooks Nexus</strong> fields in the CRM/AMSS optional panel.",
        "step-10-crm-comparison-mode.png",
        "ClfyAgent only: enable Compare ClfyAgent and disable Fast CRM. "
        "Both enabled: fast tree compare plus ClfyAgent size-only nested JAR/WAR/EAR.",
    ),
    (
        "Step 11",
        "Click <strong>Run comparison</strong> and allow the progress tab to open (enable pop-ups if the browser blocks it).",
        "step-11-run-comparison.png",
        "For <strong>crm-playbooks Nexus-only</strong> checks: fill previous/latest Nexus versions and leave Old/New build empty.",
    ),
    (
        "Step 12",
        "Watch <strong>Console Output</strong> until the comparison finishes and the HTML report opens automatically.",
        "step-12-console-output.png",
        "Large SDK JAR comparisons may take 30+ minutes depending on network and disk speed.",
    ),
    (
        "Step 13",
        "Review the HTML report: <strong>At a glance</strong>, side-by-side summary, "
        "<strong>Version-changed JARs</strong> (blue Beyond Compare style), "
        "<strong>Missing JARs</strong> (one side only), ClfyAgent nested container table, "
        "major size alerts (|change| ≥ 10%), and path/JAR count mismatch details (release-folder aligned).",
        "step-13-review-report.png",
        "Paths under <code>CRM_10.24</code> vs <code>CRM_10.25</code> are paired automatically — not listed as false orphans.",
    ),
    (
        "Step 14",
        "Save or share the HTML report from <code>\\\\YOUR-PC\\c$\\crm_compare_reports</code> if sign-off or audit is required.",
        "step-14-save-report.png",
        "",
    ),
    (
        "Step 15",
        "Optionally enable <strong>Environment Validation</strong>: enter SSH host/user/password and remote CRM "
        "deployment path to validate JAR <code>Implementation-Version</code> on the server. "
        "Use <strong>Find JAR for class</strong> (same SSH credentials) to locate which JAR contains a Java class "
        "on the deployment host, then <strong>Use in class diff</strong> to add it to the class-diff list.",
        "step-15-environment-validation.png",
        "Find JAR runs remote <code>grep</code> under the deployment path (e.g. <code>10.76.48.68</code>).",
    ),
    (
        "Step 16",
        "Run <code>STOP_SERVER.bat</code> in the install folder when verification is complete.",
        "step-16-stop-server.png",
        "",
    ),
]

FEATURE_GUIDE: list[tuple[str, str]] = [
    (
        "CRM/AMSS optional panel (home page)",
        "<ul>"
        "<li><strong>Fast CRM/AMSS full compare</strong> — skips deep archive hashing for faster SDK tree scans.</li>"
        "<li><strong>Compare ClfyAgent</strong> — size-only nested JAR/WAR/EAR/ZIP inside ClfyAgent in the SDK.</li>"
        "<li><strong>JAR names for manifest diff</strong> — <code>MANIFEST.MF Implementation-Version</code> table in the report.</li>"
        "<li><strong>JAR names for class diff</strong> — Perforce-style decompiled Java diffs (needs <code>C:\\cfr-0.152.jar</code>).</li>"
        "<li><strong>Find JAR for class</strong> — SSH grep on deployment; shares Environment Validation credentials.</li>"
        "<li><strong>crm-playbooks Nexus TAR</strong> — compare <code>amdocs-version/*.yaml</code> between two Nexus builds.</li>"
        "<li><strong>File patterns for text/config diffs</strong> — limit HTML config diff section to named patterns.</li>"
        "<li><strong>Explicit file sizes to compare</strong> — force size rows for named archives.</li>"
        "</ul>",
    ),
    (
        "HTML report — new sections",
        "<ul>"
        "<li><strong>At a glance</strong> — summary bullets for this comparison run.</li>"
        "<li><strong>Build comparison summary (Old vs New)</strong> — aligned path counts across CRM release folders.</li>"
        "<li><strong>Version-changed JARs</strong> — one row per logical JAR; blue rows = version suffix mismatch (Beyond Compare style).</li>"
        "<li><strong>Missing JARs (one side only)</strong> — true orphans after signature pairing; correct <code>CRM_10.25</code> paths on new build.</li>"
        "<li><strong>ClfyAgent bundle</strong> — nested container version mismatch &amp; missing table (blue / red / green).</li>"
        "<li><strong>Major size diffs</strong> — flagged when |% change| ≥ 10%.</li>"
        "<li><strong>Inside-JAR differences</strong> — members that differ inside archives present on both builds.</li>"
        "</ul>",
    ),
    (
        "Related runbook",
        "<p>Detailed manifest &amp; class diff walkthrough: "
        "<code>docs\\CRM_BUILD_VERIFICATION_RUNBOOK.html</code> (self-contained screenshots).</p>",
    ),
]


def _png_data_uri(png_path: Path) -> str:
    b64 = base64.b64encode(png_path.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{b64}"


def _step_section(title: str, body_html: str, png_name: str, meta: str) -> str:
    png_path = SHOT_DIR / png_name
    if not png_path.is_file():
        raise FileNotFoundError(f"Missing screenshot: {png_path}")
    data_uri = _png_data_uri(png_path)
    alt = html_module.escape(f"{title} screenshot")
    meta_html = f'<p class="meta">{meta}</p>' if meta else ""
    return f"""
    <section class="step">
      <h2>{html_module.escape(title)}</h2>
      <p>{body_html}</p>
      <img src="{data_uri}" alt="{alt}" loading="lazy" />
      {meta_html}
    </section>"""


def _feature_section(title: str, body_html: str) -> str:
    return f"""
    <section class="step feature">
      <h2>{html_module.escape(title)}</h2>
      <div class="feature-body">{body_html}</div>
    </section>"""


def build_html() -> str:
    step_html = "".join(_step_section(t, b, p, m) for t, b, p, m in STEPS)
    feature_html = "".join(_feature_section(t, b) for t, b in FEATURE_GUIDE)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>CRM Automation Runbook — Build Verification</title>
  <style>
    :root {{ --bg:#f1f5f9; --card:#fff; --text:#0f172a; --muted:#64748b; --accent:#0369a1; --border:#cbd5e1; }}
    * {{ box-sizing: border-box; }}
    body {{ font-family: "Segoe UI", system-ui, sans-serif; margin: 0; background: var(--bg); color: var(--text); line-height: 1.55; }}
    header {{ background: linear-gradient(90deg,#0c4a6e,#0369a1); color: #fff; padding: 28px 24px; }}
    header h1 {{ margin: 0 0 8px; font-size: 1.75rem; }}
    header p {{ margin: 0; opacity: .95; }}
    main {{ max-width: 980px; margin: 0 auto; padding: 24px 16px 48px; }}
    .summary {{ background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 18px 20px; margin-bottom: 24px; }}
    .summary table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
    .summary th, .summary td {{ border: 1px solid #e2e8f0; padding: 10px 12px; text-align: left; vertical-align: top; }}
    .summary th {{ background: #eff6ff; width: 160px; }}
    code {{ background: #eef2ff; padding: 2px 6px; border-radius: 4px; font-size: 12px; word-break: break-all; }}
    .step {{ background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 18px 20px; margin-bottom: 18px; box-shadow: 0 1px 3px rgba(15,23,42,.04); }}
    .step h2 {{ margin: 0 0 8px; font-size: 1.1rem; color: var(--accent); }}
    .step p {{ margin: 0 0 12px; }}
    .step img {{ width: 100%; border: 1px solid var(--border); border-radius: 8px; background: #fff; }}
    .step.feature {{ border-color: #93c5fd; background: linear-gradient(180deg,#fff 0%,#f8fafc 100%); }}
    .feature-body ul {{ margin: 8px 0 0; padding-left: 1.25rem; }}
    .feature-body li {{ margin: 6px 0; }}
    .meta {{ font-size: 12px; color: var(--muted); margin-top: 8px; }}
    .badge {{ display: inline-block; background: #ecfdf5; color: #065f46; border: 1px solid #86efac; padding: 4px 10px; border-radius: 999px; font-size: 12px; font-weight: 600; margin-bottom: 12px; }}
    footer {{ text-align: center; color: var(--muted); font-size: 13px; padding: 16px; }}
    footer a {{ color: var(--accent); }}
  </style>
</head>
<body>
  <header>
    <h1>CRM Automation Runbook</h1>
    <p>Build verification after receiving a new CRM SDK path — includes latest comparison &amp; report features</p>
  </header>
  <main>
    <section class="summary">
      <span class="badge">Self-contained — open from any folder</span>
      <p style="margin:0 0 14px 0;">Screenshots are embedded in this file. Copy or email this HTML alone — images still display.</p>
      <h2 style="margin-top:0;">Release context (example)</h2>
      <table>
        <tr><th>Old build (_67)</th><td><code>\\\\dvciisilon02\\cm_share2\\v10.24.1\\v10.24.1_67\\SDK\\Output\\SDK_CRM_10.24.1.0_linux64_20260508_1421.jar</code></td></tr>
        <tr><th>New build (_68)</th><td><code>\\\\dvciisilon02\\cm_share2\\v10.24.1\\v10.24.1_68\\SDK\\Output\\SDK_CRM_10.24.1.0_linux64_20260608_0939.jar</code></td></tr>
        <tr><th>CRM version</th><td><code>10.24.1</code></td></tr>
        <tr><th>Bundle IDs</th><td><code>v10.24.1_67</code> (baseline) → <code>v10.24.1_68</code> (new)</td></tr>
        <tr><th>Tool URL</th><td><code>http://localhost:5000/crm</code> (localhost only)</td></tr>
        <tr><th>Start tool</th><td><code>launch_comparison_tool.bat</code> or <code>start_server.bat</code></td></tr>
        <tr><th>Reports folder</th><td><code>\\\\&lt;your-pc&gt;\\c$\\crm_compare_reports</code></td></tr>
      </table>
    </section>
{step_html}
    <h2 style="margin:24px 0 12px;font-size:1.25rem;color:#0f172a;">Feature reference (no extra screenshots)</h2>
{feature_html}
  </main>
  <footer>
    CRM Automation Runbook |
    Plain-text checklist: CRM_AUTOMATION_RUN_STEPS.txt |
    Manifest/class diff: CRM_BUILD_VERIFICATION_RUNBOOK.html
  </footer>
</body>
</html>
"""


def main() -> None:
    if not SHOT_DIR.is_dir():
        raise SystemExit(f"Screenshot folder not found: {SHOT_DIR}")
    html_text = build_html()
    OUT_HTML.write_text(html_text, encoding="utf-8")
    size_mb = OUT_HTML.stat().st_size / (1024 * 1024)
    print(f"Wrote {OUT_HTML} ({size_mb:.2f} MB, embedded screenshots)")


if __name__ == "__main__":
    main()
