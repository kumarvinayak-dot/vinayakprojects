"""Shared collapsible sections (expand all / collapse all) for comparison HTML reports."""

from __future__ import annotations

import html


def report_collapse_extra_css() -> str:
    return """
        .report-section-toolbar {
            display: flex; flex-wrap: wrap; gap: 10px; align-items: center;
            margin: 0 0 16px 0; padding: 10px 12px; background: #f1f5f9;
            border: 1px solid #cbd5e1; border-radius: 10px;
        }
        .crm-report-toolbar {
            display: flex; flex-wrap: wrap; gap: 10px; align-items: center;
            margin: 0 0 16px 0; padding: 10px 12px; background: #f1f5f9;
            border: 1px solid #cbd5e1; border-radius: 10px;
        }
        .report-section-toolbar button,
        .crm-report-toolbar button {
            padding: 8px 14px; font-size: 13px; font-weight: 600; cursor: pointer;
            border: 1px solid #94a3b8; border-radius: 8px; background: #fff; color: #0f172a;
        }
        .report-section-toolbar button:hover,
        .crm-report-toolbar button:hover { background: #e2e8f0; }
        .report-section-toolbar span,
        .crm-report-toolbar span { font-size: 12px; color: #64748b; }
        details.report-collapse,
        details.crm-report-collapse {
            margin-bottom: 16px; border-radius: 10px;
            border: 1px solid #e2e8f0; background: #fff;
        }
        details.report-collapse > summary.report-collapse-summary,
        details.crm-report-collapse > summary.crm-collapse-summary {
            list-style: none; cursor: pointer; font-weight: 600; color: #0f172a;
            padding: 12px 14px; border-radius: 10px;
            display: flex; align-items: center; gap: 10px; user-select: none;
            background: linear-gradient(180deg, #f8fafc 0%, #f1f5f9 100%);
        }
        details.report-collapse[open] > summary.report-collapse-summary,
        details.crm-report-collapse[open] > summary.crm-collapse-summary {
            border-radius: 10px 10px 0 0; border-bottom: 1px solid #e2e8f0;
        }
        details.report-collapse > summary.report-collapse-summary::-webkit-details-marker,
        details.crm-report-collapse > summary.crm-collapse-summary::-webkit-details-marker { display: none; }
        details.report-collapse > summary.report-collapse-summary::marker,
        details.crm-report-collapse > summary.crm-collapse-summary::marker { content: ''; }
        details.report-collapse > summary.report-collapse-summary::before,
        details.crm-report-collapse > summary.crm-collapse-summary::before {
            content: '▼'; font-size: 14px; line-height: 1; color: #1e3a5f; flex-shrink: 0;
            font-weight: 700; width: 1.1em; text-align: center;
        }
        details.report-collapse:not([open]) > summary.report-collapse-summary::before,
        details.crm-report-collapse:not([open]) > summary.crm-collapse-summary::before { content: '▶'; }
        details.report-collapse .report-collapse-body,
        details.crm-report-collapse .crm-collapse-body,
        details.crm-report-collapse .report-collapse-body { padding: 12px 14px 14px 14px; }
        .report-legacy-section > h2,
        .report-legacy-section > .section-title {
            cursor: pointer; user-select: none; position: relative;
            padding-left: 1.35em !important;
        }
        .report-legacy-section > h2::before,
        .report-legacy-section > .section-title::before {
            content: '▼'; position: absolute; left: 0; font-size: 14px; color: #1e3a5f; font-weight: 700;
        }
        .report-legacy-section.report-section-collapsed > h2::before,
        .report-legacy-section.report-section-collapsed > .section-title::before { content: '▶'; }
        .report-legacy-section.report-section-collapsed > .report-legacy-section-body { display: none; }
        .report-legacy-section.report-section-collapsed > .archive-content { display: none; }
        .archive-section > .archive-header {
            cursor: pointer; user-select: none; position: relative;
            padding-left: 1.35em !important;
        }
        .archive-section > .archive-header::before {
            content: '▼'; position: absolute; left: 0; font-size: 14px; color: #1e3a5f; font-weight: 700;
        }
        .archive-section.report-section-collapsed > .archive-header::before { content: '▶'; }
        """


def report_collapse_toolbar() -> str:
    return """
        <div class="report-section-toolbar">
            <span>Report sections:</span>
            <button type="button" onclick="reportSetAllSections(true)">▼ Expand all</button>
            <button type="button" onclick="reportSetAllSections(false)">▶ Collapse all</button>
        </div>"""


