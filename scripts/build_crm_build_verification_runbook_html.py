#!/usr/bin/env python3
"""Build self-contained CRM_BUILD_VERIFICATION_RUNBOOK.html (embedded PNG screenshots)."""

from __future__ import annotations

import base64
import html as html_module
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DOCS = PROJECT_ROOT / "docs"
SHOT_DIR = DOCS / "crm_build_verification_screenshots"
OUT_HTML = DOCS / "CRM_BUILD_VERIFICATION_RUNBOOK.html"

STEPS: list[tuple[str, str, str, str]] = [
    (
        "Step 1",
        "Start the comparison tool (<code>launch_comparison_tool.bat</code> or <code>start_server.bat</code>) and open <code>http://localhost:5000/crm</code>.",
        "step-01-start-tool-and-open-crm.png",
        "",
    ),
    (
        "Step 2",
        "Check the <strong>Build Comparison</strong> checkbox on the home page.",
        "step-02-enable-build-comparison.png",
        "",
    ),
    (
        "Step 3",
        "Select <strong>CRM/AMSS</strong> only (do not select ASCF, UXF/OMNI, or DEX).",
        "step-03-select-crm-application.png",
        "",
    ),
    (
        "Step 4",
        "Enter the previous CRM SDK path in <strong>Old build</strong> and the new CRM SDK path in <strong>New build</strong>.",
        "step-04-enter-build-paths.png",
        "Use UNC paths to the SDK JAR or folder for both builds.",
    ),
    (
        "Step 5",
        "In <strong>JAR names for manifest diff (comma-separated)</strong>, enter JAR names or patterns "
        "(e.g. <code>AmdocsSecurityManager.jar</code>, <code>scala*.jar</code>) to verify "
        "<code>MANIFEST.MF Implementation-Version</code> between old and new builds.",
        "step-05-enter-manifest-diff-jars.png",
        "",
    ),
    (
        "Step 6",
        "In <strong>JAR names for class diff (comma-separated)</strong>, enter JAR names "
        "(e.g. <code>ClfyBillingManager.jar</code>, <code>ClfyCore.jar</code>) to compare "
        "decompiled Java/class changes in the report.",
        "step-06-enter-class-diff-jars.png",
        "",
    ),
    (
        "Step 7",
        "Leave <strong>Full content comparison</strong> as the default CRM mode (automatic when only CRM/AMSS is selected).",
        "step-07-full-content-mode.png",
        "",
    ),
    (
        "Step 8",
        "Click <strong>Run comparison</strong> and wait for the HTML report to open (enable pop-ups if blocked).",
        "step-08-run-comparison.png",
        "Large SDK comparisons may take 30+ minutes depending on network and disk speed.",
    ),
    (
        "Step 9",
        "In the report, review the <strong>JAR manifest diff (optional) — summary</strong> table for old vs new "
        "Implementation-Version per JAR and path.",
        "step-09-review-manifest-diff-table.png",
        "Check primary paths and ClfyAgent bundle paths when the JAR appears inside nested archives.",
    ),
    (
        "Step 10",
        "In the report, review the <strong>JAR class file diff (optional) — summary</strong> section and "
        "Perforce-style Java source differences for the JARs you listed.",
        "step-10-review-class-diff-section.png",
        "Use <strong>Open</strong> in the summary table to jump to each JAR’s decompiled diff.",
    ),
    (
        "Step 11",
        "Confirm manifest versions and class changes match expected build verification results before sign-off.",
        "step-11-sign-off-verification.png",
        "Save the HTML report from <code>\\\\YOUR-PC\\c$\\crm_compare_reports</code> if audit is required.",
    ),
]


def _png_data_uri(png_path: Path) -> str:
    raw = png_path.read_bytes()
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:image/png;base64,{b64}"


