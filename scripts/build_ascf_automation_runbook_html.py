#!/usr/bin/env python3
"""Build self-contained ASCF_AUTOMATION_RUNBOOK.html (embedded PNG screenshots)."""

from __future__ import annotations

import base64
import html as html_module
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DOCS = PROJECT_ROOT / "docs"
SHOT_DIR = DOCS / "runbook_screenshots"
OUT_HTML = DOCS / "ASCF_AUTOMATION_RUNBOOK.html"

STEPS: list[tuple[str, str, str, str]] = [
    (
        "Step 1",
        "Open the received new build UNC path in Windows Explorer and confirm you have read access.",
        "step-01-explorer-access.png",
        "",
    ),
    (
        "Step 2",
        "Note the previous HF build UNC path to use as the <strong>Old build</strong> baseline for comparison.",
        "step-02-baseline-paths.png",
        "",
    ),
    (
        "Step 3",
        "Double-click <code>start_server.bat</code> in the <code>python_project</code> folder "
        "(or the Desktop <strong>ASCF Compare Tool - Start</strong> shortcut) to start the automation tool server.",
        "step-03-start-server.png",
        "",
    ),
    (
        "Step 4",
        "Open <code>http://localhost:5000/crm</code> in Chrome or Edge "
        "(or the network URL on a shared server, e.g. <code>http://10.19.164.180:5000/crm</code>).",
        "step-04-open-tool-url.png",
        "Dismiss the “Before you compare” dialog if shown.",
    ),
    (
        "Step 5",
        "Check the <strong>Build Comparison</strong> checkbox on the home page.",
        "step-05-enable-build-comparison.png",
        "",
    ),
    (
        "Step 6",
        "Check the <strong>ASCF</strong> application checkbox only (do not select CRM/AMSS, UXF/OMNI, or DEX).",
        "step-06-select-ascf-application.png",
        "",
    ),
    (
        "Step 7",
        "Paste the previous HF build path into the <strong>Old build</strong> field.",
        "step-07-enter-old-build-path.png",
        "Old build: <code>\\\\INPNQASCFLB02\\rel\\10241\\uif\\ASCF_HF_10240pb01hf09</code>",
    ),
    (
        "Step 8",
        "Paste the received new build path into the <strong>New build</strong> field.",
        "step-08-enter-new-build-path.png",
        "New build: <code>\\\\INPNQASCFLB02\\rel\\10241\\uif\\ASCF_HF_10240pb01hf10</code>",
    ),
    (
        "Step 9",
        "Optionally click <strong>Load ASCF base paths</strong> and use "
        "<strong>Use as Old</strong> / <strong>Use as New</strong> to populate paths from the version table.",
        "step-09-load-ascf-base-paths.png",
        "The table lists known ASCF release roots — expand the table to see all versions.",
    ),
    (
        "Step 10",
        "Select <strong>one</strong> ASCF comparison mode (mutually exclusive radio buttons): "
        "<strong>Full content comparison</strong> (default — full tree diff), "
        "<strong>Naming convention comparison</strong> (archive/JAR filename pairing), "
        "<strong>Archives (.ear/.war/.zip/.rpm/.jar) contents</strong> (deep member compare), or "
        "<strong>FOSS/Foundation (JAR Implementation-Version)</strong> (single JAR version check). "
        "When <strong>Full content</strong> is selected, the <strong>ASCF full — optional report options</strong> "
        "panel appears for class diff, manifest diff, config/text diff, and explicit size rows.",
        "step-10-select-comparison-mode.png",
        "FOSS/Foundation mode: pick <strong>Foundation product</strong> or <strong>Foss product</strong> "
        "from the dropdown (fills Jar name) or type the JAR name manually. "
        "See the Feature reference section below for all optional fields.",
    ),
    (
        "Step 11",
        "Click <strong>Run comparison</strong> and allow the progress tab to open (enable pop-ups if blocked).",
        "step-11-run-comparison.png",
        "",
    ),
    (
        "Step 12",
        "Watch <strong>Console Output</strong> until the comparison finishes and the HTML report opens automatically.",
        "step-12-console-output.png",
        "",
    ),
    (
        "Step 13",
        "Review the HTML report: use <strong>Expand all</strong> / <strong>Collapse all</strong> on report sections, "
        "check Added/Removed in New Build, archive content differences, and any optional manifest/class/config sections "
        "you enabled on the home page.",
        "step-13-review-report.png",
        "Full-content reports include quick navigation, side-by-side comparison table, and collapsible sections.",
    ),
    (
        "Step 14",
        "Save or share the HTML report from <code>\\\\YOUR-PC\\c$\\crm_compare_reports</code> if sign-off is required.",
        "step-14-save-report.png",
        "",
    ),
    (
        "Step 15",
        "Optionally enable <strong>Environment Validation</strong>, enter SSH host/user/password and remote ASCF "
        "deployment path, and validate JAR <code>Implementation-Version</code> on the deployment server.",
        "step-15-environment-validation.png",
        "",
    ),
    (
        "Step 16",
        "After build verification passes, click <strong>Foundations Link</strong> on the home page "
        "(visible when ASCF is selected).",
        "step-16-foundations-link.png",
        "",
    ),
    (
        "Step 17",
        "In Step 1 (Prepare), select ASCF Product Version <code>10.24.0.pb01</code>, enter HF number "
        "<code>10</code>, and click <strong>Prepare Foundation Link</strong>.",
        "step-17-prepare-foundation.png",
        "",
    ),
    (
        "Step 18",
        "In Step 2 (Update), enter version <code>10.24.0.pb01</code>, HF <code>10</code>, month "
        "<code>Jun</code>, year <code>2026</code>, creation date <code>20260609</code>, then click "
        "<strong>Update Foundation Link</strong>.",
        "step-18-update-foundation.png",
        "",
    ),
    (
        "Step 19",
        "Click <strong>Update Index.html</strong>, enter ASCF version, HF number, month, and year, then submit.",
        "step-19-update-index.png",
        "",
    ),
    (
        "Step 20",
        "Click <strong>Update Javadoc.html</strong>, enter ASCF Product Version and HF number <code>10</code>, then submit.",
        "step-20-update-javadoc.png",
        "",
    ),
    (
        "Step 21",
        "Return to the home page, click <strong>Administrative Tool</strong>, fill hotfix details, and click "
        "<strong>Submit &amp; Launch Automation</strong>.",
        "step-21-administrative-tool.png",
        "Example: Hotfix <code>10.24.0.pb01_hf10</code>, Release date <code>2026-06-09</code>, "
        "Download URL <code>10240pb01hf10_download.html</code>, Release notes "
        "<code>ReleaseNotes/relnotes10_24_0_pb01_hf10.pdf</code>",
    ),
    (
        "Step 22",
        "Verify the pre-filled Foundation admin form in the opened browser window and click "
        "<strong>Add Hotfix</strong> to publish the release.",
        "step-22-verify-admin-form.png",
        "",
    ),
]