def report_collapse_script() -> str:
    return """
        <script>
        (function () {
            function initLegacyReportSections() {
                var roots = document.querySelectorAll('.content, .container .content, body > .container');
                var scope = roots.length ? roots : [document.body];
                scope.forEach(function (root) {
                    root.querySelectorAll('div.section[id]').forEach(function (section) {
                        if (section.dataset.reportCollapseInit) return;
                        if (section.closest('details.report-collapse, details.crm-report-collapse')) return;
                        var header = section.querySelector(':scope > h2, :scope > .section-title');
                        if (!header) return;
                        section.dataset.reportCollapseInit = '1';
                        section.classList.add('report-legacy-section');
                        var body = document.createElement('div');
                        body.className = 'report-legacy-section-body';
                        var sib = header.nextElementSibling;
                        while (sib) {
                            var next = sib.nextElementSibling;
                            body.appendChild(sib);
                            sib = next;
                        }
                        section.appendChild(body);
                        var open = true;
                        function setOpen(o) {
                            open = !!o;
                            section.classList.toggle('report-section-collapsed', !open);
                            header.setAttribute('aria-expanded', open ? 'true' : 'false');
                        }
                        setOpen(true);
                        header.addEventListener('click', function (e) {
                            if (e.target.closest('a, button, input, select, textarea')) return;
                            setOpen(!open);
                        });
                        section._reportSetOpen = setOpen;
                    });
                    root.querySelectorAll('.archive-section').forEach(function (section) {
                        if (section.dataset.reportCollapseInit) return;
                        var header = section.querySelector(':scope > .archive-header');
                        var body = section.querySelector(':scope > .archive-content');
                        if (!header || !body) return;
                        section.dataset.reportCollapseInit = '1';
                        section.classList.add('report-legacy-section');
                        var open = true;
                        function setOpen(o) {
                            open = !!o;
                            section.classList.toggle('report-section-collapsed', !open);
                            header.setAttribute('aria-expanded', open ? 'true' : 'false');
                        }
                        setOpen(true);
                        header.addEventListener('click', function (e) {
                            if (e.target.closest('a, button, input, select, textarea')) return;
                            setOpen(!open);
                        });
                        section._reportSetOpen = setOpen;
                    });
                });
            }
            window.reportSetAllSections = function (open) {
                document.querySelectorAll('details.report-collapse, details.crm-report-collapse').forEach(function (el) {
                    el.open = !!open;
                });
                document.querySelectorAll('.report-legacy-section').forEach(function (el) {
                    if (typeof el._reportSetOpen === 'function') el._reportSetOpen(!!open);
                });
            };
            window.crmReportSetAllSections = window.reportSetAllSections;
            if (document.readyState === 'loading') {
                document.addEventListener('DOMContentLoaded', initLegacyReportSections);
            } else {
                initLegacyReportSections();
            }
        })();
        </script>"""


def wrap_report_section(
    section_id: str,
    title_html: str,
    body_html: str,
    *,
    open_default: bool = True,
    extra_classes: str = "",
) -> str:
    open_attr = " open" if open_default else ""
    sid = html.escape(str(section_id or "").strip() or "section", quote=True)
    classes = "report-collapse"
    if extra_classes:
        classes += " " + extra_classes.strip()
    return f"""
<details class="{classes}" id="{sid}"{open_attr}>
<summary class="report-collapse-summary">{title_html}</summary>
<div class="report-collapse-body">{body_html}</div>
</details>"""


def inject_report_section_controls(html: str) -> str:
    """Add collapse CSS, toolbar, and script to an HTML report if not already present."""
    if "window.reportSetAllSections" in html:
        return html
    if "</style>" in html and 'class="report-section-toolbar"' not in html and 'class="crm-report-toolbar"' not in html:
        html = html.replace("</style>", report_collapse_extra_css() + "\n    </style>", 1)
    if 'class="report-section-toolbar"' not in html and 'class="crm-report-toolbar"' not in html:
        toolbar = report_collapse_toolbar()
        inserted = False
        for marker in ('<div class="content">', '<body>', '<div class="container">'):
            if marker in html:
                html = html.replace(marker, marker + "\n" + toolbar, 1)
                inserted = True
                break
        if not inserted:
            html = html.replace("</head>", "</head>\n" + toolbar, 1)
    if "window.reportSetAllSections" not in html:
        html = html.replace("</body>", report_collapse_script() + "\n</body>", 1)
    return html