def _step_section(title: str, body_html: str, png_name: str, meta: str) -> str:
    png_path = SHOT_DIR / png_name
    if not png_path.is_file():
        raise FileNotFoundError(f"Missing screenshot: {png_path}")
    data_uri = _png_data_uri(png_path)
    alt = html_module.escape(f"{title} screenshot")
    meta_html = (
        f'<p class="meta">{meta}</p>'
        if meta
        else ""
    )
    return f"""
    <section class="step">
      <h2>{html_module.escape(title)}</h2>
      <p>{body_html}</p>
      <img src="{data_uri}" alt="{alt}" loading="lazy" />
      {meta_html}
    </section>"""


def build_html() -> str:
    step_html = "".join(_step_section(t, b, p, m) for t, b, p, m in STEPS)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>CRM Build Verification Runbook — Manifest &amp; Class Diff</title>
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
    .summary th {{ background: #eff6ff; width: 180px; }}
    code {{ background: #eef2ff; padding: 2px 6px; border-radius: 4px; font-size: 12px; word-break: break-all; }}
    .step {{ background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 18px 20px; margin-bottom: 18px; box-shadow: 0 1px 3px rgba(15,23,42,.04); }}
    .step h2 {{ margin: 0 0 8px; font-size: 1.1rem; color: var(--accent); }}
    .step p {{ margin: 0 0 12px; }}
    .step img {{ width: 100%; border: 1px solid var(--border); border-radius: 8px; background: #fff; }}
    .meta {{ font-size: 12px; color: var(--muted); margin-top: 8px; }}
    .badge {{ display: inline-block; background: #ecfdf5; color: #065f46; border: 1px solid #86efac; padding: 4px 10px; border-radius: 999px; font-size: 12px; font-weight: 600; margin-bottom: 12px; }}
    footer {{ text-align: center; color: var(--muted); font-size: 13px; padding: 16px; }}
    footer a {{ color: var(--accent); }}
  </style>
</head>
<body>
  <header>
    <h1>CRM Build Verification Runbook</h1>
    <p>JAR manifest Implementation-Version and class diff verification (CRM/AMSS)</p>
  </header>
  <main>
    <section class="summary">
      <span class="badge">Self-contained — open from any folder</span>
      <p style="margin:0 0 14px 0;">Screenshots are embedded in this file. Copy or email this HTML alone — images still display. No <code>crm_build_verification_screenshots</code> folder required.</p>
      <h2 style="margin-top:0;">Verification context</h2>
      <table>
        <tr><th>Purpose</th><td>Compare old vs new CRM SDK builds using optional <strong>manifest diff</strong> and <strong>class diff</strong> text boxes before sign-off.</td></tr>
        <tr><th>Old build (example)</th><td><code>\\\\dvciisilon02\\cm_share2\\v10.24.1\\v10.24.1_67\\SDK\\Output\\SDK_CRM_10.24.1.0_linux64_20260508_1421.jar</code></td></tr>
        <tr><th>New build (example)</th><td><code>\\\\dvciisilon02\\cm_share2\\v10.24.1\\v10.24.1_68\\SDK\\Output\\SDK_CRM_10.24.1.0_linux64_20260608_0939.jar</code></td></tr>
        <tr><th>Manifest diff box</th><td>Comma-separated JAR names/patterns → report table with <code>MANIFEST.MF Implementation-Version</code> (old vs new)</td></tr>
        <tr><th>Class diff box</th><td>Comma-separated JAR names → summary table + Perforce-style Java source diffs</td></tr>
        <tr><th>Tool URL</th><td><code>http://localhost:5000/crm</code> (localhost only)</td></tr>
        <tr><th>Start tool</th><td><code>launch_comparison_tool.bat</code> or <code>start_server.bat</code> in the Comparison Tool install folder</td></tr>
      </table>
    </section>
{step_html}
  </main>
  <footer>
    CRM Build Verification Runbook |
    Plain-text checklist: CRM_BUILD_VERIFICATION_ONE_LINE.txt (same folder or docs folder) |
    Full CRM runbook: CRM_AUTOMATION_RUNBOOK.html
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