FEATURE_GUIDE: list[tuple[str, str]] = [
    (
        "ASCF comparison modes (select exactly one)",
        "<ul>"
        "<li><strong>Full content comparison</strong> — recursive folder tree diff between Old and New HF builds; "
        "default for HF build verification. Produces a detailed HTML report with Added/Removed/Identical sections.</li>"
        "<li><strong>Naming convention comparison</strong> — pairs archives/JARs with the same logical name but "
        "different version suffixes; lists exact matches and missing-from-old/new.</li>"
        "<li><strong>Archives (.ear/.war/.zip/.rpm/.jar) contents</strong> — deep compare inside each archive; "
        "reports identical, different, only-in-old, and only-in-new archives with member-level details.</li>"
        "<li><strong>FOSS/Foundation (JAR Implementation-Version)</strong> — compares one JAR at a time using "
        "<code>META-INF/MANIFEST.MF Implementation-Version</code> between builds. Run once per JAR on your checklist.</li>"
        "</ul>"
        "<p class=\"meta\">Modes are mutually exclusive radio buttons — only one runs per comparison.</p>",
    ),
    (
        "ASCF full — optional report options (Full content mode only)",
        "<ul>"
        "<li><strong>JAR names for class diff</strong> — comma-separated JARs (e.g. <code>AmdocsCore.jar</code>); "
        "decompiles changed <code>.class</code> files with CFR and shows Perforce-style Java diffs in the HTML report "
        "(requires <code>C:\\cfr-0.152.jar</code> or CFR on the server PC).</li>"
        "<li><strong>File patterns for text / config diffs</strong> — comma-separated patterns "
        "(e.g. <code>app.json, *.yml, web.xml</code>); adds a <strong>Text and config files changed</strong> section. "
        "Leave empty to hide unless default rules apply.</li>"
        "<li><strong>JAR names for manifest diff</strong> — comma-separated JARs or globs "
        "(e.g. <code>scala*.jar, commons-lang3*.jar</code>); adds a manifest "
        "<code>Implementation-Version</code> comparison table.</li>"
        "<li><strong>Explicit file sizes to compare</strong> — comma-separated archive paths or globs "
        "(e.g. <code>AmdocsUIF.ear, *.war</code>); forces old-vs-new size rows in the report even when not in the "
        "main diff set.</li>"
        "</ul>",
    ),
    (
        "FOSS/Foundation mode — product pickers & retry",
        "<ul>"
        "<li><strong>Jar name</strong> — type manually or let a dropdown fill it.</li>"
        "<li><strong>Foundation product</strong> — Amdocs Core, Process Manager, Security Manager, System Configuration, "
        "obfuscation-utility.</li>"
        "<li><strong>Foss product</strong> — commons-io, log4j-core, slf4j-api, jxbrowser, esapi, and other bundled FOSS JARs.</li>"
        "<li><strong>Max retries / Retry delay</strong> — network UNC access retry settings for slow shares.</li>"
        "</ul>"
        "<p>Detailed FOSS adoption workflow: <code>docs\\ASCF_FOSS_FOUNDATIONS_ADOPTION_RUNBOOK.html</code></p>",
    ),
    (
        "Other ASCF home-page helpers",
        "<ul>"
        "<li><strong>Load ASCF base paths</strong> — version table with <strong>Use as Old</strong> / "
        "<strong>Use as New</strong> and <strong>Open in Explorer</strong> (localhost).</li>"
        "<li><strong>Copy build folders (ASCF)</strong> — copy Old/New HF folders to local disk before compare "
        "(faster on slow network paths).</li>"
        "<li><strong>Environment Validation</strong> — SSH to deployment host and verify JAR versions remotely.</li>"
        "<li><strong>Foundations Link</strong> — prepare/update Foundation portal pages after build verification.</li>"
        "<li><strong>Administrative Tool</strong> — submit new HF to the Foundation admin site.</li>"
        "</ul>",
    ),
    (
        "HTML report — navigation & sections",
        "<ul>"
        "<li><strong>Expand all / Collapse all</strong> — toolbar at the top of every comparison HTML report.</li>"
        "<li><strong>Quick Navigation</strong> — jump links to comparison table, missing files, identical files, etc.</li>"
        "<li><strong>Detailed Comparison Table</strong> — Beyond Compare style side-by-side status per archive/file.</li>"
        "<li>Optional sections appear only when configured: manifest diff table, class decompilation diffs, "
        "text/config file changes, explicit size comparison rows.</li>"
        "<li>Reports saved under <code>\\\\YOUR-PC\\c$\\crm_compare_reports</code> — share the <code>.html</code> file "
        "for sign-off.</li>"
        "</ul>",
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
  <title>ASCF Automation Runbook — HF09 vs HF10</title>
  <style>
    :root {{ --bg:#f1f5f9; --card:#fff; --text:#0f172a; --muted:#64748b; --accent:#2563eb; --border:#cbd5e1; }}
    * {{ box-sizing: border-box; }}
    body {{ font-family: "Segoe UI", system-ui, sans-serif; margin: 0; background: var(--bg); color: var(--text); line-height: 1.55; }}
    header {{ background: linear-gradient(90deg,#1e3a8a,#2563eb); color: #fff; padding: 28px 24px; }}
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
    .feature-body p {{ margin: 10px 0 0; }}
    .meta {{ font-size: 12px; color: var(--muted); margin-top: 8px; }}
    .badge {{ display: inline-block; background: #ecfdf5; color: #065f46; border: 1px solid #86efac; padding: 4px 10px; border-radius: 999px; font-size: 12px; font-weight: 600; margin-bottom: 12px; }}
    footer {{ text-align: center; color: var(--muted); font-size: 13px; padding: 16px; }}
    footer a {{ color: var(--accent); }}
  </style>
</head>
<body>
  <header>
    <h1>ASCF Automation Runbook</h1>
    <p>Build verification and release automation after receiving the new HF10 build path — all ASCF comparison modes</p>
  </header>
  <main>
    <section class="summary">
      <span class="badge">Self-contained — open from any folder</span>
      <p style="margin:0 0 14px 0;">Screenshots are embedded in this file. Copy or email this HTML alone — images still display.</p>
      <h2 style="margin-top:0;">Release context (example)</h2>
      <table>
        <tr><th>Old build (HF09)</th><td><code>\\\\INPNQASCFLB02\\rel\\10241\\uif\\ASCF_HF_10240pb01hf09</code></td></tr>
        <tr><th>New build (HF10)</th><td><code>\\\\INPNQASCFLB02\\rel\\10241\\uif\\ASCF_HF_10240pb01hf10</code></td></tr>
        <tr><th>ASCF product version</th><td><code>10.24.0.pb01</code></td></tr>
        <tr><th>HF number</th><td><code>10</code></td></tr>
        <tr><th>Tool URL</th><td><code>http://localhost:5000/crm</code> (network: <code>http://10.19.164.180:5000/crm</code>)</td></tr>
        <tr><th>Reports folder</th><td><code>\\\\&lt;your-pc&gt;\\c$\\crm_compare_reports</code></td></tr>
      </table>
    </section>
{step_html}
    <h2 style="margin:24px 0 12px;font-size:1.25rem;color:#0f172a;">Feature reference — ASCF comparison options</h2>
{feature_html}
  </main>
  <footer>
    ASCF Automation Runbook — HF09 → HF10 |
    Plain-text: ASCF_AUTOMATION_RUN_STEPS.txt |
    FOSS adoption: ASCF_FOSS_FOUNDATIONS_ADOPTION_RUNBOOK.html
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
