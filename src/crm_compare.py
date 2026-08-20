#!/usr/bin/env python3
"""
CRM Application Comparison Module

This module provides comparison functionality specific to CRM application builds,
particularly for SDK.jar file comparisons. The comparison logic for CRM is different
from ASCF and is maintained separately to avoid any discrepancy with ASCF comparison behavior.

Supports all four comparison modes:
1. Full content comparison (CRMFolderComparer)
2. Naming convention comparison (CRMNamingComparer)
3. Archive contents comparison (CRMArchiveComparer)
4. FOSS/Foundation JAR manifest comparison (CRMJarManifestComparer)
"""

import os
import re
import sys
from urllib.parse import quote
import html
import fnmatch
import difflib
import time
import socket
import shutil
import zipfile
import hashlib
import tempfile
import subprocess
from datetime import datetime
from pathlib import Path
from io import BytesIO
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Callable, Optional
import logging

from crm_jar_download import make_jar_download_token

from report_html_sections import (
    inject_report_section_controls,
    report_collapse_extra_css,
    report_collapse_script,
    report_collapse_toolbar,
    wrap_report_section,
)

from folder_compare import (
    _path_exists,
    _path_is_file,
    _path_is_dir,
    _to_long_path,
    _find_java_decompiler,
    _decompile_class_file,
    _compare_decompiled_sources,
    get_compare_temp_dir_candidates,
    mkdtemp_compare,
)

# Setup logging
logger = logging.getLogger(__name__)

# CRM SDK ship layout: compare this bundle inside the SDK when enabled (ZIP or EAR)
CLFY_AGENT_ZIP_BASENAME = "clfyagent.zip"
CLFY_AGENT_EAR_BASENAME = "clfyagent.ear"
# Lowercase basenames for scan / zip entry matching (EAR contains WAR → WEB-INF → JARs)
_CLFY_AGENT_BUNDLE_BASENAMES_LOWER = frozenset(
    (CLFY_AGENT_ZIP_BASENAME.lower(), CLFY_AGENT_EAR_BASENAME.lower())
)
# Report / clfy_agent_zip_diff["label"] — UI name (option still compare_clfy_agent_zip)
CLFY_AGENT_BUNDLE_REPORT_LABEL = "ClfyAgent (.zip / .ear)"
CLFY_AGENT_ZIP_DEFAULT_FALLBACK_PATH = (
    r"\\NTNET\indiafilestore1\cm_share2\v10.24.1\v10.24.1_64\SDK\Output\SDK_CRM_10.24.1.0_linux64_20260327_1606.jar"
    r"\crm\CRM_10.24\CRM10.24.1_ALL\modules\crm\lib\sharedlib\ClfyAgent.zip"
)

# Nested containers to search for optional JAR class-diff (must match _scan_archive_contents / ClfyAgent finder)
_CRM_CLASS_DIFF_NESTED_ARCHIVE_SUFFIXES = (".jar", ".war", ".ear", ".zip")
_CRM_JAVA_DIFF_MAX_CLASSES = 60
# Foundation JARs shipped with the platform — skip automatic .class scan inside these unless
# explicitly named in the CRM class-diff JAR text box (saves SDK / ClfyAgent compare time).
_CRM_FOUNDATION_JAR_SKIP_CLASS_SCAN_LOWER = frozenset(
    {
        "amdocsaif.jar",
        "amdocscore.jar",
        "amdocsprocmgrliteclient.jar",
        "amdocsprocmgr.jar",
        "amdocsprocmgrengineexample.jar",
        "amdocsprocmgr_weblogic.jar",
        "amdocsprocmgrengine.jar",
        "amdocsprocmgrapplication.jar",
        "amdocsprocmgrbase.jar",
        "amdocsprocmgremitted.jar",
        "amdocsprocmgrdata.jar",
        "amdocsprocmgrejbdesc_clfyagent.jar",
        "amdocsprocmgrl10n.jar",
        "acfglib.jar",
        "amdocssecuritymanager.jar",
        "amf_toolkit.jar",
        "central-config-consumer.jar",
        "central-config-http.jar",
        "awsi.jar",
    }
)

# Full HTML runs heavy per-row sections; beyond this diff count use bounded compact HTML.
_CRM_HTML_FULL_REPORT_MAX_FILES = 2000
# In large runs, cap list sizes so HTML generation stays fast and the page stays usable.
_CRM_HTML_LARGE_LIST_CAP = 4000
# Inline config unified diffs are read from disk — only compute for paths shown in HTML.
_CRM_HTML_CONFIG_DIFF_ENSURE_CAP = 50
# Cap JAR missing-table rows and rename-alignment pairs in full HTML.
_CRM_HTML_JAR_MISSING_ROW_CAP = 500
_CRM_HTML_ALIGN_PAIR_ROW_CAP = 500
# If this many unchanged files, skip listing them in HTML entirely (text report has all).
_CRM_HTML_OMIT_UNCHANGED_THRESHOLD = 500
# Text report: max rows per big section (unchanged / added / removed) to avoid multi‑GB RAM and hangs.
_CRM_TXT_REPORT_MAX_PER_SECTION = 100_000
# Removed/added alignment is O(n*m); skip pairing above this product to avoid hangs.
_CRM_ALIGN_MAX_PAIR_PRODUCT = 1_000_000
# HTML reports skip rename alignment earlier (summary tables do not need greedy pairing).
_CRM_HTML_ALIGN_MAX_PAIR_PRODUCT = 50_000
# Side-by-side orphan tables skip scan-index path resolution above this path count.
_CRM_ALIGN_SIDE_BY_SIDE_FAST_MAX_PATHS = 800
# High Size Difference Alert: flag when |% size change| is at or above this (increase or decrease).
_CRM_HIGH_SIZE_ALERT_MIN_ABS_PCT = 10.0
_CRM_RELEASE_DIR_RE = re.compile(r"CRM_\d+(?:[._]\d+)*", re.IGNORECASE)
_CRM_RELEASE_PKG_RE = re.compile(r"CRM\d+(?:\.\d+)*_ALL", re.IGNORECASE)
_CRM_SDK_RELEASE_DIR_RE = re.compile(r"SDK_CRM_(\d+)\.(\d+)", re.IGNORECASE)
_CRM_SDK_UNC_RELEASE_RE = re.compile(r"/v(\d+)\.(\d+)", re.IGNORECASE)
_CRM_RELEASE_NEUTRAL_DIR = "_CRM_REL_"
_CRM_RELEASE_NEUTRAL_PKG = "_CRM_PKG_ALL_"
_CRM_ABSENT_NEW_LEAF = "absent"
# Report minor size decreases when new build is smaller but change is below this % threshold.
_CRM_MINOR_SIZE_REPORT_MAX_PCT = 20.0
_CRM_REPORT_HTML_SUFFIX = ".html"
# Dark red styling for missing paths / archive names in side-by-side mismatch tables.
_CRM_MISSING_DARK_RED = "#7f1d1d"
_CRM_MISSING_CELL_BG = "#fee2e2"
# Beyond Compare–style row colours (folder compare: blue = different, red/green = one side only).
_CRM_BC_VERSION_MISMATCH_BG = "#cce8ff"
_CRM_BC_VERSION_MISMATCH_FG = "#003366"
_CRM_BC_MISSING_OLD_BG = "#ffe7e7"
_CRM_BC_MISSING_NEW_BG = "#e7f7e7"

# Pair “only removed + only added” as one modified row when filenames differ only by CRM version tokens.
# Longest suffix first (e.g. .jar.pack.gz before .jar).
_CRM_VERSION_PAIR_ARCHIVE_SUFFIXES: tuple[str, ...] = (
    ".jar.pack.gz",
    ".war.pack.gz",
    ".ear.pack.gz",
    ".jar",
    ".war",
    ".ear",
    ".zip",
)


def _crm_html_report_path(filepath: str) -> str:
    return str(Path(filepath).with_suffix(_CRM_REPORT_HTML_SUFFIX))


def _crm_write_html_file(html_filepath: str, html_body: str) -> None:
    tmp_path = html_filepath + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(html_body)
        os.replace(tmp_path, html_filepath)
    except Exception:
        if os.path.isfile(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise


def _crm_rel_path_is_version_pairable_archive(rel_path: str) -> bool:
    low = _crm_norm_rel_path(rel_path).lower()
    return any(low.endswith(s) for s in _CRM_VERSION_PAIR_ARCHIVE_SUFFIXES)


def _crm_rel_path_is_version_jar_summary_row(rel_path: str) -> bool:
    """True for version-changed summary table: .jar / packed jars; supports ``old -> new`` paths."""
    rp = _crm_norm_rel_path(rel_path).lower().strip()
    if not rp:
        return False
    parts = [p.strip() for p in rp.split(" -> ")] if " -> " in rp else [rp]
    return any(p.endswith(_CRM_VERSION_PAIR_ARCHIVE_SUFFIXES) for p in parts if p)

# Inline helpers for CRM HTML: copy UNC/virtual paths and open Windows Explorer (localhost).
_CRM_HTML_JAR_MANUAL_SCRIPT = """<script>
(function () {
  function flashBtn(btn, msg) {
    if (!btn) return;
    var o = btn.textContent;
    btn.textContent = msg;
    setTimeout(function () { btn.textContent = o; }, 1400);
  }
  function crmCopyText(text, btn) {
    if (!text) return;
    function done() { flashBtn(btn, 'Copied'); }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done).catch(function () {
        fallbackCopy(text, btn);
      });
    } else {
      fallbackCopy(text, btn);
    }
  }
  function fallbackCopy(text, btn) {
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.left = '-9999px';
    document.body.appendChild(ta);
    ta.select();
    try {
      document.execCommand('copy');
      flashBtn(btn, 'Copied');
    } catch (e) {
      window.prompt('Copy path (Ctrl+C):', text);
    } finally {
      document.body.removeChild(ta);
    }
  }
  window.crmCopyJarPathAttr = function (btn) {
    var p = btn.getAttribute('data-crm-path');
    if (!p) return;
    crmCopyText(p, btn);
  };
  window.crmJarExplorerTarget = function (fullPath) {
    var raw = String(fullPath || '').trim();
    var probe = raw.split('\\\\').join('/');
    var idx = probe.indexOf('!/');
    if (idx < 0) return raw;
    return raw.slice(0, idx);
  };
  window.crmOpenJarExplorerAttr = function (btn) {
    var p = btn.getAttribute('data-crm-path');
    if (!p) return;
    var target = window.crmJarExplorerTarget(p);
    fetch('/api/open-path-in-explorer', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: target }),
    })
      .then(function (r) {
        return r.json().then(function (j) {
          return { ok: r.ok, j: j };
        });
      })
      .then(function (x) {
        if (!x.ok) {
          var msg = (x.j && (x.j.error || x.j.message)) || 'Explorer open failed.';
          window.alert(msg + ' Use \\"Copy path\\" and paste into Explorer (or open the outer .jar/.zip manually).');
        }
      })
      .catch(function () {
        window.alert('Could not reach the app. Copy the path from the report and open it on the machine that has share access.');
      });
  };
})();
</script>"""


def _crm_html_jar_manual_helpers_script() -> str:
    """Embed once per CRM HTML page (enables Copy path / Explorer buttons)."""
    return _CRM_HTML_JAR_MANUAL_SCRIPT


def _crm_parse_config_file_filters(raw: str) -> list[str]:
    return [p.strip() for p in (raw or "").replace("\n", ",").split(",") if p.strip()]


def _crm_config_path_matches_any_filter(path: str, filters: list[str]) -> bool:
    if not filters:
        return True
    norm = str(path).replace("\\", "/")
    norm_lower = norm.lower()
    leaf = os.path.basename(norm_lower)
    for qraw in filters:
        q = qraw.replace("\\", "/").strip()
        if not q:
            continue
        q_low = q.lower()
        if any(ch in q for ch in "*?[]"):
            if fnmatch.fnmatch(norm_lower, q_low) or fnmatch.fnmatch(leaf, q_low):
                return True
        else:
            if q_low in norm_lower or leaf == q_low:
                return True
    return False


def _crm_filter_config_paths(paths: list[str], raw_filter: str) -> tuple[list[str], list[str]]:
    """Return (paths matching filter, token list). Empty raw_filter keeps all paths."""
    flt = _crm_parse_config_file_filters(raw_filter)
    if not flt:
        return (list(paths), [])
    return ([p for p in paths if _crm_config_path_matches_any_filter(p, flt)], flt)


def _crm_path_last_segment(virt: str) -> str:
    """Last path segment for a relative or virtual path (…!/nested!/file.ext → file.ext)."""
    s = (virt or "").replace("\\", "/")
    if "!/" in s:
        s = s.split("!/")[-1]
    s = s.strip("/")
    return os.path.basename(s) if s else ""


def _crm_path_leaf_is_bundle_artifact(virt: str) -> bool:
    """True if the leaf name is a .jar / .war / .ear / .zip (nested archive), not a text file."""
    leaf = _crm_path_last_segment(virt).lower()
    return bool(leaf) and leaf.endswith((".jar", ".war", ".ear", ".zip"))


_CRM_CLASSIC_CONFIG_EXTS: tuple[str, ...] = (
    ".conf",
    ".config",
    ".xml",
    ".properties",
    ".cfg",
)


def _walk_base_path(folder: Path) -> Path:
    """
    Root path for os.walk / relative_to on Windows: uses \\?\\ prefix so UNC and
    long paths (>260 chars) work the same as in folder_compare / ASCF.
    """
    if os.name != "nt":
        return folder
    try:
        lp = _to_long_path(folder)
        return lp if isinstance(lp, Path) else Path(lp)
    except Exception:
        return folder


def _fs_path_str(path: Path) -> str:
    """String path for open() / ZipFile on Windows (long-path UNC)."""
    if os.name != "nt":
        return str(path)
    try:
        return str(_to_long_path(path))
    except Exception:
        return str(path)


_CRM_ARCHIVE_SUFFIXES: tuple[str, ...] = (
    ".jar.pack.gz",
    ".war.pack.gz",
    ".ear.pack.gz",
    ".jar",
    ".war",
    ".ear",
    ".zip",
)


def _crm_path_is_archive_file(path: Path) -> bool:
    name = (path.name or "").lower()
    return any(name.endswith(s) for s in _CRM_ARCHIVE_SUFFIXES)


def _jar_tool_cmd() -> str:
    """Resolve jar executable (JAVA_HOME first) for nested archive fallbacks on Windows."""
    java_home = os.environ.get("JAVA_HOME", "")
    if java_home:
        for name in ("jar.exe", "jar"):
            candidate = os.path.join(java_home, "bin", name)
            if os.path.isfile(candidate):
                return candidate
    return "jar"
def _crm_jar_pair_downloads_html(
    jar_old: Optional[str],
    jar_new: Optional[str],
    err: object,
    jar_old_source: Optional[str] = None,
    jar_new_source: Optional[str] = None,
) -> str:
    """
    Status / download column: signed links to the Flask /download-crm-jar route so the browser
    saves the resolved JAR from each build (old vs new). Use jar_old / jar_new, not build roots.
    Also adds Copy path / Explorer buttons so users can fetch JARs manually from the share.
    """
    if err:
        return html.escape(str(err))
    btn_style = (
        "font-size:11px;padding:2px 7px;margin-left:4px;border-radius:6px;"
        "border:1px solid #cbd5e1;background:#f8fafc;cursor:pointer;color:#334155;"
    )
    parts: list[str] = []
    for side, p, src in (
        ("old", jar_old, jar_old_source),
        ("new", jar_new, jar_new_source),
    ):
        ps = (p or "").strip()
        srcs = (src or "").strip()
        dl_path = ps or srcs
        manual_copy = srcs or ps
        if not dl_path:
            parts.append(
                f"<span style=\"color:#94a3b8;\" title=\"No JAR on the {side} build for this row\">n/a</span>"
            )
            continue
        try:
            tok = make_jar_download_token(dl_path, source_path=srcs or None)
        except ValueError:
            parts.append("—")
            continue
        q = quote(tok, safe="")
        base = os.path.basename(dl_path.replace("\\", "/")) or f"{side}.jar"
        tip = f"Download {side} build JAR: {base}"
        link_html = (
            f"<a href=\"/download-crm-jar?token={q}\" style=\"color:#2563eb;font-weight:600;\""
            f" title=\"{html.escape(tip, quote=True)}\">{html.escape(base)}</a>"
        )
        esc_manual = html.escape(manual_copy, quote=True) if manual_copy else ""
        btns = ""
        if manual_copy:
            btns = (
                f"<button type=\"button\" style=\"{btn_style}\" data-crm-path=\"{esc_manual}\" "
                f"onclick=\"crmCopyJarPathAttr(this)\" title=\"Copy full path (UNC or virtual SDK path)\">"
                f"Copy path</button>"
                f"<button type=\"button\" style=\"{btn_style}\" data-crm-path=\"{esc_manual}\" "
                f"onclick=\"crmOpenJarExplorerAttr(this)\" "
                f"title=\"Open in Explorer (localhost Windows). For nested paths, opens the outer archive.\">"
                f"Explorer</button>"
            )
        parts.append(
            f"<span style=\"white-space:normal;line-height:1.55;display:inline-block;vertical-align:top;\">"
            f"{link_html}"
            f"{('<span style=\"white-space:nowrap;margin-left:6px;\">' + btns + '</span>') if btns else ''}"
            f"<span style=\"font-size:10px;color:#94a3b8;margin-left:4px;\">({html.escape(side)})</span>"
            f"</span>"
        )
    return (
        "<span style=\"white-space:normal;line-height:1.5;\">"
        + "<span style=\"color:#cbd5e1;\"> · </span>".join(parts)
        + "</span>"
    )


def _crm_jar_download_footnote_html() -> str:
    return (
        "<p style=\"font-size:12px;color:#64748b;margin:10px 0 0 0;line-height:1.5;\">"
        "<strong>Download JARs (web)</strong> uses <code>/download-crm-jar</code> when the app can read the file. "
        "If that fails, use <strong>Copy path</strong> next to each link and paste into Windows Explorer’s address bar "
        "(or open the outer <code>.jar</code>/<code>.zip</code> manually when the path contains <code>!/</code>). "
        "<strong>Explorer</strong> asks the app (localhost only) to open the path in File Explorer; from another PC use Copy path. "
        "Open this report via <em>Browse comparison reports</em> so links target your running app.</p>"
    )


def _crm_html_jar_paths_list_cell(paths: object, *, cap: int = 16) -> str:
    pl = [str(p) for p in (paths or []) if p]
    if not pl:
        return "<span style=\"color:#94a3b8;\">—</span>"
    show = pl[:cap]
    more = (
        f"<div style=\"font-size:11px;color:#64748b;margin-top:4px;\">… and {len(pl) - cap} more</div>"
        if len(pl) > cap
        else ""
    )
    li = "".join(
        f"<li style='margin:2px 0;word-break:break-all;'>{html.escape(x)}</li>" for x in show
    )
    return f"<ol style='margin:0;padding-left:18px;font-size:11px;color:#334155;max-height:150px;overflow:auto;'>{li}</ol>{more}"


def _crm_parse_explicit_size_tokens(raw: str) -> list[str]:
    """Comma-separated file names / globs for optional size-compare table."""
    return [p.strip() for p in (raw or "").replace("\n", ",").split(",") if p.strip()]


def _format_zip_crc_display(crc: Optional[int]) -> str:
    """Human-readable CRC-32 for ZIP members (or 'n/a' if unknown)."""
    if crc is None:
        return "n/a"
    return f"0x{(crc & 0xFFFFFFFF):08X}"


# ============================================================================
# DATA CLASSES
# ============================================================================

@dataclass
class CRMComparisonResult:
    """Result of a CRM file comparison."""
    relative_path: str
    status: str  # 'added', 'removed', 'modified', 'unchanged'
    size_folder1: Optional[int] = None
    size_folder2: Optional[int] = None
    hash_folder1: Optional[str] = None
    hash_folder2: Optional[str] = None
    modified_time_folder1: str = ""
    modified_time_folder2: str = ""
    details: str = ""
    archive_diff: Optional[dict] = None  # Deep nested archive comparison details
    is_archive: bool = False  # Whether this is an archive file (JAR/WAR/EAR/ZIP)


@dataclass
class _ExplicitSizeHit:
    """One row for the explicit file-size table (top-level or archive member)."""
    relative_path: str
    status: str
    size_folder1: Optional[int] = None
    size_folder2: Optional[int] = None


def _crm_norm_rel_path(rel: str) -> str:
    return str(rel).replace("\\", "/")


def _crm_find_counterpart_key(files_map: dict[str, object], rel_path: str) -> Optional[str]:
    """
    Return the actual key in files_map for this relative path, using POSIX slashes and
    case-insensitive comparison (Windows builds often differ only by path casing).
    """
    if rel_path in files_map:
        return rel_path
    rl = _crm_norm_rel_path(rel_path).lower()
    for k in files_map:
        if _crm_norm_rel_path(k).lower() == rl:
            return k
    return None


def _crm_strip_versionish_tokens(name: str) -> str:
    """
    Remove common CRM/build version tokens from a name for similarity pairing.
    Examples: __V9_22_1_15, -9.22.1.15, _20260424_1633, _hf16, _pb1.
    """
    s = str(name or "").strip()
    if not s:
        return s
    s = re.sub(r"__v\d+(?:[_\.]\d+)*", "", s, flags=re.IGNORECASE)
    s = re.sub(r"[_-](?:hfb|hf|pb)\d+\b", "", s, flags=re.IGNORECASE)
    s = re.sub(r"[_-]v?\d+(?:[._]\d+){1,7}\b", "", s, flags=re.IGNORECASE)
    s = re.sub(r"[_-]\d{8}(?:[_-]\d{4})?\b", "", s, flags=re.IGNORECASE)
    s = re.sub(r"[_-]build\d+\b", "", s, flags=re.IGNORECASE)
    s = re.sub(r"[_-]{2,}", "_", s)
    s = s.strip("_- .")
    return s.lower()


def _crm_alignment_signature(path_value: str) -> str:
    """
    Normalize a relative path to compare naming convention while ignoring version tokens.
    """
    rp = _crm_norm_rel_path(path_value).lower().strip("/")
    if not rp:
        return rp
    parts = [p for p in rp.split("/") if p]
    norm_parts = [_crm_strip_versionish_tokens(p) or p for p in parts]
    return "/".join(norm_parts)


def _crm_align_removed_added(
    removed: list[CRMComparisonResult],
    added: list[CRMComparisonResult],
    *,
    min_ratio: float = 0.52,
    signature_fn: Optional[Callable[[str], str]] = None,
) -> tuple[
    list[tuple[CRMComparisonResult, CRMComparisonResult, float]],
    list[CRMComparisonResult],
    list[CRMComparisonResult],
]:
    """
    Best-effort pairing of removed vs added paths (Beyond Compare–style rename alignment).
    Greedy match by path similarity; unpaired rows are orphans on that side.
    """
    if not removed or not added:
        return [], list(removed), list(added)

    sig_fn = signature_fn or _crm_alignment_signature

    n_r, n_a = len(removed), len(added)
    if n_r * n_a > _CRM_ALIGN_MAX_PAIR_PRODUCT:
        logger.warning(
            "[CRM] Greedy removed/added alignment budget exceeded ("
            f"{n_r:,} × {n_a:,} > {_CRM_ALIGN_MAX_PAIR_PRODUCT:,}); "
            "using release-neutral signature bucket pairing."
        )
        return _crm_align_removed_added_signature_bucket(
            removed, added, signature_fn=sig_fn
        )

    def _score_pair(r: CRMComparisonResult, a: CRMComparisonResult) -> float:
        rp = _crm_norm_rel_path(r.relative_path)
        ap = _crm_norm_rel_path(a.relative_path)
        ratio = difflib.SequenceMatcher(None, rp, ap).ratio()
        sig_r = sig_fn(rp)
        sig_a = sig_fn(ap)
        if sig_r and sig_a and sig_r == sig_a:
            return 1.0
        pr = Path(rp)
        pa = Path(ap)
        if pr.parent == pa.parent:
            ratio = min(1.0, ratio + 0.18)
            br = difflib.SequenceMatcher(None, pr.name, pa.name).ratio()
            ratio = min(1.0, ratio + 0.12 * br)
            if _crm_strip_versionish_tokens(pr.name) == _crm_strip_versionish_tokens(pa.name):
                ratio = min(1.0, ratio + 0.45)
        if _crm_strip_versionish_tokens(pr.name) == _crm_strip_versionish_tokens(pa.name):
            ratio = min(1.0, ratio + 0.30)
        return ratio

    scored: list[tuple[float, int, int]] = []
    for ri, r in enumerate(removed):
        for aj, a in enumerate(added):
            sc = _score_pair(r, a)
            if sc >= min_ratio:
                scored.append((sc, ri, aj))
    scored.sort(key=lambda t: t[0], reverse=True)

    used_r: set[int] = set()
    used_a: set[int] = set()
    pairs: list[tuple[CRMComparisonResult, CRMComparisonResult, float]] = []
    for sc, ri, aj in scored:
        if ri in used_r or aj in used_a:
            continue
        used_r.add(ri)
        used_a.add(aj)
        pairs.append((removed[ri], added[aj], sc))

    unpaired_removed = [removed[i] for i in range(len(removed)) if i not in used_r]
    unpaired_added = [added[j] for j in range(len(added)) if j not in used_a]
    pairs.sort(key=lambda p: _crm_norm_rel_path(p[0].relative_path).lower())
    return pairs, unpaired_removed, unpaired_added


def _crm_align_removed_added_signature_bucket(
    removed: list[CRMComparisonResult],
    added: list[CRMComparisonResult],
    *,
    signature_fn: Callable[[str], str],
) -> tuple[
    list[tuple[CRMComparisonResult, CRMComparisonResult, float]],
    list[CRMComparisonResult],
    list[CRMComparisonResult],
]:
    """
    O(n) pairing by release-neutral path signature (CRM_10.24 ↔ CRM_10.25, lib ↔ modules/crm/lib).
    Used when greedy similarity alignment exceeds the pairwise budget.
    """
    if not removed or not added:
        return [], list(removed), list(added)

    old_buckets: dict[str, list[CRMComparisonResult]] = defaultdict(list)
    new_buckets: dict[str, list[CRMComparisonResult]] = defaultdict(list)
    for r in removed:
        sig = signature_fn(_crm_norm_rel_path(r.relative_path))
        if sig:
            old_buckets[sig].append(r)
    for a in added:
        sig = signature_fn(_crm_norm_rel_path(a.relative_path))
        if sig:
            new_buckets[sig].append(a)

    pairs: list[tuple[CRMComparisonResult, CRMComparisonResult, float]] = []
    unpaired_removed: list[CRMComparisonResult] = []
    unpaired_added: list[CRMComparisonResult] = []
    for sig in sorted(set(old_buckets.keys()) | set(new_buckets.keys())):
        olds = sorted(old_buckets.get(sig, []), key=lambda r: r.relative_path.lower())
        news = sorted(new_buckets.get(sig, []), key=lambda r: r.relative_path.lower())
        while olds and news:
            pairs.append((olds.pop(0), news.pop(0), 1.0))
        unpaired_removed.extend(olds)
        unpaired_added.extend(news)

    pairs.sort(key=lambda p: _crm_norm_rel_path(p[0].relative_path).lower())
    return pairs, unpaired_removed, unpaired_added


@dataclass
class CRMArchiveEntry:
    """Entry comparison result for archive contents."""
    archive_name: str
    member_path: str
    status: str  # 'only_in_old', 'only_in_new', 'modified', 'unchanged'
    size_old: Optional[int] = None
    size_new: Optional[int] = None
    crc_old: Optional[int] = None
    crc_new: Optional[int] = None


@dataclass
class CRMJarManifestResult:
    """Result of JAR manifest version comparison."""
    jar_name: str
    path_folder1: str = ""
    path_folder2: str = ""
    version_folder1: str = ""
    version_folder2: str = ""
    error_folder1: str = ""
    error_folder2: str = ""


# ============================================================================
# CRM FOLDER COMPARER (Full Content Comparison)
# ============================================================================

class CRMFolderComparer:
    """
    CRM-specific folder comparer for full content comparison.
    Compares all files in two CRM build folders.
    """
    
    def __init__(self, folder1: str, folder2: str):
        """Initialize with two folder paths."""
        self.folder1_path = Path(folder1)
        self.folder2_path = Path(folder2)
        self.results: list[CRMComparisonResult] = []
        self.max_retries = 3
        self.retry_delay = 1.0
        self.suppress_warnings = True
        self.show_missing_only = False
        self.ignore_timestamp_only_differences = True
        self.fast_mode = True
        # Fast defaults for large CRM release folders; can be re-enabled via options.
        self.compare_archive_contents = False
        self.deep_nested_archives = False
        self.use_local_copy = False
        self.should_cleanup_temp_folders = True
        self.temp_folder1 = None
        self.temp_folder2 = None
        # Optional: after full compare, diff .class entries inside named JAR(s), comma-separated (saved in report)
        self.class_diff_jar_name: str = ""
        self.jar_class_diff_list: list[dict] = []
        # Optional: comma-separated names/globs; required to show "Text and config files changed" in HTML
        self.config_files_filter_query: str = ""
        # Optional: comma-separated basenames / globs / relative suffixes — HTML/TXT size table only (not the huge auto list).
        self.explicit_size_compare_query: str = ""
        # Optional: compare only MANIFEST.MF Implementation-Version for specified JARs.
        self.manifest_diff_jar_name: str = ""
        self.jar_manifest_diff_list: list[dict] = []
        # Temp files created when extracting a nested JAR for class diff (removed after diff)
        self._jar_class_diff_temp_paths: list[Path] = []
        # Maps extracted temp JAR path -> source archive member path for report visibility.
        self._jar_extracted_source_map: dict[str, str] = {}
        # Full-file temp copies of outer SDK archives used only for ClfyAgent compare (UNC → local read)
        self._clfy_temp_copy_paths: list[Path] = []
        # One resolved read path per outer archive (in-place UNC or single temp copy) — avoids repeated multi-GB copies
        self._clfy_outer_read_cache: dict[str, Path] = {}
        # Cache for nested explicit-size lookups (abs archive path, deep) -> (names, meta)
        self._archive_members_lookup_cache: dict[tuple[str, bool], tuple[set, dict]] = {}
        # Deep-compare ClfyAgent .zip/.ear; HTML/TXT use tree + optional JAR reports (not bulk .class in Clfy block)
        self.compare_clfy_agent_zip: bool = False
        self.run_full_tree_compare: bool = True
        self.compare_application: str = "CRM"
        self.clfy_agent_zip_diff: Optional[dict] = None
        # Optional explicit overrides for ClfyAgent .zip/.ear source path.
        self.clfy_agent_zip_override_old: str = ""
        self.clfy_agent_zip_override_new: str = ""
        # Progress tracking
        self.total_files = 0
        self.processed_files = 0
        self.current_file = ""
        self.progress_percentage = 0.0
        self.estimated_time_remaining = 0
        self.comparison_start_time = None
        self.comparison_error = None
        self.count_unchanged: int = 0
        # Set in save_report() so HTML can link to /view?file=<this>.txt on the comparison app
        self.report_txt_basename: Optional[str] = None
        # Per-run performance: full nested .jar basename index per on-disk archive (wildcard + resolve)
        self._nested_jar_basename_cache: dict[str, frozenset[str]] = {}
        # First matching virtual path inside archive for (archive_key, normalized query); skip repeated DFS
        self._jar_resolve_virtual_hits: dict[tuple[str, str], str] = {}
        # ClfyAgent bundle virtual paths per outer archive (jar tf / shallow scan) — avoid repeat on same SDK
        self._clfy_bundle_paths_cache: dict[str, list[str]] = {}
        self._foundation_skip_policy_logged = False
    
    @staticmethod
    def _stable_archive_cache_key(path: Path) -> str:
        """Identity + mtime + size so cached indexes invalidate if the file changes."""
        try:
            st = path.stat()
            return f"{os.path.normcase(os.path.abspath(str(path)))}::{st.st_mtime_ns}::{st.st_size}"
        except OSError:
            return os.path.normcase(str(path))
    
    def _add_all_jar_basenames_from_zip_bytes(
        self,
        data: bytes,
        depth: int,
        max_depth: int,
        out: set[str],
    ) -> None:
        if depth >= max_depth or not data or len(data) < 4:
            return
        if not data.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
            return
        try:
            with zipfile.ZipFile(BytesIO(data), "r") as zf:
                self._add_all_jar_basenames_from_zipfile(zf, depth, max_depth, out)
        except (zipfile.BadZipFile, OSError, RuntimeError):
            return
    
    def _add_all_jar_basenames_from_zipfile(
        self,
        zf: zipfile.ZipFile,
        depth: int,
        max_depth: int,
        out: set[str],
    ) -> None:
        exts = _CRM_CLASS_DIFF_NESTED_ARCHIVE_SUFFIXES
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = info.filename.replace("\\", "/").strip("/")
            if not name:
                continue
            nl = name.lower()
            if nl.endswith(".jar"):
                out.add(os.path.basename(name))
            if nl.endswith(exts) and depth + 1 < max_depth:
                try:
                    nested = zf.read(info.filename)
                    self._add_all_jar_basenames_from_zip_bytes(nested, depth + 1, max_depth, out)
                except Exception:
                    continue
    
    def _get_cached_nested_jar_basenames_disk(self, archive_path: Path, *, max_depth: int = 18) -> frozenset[str]:
        """All .jar basenames at any nesting depth inside one on-disk zip archive (cached per file)."""
        key = self._stable_archive_cache_key(archive_path)
        if key in self._nested_jar_basename_cache:
            return self._nested_jar_basename_cache[key]
        out: set[str] = set()
        arc = archive_path
        if self._is_network_path(archive_path) and not self._zipfile_opens_ok(archive_path):
            local_arc = self._copy_file_to_temp_for_clfy_read(archive_path)
            if local_arc:
                arc = local_arc
        try:
            with zipfile.ZipFile(_fs_path_str(arc), "r") as zf:
                self._add_all_jar_basenames_from_zipfile(zf, 0, max_depth, out)
        except (zipfile.BadZipFile, OSError, RuntimeError) as e:
            logger.debug(f"[CRM] nested JAR index failed for {archive_path}: {e}")
        fs = frozenset(out)
        self._nested_jar_basename_cache[key] = fs
        return fs
    
    def _companion_txt_report_link_html(self, *, for_dark_header: bool = False) -> str:
        """HTML fragment: link to open the saved .txt via server route /view?file=... (builtin_server)."""
        bn = getattr(self, "report_txt_basename", None)
        if not bn:
            return ""
        href = f"/view?file={quote(str(bn), safe='')}"
        safe_bn = html.escape(str(bn))
        esc_href = html.escape(href)
        if for_dark_header:
            return (
                f'<div style="margin-top:12px;padding:10px 12px;background:rgba(255,255,255,0.12);border-radius:8px;border:1px solid rgba(255,255,255,0.2);">'
                f'<a href="{esc_href}" target="_blank" rel="noopener noreferrer" '
                f'style="color:#fff;font-weight:600;text-decoration:underline;">Open companion text report ({safe_bn})</a>'
                f'<span style="font-size:11px;opacity:0.9;margin-left:10px;">Full listing; works when this report is opened from the comparison app.</span>'
                f"</div>"
            )
        return (
            f'<div style="margin:10px 0;padding:10px 14px;background:#eff6ff;border:1px solid #93c5fd;border-radius:8px;font-size:13px;line-height:1.5;">'
            f'<strong style="color:#1e3a8a;">Plain-text report:</strong> '
            f'<a href="{esc_href}" target="_blank" rel="noopener noreferrer" style="color:#1d4ed8;font-weight:600;">{safe_bn}</a>'
            f'<span style="color:#64748b;font-size:12px;"> — click to open in the browser (comparison app server).</span>'
            f"</div>"
        )
    
    def _is_network_path(self, file_path) -> bool:
        """Check if the path is a UNC network path."""
        path_str = str(file_path)
        return (
            path_str.startswith("\\\\")
            or path_str.startswith("//")
            or path_str.lower().startswith("\\\\?\\unc\\")
        )

    def _zipfile_opens_ok(self, path: Path) -> bool:
        """True if ZipFile can read the archive without a full local copy (saves disk on UNC SDK .jar)."""
        try:
            with zipfile.ZipFile(_fs_path_str(path), "r") as zf:
                zf.infolist()[:1]
            return True
        except Exception:
            return False

    def _crm_mkstemp_with_space(self, suffix: str, prefix: str, min_bytes: int) -> tuple[int, str]:
        """
        Like tempfile.mkstemp but prefers volumes with enough free space (Errno 28 mitigation).
        """
        margin = max(256 * 1024 * 1024, min_bytes // 8)
        need = min_bytes + margin
        dirs = get_compare_temp_dir_candidates(need)
        last_err: Optional[OSError] = None
        for d in dirs:
            try:
                d.mkdir(parents=True, exist_ok=True)
                return tempfile.mkstemp(suffix=suffix, prefix=prefix, dir=str(d))
            except OSError as e:
                last_err = e
                continue
        # Last resort: default temp (may still raise)
        return tempfile.mkstemp(suffix=suffix, prefix=prefix)

    def _copy_file_to_temp_for_clfy_read(self, src: Path) -> Optional[Path]:
        """
        Return a path ZipFile/jar can read for ``src``: reuse a cached temp copy, read UNC in place
        when ZipFile works, or copy once per outer archive per comparison run.
        """
        if not _path_is_file(src) or not _path_exists(src):
            return None
        cache_key = self._stable_archive_cache_key(src)
        cached = self._clfy_outer_read_cache.get(cache_key)
        if cached is not None and _path_exists(cached) and _path_is_file(cached):
            return cached
        if self._zipfile_opens_ok(src):
            self._clfy_outer_read_cache[cache_key] = src
            return src
        try:
            try:
                sz = src.stat().st_size
            except OSError:
                sz = 0
            fd, name = self._crm_mkstemp_with_space(
                src.suffix or ".jar", "crm_clfy_outer_", sz
            )
            os.close(fd)
            dst = Path(name)
            for attempt in range(self.max_retries):
                try:
                    shutil.copy2(_fs_path_str(src), _fs_path_str(dst))
                    self._clfy_temp_copy_paths.append(dst)
                    print(
                        f"[CRM] ClfyAgent: copied outer archive to local temp for read "
                        f"({src.name} -> {dst})"
                    )
                    self._clfy_outer_read_cache[cache_key] = dst
                    return dst
                except OSError as e:
                    err = getattr(e, "winerror", getattr(e, "errno", 0))
                    if err in (53, 59, 67, 1231, 1326) and attempt < self.max_retries - 1:
                        wait_time = self.retry_delay * (2**attempt)
                        logger.warning(
                            f"[CRM] ClfyAgent outer copy attempt {attempt + 1}/{self.max_retries}: {e}"
                        )
                        time.sleep(wait_time)
                    else:
                        logger.warning(f"[CRM] ClfyAgent outer copy failed: {e}")
                        break
            try:
                dst.unlink(missing_ok=True)
            except OSError:
                pass
            rem = self._copy_outer_to_remote_temp_for_clfy(src)
            if rem:
                self._clfy_outer_read_cache[cache_key] = rem
                return rem
            return None
        except Exception as e:
            logger.warning(f"[CRM] ClfyAgent could not create temp copy of {src}: {e}")
            rem = self._copy_outer_to_remote_temp_for_clfy(src)
            if rem:
                self._clfy_outer_read_cache[cache_key] = rem
                return rem
            return None

    @staticmethod
    def _remote_compare_temp_root() -> str:
        try:
            from client_temp_paths import remote_fallback_temp_root

            return remote_fallback_temp_root()
        except ImportError:
            return (os.environ.get("CRM_REMOTE_TEMP_ROOT") or r"\\inpnqbdnc04\software\crm_compare_temp").strip()

    def _is_under_remote_compare_root(self, p: Path) -> bool:
        root = self._remote_compare_temp_root()
        if not root:
            return False
        try:
            rp = p.resolve()
        except OSError:
            rp = p
        try:
            rr = Path(root).resolve()
        except OSError:
            rr = Path(root)
        srp, srr = str(rp).lower(), str(rr).lower()
        return srp == srr or srp.startswith(srr.rstrip("\\") + "\\")

    def _copy_outer_to_remote_temp_for_clfy(self, src: Path) -> Optional[Path]:
        """
        Copy an outer SDK/archive to CRM_REMOTE_TEMP_ROOT when local temp is full or unavailable.
        Skips if src is already under that folder (avoid duplicate multi-GB copies).
        """
        if not _path_is_file(src) or not _path_exists(src):
            return None
        cache_key = self._stable_archive_cache_key(src)
        cached = self._clfy_outer_read_cache.get(cache_key)
        if cached is not None and _path_exists(cached) and _path_is_file(cached):
            return cached
        root = self._remote_compare_temp_root()
        if not root:
            return None
        try:
            if self._is_under_remote_compare_root(src):
                return None
        except Exception:
            pass
        base = Path(root)
        dst: Optional[Path] = None
        try:
            base.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(
                suffix=src.suffix or ".jar",
                prefix="crm_clfy_outer_remote_",
                dir=str(base),
            )
            os.close(fd)
            dst = Path(name)
            shutil.copy2(_fs_path_str(src), _fs_path_str(dst))
            self._clfy_temp_copy_paths.append(dst)
            print(
                f"[CRM] ClfyAgent: copied outer archive to fallback compare temp "
                f"(client/server full — {root}; {src.name} -> {dst})"
            )
            self._clfy_outer_read_cache[cache_key] = dst
            return dst
        except OSError as e:
            logger.warning(f"[CRM] Remote compare temp copy failed ({root}): {e}")
            if dst is not None:
                try:
                    dst.unlink(missing_ok=True)
                except OSError:
                    pass
            return None

    def _normalize_virtual_member_path(self, outer: Path, member: str) -> str:
        """Strip duplicated outer-archive prefixes from a virtual member path."""
        raw = (member or "").replace("\\", "/").strip()
        if not raw:
            return raw
        shell = self._parse_shell_zip_appended_path(Path(raw))
        if shell is not None:
            outer_shell, inner = shell
            if outer_shell.name.lower() == outer.name.lower() or str(outer_shell).lower() == str(outer).lower():
                raw = self._shell_inner_to_virtual_member_path(inner)
            else:
                raw = f"{outer_shell.name}!/{self._shell_inner_to_virtual_member_path(inner)}"
        used_nested_sep = "!/" in raw or "!\\" in raw
        parts = [p.strip("/") for p in re.split(r"![\\/]", raw) if p.strip("/")]
        if not parts:
            return raw.strip("/")
        outer_name = outer.name.lower()
        while parts:
            head = parts[0].lower()
            if head == outer_name or head.endswith("/" + outer_name):
                parts.pop(0)
                continue
            break
        if not parts:
            return raw.strip("/")
        if len(parts) == 1:
            return parts[0]
        return "!/".join(parts) if used_nested_sep else parts[0]

    def _resolve_outer_archive_for_zip(self, outer_path: Path) -> Optional[Path]:
        """Return a path ZipFile can read (local copy / gzip-wrapped jar when needed)."""
        if not outer_path or not _path_exists(outer_path) or not _path_is_file(outer_path):
            return None
        if self._zipfile_opens_ok(outer_path):
            return outer_path
        local = self._copy_file_to_temp_for_clfy_read(outer_path)
        if local and self._zipfile_opens_ok(local):
            return local
        candidate = local or outer_path
        if candidate.name.lower().endswith(".gz"):
            try:
                import gzip

                with gzip.open(_fs_path_str(candidate), "rb") as gf:
                    payload = gf.read()
                if payload.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
                    fd, name = self._crm_mkstemp_with_space(
                        ".jar", "crm_unpack_gz_", len(payload)
                    )
                    try:
                        os.write(fd, payload)
                    finally:
                        os.close(fd)
                    unpacked = Path(name)
                    self._clfy_temp_copy_paths.append(unpacked)
                    if self._zipfile_opens_ok(unpacked):
                        print(
                            f"[CRM] Nested extract: decompressed gzip archive wrapper for {outer_path.name}"
                        )
                        return unpacked
            except Exception as e:
                logger.debug(f"[CRM] gzip archive unwrap failed for {outer_path}: {e}")
        return local

    def _collect_sdk_archive_candidates(
        self, walk_base: Path, *, limit: int = 50
    ) -> list[tuple[tuple, Path]]:
        """Prefer SDK-named archives via targeted globs before a full tree walk (NAS speed)."""
        ranked: list[tuple[tuple, Path]] = []
        seen: set[str] = set()

        def _add(full: Path) -> None:
            if not _path_is_file(full) or not _crm_path_is_archive_file(full):
                return
            key = os.path.normcase(str(full))
            if key in seen:
                return
            seen.add(key)
            fl = full.name.lower()
            pri = (
                0 if fl.startswith("sdk") else 1,
                0 if "output" in str(full).lower() else 1,
                len(str(full)),
                fl,
            )
            ranked.append((pri, full))

        for pattern in ("SDK*.jar", "SDK*.ear", "SDK*.zip", "*SDK*.jar", "*SDK*.ear"):
            try:
                for full in walk_base.glob(f"**/{pattern}"):
                    _add(full)
                    if len(ranked) >= limit:
                        break
            except Exception:
                pass
            if len(ranked) >= limit:
                break
        if ranked:
            ranked.sort(key=lambda x: x[0])
            return ranked[:limit]
        try:
            for r, _, fnames in os.walk(str(walk_base)):
                for fn in fnames:
                    _add(Path(r) / fn)
                    if len(ranked) >= limit:
                        break
                if len(ranked) >= limit:
                    break
        except Exception as e:
            logger.debug(f"[CRM] SDK archive walk failed under {walk_base}: {e}")
            return []
        ranked.sort(key=lambda x: x[0])
        return ranked[:limit]

    def _find_sdk_archive_with_clfy_in_folder(
        self, folder: Path, peer_hint: Optional[str] = None
    ) -> Optional[dict]:
        """When the build root is a folder, locate an SDK/archive that contains ClfyAgent inside."""
        if not _path_is_dir(folder):
            return None
        walk_base = _walk_base_path(folder)
        print(
            f"[CRM] ClfyAgent: locating SDK archive under {walk_base}...",
            flush=True,
        )
        ranked = self._collect_sdk_archive_candidates(walk_base, limit=50)
        total = len(ranked)
        for idx, (_, arc) in enumerate(ranked[:50]):
            if idx == 0 or (idx + 1) % 3 == 0:
                print(
                    f"[CRM] ClfyAgent: checking SDK archive {idx + 1}/{total}: {arc.name}",
                    flush=True,
                )
            paths = self._list_clfy_paths_from_outer_archive(arc)
            if paths:
                virt = self._pick_basename_hits(paths, peer_hint or "", peer_hint)[0]
                print(
                    f"[CRM] ClfyAgent: discovered nested bundle via fast scan: {arc} -> {virt}",
                    flush=True,
                )
                return {
                    "path": arc,
                    "is_nested": True,
                    "archive_member": virt,
                    "rel_key": virt,
                }
        return None

    def _normalize_clfy_dispatch_info(
        self, info: dict, peer: Optional[dict] = None
    ) -> dict:
        """Normalize outer path + archive_member before nested ClfyAgent extraction."""
        d = dict(info or {})
        peer_member = str((peer or {}).get("archive_member") or (peer or {}).get("rel_key") or "")
        p = Path(str(d.get("path") or ""))
        shell = self._parse_shell_zip_appended_path(p)
        if shell is not None:
            outer, inner = shell
            d["path"] = outer
            d["is_nested"] = True
            member = self._shell_inner_to_virtual_member_path(inner)
            if os.path.basename(member).lower() not in _CLFY_AGENT_BUNDLE_BASENAMES_LOWER:
                resolved = self._resolve_outer_archive_for_zip(outer) or outer
                paths = self._list_clfy_paths_from_outer_archive(resolved)
                if paths:
                    member = self._pick_basename_hits(paths, peer_member, peer_member)[0]
            d["archive_member"] = self._normalize_virtual_member_path(outer, member)
            d.setdefault("rel_key", d["archive_member"])
            return d
        if _path_is_file(p) and _crm_path_is_archive_file(p):
            resolved = self._resolve_outer_archive_for_zip(p) or p
            d["path"] = resolved
            if d.get("is_nested"):
                member = self._normalize_virtual_member_path(
                    resolved, str(d.get("archive_member") or d.get("rel_key") or "")
                )
                if not member:
                    paths = self._list_clfy_paths_from_outer_archive(resolved)
                    if paths:
                        member = self._pick_basename_hits(paths, peer_member, peer_member)[0]
                if member:
                    d["archive_member"] = member
                    d.setdefault("rel_key", member)
            return d
        if _path_is_dir(p):
            found = self._find_sdk_archive_with_clfy_in_folder(p, peer_hint=peer_member)
            if found:
                return self._normalize_clfy_dispatch_info(found, peer)
        return d

    def _ensure_local_paths_for_clfy(self, info1: dict, info2: dict) -> tuple[dict, dict]:
        """
        For nested ClfyAgent compares, the outer path is often a UNC SDK .jar. Copy to local temp
        before extract/compare so reads match the reliability of use_local_copy for the main scan.
        """
        def patch(inf: dict) -> dict:
            d = dict(inf)
            p = Path(d.get("path", ""))
            d["_clfy_outer_src_path"] = p
            if not p.is_file() or not _path_exists(p):
                return d
            resolved = self._resolve_outer_archive_for_zip(p)
            if resolved and resolved != p:
                d["path"] = resolved
                p = resolved
            if not self._is_network_path(str(p)):
                return d
            # Avoid copying multi‑GB SDK .jar to a full temp when ZipFile can read UNC directly (fixes Errno 28).
            if self._zipfile_opens_ok(p):
                print(
                    "[CRM] ClfyAgent: outer SDK archive readable via ZipFile on network path — "
                    "skipping full local copy (set CRM_TEMP_DIR to another drive if reads fail)."
                )
                sys.stdout.flush()
                return d
            local = self._copy_file_to_temp_for_clfy_read(p)
            if local:
                d["path"] = local
            return d

        return patch(info1), patch(info2)

    def _clfy_refresh_member_from_peer_hint(self, info: dict, peer_archive_member: Optional[str]) -> dict:
        """
        Re-resolve ClfyAgent.zip / ClfyAgent.ear member path inside the outer SDK using `jar tf` + peer alignment.
        Use when scan-stored archive_member no longer matches the zip central directory.
        """
        out = dict(info)
        p = Path(out.get("path") or "")
        if not p.is_file() or not out.get("is_nested"):
            return out
        outer = self._resolve_outer_archive_for_zip(p) or p
        try:
            res = subprocess.run(
                [_jar_tool_cmd(), "tf", _fs_path_str(outer)],
                capture_output=True,
                text=True,
                timeout=300,
            )
            if res.returncode != 0 or not res.stdout:
                return out
            rows = [
                ln.strip().replace("\\", "/").strip("/")
                for ln in res.stdout.splitlines()
                if ln.strip()
            ]
            cand = [
                ln
                for ln in rows
                if os.path.basename(ln).lower() in _CLFY_AGENT_BUNDLE_BASENAMES_LOWER
            ]
            if not cand:
                return out
            if len(cand) > 1:
                cand = self._pick_basename_hits(cand, peer_archive_member or "", peer_archive_member)
            out["archive_member"] = cand[0]
            print(f"[CRM] ClfyAgent: refreshed archive_member -> {cand[0]!r}")
        except Exception as e:
            logger.debug(f"[CRM] ClfyAgent refresh archive_member failed: {e}")
        return out

    @staticmethod
    def _clfy_report_outer_path(info: dict) -> str:
        """Original outer archive path for report rows (before any temp copy)."""
        p = info.get("_clfy_outer_src_path")
        if p is not None:
            return str(p)
        return str(info.get("path", ""))

    def _logical_jar_source_path(self, jp: Optional[Path]) -> str:
        """
        Reportable JAR location inside the old/new build (SDK path or outer.jar!/member).
        When the tool extracted the JAR to a temp file, _jar_extracted_source_map holds the logical path.
        """
        if jp is None:
            return ""
        key = str(jp)
        return self._jar_extracted_source_map.get(key, key)
    
    def _update_progress(self, current_file=""):
        """Update progress tracking information."""
        if self.total_files > 0:
            self.progress_percentage = (self.processed_files / self.total_files) * 100
            if self.comparison_start_time and self.processed_files > 0:
                elapsed_time = time.time() - self.comparison_start_time
                avg_time_per_file = elapsed_time / self.processed_files
                remaining_files = self.total_files - self.processed_files
                self.estimated_time_remaining = int(avg_time_per_file * remaining_files)
        self.current_file = current_file

    def _cleanup_temp_artifacts(self) -> None:
        """
        Best-effort cleanup for CRM temp files created during comparison/report generation.
        This keeps local temp usage bounded on long-running server sessions.
        """
        # First remove tracked temp paths from current run.
        for p in self._jar_class_diff_temp_paths:
            try:
                if p.is_file():
                    p.unlink()
            except OSError:
                pass
            self._jar_extracted_source_map.pop(str(p), None)
        self._jar_class_diff_temp_paths.clear()

        for p in self._clfy_temp_copy_paths:
            try:
                if p.is_file():
                    p.unlink()
            except OSError:
                pass
        self._clfy_temp_copy_paths.clear()
        self._clfy_outer_read_cache.clear()

    def _cleanup_stale_temp_artifacts(self, max_age_hours: int = 2) -> int:
        """
        Delete stale CRM temp artifacts from the system temp folder before a new run.
        Only removes files/directories created by this tool using known prefixes.
        """
        tmp_root = Path(tempfile.gettempdir())
        prefixes = (
            "crm_class_diff_",
            "crm_clfy_outer_",
            "crm_clfy_agent_",
            "crm_nested_cmp_old_",
            "crm_nested_cmp_new_",
            "crm_clfy_jar_extract_",
            "crm_class_diff_jarxf_",
        )
        cutoff = time.time() - (max_age_hours * 3600)
        deleted = 0
        for prefix in prefixes:
            try:
                for p in tmp_root.glob(f"{prefix}*"):
                    try:
                        st = p.stat()
                    except OSError:
                        continue
                    # Keep very recent artifacts from active runs.
                    if st.st_mtime >= cutoff:
                        continue
                    try:
                        if p.is_dir():
                            shutil.rmtree(p, ignore_errors=True)
                            deleted += 1
                        elif p.is_file():
                            p.unlink()
                            deleted += 1
                    except OSError:
                        continue
            except Exception:
                continue
        return deleted
    
    def _get_file_hash(self, file_path: Path) -> Optional[str]:
        """Calculate MD5 hash of a file."""
        try:
            hasher = hashlib.md5()
            with open(_fs_path_str(file_path), 'rb') as f:
                for chunk in iter(lambda: f.read(65536), b''):
                    hasher.update(chunk)
            return hasher.hexdigest()
        except Exception as e:
            logger.warning(f"[CRM] Could not hash file {file_path}: {e}")
            return None
    
    def _get_all_files(self, folder: Path) -> dict:
        """Get all files in a folder with their relative paths.
        Also handles direct archive files (JAR/WAR/EAR/ZIP) by scanning inside them."""
        files = {}
        
        # Check if path is an archive file (not a directory)
        archive_extensions = ('.jar', '.war', '.ear', '.zip')
        path_str = str(folder).lower()
        is_archive_file = any(path_str.endswith(ext) for ext in archive_extensions)
        
        if is_archive_file and _path_exists(folder) and _path_is_file(folder):
            # Path is an archive file - scan inside it recursively
            print(f"[CRM] Scanning inside archive: {folder}")
            files = self._scan_archive_contents(folder)
            print(f"[CRM] Found {len(files)} files inside archive")
        else:
            # Path is a directory - use scandir DFS for much faster UNC traversal.
            walk_base = _walk_base_path(folder)
            base_str = str(walk_base)
            started = time.time()
            scanned = 0
            # Time-based heartbeat so it doesn't look stuck on large network trees.
            next_heartbeat = started + 10.0
            try:
                stack = [base_str]
                while stack:
                    cur = stack.pop()
                    try:
                        with os.scandir(cur) as it:
                            for entry in it:
                                try:
                                    if entry.is_dir(follow_symlinks=False):
                                        stack.append(entry.path)
                                        continue
                                    if not entry.is_file(follow_symlinks=False):
                                        continue
                                    stat_info = entry.stat(follow_symlinks=False)
                                    rel_path = entry.path[len(base_str):].lstrip("\\/").replace("\\", "/")
                                    files[rel_path] = {
                                        'path': Path(entry.path),
                                        'size': stat_info.st_size,
                                        'modified_time': (
                                            ""
                                            if self.fast_mode
                                            else datetime.fromtimestamp(stat_info.st_mtime).strftime('%Y-%m-%d %H:%M:%S')
                                        ),
                                        'is_archive': entry.name.lower().endswith(archive_extensions)
                                    }
                                    scanned += 1
                                    now = time.time()
                                    if now >= next_heartbeat:
                                        elapsed = int(now - started)
                                        print(f"[CRM] Scan progress {folder.name}: {scanned} files ({elapsed}s elapsed)")
                                        self.current_file = f"Scanning {folder.name}: {scanned} files"
                                        next_heartbeat = now + 10.0
                                except Exception as e:
                                    if not self.suppress_warnings:
                                        logger.warning(f"[CRM] Could not stat file {entry.path}: {e}")
                    except Exception as e:
                        if not self.suppress_warnings:
                            logger.warning(f"[CRM] Could not scan directory {cur}: {e}")
                elapsed = int(time.time() - started)
                print(f"[CRM] Scan complete {folder.name}: {scanned} files ({elapsed}s)")
            except Exception as e:
                logger.error(f"[CRM] Error walking folder {folder}: {e}")
        return files
    
    def _scan_archive_contents(self, archive_path: Path, prefix: str = "") -> dict:
        """Recursively scan an archive and return all nested files with their metadata."""
        print(f"[CRM] " + "=" * 60)
        print(f"[CRM] _scan_archive_contents called")
        print(f"[CRM] Archive: {archive_path}")
        print(f"[CRM] " + "=" * 60)
        
        files = {}
        archive_extensions = ('.jar', '.war', '.ear', '.zip')
        nested_jars_scanned = []
        class_files_found = []
        
        def _looks_like_zip_bytes(blob: bytes) -> bool:
            """Quick signature check to skip non-zip payloads named like archives."""
            if not blob or len(blob) < 4:
                return False
            # Local file header / empty archive EOCD / spanned marker
            return blob.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"))

        def scan_nested(zf_or_data, current_prefix: str, depth: int = 0, max_depth: int = 10):
            """Recursively scan nested archives including all class files."""
            if depth >= max_depth:
                logger.debug(f"[CRM] Max depth {max_depth} reached at {current_prefix}")
                return
            
            try:
                # If zf_or_data is bytes, wrap in BytesIO and open as ZipFile
                if isinstance(zf_or_data, bytes):
                    if not _looks_like_zip_bytes(zf_or_data):
                        logger.debug(f"[CRM] Skipping non-zip nested payload at {current_prefix}")
                        return
                    try:
                        with zipfile.ZipFile(BytesIO(zf_or_data), 'r') as nested_zf:
                            scan_nested(nested_zf, current_prefix, depth, max_depth)
                    except zipfile.BadZipFile as e:
                        logger.warning(f"[CRM] Bad ZIP file at {current_prefix}: {e}")
                        # Record that we couldn't read this archive
                        files[f"{current_prefix}[UNREADABLE-ARCHIVE]"] = {
                            'path': archive_path,
                            'size': len(zf_or_data) if zf_or_data else 0,
                            'crc': 0,
                            'is_nested': True,
                            'archive_member': f"{current_prefix}[UNREADABLE]",
                            'error': str(e)
                        }
                    except Exception as e:
                        logger.warning(f"[CRM] Error opening nested archive {current_prefix}: {e}")
                    return
                
                # zf_or_data is a ZipFile object
                for info in zf_or_data.infolist():
                    try:
                        if info.is_dir():
                            continue
                        
                        member_name = info.filename.replace('\\', '/').strip('/')
                        if not member_name:
                            continue
                        
                        full_path = f"{current_prefix}{member_name}" if current_prefix else member_name
                        ext = os.path.splitext(member_name)[1].lower()
                        
                        # Store file info (using archive_path for 'path' since we can't get a real path)
                        # Include class files and all other file types
                        is_archive_file = ext in archive_extensions
                        files[full_path] = {
                            'path': archive_path,  # Reference to containing archive
                            'size': info.file_size,
                            'crc': getattr(info, 'CRC', 0),
                            'is_nested': True,
                            'archive_member': full_path,
                            'is_class_file': ext == '.class',
                            'is_archive': is_archive_file,  # Flag for nested archive detection
                            'nested_depth': depth
                        }
                        
                        # Track class files for summary
                        if ext == '.class':
                            class_files_found.append(full_path)
                        
                        # Recursively scan nested archives (JAR, WAR, EAR, ZIP)
                        if ext in archive_extensions and self.deep_nested_archives:
                            if self._should_skip_class_scan_inside_nested_archive(member_name):
                                continue
                            nested_jars_scanned.append(full_path)
                            try:
                                nested_data = zf_or_data.read(info.filename)
                                if not _looks_like_zip_bytes(nested_data):
                                    logger.debug(f"[CRM] Skipping non-zip nested member: {full_path}")
                                    continue
                                nested_prefix = f"{full_path}!/"
                                print(f"[CRM] Scanning nested archive: {full_path} (depth={depth+1})")
                                scan_nested(nested_data, nested_prefix, depth + 1, max_depth)
                            except Exception as e:
                                logger.warning(f"[CRM] Could not scan nested archive {member_name}: {e}")
                                print(f"[CRM] Warning: Could not scan nested archive {member_name}: {e}")
                    except Exception as e:
                        logger.debug(f"[CRM] Error processing entry: {e}")
                        continue  # Skip problematic entries
            except Exception as e:
                logger.warning(f"[CRM] Error scanning archive at depth {depth}: {e}")
                print(f"[CRM] Error scanning archive at depth {depth}: {e}")
        
        def try_jar_command():
            """Use jar command to list archive contents."""
            try:
                # Try JAVA_HOME first
                java_home = os.environ.get('JAVA_HOME', '')
                jar_cmd = 'jar'
                if java_home and os.path.exists(os.path.join(java_home, 'bin', 'jar.exe')):
                    jar_cmd = os.path.join(java_home, 'bin', 'jar.exe')
                elif java_home and os.path.exists(os.path.join(java_home, 'bin', 'jar')):
                    jar_cmd = os.path.join(java_home, 'bin', 'jar')
                
                result = subprocess.run(
                    [jar_cmd, 'tf', str(archive_path)],
                    capture_output=True, text=True, timeout=300
                )
                if result.returncode == 0:
                    for line in result.stdout.strip().split('\n'):
                        name = line.strip().replace('\\', '/')
                        if name and not name.endswith('/'):
                            full_path = f"{prefix}{name}" if prefix else name
                            files[full_path] = {
                                'path': archive_path,
                                'size': 0,
                                'crc': 0,
                                'is_nested': True,
                                'archive_member': full_path
                            }
                    print(f"[CRM] Scanned {len(files)} entries using jar command")
                    return True
            except Exception as e:
                logger.debug(f"[CRM] jar command failed: {e}")
            return False
        
        def try_powershell_expand():
            """Use PowerShell to list archive contents (Windows only)."""
            try:
                # PowerShell command to list ZIP contents
                ps_cmd = f'''
                Add-Type -AssemblyName System.IO.Compression.FileSystem
                $zip = [System.IO.Compression.ZipFile]::OpenRead("{str(archive_path).replace(chr(92), chr(92)+chr(92))}")
                foreach ($entry in $zip.Entries) {{
                    if (-not $entry.FullName.EndsWith("/")) {{
                        Write-Output "$($entry.Length)|$($entry.Crc32)|$($entry.FullName)"
                    }}
                }}
                $zip.Dispose()
                '''
                result = subprocess.run(
                    ['powershell', '-NoProfile', '-Command', ps_cmd],
                    capture_output=True, text=True, timeout=600
                )
                if result.returncode == 0 and result.stdout.strip():
                    for line in result.stdout.strip().split('\n'):
                        line = line.strip()
                        if not line:
                            continue
                        parts = line.split('|', 2)
                        if len(parts) >= 3:
                            size_str, crc_str, name = parts
                            name = name.replace('\\', '/')
                            if name and not name.endswith('/'):
                                full_path = f"{prefix}{name}" if prefix else name
                                try:
                                    size = int(size_str) if size_str else 0
                                    crc = int(crc_str) if crc_str else 0
                                except ValueError:
                                    size, crc = 0, 0
                                files[full_path] = {
                                    'path': archive_path,
                                    'size': size,
                                    'crc': crc,
                                    'is_nested': True,
                                    'archive_member': full_path
                                }
                    print(f"[CRM] Scanned {len(files)} entries using PowerShell")
                    return True
            except Exception as e:
                logger.debug(f"[CRM] PowerShell fallback failed: {e}")
            return False
        
        def try_copy_and_scan():
            """Copy file to temp and try scanning again (helps with network files)."""
            try:
                with tempfile.NamedTemporaryFile(suffix='.jar', delete=False) as tmp:
                    tmp_path = tmp.name
                try:
                    print(f"[CRM] Copying archive to temp location...")
                    shutil.copy2(_fs_path_str(archive_path), tmp_path)
                    print(f"[CRM] Copied, now scanning temp file...")
                    with zipfile.ZipFile(tmp_path, 'r') as zf:
                        scan_nested(zf, prefix, depth=0, max_depth=10)
                    if files:
                        print(f"[CRM] Successfully scanned {len(files)} entries from temp copy")
                        return True
                except Exception as e:
                    logger.debug(f"[CRM] Temp copy scan failed: {e}")
                finally:
                    try:
                        os.unlink(tmp_path)
                    except Exception:
                        pass
            except Exception as e:
                logger.debug(f"[CRM] Copy and scan failed: {e}")
            return False
        
        # Check if this is a network path - if so, copy locally first
        ap_s = str(archive_path)
        is_network = (
            ap_s.startswith("\\\\")
            or ap_s.startswith("//")
            or ap_s.startswith("\\\\?\\UNC\\")
        )
        
        if is_network:
            print(f"[CRM] Network path detected, copying to local temp first...")
            # Copy to local temp and scan
            if try_copy_and_scan():
                return files
            print(f"[CRM] Local copy failed, trying direct access...")
        
        # Try standard zipfile first
        try:
            with zipfile.ZipFile(_fs_path_str(archive_path), 'r') as zf:
                scan_nested(zf, prefix, depth=0, max_depth=10)
                if files:
                    print(f"[CRM] Successfully scanned {len(files)} entries with zipfile")
                    return files
        except Exception as e:
            error_msg = str(e).lower()
            if 'corrupt' in error_msg or 'extra field' in error_msg:
                print(f"[CRM] Archive has corrupt extra fields, trying fallback methods...")
            else:
                print(f"[CRM] zipfile failed: {e}, trying fallbacks...")
        
        # Try copying to temp (if not already tried for network path)
        if not files and not is_network:
            if try_copy_and_scan():
                return files
        
        # Try PowerShell fallback (Windows)
        if not files and os.name == 'nt':
            try_powershell_expand()
        
        # Try jar command as fallback
        if not files:
            try_jar_command()
        
        # Print summary of what was scanned
        print(f"[CRM] " + "-" * 60)
        print(f"[CRM] SCAN COMPLETE for {archive_path.name if hasattr(archive_path, 'name') else archive_path}")
        print(f"[CRM] Total files found: {len(files)}")
        print(f"[CRM] Nested JARs scanned: {len(nested_jars_scanned)}")
        print(f"[CRM] Class files found: {len(class_files_found)}")
        
        # Show ClfyCore-related files
        clfy_files = [f for f in files.keys() if 'clfycore' in f.lower()]
        if clfy_files:
            print(f"[CRM] Files containing 'ClfyCore': {len(clfy_files)}")
            for f in clfy_files[:10]:
                print(f"[CRM]   - {f}")
            if len(clfy_files) > 10:
                print(f"[CRM]   ... and {len(clfy_files) - 10} more")
        
        # Show config files
        config_files = [f for f in files.keys() if f.lower().endswith(('.conf', '.config', '.xml', '.properties'))]
        if config_files:
            print(f"[CRM] Config files found: {len(config_files)}")
            for f in config_files[:5]:
                print(f"[CRM]   - {f}")
        
        print(f"[CRM] " + "-" * 60)
        
        return files
    
    def _normalize_archive_filename(self, filename: str) -> str:
        """Normalize archive filename for comparison."""
        base, ext = os.path.splitext(filename)
        base = re.sub(r'[-_]unsigned$', '', base, flags=re.IGNORECASE)
        base = re.sub(r'__+V\d+(?:[_\.]\d+)*', '', base, flags=re.IGNORECASE)
        base = re.sub(r'_\d+\.\d+(?:\.\d+)*(?:\.\d+)*_', '_', base)
        base = re.sub(r'[-_]\d+\.\d+(?:\.\d+)*(?:-[A-Za-z0-9]+)*$', '', base)
        base = re.sub(r'[-_](?:b?\d{3,}|build-?\d+|\d{8}(?:_\d{4})?|\d{14})$', '', base)
        base = re.sub(r'_+', '_', base)
        base = base.strip('_-')
        return f"{base}{ext.lower()}"

    @staticmethod
    def _canonicalize_nested_archive_path(member_path: str) -> str:
        """
        Unify nested archive spellings so ``foo.war/WEB-INF`` matches ``foo.war!/WEB-INF``.
        EAR/WAR layouts often list inner paths with ``/`` after the archive leaf on one build
        and ``!/`` on the other; without this, identical .class files appear as only-in-old/new.
        """
        s = str(member_path or "").replace("\\", "/")
        if not s:
            return s
        for ext in (".jar", ".war", ".ear", ".zip"):
            pat = ext.replace(".", r"\.")
            s = re.sub(rf"({pat})/(?=[^/])", rf"\1!/", s, flags=re.IGNORECASE)
        return s

    def _member_match_key(self, member_path: str) -> str:
        """Stable key for matching nested archive members across builds and merge pairs."""
        return self._normalize_inner_archive_marker(
            self._canonicalize_nested_archive_path(member_path)
        ).lower()

    @staticmethod
    def _inner_member_tail_key(member_path: str) -> str:
        """Tail after ClfyAgent bundle for cross-location merge reconciliation."""
        s = CRMFolderComparer._canonicalize_nested_archive_path(member_path).lower()
        for marker in (
            "clfyagent.war!/",
            "clfyagent.war/",
            "clfyagent.ear!/",
            "clfyagent.ear/",
            "clfyagent.zip!/",
            "clfyagent.zip/",
        ):
            idx = s.find(marker)
            if idx >= 0:
                return s[idx + len(marker) :]
        if "!/" in s:
            return s.split("!/", 1)[-1]
        return s

    def _jar_inner_file_signature(self, member_path: str) -> str:
        """Path inside the innermost archive segment — matches same file across outer layout differences."""
        s = self._member_match_key(member_path)
        low = s.lower()
        cut = -1
        cut_len = 0
        for ext in (".jar!/", ".war!/", ".ear!/", ".zip!/"):
            idx = low.rfind(ext)
            if idx >= cut:
                cut = idx
                cut_len = len(ext)
        if cut >= 0:
            return low[cut + cut_len :]
        return low

    @staticmethod
    def _nested_path_is_strict_prefix(prefix_key: str, path_key: str) -> bool:
        if not prefix_key or not path_key or prefix_key == path_key:
            return False
        p = prefix_key.rstrip("/")
        q = path_key.rstrip("/")
        return q.startswith(p + "/") or q.startswith(p + "!/")

    def _drop_implicit_directory_only_members(
        self, only_members: set, full_other: set, common: set
    ) -> set:
        """
        Drop zip directory-marker paths (e.g. META-INF/maven) when the other build lists
        real files under that prefix — common when one EAR lists folder entries explicitly.
        """
        out = set(only_members)
        other_keys = {self._member_match_key(m) for m in full_other}
        common_keys = {self._member_match_key(m) for m in common}
        for p in list(out):
            pk = self._member_match_key(p)
            if any(self._nested_path_is_strict_prefix(pk, ok) for ok in other_keys):
                out.discard(p)
                continue
            if any(self._nested_path_is_strict_prefix(pk, ck) for ck in common_keys):
                out.discard(p)
        return out

    def _reconcile_only_in_member_sets(
        self, only_old: set, only_new: set
    ) -> tuple[set, set, list[tuple[str, str]]]:
        """
        Drop members that appear as only-old on one side and only-new on the other after
        multi-location ClfyAgent merges, or that share the same normalized nested path.
        Returns version-rename pairs (old path, new path) removed from the only-in sets.
        """
        o = {str(p) for p in (only_old or set()) if str(p).strip()}
        n = {str(p) for p in (only_new or set()) if str(p).strip()}
        version_pairs: list[tuple[str, str]] = []
        by_key_o = {self._member_match_key(p): p for p in o}
        by_key_n = {self._member_match_key(p): p for p in n}
        for k in set(by_key_o.keys()) & set(by_key_n.keys()):
            op, np = by_key_o[k], by_key_n[k]
            o.discard(op)
            n.discard(np)
            if op != np:
                version_pairs.append((op, np))
        tail_o = {self._inner_member_tail_key(p): p for p in list(o)}
        tail_n = {self._inner_member_tail_key(p): p for p in list(n)}
        for k in set(tail_o.keys()) & set(tail_n.keys()):
            if not k:
                continue
            op, np = tail_o[k], tail_n[k]
            o.discard(op)
            n.discard(np)
            if op != np:
                version_pairs.append((op, np))
        sig_o = {self._jar_inner_file_signature(p): p for p in list(o)}
        sig_n = {self._jar_inner_file_signature(p): p for p in list(n)}
        for k in set(sig_o.keys()) & set(sig_n.keys()):
            if not k:
                continue
            op, np = sig_o[k], sig_n[k]
            o.discard(op)
            n.discard(np)
            if op != np:
                version_pairs.append((op, np))
        return o, n, version_pairs

    @staticmethod
    def _is_phantom_archive_member_path(member_path: str) -> bool:
        """Synthetic or unreadable zip listing entries — not real files inside the archive."""
        low = str(member_path or "").lower()
        if not low.strip():
            return True
        return (
            "[error-reading-inner" in low
            or "[unreadable" in low
            or low.endswith("/")
        )

    def _drop_only_in_cross_build_sigs(self, only_set: set, other_sigs: set) -> set:
        """
        Drop only-in-* members whose inner signature already exists on the other build.
        Prevents false only-in-new/old rows when nested path spellings differ.
        """
        if not only_set or not other_sigs:
            return set(only_set) if only_set else set()
        kept: set = set()
        for p in only_set:
            ps = self._jar_inner_file_signature(str(p))
            if not ps:
                continue
            if ps in other_sigs:
                continue
            if any(self._nested_path_is_strict_prefix(ps, sk) for sk in other_sigs):
                continue
            kept.add(p)
        return kept

    def _archive_member_exists(self, archive_path: Path, member_path: str) -> bool:
        """True when member_path can be read from archive_path (supports nested !/ chains)."""
        if not archive_path or not _path_exists(archive_path) or not _path_is_file(archive_path):
            return False
        if self._is_phantom_archive_member_path(member_path):
            return False
        mp = str(member_path or "").replace("\\", "/").strip()
        if not mp:
            return False
        if "!/" in mp:
            try:
                data = self._extract_nested_archive(archive_path, mp)
                return bool(data)
            except Exception:
                return False
        try:
            with zipfile.ZipFile(_fs_path_str(archive_path), "r") as zf:
                for v in self._zip_member_lookup_variants(mp):
                    try:
                        zf.getinfo(v)
                        return True
                    except KeyError:
                        continue
        except Exception:
            return False
        return False

    def _filter_only_in_to_verified_members(
        self,
        archive_path: Path,
        only_set: set,
        norm_to_orig: dict[str, str],
    ) -> set:
        """Keep only members that actually exist inside the archive on this build."""
        out: set = set()
        for nm in only_set or set():
            if self._is_phantom_archive_member_path(nm):
                continue
            orig = norm_to_orig.get(nm, nm)
            if self._archive_member_exists(archive_path, orig):
                out.add(nm)
        return out

    @staticmethod
    def _nested_expanded_member_count(names: set) -> int:
        return sum(1 for n in (names or set()) if "!/" in str(n))

    def _verify_archive_diff_member(
        self, r: CRMComparisonResult, member_path: str, side: str
    ) -> bool:
        """Verify a member from archive_diff exists on the given build side."""
        rp = _crm_norm_rel_path(r.relative_path)
        if " -> " in rp:
            rp = rp.split(" -> ", 1)[0 if side == "old" else 1].strip()
        root = self.folder1_path if side == "old" else self.folder2_path
        ms = str(member_path or "").replace("\\", "/").lstrip("/")
        if not ms:
            return False
        if self._path_is_archive_file(root) and "!/" not in rp:
            chain = f"{rp}!/{ms}" if not ms.lower().startswith(rp.lower() + "!/") else ms
            return self._archive_member_exists(root, chain)
        ap = self._abs_pair_build_paths(r.relative_path)[0 if side == "old" else 1]
        return self._archive_member_exists(ap, ms)

    def _verify_clfy_archive_diff_member(
        self, czd: dict, member_path: str, side: str
    ) -> bool:
        """Verify a ClfyAgent archive_diff member exists on the given SDK outer archive."""
        outer_s = str(
            czd.get("zip_old" if side == "old" else "zip_new") or ""
        ).strip()
        outer = Path(outer_s) if outer_s else None
        if not outer or not _path_exists(outer) or not _path_is_file(outer):
            root = self.folder1_path if side == "old" else self.folder2_path
            if self._path_is_archive_file(root):
                outer = root
            else:
                return False
        ms = str(member_path or "").replace("\\", "/").lstrip("/")
        return self._archive_member_exists(outer, ms)
    
    def _normalize_inner_archive_marker(self, member_path: str) -> str:
        """Normalize paths within archives for proper comparison.
        
        This handles nested archive paths like:
        - lib/ClfyCore-1.2.3.jar!/com/xyz/MyClass.class
        - SDK-1.0.0.jar!/lib/Common-2.1.jar!/pkg/File.class
        
        All archive names (including those with !/ suffix) are normalized
        to strip version numbers for matching across builds.
        """
        member_path = self._canonicalize_nested_archive_path(member_path)
        parts = member_path.replace('\\', '/').split('/')
        if not parts:
            return member_path
        
        # Normalize ALL segments, not just the last one
        # This handles nested archive paths with version numbers
        normalized_parts = []
        for part in parts:
            lower = part.lower()
            # Check for archive names with !/ marker (e.g., "ClfyCore-1.2.3.jar!")
            if lower.endswith(('.jar!', '.war!', '.ear!', '.zip!')):
                # Remove trailing ! for normalization, then add it back
                base = part[:-1]  # Remove !
                normalized = self._normalize_archive_filename(base)
                normalized_parts.append(normalized + '!')
            elif lower.endswith('.pack.gz'):
                inner_base = part[:-8]
                normalized_inner = self._normalize_archive_filename(inner_base)
                normalized_parts.append(normalized_inner + '.pack.gz')
            elif lower.endswith(('.jar', '.war', '.ear', '.zip')):
                normalized_parts.append(self._normalize_archive_filename(part))
            else:
                normalized_parts.append(part)
        
        return '/'.join(normalized_parts)
    
    def _process_nested_archive(self, outer_zip, member_name: str, normalized_name: str,
                                names_set: set, meta_dict: dict, depth: int = 0, max_depth: int = 5):
        """Recursively process nested archives."""
        if depth >= max_depth:
            return
        try:
            data = outer_zip.read(member_name)
            with zipfile.ZipFile(BytesIO(data), 'r') as inner:
                for inner_info in inner.infolist():
                    inner_name = inner_info.filename.replace('\\', '/').strip('/')
                    if inner_name and inner_name.endswith('/'):
                        inner_name = inner_name[:-1]
                    if inner_name:
                        norm_outer = self._normalize_inner_archive_marker(normalized_name)
                        full_path = f"{norm_outer}!/{inner_name}"
                        names_set.add(full_path)
                        if not inner_info.is_dir():
                            meta_dict[full_path] = (inner_info.file_size, inner_info.CRC)
                        if inner_name.lower().endswith(('.jar', '.war', '.ear')):
                            try:
                                self._process_nested_archive(inner, inner_info.filename, full_path,
                                                            names_set, meta_dict, depth + 1, max_depth)
                            except Exception:
                                pass
        except Exception as ie:
            norm_outer = self._normalize_inner_archive_marker(normalized_name)
            names_set.add(f"{norm_outer}!/[error-reading-inner:{ie.__class__.__name__}]")
    
    def _get_archive_members(self, archive_path: Path, *, deep: Optional[bool] = None) -> tuple:
        """Get all members of an archive. When deep is True, recurse into nested jar/war/ear/zip.

        If deep is None, uses self.deep_nested_archives. Callers can pass deep=False to list only
        top-level zip entries (avoids hangs on fat JARs with thousands of embedded archives).
        """
        use_deep = self.deep_nested_archives if deep is None else deep
        names = set()
        meta = {}
        nested_archives_found = []
        class_files_found = []
        nested_expansions = [0]

        def process_nested_deep(outer_zf, member_name: str, prefix: str, depth: int = 0, max_depth: int = 10):
            """Recursively process nested archives to find all class files."""
            if depth >= max_depth:
                return
            if self._should_skip_class_scan_inside_nested_archive(member_name):
                return
            nested_expansions[0] += 1
            if use_deep and nested_expansions[0] % 100 == 0:
                print(
                    f"[CRM]   ... nested scan in progress ({nested_expansions[0]} nested "
                    f"archive expansions, {len(names):,} member path(s) so far)",
                    flush=True,
                )
            try:
                # Guard against entries with archive-like names but non-zip payloads.
                with outer_zf.open(member_name) as member_stream:
                    sig = member_stream.read(4)
                if not sig.startswith(b"PK"):
                    logger.debug(f"[CRM] Skipping non-zip nested entry: {prefix}")
                    return
                data = outer_zf.read(member_name)
                with zipfile.ZipFile(BytesIO(data), 'r') as inner_zf:
                    for inner_info in inner_zf.infolist():
                        inner_name = inner_info.filename.replace('\\', '/').strip('/')
                        if not inner_name or inner_name.endswith('/'):
                            continue
                        full_path = f"{prefix}!/{inner_name}"
                        names.add(full_path)
                        if not inner_info.is_dir():
                            meta[full_path] = (inner_info.file_size, inner_info.CRC)
                            if inner_name.lower().endswith('.class'):
                                class_files_found.append(full_path)
                        # Recursively process nested archives
                        if inner_name.lower().endswith(('.jar', '.war', '.ear', '.zip')):
                            nested_archives_found.append(full_path)
                            process_nested_deep(inner_zf, inner_info.filename, full_path, depth + 1, max_depth)
            except zipfile.BadZipFile:
                # Not every *.jar/*.war member is a valid zip stream in practice.
                logger.debug(f"[CRM] Skipping invalid nested zip stream: {prefix}")
            except Exception as e:
                logger.debug(f"[CRM] Nested scan skipped for {prefix}: {e}")
        
        def read_with_zipfile():
            """Try reading with standard zipfile."""
            nested_expansions[0] = 0
            mode = "deep nested" if use_deep else "shallow"
            print(f"[CRM] Scanning archive ({mode}): {archive_path}")
            if use_deep:
                self._log_foundation_jar_class_scan_policy_once()
            if use_deep:
                print(
                    "[CRM]   (Deep mode can take a while on large stacks; progress lines appear every "
                    "100 nested archives.)",
                    flush=True,
                )
            sys.stdout.flush()
            with zipfile.ZipFile(_fs_path_str(archive_path), 'r') as zf:
                for info in zf.infolist():
                    name = info.filename.replace('\\', '/').strip('/')
                    if not name or name.endswith('/'):
                        continue
                    names.add(name)
                    if not info.is_dir():
                        meta[name] = (info.file_size, info.CRC)
                        if name.lower().endswith('.class'):
                            class_files_found.append(name)
                    # Optionally recurse into nested archives (expensive on fat JARs)
                    if name.lower().endswith(('.jar', '.war', '.ear', '.zip')):
                        nested_archives_found.append(name)
                        if use_deep and not self._should_skip_class_scan_inside_nested_archive(name):
                            process_nested_deep(zf, info.filename, name, depth=0, max_depth=10)
            print(f"[CRM] Found {len(names)} total members, {len(nested_archives_found)} nested archives, {len(class_files_found)} class files")
            sys.stdout.flush()
        
        def read_with_jar_command():
            """Try reading using jar command (for problematic ZIP files)."""
            try:
                # Use jar -tf to list contents
                result = subprocess.run(
                    ['jar', 'tf', _fs_path_str(archive_path)],
                    capture_output=True, text=True, timeout=300
                )
                if result.returncode == 0:
                    for line in result.stdout.strip().split('\n'):
                        name = line.strip().replace('\\', '/')
                        if name and not name.endswith('/'):
                            names.add(name)
                            meta[name] = (0, 0)  # Size/CRC not available from jar tf
                    print(f"[CRM] Read {len(names)} entries using jar command")
            except Exception as e:
                logger.debug(f"[CRM] jar command failed: {e}")
        
        def read_binary_fallback():
            """Fallback: read ZIP central directory manually to handle corrupt extra fields."""
            try:
                with open(_fs_path_str(archive_path), 'rb') as f:
                    # Seek to end to find EOCD (End of Central Directory)
                    f.seek(-65536, 2)  # Read last 64KB
                    data = f.read()
                    
                    # Find EOCD signature (0x06054b50)
                    eocd_pos = data.rfind(b'PK\x05\x06')
                    if eocd_pos == -1:
                        return False
                    
                    # Try with lenient ZipFile - ignore extra field errors
                    f.seek(0)
                    # Read the entire file into memory and patch problematic parts
                    file_data = f.read()
                    
                # Try opening from memory with lenient parsing
                try:
                    zf = zipfile.ZipFile(BytesIO(file_data), 'r')
                    for info in zf.infolist():
                        try:
                            name = info.filename.replace('\\', '/').strip('/')
                            if name and not name.endswith('/'):
                                names.add(name)
                                if not info.is_dir():
                                    meta[name] = (info.file_size, getattr(info, 'CRC', 0))
                        except Exception:
                            continue
                    zf.close()
                    print(f"[CRM] Read {len(names)} entries using binary fallback")
                    return True
                except Exception:
                    pass
                return False
            except Exception as e:
                logger.debug(f"[CRM] Binary fallback failed: {e}")
                return False
        
        # Try standard zipfile first
        try:
            read_with_zipfile()
            if names:
                return names, meta
        except zipfile.BadZipFile as e:
            logger.warning(f"[CRM] BadZipFile for {archive_path}: {e}")
        except Exception as e:
            error_msg = str(e).lower()
            if 'corrupt' in error_msg or 'extra field' in error_msg:
                print(f"[CRM] ZIP has corrupt extra fields, trying alternatives...")
            else:
                logger.warning(f"[CRM] Could not read archive {archive_path}: {e}")
        
        # Try jar command as fallback
        if not names:
            read_with_jar_command()
        
        # Try binary fallback if still no results
        if not names:
            read_binary_fallback()
        
        return names, meta

    def _bytes_to_unified_diff_html(self, b1: bytes, b2: bytes) -> str:
        """HTML-safe unified diff of two byte blobs (UTF-8 with replacement)."""
        t1 = b1.decode("utf-8", errors="replace").splitlines(True)
        t2 = b2.decode("utf-8", errors="replace").splitlines(True)
        lines = list(difflib.unified_diff(t1, t2, "old", "new", n=3))
        max_lines = 600
        if len(lines) > max_lines:
            text = "".join(lines[:max_lines])
            text += f"\n... [{len(lines) - max_lines} more diff lines omitted]\n"
        else:
            text = "".join(lines)
        if not text.strip():
            return (
                "<p style='color:#64748b;font-size:12px;'>"
                "No text diff (identical after decode or empty files).</p>"
            )
        return (
            "<pre style=\"white-space:pre-wrap;word-break:break-word;font-size:11px;"
            "background:#f8fafc;padding:10px;border-radius:8px;border:1px solid #e2e8f0;"
            "max-height:420px;overflow:auto;\">"
            f"{html.escape(text)}</pre>"
        )

    def _unified_diff_zip_members(
        self, zp1: Path, zp2: Path, member_old: str, member_new: str
    ) -> str:
        """Unified diff for two zip members (by original entry names)."""
        b1 = b2 = None
        try:
            with zipfile.ZipFile(_fs_path_str(zp1), "r") as z1:
                for v in self._zip_member_lookup_variants(member_old):
                    try:
                        b1 = z1.read(v)
                        break
                    except KeyError:
                        continue
            with zipfile.ZipFile(_fs_path_str(zp2), "r") as z2:
                for v in self._zip_member_lookup_variants(member_new):
                    try:
                        b2 = z2.read(v)
                        break
                    except KeyError:
                        continue
        except Exception as e:
            return (
                f"<p style='color:#991b1b;font-size:12px;'>Could not read entries for diff: "
                f"{html.escape(str(e))}</p>"
            )
        if b1 is None or b2 is None:
            return (
                "<p style='color:#92400e;font-size:12px;'>"
                "Could not locate both entries in the archives for a text diff.</p>"
            )
        return self._bytes_to_unified_diff_html(b1, b2)

    def _unified_diff_plain_files(self, p1: Path, p2: Path) -> str:
        """Unified diff for two on-disk (non-zip) files under the two build roots."""
        try:
            b1 = Path(p1).read_bytes()
        except OSError as e:
            return (
                f"<p style='color:#991b1b;font-size:12px;'>"
                f"Could not read old file: {html.escape(str(e))}</p>"
            )
        try:
            b2 = Path(p2).read_bytes()
        except OSError as e:
            return (
                f"<p style='color:#991b1b;font-size:12px;'>"
                f"Could not read new file: {html.escape(str(e))}</p>"
            )
        return self._bytes_to_unified_diff_html(b1, b2)

    def _abs_pair_build_paths(self, relative_path: str) -> tuple[Path, Path]:
        """Map a result relative_path to absolute old/new build paths (handles a.jar -> b.jar)."""
        rp = (relative_path or "").strip()
        if " -> " in rp:
            old_r, new_r = rp.split(" -> ", 1)
            o = old_r.strip()
            n = new_r.strip()
            if o and n:
                n = self._crm_map_path_across_release(n, to_new=True)
                return (self.folder1_path / o, self.folder2_path / n)
        p = Path(rp)
        mapped_new = self._crm_map_path_across_release(str(p), to_new=True)
        return (self.folder1_path / p, self.folder2_path / Path(mapped_new))

    _ARCHIVE_ROOT_SUFFIXES = (".jar", ".war", ".ear", ".zip")

    @classmethod
    def _path_is_archive_file(cls, path: Path) -> bool:
        if not path or not _path_exists(path) or not _path_is_file(path):
            return False
        return str(path).lower().endswith(cls._ARCHIVE_ROOT_SUFFIXES)

    def _build_root_is_archive(self) -> bool:
        return self._path_is_archive_file(self.folder1_path) or self._path_is_archive_file(
            self.folder2_path
        )

    def _resolve_build_file_target(self, side: int, rel_path: str) -> tuple[Path, Optional[str]]:
        """
        Map a comparison relative path to (outer archive or file path, optional zip member).
        Handles SDK.jar comparison roots, Explorer shell paths (a.jar\\inner\\file), and !/ virtual paths.
        """
        root = self.folder1_path if side == 1 else self.folder2_path
        raw = (rel_path or "").strip()
        if not raw:
            return root, None

        if " -> " in raw and "!/" not in raw:
            parts = raw.split(" -> ", 1)
            raw = parts[0].strip() if side == 1 else parts[1].strip()

        rp = raw.replace("\\", "/")
        if "!/" in rp:
            outer_rel, _, member = rp.partition("!/")
            member = member.strip("/")
            if self._path_is_archive_file(self.folder1_path):
                if side == 1:
                    return self.folder1_path, member
                peer = self.folder2_path if self._path_is_archive_file(self.folder2_path) else self.folder1_path
                member = self._crm_map_path_across_release(member, to_new=True)
                return peer, member
            outer = self._abs_pair_build_paths(outer_rel.strip())[0 if side == 1 else 1]
            return outer, member

        shell = self._parse_shell_zip_appended_path(Path(raw))
        if shell:
            return shell

        if self._path_is_archive_file(root):
            member = rp.lstrip("/")
            if side == 2:
                member = self._crm_map_path_across_release(member, to_new=True)
            return root, member

        shell_under_root = self._parse_shell_zip_appended_path(
            Path(str(root).replace("/", "\\")) / Path(raw.replace("/", "\\"))
        )
        if shell_under_root:
            return shell_under_root

        inner_virt = self._shell_inner_to_virtual_member_path(rp)
        if "!/" in inner_virt:
            outer_rel, _, member = inner_virt.partition("!/")
            outer = root / outer_rel.replace("/", os.sep)
            if _path_exists(outer) and _path_is_file(outer):
                return outer, member.strip("/")

        return root / raw.replace("/", os.sep), None

    def _unified_diff_for_config_path(self, cf: str) -> str:
        """Unified diff for a config path (plain, virtual !/, or inner SDK.jar member)."""
        cf_norm = (cf or "").strip().replace("\\", "/")
        if not cf_norm:
            return (
                "<p style='color:#991b1b;font-size:12px;'>Empty config path.</p>"
            )
        if "!/" in cf_norm:
            base, _, memb = cf_norm.partition("!/")
            if base and memb:
                p1, p2 = self._abs_pair_build_paths(base)
                return self._unified_diff_zip_members(p1, p2, memb.strip("/"), memb.strip("/"))

        parsed = self._parse_shell_zip_appended_path(Path(cf))
        if parsed:
            ap, inner = parsed
            peer = self._resolve_build_file_target(2 if ap == self.folder1_path else 1, inner)
            if peer[1]:
                return self._unified_diff_zip_members(ap, peer[0], inner, peer[1])
            return self._unified_diff_zip_members(ap, ap, inner, inner)

        o1, m1 = self._resolve_build_file_target(1, cf_norm)
        o2, m2 = self._resolve_build_file_target(2, cf_norm)
        if m1 is not None or m2 is not None:
            member = (m1 or m2 or cf_norm.lstrip("/")).strip("/")
            return self._unified_diff_zip_members(o1, o2, member, member)

        p1, p2 = self._abs_pair_build_paths(cf_norm)
        return self._unified_diff_plain_files(p1, p2)

    def _virtual_display_path(self, member_rel: str) -> str:
        """Report-friendly path for a member inside build-root archive(s)."""
        memb = (member_rel or "").replace("\\", "/").lstrip("/")
        r1, r2 = self.folder1_path, self.folder2_path
        if self._path_is_archive_file(r1):
            n1 = r1.name
            n2 = r2.name if self._path_is_archive_file(r2) else n1
            prefix = f"{n1} -> {n2}" if n1 != n2 else n1
            return f"{prefix}!/{memb}"
        return memb

    def _crm_iter_explicit_size_archive_pairs(self) -> list[tuple[str, Path, Path]]:
        """Archive rows (display prefix, old path, new path) for nested explicit-size lookup."""
        pairs: list[tuple[str, Path, Path]] = []
        seen: set[str] = set()

        r1, r2 = self.folder1_path, self.folder2_path
        if self._path_is_archive_file(r1):
            n1, n2 = r1.name, (r2.name if self._path_is_archive_file(r2) else r1.name)
            disp = f"{n1} -> {n2}" if n1 != n2 else n1
            key = f"root:{disp}".lower()
            if key not in seen:
                seen.add(key)
                pairs.append((disp, r1, r2 if self._path_is_archive_file(r2) else r1))

        for r in self.results:
            rp = _crm_norm_rel_path(r.relative_path)
            parts = [p.strip() for p in rp.split(" -> ", 1)] if " -> " in rp else [rp]
            is_archive = r.is_archive or any(
                p.lower().endswith(self._ARCHIVE_ROOT_SUFFIXES) for p in parts
            )
            if not is_archive:
                continue
            disp = self._crm_archive_display_prefix(r.relative_path)
            key = disp.lower()
            if key in seen:
                continue
            seen.add(key)
            p1, p2 = self._abs_pair_build_paths(r.relative_path)
            pairs.append((disp, p1, p2))
        return pairs

    def _collect_config_text_diff_path_pool(self) -> list[str]:
        """
        Paths eligible for the HTML "Text and config files changed" block.

        The HTML section is shown only when ``config_files_filter_query`` is non-empty.
        With a filter: classic config extensions plus any changed non-archive paths matching the patterns.
        """
        fraw = (self.config_files_filter_query or "").strip()
        uflt = _crm_parse_config_file_filters(fraw)
        allow_user = bool(uflt)
        out: list[str] = []
        seen: set[str] = set()

        def add(virt: str) -> None:
            k = virt.replace("\\", "/")
            if k in seen:
                return
            seen.add(k)
            out.append(virt)

        for r in self.results:
            if r.status != "modified":
                continue
            if r.is_archive and r.archive_diff:
                ad = r.archive_diff
                rp = r.relative_path
                for f in ad.get("config_files_changed", []) or []:
                    add(f"{rp}!/{f}")
                for f in ad.get("different_content", []) or []:
                    f_s = str(f)
                    f_low = f_s.lower()
                    if f_low.endswith(".class") or _crm_path_leaf_is_bundle_artifact(f_s):
                        continue
                    full = f"{rp}!/{f_s}"
                    is_classic = f_low.endswith(_CRM_CLASSIC_CONFIG_EXTS)
                    match_usr = bool(
                        uflt
                        and (
                            _crm_config_path_matches_any_filter(f_s, uflt)
                            or _crm_config_path_matches_any_filter(full, uflt)
                        )
                    )
                    if is_classic or (allow_user and match_usr):
                        add(full)
                continue
            if r.is_archive:
                continue
            rpath = r.relative_path
            r_low = rpath.lower()
            if " -> " in rpath:
                continue
            if r_low.endswith(".class") or _crm_path_leaf_is_bundle_artifact(rpath):
                continue
            is_classic = r_low.endswith(_CRM_CLASSIC_CONFIG_EXTS)
            match_usr = bool(uflt and _crm_config_path_matches_any_filter(rpath, uflt))
            if is_classic or (allow_user and match_usr):
                if self._build_root_is_archive() and "!/" not in rpath:
                    add(self._virtual_display_path(rpath))
                else:
                    add(rpath)
        if allow_user:
            for virt in self._crm_find_config_filter_virtual_paths(uflt):
                add(virt)
        return out

    def _crm_find_config_filter_virtual_paths(self, uflt: list[str]) -> list[str]:
        """On-demand config paths inside archives for user filter tokens (e.g. Crm.jnlp in SDK.jar)."""
        if not uflt:
            return []
        found: list[str] = []
        seen: set[str] = set()
        for disp, p1, p2 in self._crm_iter_explicit_size_archive_pairs():
            names1, _ = self._cached_get_archive_members(p1, deep=True)
            names2, _ = self._cached_get_archive_members(p2, deep=True)
            for memb in sorted(names1 | names2):
                memb_n = memb.replace("\\", "/")
                virt = f"{disp}!/{memb_n}"
                if not (
                    _crm_config_path_matches_any_filter(memb_n, uflt)
                    or _crm_config_path_matches_any_filter(virt, uflt)
                ):
                    continue
                k = virt.lower()
                if k not in seen:
                    seen.add(k)
                    found.append(virt)
        return found

    def _config_diff_html_lookup_ensure(
        self,
        rel_keys: list[str],
        base_lookup: dict[str, str],
        *,
        max_paths: Optional[int] = None,
    ) -> dict[str, str]:
        """Fill in unified HTML for paths in rel_keys not already present in base_lookup."""
        keys = rel_keys if max_paths is None else rel_keys[: max(0, max_paths)]
        for cf in keys:
            if not cf:
                continue
            if (base_lookup.get(cf) or "").strip():
                continue
            base_lookup[cf] = self._unified_diff_for_config_path(cf)
        return base_lookup

    def _list_clfy_bundle_paths_via_jar_tf(self, outer: Path, *, timeout_sec: int = 90) -> list[str]:
        """List ClfyAgent.zip/.ear virtual paths via jar tf (fallback when ZipFile shallow scan fails)."""
        if not outer or not _path_exists(outer) or not _path_is_file(outer):
            return []
        try:
            print(
                f"[CRM] ClfyAgent: jar tf listing {outer.name} (fallback, timeout {timeout_sec}s)...",
                flush=True,
            )
            result = subprocess.run(
                [_jar_tool_cmd(), "tf", _fs_path_str(outer)],
                capture_output=True,
                text=True,
                timeout=timeout_sec,
            )
            if result.returncode != 0 or not result.stdout:
                return []
            hits = [
                ln.strip().replace("\\", "/").strip("/")
                for ln in result.stdout.splitlines()
                if ln.strip()
                and os.path.basename(ln.strip().replace("\\", "/")).lower()
                in _CLFY_AGENT_BUNDLE_BASENAMES_LOWER
            ]
            seen: set[str] = set()
            out: list[str] = []
            for h in hits:
                if h not in seen:
                    seen.add(h)
                    out.append(h)
            return sorted(out, key=lambda x: x.lower())
        except Exception as e:
            logger.debug(f"[CRM] jar tf ClfyAgent list failed for {outer}: {e}")
            return []

    def _collect_clfy_virt_paths_shallow_from_zip(
        self,
        zf: zipfile.ZipFile,
        prefix: str,
        out: list[str],
        *,
        stats: Optional[dict] = None,
        depth: int = 0,
        max_depth: int = 12,
    ) -> None:
        """
        Find ClfyAgent.zip/.ear using zip central directory only.
        Recurse into nested .war/.ear/.zip — never read nested .jar bodies (SDK speed).
        """
        if depth >= max_depth:
            return
        st = stats if stats is not None else {}
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = info.filename.replace("\\", "/").strip("/")
            if not name:
                continue
            st["entries"] = int(st.get("entries", 0)) + 1
            if st["entries"] % 500 == 0:
                print(
                    f"[CRM] ClfyAgent shallow scan: {st['entries']} zip entries checked "
                    f"(depth={depth})...",
                    flush=True,
                )
            virt = f"{prefix}!/{name}" if prefix else name
            if os.path.basename(name).lower() in _CLFY_AGENT_BUNDLE_BASENAMES_LOWER:
                out.append(virt)
            if not self._clfy_container_recurse_suffix(name):
                continue
            data = self._clfy_read_nested_zip_bytes(zf, info.filename)
            if not data:
                continue
            st["expanded"] = int(st.get("expanded", 0)) + 1
            try:
                with zipfile.ZipFile(BytesIO(data), "r") as inner:
                    self._collect_clfy_virt_paths_shallow_from_zip(
                        inner, virt, out, stats=st, depth=depth + 1, max_depth=max_depth
                    )
            except (zipfile.BadZipFile, OSError, RuntimeError):
                continue

    def _list_clfy_paths_from_outer_archive(self, outer_path: Path) -> list[str]:
        """Discover ClfyAgent bundle virtual paths inside an SDK/archive (shallow zip first, jar tf fallback)."""
        if not outer_path or not _path_exists(outer_path):
            return []
        cache_key = self._stable_archive_cache_key(outer_path)
        cached = self._clfy_bundle_paths_cache.get(cache_key)
        if cached is not None:
            return list(cached)
        resolved = self._resolve_outer_archive_for_zip(outer_path) or outer_path
        candidates: list[str] = []
        shallow_ok = False
        try:
            arc = resolved
            if not self._zipfile_opens_ok(resolved):
                print(
                    f"[CRM] ClfyAgent: ZipFile cannot open {resolved.name}; "
                    f"copying to local temp for shallow scan...",
                    flush=True,
                )
                local = self._copy_file_to_temp_for_clfy_read(resolved)
                if local:
                    arc = local
            print(
                f"[CRM] ClfyAgent: shallow zip scan in {arc.name} (no nested .jar reads)...",
                flush=True,
            )
            stats: dict = {"entries": 0, "expanded": 0}
            with zipfile.ZipFile(_fs_path_str(arc), "r") as zf:
                self._collect_clfy_virt_paths_shallow_from_zip(zf, "", candidates, stats=stats)
            shallow_ok = True
            if stats.get("entries"):
                print(
                    f"[CRM] ClfyAgent shallow scan done: {stats.get('entries', 0)} entries, "
                    f"{stats.get('expanded', 0)} nested containers opened, "
                    f"{len(candidates)} bundle hit(s)",
                    flush=True,
                )
        except Exception as e:
            logger.debug(f"[CRM] ClfyAgent shallow path list failed for {outer_path}: {e}")
        seen: set[str] = set()
        uniq: list[str] = []
        for c in candidates:
            cl = c.replace("\\", "/")
            if cl not in seen:
                seen.add(cl)
                uniq.append(cl)
        if uniq:
            out = sorted(uniq, key=lambda x: x.lower())
            self._clfy_bundle_paths_cache[cache_key] = out
            return out
        if shallow_ok:
            self._clfy_bundle_paths_cache[cache_key] = []
            return []
        hits = self._list_clfy_bundle_paths_via_jar_tf(resolved)
        if hits:
            print(
                f"[CRM] ClfyAgent: jar tf listed {len(hits)} bundle path(s) in {resolved.name}",
                flush=True,
            )
            self._clfy_bundle_paths_cache[cache_key] = hits
            return hits
        self._clfy_bundle_paths_cache[cache_key] = []
        return []

    def _clfy_size_diff_min_pct(self) -> float:
        try:
            return float(
                os.environ.get(
                    "CRM_CLFY_SIZE_DIFF_MIN_PCT", str(_CRM_HIGH_SIZE_ALERT_MIN_ABS_PCT)
                )
            )
        except (TypeError, ValueError):
            return _CRM_HIGH_SIZE_ALERT_MIN_ABS_PCT

    @staticmethod
    def _clfy_container_recurse_suffix(name: str) -> bool:
        """Recurse into nested archives to list inner containers (.jar in .war); never open .jar bodies."""
        nl = (name or "").replace("\\", "/").strip("/").lower()
        return nl.endswith((".war", ".ear", ".zip"))

    def _clfy_collect_container_size_index(
        self,
        zf: zipfile.ZipFile,
        prefix: str,
        out: dict[str, int],
        *,
        depth: int = 0,
        max_depth: int = 10,
    ) -> None:
        """Map normalized container virtual path -> zip entry size (central directory only)."""
        if depth >= max_depth:
            return
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = info.filename.replace("\\", "/").strip("/")
            if not name:
                continue
            if not self._clfy_is_nested_archive_entry(name):
                continue
            virt = self._clfy_virtual_member_path(prefix, name)
            norm = self._normalize_inner_archive_marker(virt)
            out[norm] = {"size": int(info.file_size), "path": virt}
            if not self._clfy_container_recurse_suffix(name):
                continue
            data = self._clfy_read_nested_zip_bytes(zf, info.filename)
            if not data:
                continue
            try:
                with zipfile.ZipFile(BytesIO(data), "r") as inner:
                    self._clfy_collect_container_size_index(
                        inner, virt, out, depth=depth + 1, max_depth=max_depth
                    )
            except (zipfile.BadZipFile, OSError, RuntimeError):
                continue

    def _diff_clfy_container_size_from_zipfiles(
        self, zf1: zipfile.ZipFile, zf2: zipfile.ZipFile
    ) -> dict:
        idx1: dict[str, int] = {}
        idx2: dict[str, int] = {}
        self._clfy_collect_container_size_index(zf1, "", idx1)
        self._clfy_collect_container_size_index(zf2, "", idx2)
        return self._diff_clfy_container_size_maps(idx1, idx2)

    @staticmethod
    def _clfy_index_entry_size(entry: object) -> int:
        if isinstance(entry, dict):
            return int(entry.get("size") or 0)
        return int(entry or 0)

    @staticmethod
    def _clfy_index_entry_path(entry: object, norm_key: str) -> str:
        if isinstance(entry, dict):
            return str(entry.get("path") or norm_key)
        return str(norm_key)

    def _diff_clfy_container_size_maps(
        self, idx1: dict[str, object], idx2: dict[str, object]
    ) -> dict:
        """Diff two container inventories using naming normalization; size only (no CRC)."""
        keys1 = set(idx1.keys())
        keys2 = set(idx2.keys())
        only_in_1 = keys1 - keys2
        only_in_2 = keys2 - keys1
        only_in_1, only_in_2, reconcile_version_pairs = self._reconcile_only_in_member_sets(
            only_in_1, only_in_2
        )
        common = keys1 & keys2
        size_changed: list[str] = []
        jar_size_diff_rows: list[dict] = []
        version_mismatch_rows: list[dict] = []
        seen_version: set[tuple[str, str]] = set()
        min_pct = self._clfy_size_diff_min_pct()

        def _add_version_row(old_path: str, new_path: str, *, size_old: int = 0, size_new: int = 0) -> None:
            op = str(old_path or "").strip()
            np = str(new_path or "").strip()
            if not op or not np:
                return
            leaf_o = self._path_leaf_name_static(op)
            leaf_n = self._path_leaf_name_static(np)
            if leaf_o.lower() == leaf_n.lower():
                return
            key = (op.lower(), np.lower())
            if key in seen_version:
                return
            seen_version.add(key)
            summary = self._format_jar_version_change_summary(
                {self._extract_jar_version_display_token(leaf_o) or leaf_o},
                {self._extract_jar_version_display_token(leaf_n) or leaf_n},
            )
            version_mismatch_rows.append(
                {
                    "old_path": op,
                    "new_path": np,
                    "logical_name": self._logical_jar_display_label(leaf_o or leaf_n),
                    "summary": summary or f"{leaf_o} → {leaf_n}",
                    "size_old": size_old,
                    "size_new": size_new,
                }
            )

        for old_p, new_p in reconcile_version_pairs:
            if self._clfy_member_is_container_archive(old_p) or self._clfy_member_is_container_archive(
                new_p
            ):
                _add_version_row(old_p, new_p)

        for nm in sorted(common, key=lambda x: x.lower()):
            s1 = self._clfy_index_entry_size(idx1[nm])
            s2 = self._clfy_index_entry_size(idx2[nm])
            p1 = self._clfy_index_entry_path(idx1[nm], nm)
            p2 = self._clfy_index_entry_path(idx2[nm], nm)
            leaf1 = self._path_leaf_name_static(p1)
            leaf2 = self._path_leaf_name_static(p2)
            if leaf1.lower() != leaf2.lower():
                _add_version_row(p1, p2, size_old=s1, size_new=s2)
            if s1 == s2:
                continue
            size_changed.append(nm)
            pct = ((s2 - s1) / s1 * 100.0) if s1 else (100.0 if s2 else 0.0)
            row = {
                "path": nm,
                "path_old": p1,
                "path_new": p2,
                "size_old": s1,
                "size_new": s2,
                "pct_change": pct,
                "significant": abs(pct) >= min_pct,
            }
            jar_size_diff_rows.append(row)
            if nm.lower().endswith(".jar"):
                print(
                    f"[CRM] ClfyAgent JAR size change: {nm} "
                    f"({s1:,} -> {s2:,} bytes, {pct:+.1f}%)"
                )
        jar_size_diff_rows.sort(
            key=lambda r: (-int(r.get("significant", False)), -abs(r.get("pct_change", 0)))
        )
        version_mismatch_rows.sort(
            key=lambda r: str(r.get("logical_name") or "").lower()
        )
        sig_n = sum(1 for r in jar_size_diff_rows if r.get("significant"))
        print(
            f"[CRM] ClfyAgent size-only: containers old={len(keys1)}, new={len(keys2)}, "
            f"only old={len(only_in_1)}, only new={len(only_in_2)}, "
            f"version renamed={len(version_mismatch_rows)}, "
            f"size diff={len(size_changed)} (significant JAR/WAR/EAR/ZIP >= {min_pct:.0f}%: {sig_n})",
            flush=True,
        )
        container_diff = self._clfy_build_container_diff_from_members(
            keys1, keys2, size_changed
        )
        return {
            "only_in_old": only_in_1,
            "only_in_new": only_in_2,
            "different_content": size_changed,
            "class_files_changed": [],
            "config_files_changed": [],
            "config_diff_details": [],
            "class_modified_detail": [],
            "total_members_old": len(keys1),
            "total_members_new": len(keys2),
            "jar_size_diff_rows": jar_size_diff_rows,
            "version_mismatch_rows": version_mismatch_rows,
            "size_compare_mode": True,
            **container_diff,
        }

    def _compare_archive_contents_clfy_size_only(
        self,
        path1: Path,
        path2: Path,
    ) -> dict:
        """Fast ClfyAgent compare: nested .jar/.war/.ear/.zip sizes with naming normalization."""
        print(f"[CRM] " + "=" * 60)
        print(f"[CRM] ClfyAgent size-only container compare:")
        print(f"[CRM]   Old: {path1}")
        print(f"[CRM]   New: {path2}")
        print(f"[CRM] " + "=" * 60)
        sys.stdout.flush()
        try:
            with zipfile.ZipFile(_fs_path_str(path1), "r") as zf1, zipfile.ZipFile(
                _fs_path_str(path2), "r"
            ) as zf2:
                return self._diff_clfy_container_size_from_zipfiles(zf1, zf2)
        except Exception as e:
            logger.warning(f"[CRM] ClfyAgent size-only compare failed: {e}")
            return {
                "error": str(e),
                "only_in_old": set(),
                "only_in_new": set(),
                "different_content": [],
                "class_files_changed": [],
                "config_files_changed": [],
                "config_diff_details": [],
                "class_modified_detail": [],
                "total_members_old": 0,
                "total_members_new": 0,
                "jar_size_diff_rows": [],
            }

    @staticmethod
    def _clfy_is_nested_archive_entry(name: str) -> bool:
        n = (name or "").replace("\\", "/").strip("/").lower()
        return n.endswith((".jar", ".war", ".ear", ".zip"))

    @staticmethod
    def _clfy_virtual_member_path(prefix: str, inner_name: str) -> str:
        inner = (inner_name or "").replace("\\", "/").strip("/")
        if not inner:
            return (prefix or "").strip("/")
        pfx = (prefix or "").replace("\\", "/").strip("/")
        return inner if not pfx else f"{pfx}!/{inner}"

    @staticmethod
    def _clfy_read_nested_zip_bytes(
        zf: zipfile.ZipFile, member_name: str
    ) -> Optional[bytes]:
        try:
            with zf.open(member_name) as member_stream:
                sig = member_stream.read(4)
            if not sig.startswith(b"PK"):
                return None
            return zf.read(member_name)
        except Exception:
            return None

    def _diff_collected_archive_members(
        self,
        path1: Path,
        path2: Path,
        names1: set,
        meta1: dict,
        names2: set,
        meta2: dict,
    ) -> dict:
        """Diff two member inventories (shared by archive deep scan paths)."""
        print(f"[CRM] Old archive: {len(names1)} members")
        print(f"[CRM] New archive: {len(names2)} members")

        sample_class_files_1 = [n for n in names1 if n.lower().endswith(".class")][:5]
        sample_class_files_2 = [n for n in names2 if n.lower().endswith(".class")][:5]
        if sample_class_files_1:
            print(f"[CRM] Sample class files in old: {sample_class_files_1}")
        if sample_class_files_2:
            print(f"[CRM] Sample class files in new: {sample_class_files_2}")

        clfy_files_1 = [n for n in names1 if "clfycore" in n.lower()][:5]
        clfy_files_2 = [n for n in names2 if "clfycore" in n.lower()][:5]
        if clfy_files_1:
            print(
                f"[CRM] Files with ClfyCore in old: "
                f"{len([n for n in names1 if 'clfycore' in n.lower()])} total"
            )
            print(f"[CRM] Sample ClfyCore files: {clfy_files_1}")
        if clfy_files_2:
            print(
                f"[CRM] Files with ClfyCore in new: "
                f"{len([n for n in names2 if 'clfycore' in n.lower()])} total"
            )

        norm_to_orig1 = {self._normalize_inner_archive_marker(m): m for m in names1}
        norm_to_orig2 = {self._normalize_inner_archive_marker(m): m for m in names2}
        normalized1 = set(norm_to_orig1.keys())
        normalized2 = set(norm_to_orig2.keys())
        common = normalized1 & normalized2
        only_in_1 = normalized1 - normalized2
        only_in_2 = normalized2 - normalized1
        only_in_1 = self._drop_implicit_directory_only_members(
            only_in_1, normalized2, common
        )
        only_in_2 = self._drop_implicit_directory_only_members(
            only_in_2, normalized1, common
        )
        only_in_1, only_in_2 = self._reconcile_only_in_member_sets(only_in_1, only_in_2)[:2]
        sig_new = {self._jar_inner_file_signature(m) for m in normalized2}
        sig_old = {self._jar_inner_file_signature(m) for m in normalized1}
        only_in_1 = self._drop_only_in_cross_build_sigs(only_in_1, sig_new)
        only_in_2 = self._drop_only_in_cross_build_sigs(only_in_2, sig_old)
        only_in_1 = self._filter_only_in_to_verified_members(path1, only_in_1, norm_to_orig1)
        only_in_2 = self._filter_only_in_to_verified_members(path2, only_in_2, norm_to_orig2)

        different_content: list = []
        class_files_changed: list = []
        config_files_changed: list = []
        config_diff_details: list[dict] = []
        class_modified_detail: list[dict] = []

        for nm in common:
            orig1, orig2 = norm_to_orig1.get(nm), norm_to_orig2.get(nm)
            m1, m2 = meta1.get(orig1), meta2.get(orig2)
            if m1 and m2:
                size1, crc1 = m1
                size2, crc2 = m2
                if size1 != size2 or crc1 != crc2:
                    different_content.append(nm)
                    if nm.lower().endswith(".class"):
                        class_files_changed.append(nm)
                        class_modified_detail.append(
                            {
                                "path": orig1 or orig2 or nm,
                                "size_old": size1,
                                "size_new": size2,
                                "crc_old": crc1,
                                "crc_new": crc2,
                            }
                        )
                        print(
                            f"[CRM] Class file changed: {nm} "
                            f"(size: {size1}->{size2}, CRC: {crc1}->{crc2})"
                        )
                    elif nm.lower().endswith(
                        (".conf", ".xml", ".properties", ".cfg", ".config")
                    ):
                        config_files_changed.append(nm)
                        mo = orig1 or nm
                        mn = orig2 or nm
                        diff_html = self._unified_diff_zip_members(path1, path2, mo, mn)
                        config_diff_details.append(
                            {"member": nm, "unified_html": diff_html}
                        )
                        print(f"[CRM] Config file changed: {nm} (size: {size1}->{size2})")

        uflt = _crm_parse_config_file_filters(self.config_files_filter_query or "")
        if uflt and different_content:
            have_cfg: set[str] = set(config_files_changed)
            for nm in different_content:
                nml = str(nm).lower()
                if nml.endswith(".class") or _crm_path_leaf_is_bundle_artifact(str(nm)):
                    continue
                if nml.endswith(_CRM_CLASSIC_CONFIG_EXTS):
                    continue
                if nm in have_cfg:
                    continue
                if not _crm_config_path_matches_any_filter(str(nm), uflt):
                    continue
                have_cfg.add(nm)
                orig1, orig2 = norm_to_orig1.get(nm), norm_to_orig2.get(nm)
                mo = orig1 or nm
                mn = orig2 or nm
                config_files_changed.append(nm)
                diff_html = self._unified_diff_zip_members(path1, path2, mo, mn)
                config_diff_details.append({"member": nm, "unified_html": diff_html})
                print(f"[CRM] User-filter file changed (in archive): {nm}")

        if different_content:
            print(f"[CRM] Found {len(different_content)} files with different content")
            print(f"[CRM]   - Class files changed: {len(class_files_changed)}")
            print(f"[CRM]   - Config files changed: {len(config_files_changed)}")

        if only_in_1:
            print(f"[CRM] Files only in old: {len(only_in_1)}")
            for f in list(only_in_1)[:5]:
                print(f"[CRM]   - {f}")
            if len(only_in_1) > 5:
                print(f"[CRM]   ... and {len(only_in_1) - 5} more")

        if only_in_2:
            print(f"[CRM] Files only in new: {len(only_in_2)}")
            for f in list(only_in_2)[:5]:
                print(f"[CRM]   - {f}")
            if len(only_in_2) > 5:
                print(f"[CRM]   ... and {len(only_in_2) - 5} more")

        container_diff = self._clfy_build_container_diff_from_members(
            names1, names2, different_content
        )
        c_old = container_diff.get("container_only_old") or set()
        c_new = container_diff.get("container_only_new") or set()
        c_diff = container_diff.get("container_different") or set()
        if c_old or c_new or c_diff:
            print(
                f"[CRM] Container archives (.jar/.war/.ear/.zip): "
                f"only old={len(c_old)}, only new={len(c_new)}, content diff={len(c_diff)}"
            )

        return self._filter_archive_diff_foundation_classes(
            {
                "only_in_old": only_in_1,
                "only_in_new": only_in_2,
                "different_content": different_content,
                "class_files_changed": class_files_changed,
                "config_files_changed": config_files_changed,
                "config_diff_details": config_diff_details,
                "class_modified_detail": class_modified_detail,
                "total_members_old": len(names1),
                "total_members_new": len(names2),
                "all_member_sig_keys_old": {
                    self._jar_inner_file_signature(m) for m in normalized1
                },
                "all_member_sig_keys_new": {
                    self._jar_inner_file_signature(m) for m in normalized2
                },
                **container_diff,
            }
        )

    def _compare_clfy_agent_archives(self, path1: Path, path2: Path) -> dict:
        """ClfyAgent compare: nested container sizes with naming normalization (no CRC / .class scan)."""
        return self._compare_archive_contents_clfy_size_only(path1, path2) or {}

    def _compare_archive_contents(
        self,
        path1: Path,
        path2: Path,
        *,
        skip_unbalanced_shallow_rebalance: bool = False,
    ) -> dict:
        """Compare contents of two archives with deep nested support including class files.

        When ``skip_unbalanced_shallow_rebalance`` is True (ClfyAgent .zip/.ear bundle compare),
        the unbalanced deep-scan heuristic is not applied. That heuristic retries both sides in
        shallow mode when member counts differ by 50× and one side has 200k+ entries; for
        ClfyAgent EAR→WAR→WEB-INF/lib it incorrectly drops nested lib JARs (e.g. removed
        commons-fileupload) and WEB-INF-level JARs from the diff.
        """
        print(f"[CRM] " + "=" * 60)
        print(f"[CRM] Deep comparing archives:")
        print(f"[CRM]   Old: {path1}")
        print(f"[CRM]   New: {path2}")
        print(f"[CRM] " + "=" * 60)
        
        names1, meta1 = self._get_archive_members(path1)
        names2, meta2 = self._get_archive_members(path2)

        # If one side fell back to shallow listing while the other was deeply expanded,
        # rebalance to shallow mode for both sides so comparison remains tractable.
        n1 = len(names1)
        n2 = len(names2)
        small = min(n1, n2)
        large = max(n1, n2)
        ne1 = self._nested_expanded_member_count(names1)
        ne2 = self._nested_expanded_member_count(names2)
        asymmetric_nested = (
            (ne1 == 0 and ne2 > 50)
            or (ne2 == 0 and ne1 > 50)
            or (
                ne1 > 0
                and ne2 > 0
                and max(ne1, ne2) / max(min(ne1, ne2), 1) >= 20
            )
        )
        if (
            not skip_unbalanced_shallow_rebalance
            and self.deep_nested_archives
            and (
                asymmetric_nested
                or (
                    small > 0
                    and large >= 200000
                    and (large / small) >= 50
                )
            )
        ):
            reason = (
                f"asymmetric nested expansion (old_nested={ne1}, new_nested={ne2})"
                if asymmetric_nested
                else f"unbalanced deep scan (old={n1}, new={n2})"
            )
            print(
                f"[CRM] Detected {reason}; retrying both archives in shallow mode..."
            )
            saved_deep = self.deep_nested_archives
            try:
                self.deep_nested_archives = False
                names1, meta1 = self._get_archive_members(path1)
                names2, meta2 = self._get_archive_members(path2)
                print(
                    "[CRM] Shallow rescan complete "
                    f"(old={len(names1)}, new={len(names2)})"
                )
            finally:
                self.deep_nested_archives = saved_deep
        
        return self._diff_collected_archive_members(
            path1, path2, names1, meta1, names2, meta2
        )

    @staticmethod
    def _clfy_agent_root_member_from_path(member_path: Optional[str]) -> Optional[str]:
        """
        Virtual member path up to and including ClfyAgent.zip or ClfyAgent.ear (first bundle in chain).
        Strips deeper nesting (e.g. …/ClfyAgent.zip!/ClfyAgent.war/…) so both builds can be compared
        when inner layout differs (WAR vs flat lib/).
        """
        if not member_path:
            return None
        raw = str(member_path).replace("\\", "/").strip()
        parts = [p.strip("/") for p in re.split(r"![\\/]", raw) if p.strip()]
        if not parts:
            return None
        for i, p in enumerate(parts):
            bn = os.path.basename(p).lower()
            if bn in _CLFY_AGENT_BUNDLE_BASENAMES_LOWER:
                return "!/".join(parts[: i + 1])
        return None

    def _extract_clfy_bundle_via_outer_scan(
        self, outer_path: Path, peer_member_hint: Optional[str]
    ) -> Optional[bytes]:
        """
        Discover ClfyAgent.zip / .ear inside the outer SDK archive and extract bytes.
        Used when paired virtual paths fail on one build (UNC, or different nesting under ClfyAgent).
        """
        if not outer_path or not _path_exists(outer_path):
            return None
        try:
            local = self._copy_file_to_temp_for_clfy_read(outer_path)
            arc = local or outer_path
            with zipfile.ZipFile(_fs_path_str(arc), "r") as zf:
                virt = self._find_clfy_agent_bundle_virtual_in_zip(zf, "")
            if not virt:
                return None
            print(
                f"[CRM] ClfyAgent: using scan-discovered member {virt!r} under {outer_path.name} "
                f"(peer hint {peer_member_hint!r})"
            )
            return self._extract_nested_archive(
                arc, virt, peer_member_hint=peer_member_hint
            )
        except Exception as e:
            logger.debug(f"[CRM] ClfyAgent outer scan extract failed for {outer_path}: {e}")
            return None

    def _extract_clfyagent_bytes_zipfile_nested_scan(
        self, outer_path: Path, peer_hint: Optional[str]
    ) -> Optional[bytes]:
        """
        Enumerate all ClfyAgent bundle paths under the outer SDK using ZipFile only.
        Use when `jar` is missing or `_extract_nested_archive` fails on UNC/long paths.
        """
        outer = outer_path
        local = self._copy_file_to_temp_for_clfy_read(outer_path)
        if local:
            outer = local
        uniq = self._list_clfy_paths_from_outer_archive(outer_path)
        if not uniq:
            return None
        ranked = uniq
        if len(uniq) > 1:
            ranked = self._pick_basename_hits(uniq, peer_hint or "", peer_hint)
        for virt in ranked:
            try:
                blob = self._extract_nested_archive(
                    outer, virt, peer_member_hint=peer_hint
                )
                if blob and self._looks_like_zip_file_bytes(blob):
                    print(
                        f"[CRM] ClfyAgent: ZipFile nested-scan extracted {virt!r} "
                        f"from {outer_path.name}"
                    )
                    return blob
            except Exception:
                continue
        return None

    def _extract_clfy_side_all_strategies(
        self,
        outer_path: Path,
        member_self: str,
        member_peer: Optional[str],
    ) -> Optional[bytes]:
        """
        Full nested ClfyAgent extraction chain for one outer archive (used after copying
        the outer SDK to CRM_REMOTE_TEMP_ROOT when the original path fails).
        """
        data = self._extract_nested_archive(
            outer_path, member_self, peer_member_hint=member_peer
        )
        b = os.path.basename((member_self or "").replace("\\", "/").strip("/"))
        if data is None and b:
            data = self._extract_member_by_basename_from_outer(
                outer_path, b, peer_hint=member_peer
            )
        if data is None and b:
            data = self._extract_clfyagent_bytes_rediscover(outer_path, member_peer)
        root_virt = self._clfy_agent_root_member_from_path(
            member_self
        ) or self._clfy_agent_root_member_from_path(member_peer or "")
        if root_virt and data is None:
            data = self._extract_nested_archive(
                outer_path, root_virt, peer_member_hint=member_peer
            )
        if data is None:
            data = self._extract_clfy_bundle_via_outer_scan(outer_path, member_peer)
        if data is None:
            data = self._extract_clfyagent_bytes_zipfile_nested_scan(outer_path, member_peer)
        if data is None:
            data = self._try_extract_clfy_after_remote_outer_copy(
                outer_path, member_self, member_peer
            )
        return data

    def _try_extract_clfy_after_remote_outer_copy(
        self,
        outer_path: Path,
        member_self: str,
        member_peer: Optional[str],
    ) -> Optional[bytes]:
        remote = self._copy_outer_to_remote_temp_for_clfy(outer_path)
        if not remote:
            return None
        print(
            f"[CRM] ClfyAgent: retrying nested extract from remote temp copy "
            f"({outer_path.name})"
        )
        return self._extract_clfy_side_all_strategies(remote, member_self, member_peer)
    
    def _compare_nested_archive_contents(self, outer_path1: Path, member_path1: str,
                                          outer_path2: Path, member_path2: str) -> dict:
        """Compare contents of two nested archives (e.g., ClfyCore.jar inside SDK.jar).
        
        This handles the case where both archives are nested inside parent archives
        and we need to extract them to compare their contents.
        """
        print(f"[CRM] Comparing nested archives:")
        print(f"[CRM]   Old: {outer_path1}!/{member_path1}")
        print(f"[CRM]   New: {outer_path2}!/{member_path2}")
        
        try:
            resolved1 = self._resolve_outer_archive_for_zip(Path(outer_path1)) or Path(outer_path1)
            resolved2 = self._resolve_outer_archive_for_zip(Path(outer_path2)) or Path(outer_path2)
            member_path1 = self._normalize_virtual_member_path(resolved1, member_path1)
            member_path2 = self._normalize_virtual_member_path(resolved2, member_path2)

            data1 = self._extract_clfy_side_all_strategies(
                resolved1, member_path1, member_path2
            )
            data2 = self._extract_clfy_side_all_strategies(
                resolved2, member_path2, member_path1
            )

            if data1 is None or data2 is None:
                # Legacy fallbacks for stale virtual paths saved before normalization.
                b1 = os.path.basename((member_path1 or "").replace("\\", "/").strip("/"))
                b2 = os.path.basename((member_path2 or "").replace("\\", "/").strip("/"))
                if data1 is None and b1:
                    data1 = self._extract_member_by_basename_from_outer(
                        resolved1, b1, peer_hint=member_path2
                    )
                if data2 is None and b2:
                    data2 = self._extract_member_by_basename_from_outer(
                        resolved2, b2, peer_hint=member_path1
                    )
                root_virt = self._clfy_agent_root_member_from_path(member_path1) or self._clfy_agent_root_member_from_path(
                    member_path2
                )
                if root_virt and (data1 is None or data2 is None):
                    if data1 is None:
                        data1 = self._extract_clfy_side_all_strategies(
                            resolved1, root_virt, member_path2
                        )
                    if data2 is None:
                        data2 = self._extract_clfy_side_all_strategies(
                            resolved2, root_virt, member_path1
                        )
                if data1 is None:
                    data1 = self._extract_clfy_bundle_via_outer_scan(resolved1, member_path2)
                if data2 is None:
                    data2 = self._extract_clfy_bundle_via_outer_scan(resolved2, member_path1)

            if data1 is None or data2 is None:
                msg = (
                    "Could not extract nested archives for comparison "
                    f"(old_ok={data1 is not None}, new_ok={data2 is not None})"
                )
                print(f"[CRM] {msg}")
                return {
                    "error": msg,
                    "only_in_old": set(),
                    "only_in_new": set(),
                    "different_content": [],
                    "class_files_changed": [],
                    "config_files_changed": [],
                    "config_diff_details": [],
                    "class_modified_detail": [],
                    "total_members_old": 0,
                    "total_members_new": 0,
                }

            try:
                with zipfile.ZipFile(BytesIO(data1), "r") as zf1, zipfile.ZipFile(
                    BytesIO(data2), "r"
                ) as zf2:
                    return self._diff_clfy_container_size_from_zipfiles(zf1, zf2) or {}
            except Exception as e:
                logger.warning(f"[CRM] ClfyAgent nested size-only compare failed: {e}")
                return {"error": str(e), "only_in_old": set(), "only_in_new": set()}

        except Exception as e:
            print(f"[CRM] Error comparing nested archives: {e}")
            logger.warning(f"[CRM] Error comparing nested archives: {e}")
            return {
                "error": str(e),
                "only_in_old": set(),
                "only_in_new": set(),
                "different_content": [],
                "class_files_changed": [],
                "config_files_changed": [],
                "config_diff_details": [],
                "class_modified_detail": [],
                "total_members_old": 0,
                "total_members_new": 0,
            }
    
    @staticmethod
    def _zip_member_lookup_variants(member_name: str) -> list[str]:
        """
        Candidate path variants for ZIP entry lookup.
        Handles slash direction, leading './', and repeated slashes.
        """
        raw = (member_name or "").replace("\\", "/").strip()
        if not raw:
            return []
        variants = [raw]
        raw2 = raw.strip("/")
        if raw2 and raw2 not in variants:
            variants.append(raw2)
        if raw2.startswith("./"):
            v = raw2[2:]
            if v and v not in variants:
                variants.append(v)
        if "//" in raw2:
            v = re.sub(r"/+", "/", raw2)
            if v and v not in variants:
                variants.append(v)
        return variants

    @staticmethod
    def _score_clfyagent_path_choice(entry: str, peer_hint: Optional[str]) -> int:
        """
        Prefer the same logical location as the peer build when multiple ClfyAgent.zip
        entries exist (e.g. lib/ vs sharedlib/, or different nesting depth).
        """
        nn = entry.replace("\\", "/").strip("/").lower()
        score = 0
        if "clfyagent" in nn:
            if "/modules/crm/lib/" in nn and "/sharedlib/" not in nn:
                score += 400
            if "/sharedlib/" in nn:
                score -= 200
        if not peer_hint:
            score -= len(nn)
            return score
        peer = peer_hint.replace("\\", "/").strip("/").lower()
        if nn == peer:
            score += 10000
        pe, ne = peer.split("/"), nn.split("/")
        for a, b in zip(reversed(pe), reversed(ne)):
            if a == b:
                score += 80
            else:
                break
        if peer and (peer in nn or nn in peer):
            score += 500
        score -= len(nn) // 100
        return score

    @staticmethod
    def _pick_basename_hits(
        hits: list[str], member_name: str, basename_peer_hint: Optional[str]
    ) -> list[str]:
        """Order duplicate basename matches: best peer alignment first, then stable tie-break."""
        if len(hits) <= 1:
            return hits
        peer = basename_peer_hint or member_name
        return sorted(
            hits,
            key=lambda h: (
                CRMFolderComparer._score_clfyagent_path_choice(h, peer),
                -len(h.replace("\\", "/")),
            ),
            reverse=True,
        )

    @staticmethod
    def _read_member_bytes_from_zip(
        zf: zipfile.ZipFile,
        member_name: str,
        basename_peer_hint: Optional[str] = None,
    ) -> Optional[bytes]:
        """
        Read a member from a zip with robust matching:
        1) exact / normalized variants
        2) case-insensitive path match
        3) basename fallback (unique, or best match vs peer_hint when duplicates exist)
        """
        variants = CRMFolderComparer._zip_member_lookup_variants(member_name)
        if not variants:
            return None
        # Fast path: exact lookup
        for v in variants:
            try:
                return zf.read(v)
            except KeyError:
                pass

        # Case-insensitive path lookup
        lower_to_name = {}
        for n in zf.namelist():
            nk = n.replace("\\", "/").strip("/").lower()
            if nk and nk not in lower_to_name:
                lower_to_name[nk] = n
        for v in variants:
            hit = lower_to_name.get(v.lower().strip("/"))
            if hit:
                try:
                    return zf.read(hit)
                except Exception:
                    return None

        # Last fallback: basename match (disambiguate duplicates using peer hint)
        basenames = [os.path.basename(v).lower() for v in variants if os.path.basename(v)]
        for b in basenames:
            hits = [n for n in zf.namelist() if os.path.basename(n.replace("\\", "/")).lower() == b]
            if not hits:
                continue
            ordered = CRMFolderComparer._pick_basename_hits(hits, member_name, basename_peer_hint)
            for cand in ordered:
                try:
                    return zf.read(cand)
                except Exception:
                    continue
        return None

    @staticmethod
    def _manifest_implementation_version_from_jar(jar_path: Path) -> tuple[str, str]:
        """
        Read Implementation-Version from META-INF/MANIFEST.MF in a jar.
        Returns: (value, error). If value is empty, error describes why.
        """
        try:
            with zipfile.ZipFile(_fs_path_str(jar_path), "r") as zf:
                try:
                    raw = zf.read("META-INF/MANIFEST.MF")
                except KeyError:
                    return "", "MANIFEST.MF not found"
        except Exception as e:
            return "", f"Could not open JAR: {e}"

        try:
            text = raw.decode("utf-8")
        except Exception:
            text = raw.decode("latin-1", errors="replace")

        # Merge continuation lines (manifest spec: line starts with space).
        merged: list[str] = []
        for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            if ln.startswith(" ") and merged:
                merged[-1] += ln[1:]
            else:
                merged.append(ln)

        for ln in merged:
            if ln.lower().startswith("implementation-version:"):
                return ln.split(":", 1)[1].strip(), ""
        return "", "Implementation-Version not found"

    def _extract_nested_archive(
        self, outer_path: Path, member_path: str, peer_member_hint: Optional[str] = None
    ) -> Optional[bytes]:
        """Extract a nested archive from its parent archive.
        
        member_path can be a path with !/ separators indicating nesting levels.
        E.g., "outer.jar!/inner.jar!/target.jar"
        
        peer_member_hint: when multiple entries share the same basename (e.g. ClfyAgent.zip under
        lib/ and sharedlib/), prefer the path that best matches the other build's member path.
        """
        def extract_from_outer_path(outer: Path) -> Optional[bytes]:
            member_path_norm = self._normalize_virtual_member_path(outer, member_path)
            raw_member = (member_path_norm or "").replace("\\", "/").strip()
            # Accept both "!/" and "!\\" separators from mixed Windows/zip style paths.
            split_parts = [p for p in re.split(r"![\\/]", raw_member) if p and p.strip()]
            parts = [p.replace("\\", "/").strip("/") for p in split_parts if p.strip("/").strip()]
            if not parts:
                return None

            # Some call paths include the outer archive itself as the first segment:
            #   \\server\...\SDK_x.jar!/crm/.../ClfyAgent.zip
            # Remove that segment so lookup starts at the first real member.
            first = parts[0]
            first_base = os.path.basename(first).lower()
            outer_base = outer.name.lower()
            if first_base == outer_base:
                parts = parts[1:]
            elif first.lower().endswith("/" + outer_base):
                parts = parts[1:]
            if not parts:
                return None

            with zipfile.ZipFile(_fs_path_str(outer), "r") as zf:
                current_data = self._read_member_bytes_from_zip(
                    zf, parts[0], basename_peer_hint=peer_member_hint
                )
            if current_data is None:
                return None
            for part in parts[1:]:
                current_data = self._zip_read_member_from_nested_bytes(current_data, part)
                if current_data is None:
                    return None
            return current_data

        try:
            outer_path = self._resolve_outer_archive_for_zip(outer_path) or outer_path
            out = extract_from_outer_path(outer_path)
            if out is not None:
                return out
        except Exception as e:
            logger.debug(f"[CRM] Initial extract failed for {member_path} from {outer_path}: {e}")

        # Fallback for flaky direct reads (especially UNC/long-path): use local temp copy.
        try:
            local_outer = self._copy_file_to_temp_for_clfy_read(outer_path)
            if local_outer:
                out = extract_from_outer_path(local_outer)
                if out is not None:
                    print(
                        f"[CRM] Nested extract succeeded via local copy for "
                        f"{outer_path.name}!/{member_path}"
                    )
                    return out
        except Exception as e:
            logger.debug(f"[CRM] Local-copy extract failed for {member_path} from {outer_path}: {e}")

        logger.debug(f"[CRM] Could not extract {member_path} from {outer_path}")
        # Final fallback: use `jar tf` + `jar xf` for top-level members when ZipFile parsing
        # is unreliable on some SDK archives (corrupt/odd extra fields).
        try:
            raw_member = (member_path or "").replace("\\", "/").strip().strip("/")
            split_parts = [p for p in re.split(r"![\\/]", raw_member) if p and p.strip()]
            parts = [p.replace("\\", "/").strip("/") for p in split_parts if p.strip("/").strip()]
            if parts:
                first = parts[0]
                first_base = os.path.basename(first).lower()
                outer_base = outer_path.name.lower()
                if first_base == outer_base or first.lower().endswith("/" + outer_base):
                    parts = parts[1:]
            # jar tool can extract only top-level entries from the outer archive.
            if len(parts) == 1:
                target = parts[0]
                outer = outer_path
                local_outer = self._copy_file_to_temp_for_clfy_read(outer)
                if local_outer:
                    outer = local_outer
                listed_member = None
                try:
                    result = subprocess.run(
                        [_jar_tool_cmd(), "tf", _fs_path_str(outer)],
                        capture_output=True,
                        text=True,
                        timeout=300,
                    )
                    if result.returncode == 0 and result.stdout:
                        rows = [ln.strip().replace("\\", "/").strip("/") for ln in result.stdout.splitlines() if ln.strip()]
                        target_norm = target.replace("\\", "/").strip("/")
                        # Prefer exact match first, then basename fallback.
                        for ln in rows:
                            if ln == target_norm:
                                listed_member = ln
                                break
                        if listed_member is None:
                            b = os.path.basename(target_norm).lower()
                            hits = [ln for ln in rows if os.path.basename(ln).lower() == b]
                            if len(hits) == 1:
                                listed_member = hits[0]
                            elif len(hits) > 1:
                                ranked = self._pick_basename_hits(
                                    hits, peer_member_hint or target_norm, peer_member_hint
                                )
                                listed_member = ranked[0] if ranked else None
                except Exception as e:
                    logger.debug(f"[CRM] jar tf fallback list failed for {outer}: {e}")
                if listed_member:
                    tmp_dir = mkdtemp_compare(prefix="crm_clfy_jar_extract_")
                    try:
                        result = subprocess.run(
                            [_jar_tool_cmd(), "xf", _fs_path_str(outer), listed_member],
                            cwd=tmp_dir,
                            capture_output=True,
                            text=True,
                            timeout=300,
                        )
                        if result.returncode == 0:
                            out_file = Path(tmp_dir) / listed_member
                            if out_file.is_file():
                                data = out_file.read_bytes()
                                print(
                                    f"[CRM] Nested extract succeeded via jar fallback for "
                                    f"{outer_path.name}!/{listed_member}"
                                )
                                return data
                    finally:
                        try:
                            shutil.rmtree(tmp_dir, ignore_errors=True)
                        except Exception:
                            pass
        except Exception as e:
            logger.debug(f"[CRM] jar extraction fallback failed for {member_path} from {outer_path}: {e}")
        return None

    def _extract_member_by_basename_from_outer(
        self,
        outer_path: Path,
        basename: str,
        peer_hint: Optional[str] = None,
    ) -> Optional[bytes]:
        """
        Fallback extractor for nested members: find by basename anywhere in outer archive
        and extract using the discovered virtual member path.
        """
        b = (basename or "").strip().lower()
        if not b:
            return None
        outer = outer_path
        local = self._copy_file_to_temp_for_clfy_read(outer_path)
        if local:
            outer = local
        try:
            with zipfile.ZipFile(_fs_path_str(outer), "r") as zf:
                virt = self._find_member_virtual_by_basename_in_zip(zf, b, "")
            if virt:
                return self._extract_nested_archive(outer, virt, peer_member_hint=peer_hint)
        except Exception as e:
            logger.debug(
                f"[CRM] Basename ZipFile path failed for {basename} from {outer_path}: {e}"
            )
        blob = self._extract_member_bytes_jar_tf_xf(outer_path, b, peer_hint=peer_hint)
        if blob:
            print(f"[CRM] Basename extract for {basename!r} succeeded via jar tf/xf from {outer_path.name}")
            return blob
        return None

    def _looks_like_zip_file_bytes(self, blob: Optional[bytes]) -> bool:
        if not blob or len(blob) < 4:
            return False
        return blob.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"))

    def _extract_clfyagent_bytes_rediscover(
        self, outer_path: Path, peer_member_hint: Optional[str]
    ) -> Optional[bytes]:
        """
        Last resort for ClfyAgent.zip / .ear: try every jar-tf entry with this basename (peer-ranked)
        until one yields a valid zip payload. Fixes duplicate basenames where the shortest path
        is not the real agent bundle (jar tf sort-by-len previously hid the correct member).
        """
        outer = outer_path
        local = self._copy_file_to_temp_for_clfy_read(outer_path)
        if local:
            outer = local
        tmp_dir: Optional[str] = None
        try:
            result = subprocess.run(
                [_jar_tool_cmd(), "tf", _fs_path_str(outer)],
                capture_output=True,
                text=True,
                timeout=300,
            )
            if result.returncode != 0 or not result.stdout:
                return None
            rows = [
                ln.strip().replace("\\", "/").strip("/")
                for ln in result.stdout.splitlines()
                if ln.strip()
            ]
            hits = [
                ln
                for ln in rows
                if os.path.basename(ln).lower() in _CLFY_AGENT_BUNDLE_BASENAMES_LOWER
            ]
            if not hits:
                return None
            if len(hits) > 1:
                hits = self._pick_basename_hits(
                    hits, peer_member_hint or "", peer_member_hint
                )
            for listed_member in hits:
                tmp_dir = mkdtemp_compare(prefix="crm_clfy_redisc_")
                try:
                    x = subprocess.run(
                        ["jar", "xf", _fs_path_str(outer), listed_member],
                        cwd=tmp_dir,
                        capture_output=True,
                        text=True,
                        timeout=300,
                    )
                    if x.returncode != 0:
                        continue
                    segs = [p for p in listed_member.replace("\\", "/").split("/") if p]
                    out_file = Path(tmp_dir).joinpath(*segs) if segs else Path(tmp_dir)
                    if not out_file.is_file():
                        continue
                    data = out_file.read_bytes()
                    if self._looks_like_zip_file_bytes(data):
                        print(
                            f"[CRM] ClfyAgent rediscover: jar xf {listed_member!r} "
                            f"from {outer_path.name}"
                        )
                        return data
                finally:
                    try:
                        shutil.rmtree(tmp_dir, ignore_errors=True)
                    except Exception:
                        pass
                    tmp_dir = None
        except Exception as e:
            logger.debug(f"[CRM] ClfyAgent rediscover failed for {outer_path}: {e}")
        return None
    
    @staticmethod
    def _parse_shell_zip_appended_path(path: Path) -> Optional[tuple[Path, str]]:
        """
        Windows Explorer "folder" paths inside a zip look like:
          \\\\server\\share\\...\\SDK_CRM_x.jar\\crm\\CRM_10.24\\...\\lib\\ClfyBillingManager.jar
        That path is not a real Win32 file path; split into outer archive file + inner member path.
        Tries each '.jar\\\\' occurrence until the archive prefix exists as a file.
        """
        s = str(path).replace("/", "\\")
        lower = s.lower()
        start = 0
        while True:
            idx = lower.find(".jar\\", start)
            if idx == -1:
                return None
            archive_str = s[: idx + 4]
            inner = s[idx + 5 :].replace("\\", "/").strip("/")
            if inner and _path_exists(archive_str) and _path_is_file(archive_str):
                ap = Path(archive_str)
                if ap.suffix.lower() in (".jar", ".war", ".ear", ".zip"):
                    return ap, inner
            start = idx + 4
    
    @staticmethod
    def _shell_inner_to_virtual_member_path(inner_posix: str) -> str:
        """Explorer uses 'a.jar\\\\b\\\\c'; nested zip members use a.jar!/b/c."""
        s = inner_posix.replace("\\", "/").strip("/")
        if ".jar/" not in s.lower():
            return s
        prev = None
        out = s
        while prev != out:
            prev = out
            out = re.sub(r"(?i)\.jar/", ".jar!/", out)
        return out
    
    def _extract_jar_via_shell_zip_path(self, archive_path: Path, inner_posix: str, raw_query: str) -> Optional[Path]:
        """Read inner .jar member from outer archive (shell-style path); write temp for class diff."""
        raw = (raw_query or "").strip()
        if not raw:
            return None
        orig_for_report = str(archive_path)
        arc = archive_path
        if self._is_network_path(arc):
            local_arc = self._copy_file_to_temp_for_clfy_read(arc)
            if local_arc:
                arc = local_arc
                self._jar_extracted_source_map[str(arc)] = orig_for_report
        q, q_file, q_base = self._class_diff_jar_query_parts(raw)
        base_l = os.path.basename(inner_posix).lower()
        if not self._class_diff_jar_name_matches(base_l, q, q_file, q_base):
            return None
        if not inner_posix.lower().endswith(".jar"):
            return None
        virtual = self._shell_inner_to_virtual_member_path(inner_posix)
        try:
            blob = self._extract_nested_archive(arc, virtual)
            if not blob:
                logger.warning(
                    f"[CRM] JAR resolve: could not extract shell path member {virtual!r} from {arc}"
                )
                return None
        except Exception as e:
            logger.warning(f"[CRM] JAR resolve: could not read {virtual!r} from {arc}: {e}")
            return None
        try:
            fd, tmp_name = tempfile.mkstemp(suffix=".jar", prefix="crm_class_diff_")
            try:
                os.write(fd, blob)
            finally:
                os.close(fd)
            p = Path(tmp_name)
            self._jar_class_diff_temp_paths.append(p)
            self._jar_extracted_source_map[str(p)] = f"{orig_for_report}!/{virtual}"
            print(f"[CRM] JAR resolve: shell/Explorer member {virtual!r} -> {p}")
            return p
        except Exception as e:
            logger.warning(f"[CRM] Could not write temp JAR from shell path: {e}")
            return None
    
    @staticmethod
    def _normalize_class_diff_query(query: str) -> str:
        """Strip BOM/quotes/whitespace from the JAR name users paste into the class-diff field."""
        s = (query or "").replace("\ufeff", "").strip()
        if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
            s = s[1:-1].strip()
        return s.strip()

    @classmethod
    def _parse_class_diff_jar_names(cls, raw: str) -> list[str]:
        """Split comma/newline-separated JAR names, normalize, dedupe (order preserved)."""
        out: list[str] = []
        seen: set[str] = set()
        for part in (raw or "").replace("\n", ",").split(","):
            q = cls._normalize_class_diff_query(part)
            if not q:
                continue
            key = q.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(q)
        return out
    
    @staticmethod
    def _class_diff_jar_query_parts(raw: str) -> tuple[str, str, str]:
        """Lowercased query pieces: (q_full, q_file basename with .jar, q_base stem)."""
        q_full = raw.strip().lower().replace("\\", "/")
        q_name = os.path.basename(q_full)
        if "*" in q_name or "?" in q_name:
            q_base = q_name
            if q_base.endswith(".jar"):
                q_base = q_base[: -len(".jar")]
            q_base = q_base.replace("*", "").replace("?", "")
            return q_full, q_name, q_base
        if not q_name.endswith(".jar"):
            return q_full, q_name + ".jar", q_name
        return q_full, q_name, q_name[: -len(".jar")]

    def _foundation_jar_skip_class_scan_lower(self) -> frozenset[str]:
        extra = (os.environ.get("CRM_FOUNDATION_JAR_SKIP_LIST") or "").strip()
        if not extra:
            return _CRM_FOUNDATION_JAR_SKIP_CLASS_SCAN_LOWER
        names: set[str] = set()
        for part in extra.split(","):
            n = part.strip().lower()
            if not n:
                continue
            names.add(n if n.endswith(".jar") else f"{n}.jar")
        return _CRM_FOUNDATION_JAR_SKIP_CLASS_SCAN_LOWER | frozenset(names)

    def _explicit_class_diff_jar_queries(self) -> list[str]:
        return self._parse_class_diff_jar_names(self.class_diff_jar_name or "")

    def _should_skip_class_scan_inside_nested_archive(self, member_name_or_path: str) -> bool:
        """
        Skip deep .class listing inside foundation JARs unless the user named this JAR
        in the optional class-diff text box.
        """
        basename = os.path.basename(
            str(member_name_or_path or "").replace("\\", "/").rstrip("/")
        ).lower()
        if basename not in self._foundation_jar_skip_class_scan_lower():
            return False
        for raw_q in self._explicit_class_diff_jar_queries():
            qn = self._normalize_class_diff_query(raw_q)
            q, q_file, q_base = self._class_diff_jar_query_parts(qn)
            if self._class_diff_jar_name_matches(basename, q, q_file, q_base):
                return False
        return True

    def _member_path_under_skipped_foundation_jar(self, member_path: str) -> bool:
        """True when a virtual member path lies inside a foundation JAR excluded from class scan."""
        raw = str(member_path or "").replace("\\", "/").strip("/")
        if not raw:
            return False
        for seg in re.split(r"![\\/]", raw):
            bn = os.path.basename(seg).strip().lower()
            if bn and self._should_skip_class_scan_inside_nested_archive(bn):
                return True
        return False

    def _log_foundation_jar_class_scan_policy_once(self) -> None:
        if self._foundation_skip_policy_logged:
            return
        self._foundation_skip_policy_logged = True
        sample = ", ".join(sorted(self._foundation_jar_skip_class_scan_lower())[:6])
        print(
            f"[CRM] Foundation JARs excluded from automatic .class scan ({len(self._foundation_jar_skip_class_scan_lower())} names; "
            f"e.g. {sample}, …). Name a JAR in the class-diff text box to include it.",
            flush=True,
        )

    def _drop_class_paths_under_skipped_foundation_jars(self, paths) -> list:
        out: list = []
        for p in paths or []:
            ps = str(p)
            if ps.lower().endswith(".class") and self._member_path_under_skipped_foundation_jar(ps):
                continue
            out.append(p)
        return out

    def _drop_class_set_under_skipped_foundation_jars(self, paths: set) -> set:
        return set(self._drop_class_paths_under_skipped_foundation_jars(paths))

    def _filter_archive_diff_foundation_classes(self, diff: Optional[dict]) -> Optional[dict]:
        """Remove .class diffs inside foundation JARs from an archive_diff payload."""
        if not diff or diff.get("error"):
            return diff
        d = dict(diff)
        for key in ("only_in_old", "only_in_new"):
            val = d.get(key)
            if isinstance(val, set):
                d[key] = self._drop_class_set_under_skipped_foundation_jars(val)
        for key in ("different_content", "class_files_changed", "config_files_changed"):
            val = d.get(key)
            if isinstance(val, list):
                d[key] = self._drop_class_paths_under_skipped_foundation_jars(val)
        cmd = d.get("class_modified_detail")
        if isinstance(cmd, list):
            d["class_modified_detail"] = [
                row
                for row in cmd
                if not self._member_path_under_skipped_foundation_jar(
                    str(row.get("path") or "")
                )
            ]
        return d
    
    def _class_diff_jar_name_matches(self, basename_lower: str, q: str, q_file: str, q_base: str) -> bool:
        b = (basename_lower or "").lower().strip()
        if not b:
            return False
        if "*" in q_file or "?" in q_file:
            qf = (q_file or "").lower()
            b_stem = b[: -len(".jar")] if b.endswith(".jar") else b
            q_stem = qf[: -len(".jar")] if qf.endswith(".jar") else qf

            def _wm(pat: str, name: str) -> bool:
                return fnmatch.fnmatch(name, pat) or fnmatch.fnmatch(name.lower(), pat.lower())

            # Full basename (e.g. scala-library*.jar vs scala-library-2.13.18.jar)
            if _wm(qf, b):
                return True
            # Stem glob (e.g. scala-library* / scala-library*.jar vs stem scala-library-2.13.18)
            if _wm(q_stem, b_stem):
                return True
            # Glob omitted ".jar" (e.g. scala-library*); match full jar basename.
            if not qf.endswith(".jar") and _wm(qf + ".jar", b):
                return True
            return False
        # Fast exact/contains checks.
        if b == q_file:
            return True
        if bool(q_base) and q_base in b:
            return True
        # Compare compact stems so names like foo-1.2.3.jar still match foo.jar.
        b_stem = b[: -len(".jar")] if b.endswith(".jar") else b
        q_stem = q_base or (q_file[: -len(".jar")] if q_file.endswith(".jar") else q_file)
        b_compact = re.sub(r"[^a-z0-9]+", "", b_stem)
        q_compact = re.sub(r"[^a-z0-9]+", "", q_stem)
        if q_compact and q_compact in b_compact:
            return True
        if q_compact and b_compact and ("*" not in q_file and "?" not in q_file):
            if difflib.SequenceMatcher(None, q_compact, b_compact).ratio() >= 0.95:
                return True
        # If query included a full member path, allow basename token to match that path.
        if q and (f"/{b}" in q or q.endswith(f"/{b}")):
            return True
        return False

    @staticmethod
    def _find_preferred_decompiler() -> tuple[Optional[str], Optional[str]]:
        """
        Prefer CFR (explicit cfr-0.152 path first), then fall back to generic finder.
        """
        cfr_env = os.environ.get("CFR_JAR", "").strip()
        candidates = [
            cfr_env,
            r"C:\cfr-0.152.jar",
            str((Path.cwd() / "cfr-0.152.jar")),
            str((Path.cwd() / "cfr.jar")),
            str((Path(__file__).resolve().parent / "cfr-0.152.jar")),
            str((Path(__file__).resolve().parent.parent / "cfr-0.152.jar")),
        ]
        for p in candidates:
            if p and os.path.exists(p):
                return "cfr", os.path.abspath(p)
        return _find_java_decompiler()

    def _extract_member_bytes_from_archive(self, archive_path: Path, member_path: str) -> Optional[bytes]:
        """
        Read bytes for an entry from archive_path.
        Supports nested paths like outer.jar!/inner.jar!/a/b/C.class.
        """
        member = (member_path or "").replace("\\", "/").strip("/")
        if not member:
            return None
        if "!/" in member:
            return self._extract_nested_archive(archive_path, member)
        try:
            with zipfile.ZipFile(_fs_path_str(archive_path), "r") as zf:
                try:
                    return zf.read(member)
                except KeyError:
                    for n in zf.namelist():
                        if n.lower() == member.lower():
                            return zf.read(n)
        except Exception:
            return None
        return None

    @staticmethod
    def _extract_nested_archive_from_bytes(blob: bytes, member_path: str) -> Optional[bytes]:
        parts = (member_path or "").split("!/")
        cur = blob
        for part in parts:
            if not part:
                continue
            try:
                with zipfile.ZipFile(BytesIO(cur), "r") as zf:
                    try:
                        cur = zf.read(part)
                    except KeyError:
                        hit = next((n for n in zf.namelist() if n.lower() == part.lower()), None)
                        if not hit:
                            return None
                        cur = zf.read(hit)
            except Exception:
                return None
        return cur

    def _resolve_jar_from_clfy_agent(self, root: Path, query: str) -> Optional[Path]:
        """
        Fallback: find requested JAR inside ClfyAgent.zip / ClfyAgent.ear under root and extract to temp .jar.
        """
        info = self._synthetic_clfy_agent_info(root)
        if not info:
            return None
        outer = Path(info.get("path"))
        outer_report = str(outer)
        if _path_exists(outer) and _path_is_file(outer) and self._is_network_path(outer):
            local_outer = self._copy_file_to_temp_for_clfy_read(outer)
            if local_outer:
                outer = local_outer
                self._jar_extracted_source_map[str(outer)] = outer_report
        member = str(info.get("archive_member") or "")
        clfy_blob = self._extract_nested_archive(outer, member) if info.get("is_nested") else (
            outer.read_bytes() if _path_exists(outer) and _path_is_file(outer) else None
        )
        if not clfy_blob:
            return None
        raw = self._normalize_class_diff_query(query)
        q, q_file, q_base = self._class_diff_jar_query_parts(raw)
        try:
            with zipfile.ZipFile(BytesIO(clfy_blob), "r") as zf:
                virt = self._find_matching_jar_virtual_in_zipfile(zf, q, q_file, q_base, "")
            if not virt:
                return None
            jar_blob = self._extract_nested_archive_from_bytes(clfy_blob, virt)
        except Exception:
            jar_blob = None
        if not jar_blob:
            return None
        try:
            fd, tmp_name = tempfile.mkstemp(suffix=".jar", prefix="crm_class_diff_clfy_")
            try:
                os.write(fd, jar_blob)
            finally:
                os.close(fd)
            p = Path(tmp_name)
            self._jar_class_diff_temp_paths.append(p)
            mem_sl = str(info.get("archive_member") or info.get("rel_key") or "").replace("\\", "/").strip("/")
            virt_sl = virt.replace("\\", "/").strip("/")
            if mem_sl:
                self._jar_extracted_source_map[str(p)] = f"{outer_report}!/{mem_sl}!/{virt_sl}"
            else:
                self._jar_extracted_source_map[str(p)] = f"{outer_report}!/{virt_sl}"
            print(f"[CRM] JAR resolve fallback: extracted {query!r} from ClfyAgent bundle -> {p}")
            return p
        except Exception:
            return None

    def _clfy_bundle_logical_jar_path_for_query(self, root: Path, query: str) -> Optional[str]:
        """
        Report-style `outer!/…!/member.jar` path to the JAR as resolved **only** under the
        ClfyAgent .zip / .ear bundle (same virtual layout as the class-diff fallback, without a full-tree search).
        """
        info = self._synthetic_clfy_agent_info(root)
        if not info:
            return None
        outer = Path(info.get("path"))
        outer_report = str(outer)
        if _path_exists(outer) and _path_is_file(outer) and self._is_network_path(outer):
            local_outer = self._copy_file_to_temp_for_clfy_read(outer)
            if local_outer:
                outer = local_outer
        member = str(info.get("archive_member") or "")
        clfy_blob = self._extract_nested_archive(outer, member) if info.get("is_nested") else (
            outer.read_bytes() if _path_exists(outer) and _path_is_file(outer) else None
        )
        if not clfy_blob:
            return None
        raw = self._normalize_class_diff_query(query)
        q, q_file, q_base = self._class_diff_jar_query_parts(raw)
        try:
            with zipfile.ZipFile(BytesIO(clfy_blob), "r") as zf:
                virt = self._find_matching_jar_virtual_in_zipfile(zf, q, q_file, q_base, "")
        except Exception:
            return None
        if not virt:
            return None
        mem_sl = str(info.get("archive_member") or info.get("rel_key") or "").replace("\\", "/").strip("/")
        virt_sl = virt.replace("\\", "/").strip("/")
        if mem_sl:
            return f"{outer_report}!/{mem_sl}!/{virt_sl}"
        return f"{outer_report}!/{virt_sl}"

    def _collect_jar_query_path_strings(
        self,
        root: Path,
        query: str,
        files_map: Optional[dict],
        *,
        clfy_only_hint: Optional[str] = None,
        primary_logical: Optional[str] = None,
        abs_file_path: Optional[str] = None,
        cap: int = 50,
    ) -> list[str]:
        """
        All reportable path strings for JARs matching the query: scan map, primary resolution, Clfy path.
        """
        out: list[str] = []
        seen: set[str] = set()

        def add(s: Optional[str]) -> None:
            if s is None:
                return
            t = str(s).strip()
            if not t or t in seen:
                return
            seen.add(t)
            out.append(t)

        add(clfy_only_hint)
        add(primary_logical)
        add(abs_file_path)
        raw = self._normalize_class_diff_query(query)
        if not raw or len(out) >= cap:
            return out[:cap]
        q, q_file, q_base = self._class_diff_jar_query_parts(raw)
        if isinstance(files_map, dict) and files_map:
            for rel in files_map.keys():
                rel_s = str(rel).replace("\\", "/")
                b = os.path.basename(rel_s).lower()
                if b.endswith(".jar") and self._class_diff_jar_name_matches(b, q, q_file, q_base):
                    add(rel_s)
                if len(out) >= cap:
                    return out[:cap]
        return out[:cap]

    def _enrich_jar_entry_path_metadata(
        self,
        d: dict,
        root1: Path,
        root2: Path,
        query: str,
        files1_map: Optional[dict],
        files2_map: Optional[dict],
    ) -> None:
        """Add clfy-only paths and all-matching path lists for manifest/class optional JAR rows."""
        jo = d.get("jar_old")
        jn = d.get("jar_new")
        jo_s = str(jo) if jo else None
        jn_s = str(jn) if jn else None
        try:
            jo_p = Path(jo) if jo else None
        except TypeError:
            jo_p = Path(jo_s) if jo_s else None
        try:
            jn_p = Path(jn) if jn else None
        except TypeError:
            jn_p = Path(jn_s) if jn_s else None
        clfo = self._clfy_bundle_logical_jar_path_for_query(root1, query) or ""
        clfn = self._clfy_bundle_logical_jar_path_for_query(root2, query) or ""
        d["clfy_jar_path_old"] = clfo
        d["clfy_jar_path_new"] = clfn
        d["jar_paths_all_old"] = self._collect_jar_query_path_strings(
            root1,
            query,
            files1_map,
            clfy_only_hint=clfo or None,
            primary_logical=self._logical_jar_source_path(jo_p) if jo_p else None,
            abs_file_path=jo_s,
        )
        d["jar_paths_all_new"] = self._collect_jar_query_path_strings(
            root2,
            query,
            files2_map,
            clfy_only_hint=clfn or None,
            primary_logical=self._logical_jar_source_path(jn_p) if jn_p else None,
            abs_file_path=jn_s,
        )

    @staticmethod
    def _sorted_infolist_sharedlib_first(zf: zipfile.ZipFile) -> list:
        """Prefer entries under sharedlib/ and CrmSharedLib.ear so JAR resolve matches CRM layout."""
        infos = [i for i in zf.infolist() if not i.is_dir()]
        def _sk(info: zipfile.ZipInfo) -> tuple:
            n = (info.filename or "").replace("\\", "/").strip("/").lower()
            return (
                0 if "sharedlib" in n else 1,
                0 if "crmsharedlib.ear" in n else 1,
                n,
            )
        return sorted(infos, key=_sk)

    def _find_matching_jar_virtual_in_zipfile(
        self,
        zf: zipfile.ZipFile,
        q: str,
        q_file: str,
        q_base: str,
        prefix: str,
    ) -> Optional[str]:
        """
        First nested .jar member matching the class-diff query. Returns a virtual path
        for _extract_nested_archive (use !/ between nested archive segments).
        Descends into .jar/.war/.ear/.zip like _scan_archive_contents so JARs inside
        nested .zip bundles (e.g. CRM SDK) are discoverable.
        Iteration order prefers sharedlib/CrmSharedLib.ear paths so scala/libs there win
        over duplicate basenames elsewhere in the SDK.
        """
        exts = _CRM_CLASS_DIFF_NESTED_ARCHIVE_SUFFIXES
        for info in self._sorted_infolist_sharedlib_first(zf):
            name = info.filename.replace("\\", "/").strip("/")
            if not name:
                continue
            name_l = name.lower()
            if not name_l.endswith(exts):
                continue
            base_l = os.path.basename(name).lower()
            full_key = f"{prefix}!/{name}" if prefix else name
            if name_l.endswith(".jar") and self._class_diff_jar_name_matches(base_l, q, q_file, q_base):
                return full_key
            try:
                data = zf.read(info.filename)
                with zipfile.ZipFile(BytesIO(data), "r") as inner:
                    hit = self._find_matching_jar_virtual_in_zipfile(inner, q, q_file, q_base, full_key)
                    if hit:
                        return hit
            except (zipfile.BadZipFile, OSError, RuntimeError):
                continue
        return None

    def _collect_wildcard_jar_matches_from_zip_bytes(
        self,
        data: bytes,
        virt_prefix: str,
        depth: int,
        max_depth: int,
        q: str,
        q_file: str,
        q_base: str,
        out: set[str],
        pri: dict[str, int],
    ) -> None:
        if depth >= max_depth or not data or len(data) < 4:
            return
        if not data.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
            return
        try:
            with zipfile.ZipFile(BytesIO(data), "r") as zf:
                self._collect_wildcard_jar_matches_from_zipfile(
                    zf, virt_prefix, depth, max_depth, q, q_file, q_base, out, pri
                )
        except (zipfile.BadZipFile, OSError, RuntimeError):
            return

    def _collect_wildcard_jar_matches_from_zipfile(
        self,
        zf: zipfile.ZipFile,
        virt_prefix: str,
        depth: int,
        max_depth: int,
        q: str,
        q_file: str,
        q_base: str,
        out: set[str],
        pri: dict[str, int],
    ) -> None:
        """DFS wildcard basename collection with sharedlib/CrmSharedLib.ear path priority (0=best)."""
        exts = _CRM_CLASS_DIFF_NESTED_ARCHIVE_SUFFIXES
        for info in self._sorted_infolist_sharedlib_first(zf):
            name = info.filename.replace("\\", "/").strip("/")
            if not name:
                continue
            nl = name.lower()
            full_virt = f"{virt_prefix}!/{name}" if virt_prefix else name
            vp_low = full_virt.lower()
            region_pri = 0 if ("/sharedlib/" in vp_low or "crmsharedlib.ear" in vp_low) else 1
            if nl.endswith(".jar"):
                base_l = os.path.basename(name).lower()
                if self._class_diff_jar_name_matches(base_l, q, q_file, q_base):
                    bn = os.path.basename(name)
                    out.add(bn)
                    pri[bn] = min(pri.get(bn, 9), region_pri)
            if nl.endswith(exts) and depth + 1 < max_depth:
                try:
                    nested = zf.read(info.filename)
                    self._collect_wildcard_jar_matches_from_zip_bytes(
                        nested, full_virt, depth + 1, max_depth, q, q_file, q_base, out, pri
                    )
                except (zipfile.BadZipFile, OSError, RuntimeError):
                    continue

    def _deep_collect_matching_jar_basenames_under_root(
        self,
        root: Path,
        q: str,
        q_file: str,
        q_base: str,
        *,
        max_depth: int = 18,
    ) -> tuple[set[str], dict[str, int]]:
        """
        For wildcard JAR queries: collect basenames that match at any nesting depth, including
        JARs inside sharedlib/*.ear (e.g. CrmSharedLib.ear/lib/scala-library-*.jar).
        Independent of deep_nested_archives / shallow _scan_archive_contents.

        Returns (basenames, priority_per_basename) where priority 0 means the match was seen
        under a sharedlib/CrmSharedLib.ear path (virtual path for monolithic SDK, or on-disk path).

        Performance: indexes each on-disk archive once (cached), then filters basenames in memory.
        On disk, only traverses .ear/.war/.zip by default. Set CRM_CLASS_DIFF_WILDCARD_SCAN_DISK_JARS=1
        to also index every on-disk .jar. sharedlib/* archives are processed first.
        """
        out: set[str] = set()
        pri: dict[str, int] = {}
        scan_disk_jars = os.environ.get(
            "CRM_CLASS_DIFF_WILDCARD_SCAN_DISK_JARS", ""
        ).strip().lower() in ("1", "true", "yes", "y", "on")
        opened = [0]

        def _heartbeat() -> None:
            o = opened[0]
            if o > 0 and o % 80 == 0:
                print(
                    f"[CRM] Wildcard deep scan: indexed {o} on-disk archive(s), "
                    f"{len(out)} name(s) matched pattern so far...",
                    flush=True,
                )

        def disk_archive_candidate(name_lower: str) -> bool:
            if name_lower.endswith((".ear", ".war", ".zip")):
                return True
            if name_lower.endswith(".jar") and scan_disk_jars:
                return True
            return False

        if not _path_exists(root):
            return out, pri
        if _path_is_file(root):
            suf = root.suffix.lower()
            if suf in (".jar", ".war", ".ear", ".zip"):
                print(
                    f"[CRM] Wildcard deep scan: indexing nested JARs in {root.name} "
                    f"(sharedlib/CrmSharedLib.ear paths prioritized)...",
                    flush=True,
                )
                arc = root
                if self._is_network_path(root):
                    local_arc = self._copy_file_to_temp_for_clfy_read(root)
                    if local_arc:
                        arc = local_arc
                try:
                    with zipfile.ZipFile(_fs_path_str(arc), "r") as zf:
                        self._collect_wildcard_jar_matches_from_zipfile(
                            zf, "", 0, max_depth, q, q_file, q_base, out, pri
                        )
                except (zipfile.BadZipFile, OSError, RuntimeError) as e:
                    logger.debug(f"[CRM] wildcard zip walk failed for {root}: {e}")
                print(
                    f"[CRM] Wildcard deep scan: done ({len(out)} matching basename(s)).",
                    flush=True,
                )
            return out, pri
        if _path_is_dir(root):
            print(
                "[CRM] Wildcard deep scan: indexing nested JARs "
                "(sharedlib/*.ear first; CRM_CLASS_DIFF_WILDCARD_SCAN_DISK_JARS=1 indexes all .jar)...",
                flush=True,
            )
            to_scan: list[Path] = []
            walk_base = _walk_base_path(root)
            try:
                for dirpath, _, files in os.walk(str(walk_base)):
                    for fn in files:
                        fl = fn.lower()
                        full = Path(dirpath) / fn
                        if fl.endswith(".jar") and self._class_diff_jar_name_matches(
                            fl, q, q_file, q_base
                        ):
                            out.add(fn)
                            dp = 0 if "sharedlib" in str(full).lower() else 1
                            pri[fn] = min(pri.get(fn, 9), dp)
                        if disk_archive_candidate(fl):
                            to_scan.append(full)
            except Exception as e:
                logger.debug(f"[CRM] deep JAR basename walk failed under {root}: {e}")
                return out, pri
            to_scan.sort(
                key=lambda p: (
                    0 if "sharedlib" in str(p).lower() else 1,
                    len(str(p)),
                    str(p).lower(),
                )
            )
            for full in to_scan:
                opened[0] += 1
                _heartbeat()
                try:
                    disk_p = str(full).lower()
                    base_pri = 0 if "sharedlib" in disk_p else 1
                    for bn in self._get_cached_nested_jar_basenames_disk(full, max_depth=max_depth):
                        bl = bn.lower()
                        if self._class_diff_jar_name_matches(bl, q, q_file, q_base):
                            out.add(bn)
                            pri[bn] = min(pri.get(bn, 9), base_pri)
                except Exception:
                    continue
            print(
                f"[CRM] Wildcard deep scan: done ({opened[0]} archive(s) indexed, "
                f"{len(out)} matching basename(s)).",
                flush=True,
            )
        return out, pri

    def _find_jar_in_archive_file(
        self,
        archive_path: Path,
        raw_query: str,
        *,
        logical_archive_str: Optional[str] = None,
    ) -> Optional[Path]:
        """
        Resolve a JAR matching raw_query inside archive_path (zip/jar/war/ear).
        Returns archive_path if its basename matches; otherwise extracts the first matching
        nested .jar to a temp file and returns that path.

        Caches successful virtual paths per (archive identity, query) so repeated resolves skip DFS/jar tf.
        Failed lookups are not cached: UNC I/O, timeouts, or subprocess errors are often transient; caching
        them caused "Could not find matching .jar" on later attempts within the same run or across runs.

        logical_archive_str: when resolving from a temp-extracted nested archive, the full
        build/SDK path prefix (e.g. \\\\share\\SDK.jar!/crm/.../inner.war) for report lines.
        """
        raw = (raw_query or "").strip()
        if not raw or not _path_exists(archive_path) or not _path_is_file(archive_path):
            return None

        arc_key = self._stable_archive_cache_key(archive_path)
        qn = self._normalize_class_diff_query(raw)
        ck = (arc_key, qn)

        display_arc = (logical_archive_str or str(archive_path)).rstrip("/\\")
        arc = archive_path
        if self._is_network_path(arc):
            local_arc = self._copy_file_to_temp_for_clfy_read(arc)
            if local_arc:
                arc = local_arc
                self._jar_extracted_source_map[str(arc)] = display_arc
        ext = arc.suffix.lower()
        if ext not in (".jar", ".war", ".ear", ".zip"):
            return None
        q, q_file, q_base = self._class_diff_jar_query_parts(raw)
        outer_name = arc.name.lower()
        if self._class_diff_jar_name_matches(outer_name, q, q_file, q_base):
            if str(arc) != display_arc:
                self._jar_extracted_source_map[str(arc)] = display_arc
            return arc

        virt: Optional[str] = None
        if ck in self._jar_resolve_virtual_hits:
            virt = self._jar_resolve_virtual_hits[ck]
        else:
            try:
                with zipfile.ZipFile(_fs_path_str(arc), "r") as zf:
                    virt = self._find_matching_jar_virtual_in_zipfile(zf, q, q_file, q_base, "")
                if virt:
                    self._jar_resolve_virtual_hits[ck] = virt
            except Exception as e:
                logger.warning(f"[CRM] Could not scan archive for nested JAR {arc}: {e}")
                virt = None

        if virt:
            blob = self._extract_nested_archive(arc, virt)
            if blob:
                try:
                    fd, tmp_name = tempfile.mkstemp(suffix=".jar", prefix="crm_class_diff_")
                    try:
                        os.write(fd, blob)
                    finally:
                        os.close(fd)
                    p = Path(tmp_name)
                    self._jar_class_diff_temp_paths.append(p)
                    virt_sl = virt.replace("\\", "/")
                    self._jar_extracted_source_map[str(p)] = f"{display_arc}!/{virt_sl}"
                    print(f"[CRM] JAR resolve: extracted nested JAR {virt!r} -> {p}")
                    sys.stdout.flush()
                    return p
                except Exception as e:
                    logger.warning(f"[CRM] Could not write temp JAR for class diff: {e}")
            else:
                logger.warning(f"[CRM] Could not extract nested JAR {virt!r} from {arc}")
            self._jar_resolve_virtual_hits.pop(ck, None)
            virt = None

        # Fallback for archives where Python zip parsing fails (e.g. corrupt extra fields).
        try:
            result = subprocess.run(
                ["jar", "tf", str(arc)],
                capture_output=True,
                text=True,
                timeout=300,
            )
            if result.returncode == 0 and result.stdout:
                rows = [
                    ln.strip().replace("\\", "/").strip("/")
                    for ln in result.stdout.splitlines()
                    if ln.strip()
                ]
                hits: list[tuple[int, int, str]] = []
                for ln in rows:
                    if not ln.lower().endswith(".jar"):
                        continue
                    b = os.path.basename(ln).lower()
                    if not self._class_diff_jar_name_matches(b, q, q_file, q_base):
                        continue
                    score = ln.count("/")
                    hits.append((score, len(ln), ln))
                if hits:
                    hits.sort()
                    listed_member = hits[0][2]
                    tmp_dir = mkdtemp_compare(prefix="crm_class_diff_jarxf_")
                    try:
                        x = subprocess.run(
                            ["jar", "xf", str(arc), listed_member],
                            cwd=tmp_dir,
                            capture_output=True,
                            text=True,
                            timeout=300,
                        )
                        if x.returncode == 0:
                            out_file = Path(tmp_dir) / listed_member
                            if out_file.is_file():
                                blob = out_file.read_bytes()
                                fd, tmp_name = tempfile.mkstemp(suffix=".jar", prefix="crm_class_diff_")
                                try:
                                    os.write(fd, blob)
                                finally:
                                    os.close(fd)
                                p = Path(tmp_name)
                                self._jar_class_diff_temp_paths.append(p)
                                lm_sl = listed_member.replace("\\", "/")
                                self._jar_extracted_source_map[str(p)] = f"{display_arc}!/{lm_sl}"
                                self._jar_resolve_virtual_hits[ck] = listed_member
                                print(f"[CRM] JAR resolve: extracted via jar fallback {listed_member!r} -> {p}")
                                return p
                    finally:
                        try:
                            shutil.rmtree(tmp_dir, ignore_errors=True)
                        except Exception:
                            pass
                nested_archives = [
                    ln for ln in rows if ln.lower().endswith((".ear", ".war", ".zip", ".jar"))
                ]
                if nested_archives:
                    preferred = [m for m in nested_archives if "/sharedlib/" in m.lower()]
                    remaining = [m for m in nested_archives if "/sharedlib/" not in m.lower()]
                    ordered = preferred + remaining
                    for member in ordered[:80]:
                        tmp_dir = mkdtemp_compare(prefix="crm_class_diff_nested_")
                        try:
                            x = subprocess.run(
                                ["jar", "xf", str(arc), member],
                                cwd=tmp_dir,
                                capture_output=True,
                                text=True,
                                timeout=300,
                            )
                            if x.returncode != 0:
                                continue
                            extracted = Path(tmp_dir) / member
                            if not extracted.is_file():
                                continue
                            mem_sl = member.replace("\\", "/")
                            chain = f"{display_arc}!/{mem_sl}"
                            hit = self._find_jar_in_archive_file(
                                extracted, raw, logical_archive_str=chain
                            )
                            if hit:
                                print(
                                    f"[CRM] JAR resolve: extracted nested archive member {member!r} "
                                    f"and resolved query {raw!r}"
                                )
                                return hit
                        except Exception:
                            continue
                        finally:
                            try:
                                shutil.rmtree(tmp_dir, ignore_errors=True)
                            except Exception:
                                pass
        except Exception as e:
            logger.debug(f"[CRM] jar tf/xf fallback failed for {arc}: {e}")

        return None
    
    def _find_jar_by_query(self, folder: Path, query: str, scanned_map: Optional[dict] = None) -> Optional[Path]:
        """Find a .jar matching query: on disk under folder, or nested inside a root .jar/.zip."""
        raw = self._normalize_class_diff_query(query)
        if not raw:
            return None
        # Windows Explorer "compressed folder" path: outer.jar\path\to\Module.jar (not a real FS path)
        shell = self._parse_shell_zip_appended_path(folder)
        if shell:
            ap, inner = shell
            hit = self._extract_jar_via_shell_zip_path(ap, inner, raw)
            if hit:
                return hit
            # Explorer path often does not match zip entry names exactly; scan whole SDK archive
            hit = self._find_jar_in_archive_file(ap, raw)
            if hit:
                return hit
            logger.warning(
                "[CRM] JAR resolve: Explorer-style path %s could not resolve %r "
                "(member extract + nested scan failed on %s)",
                folder,
                raw,
                ap,
            )
            return None
        if not _path_exists(folder):
            return None
        archive_ext = (".jar", ".war", ".ear", ".zip")
        if _path_is_file(folder) and folder.suffix.lower() in archive_ext:
            return self._find_jar_in_archive_file(folder, raw)
        if not _path_is_dir(folder):
            return None
        q, q_file, q_base = self._class_diff_jar_query_parts(raw)
        if isinstance(scanned_map, dict) and scanned_map:
            candidates: list[tuple[int, str, Path]] = []
            for rel, info in scanned_map.items():
                try:
                    rel_s = str(rel).replace("\\", "/")
                    b = os.path.basename(rel_s).lower()
                    if not b.endswith(".jar"):
                        continue
                    if not self._class_diff_jar_name_matches(b, q, q_file, q_base):
                        continue
                    p = info.get("path") if isinstance(info, dict) else None
                    if isinstance(p, Path) and _path_exists(p) and _path_is_file(p):
                        candidates.append((len(b), rel_s.lower(), p))
                except Exception:
                    continue
            if candidates:
                candidates.sort(key=lambda x: (x[0], x[1]))
                return candidates[0][2]
        exact: list[Path] = []
        partial: list[Path] = []
        all_disk_jars: list[Path] = []
        try:
            for root, _, files in os.walk(str(_walk_base_path(folder))):
                for f in files:
                    fl = f.lower()
                    full = Path(root) / f
                    if fl.endswith(archive_ext):
                        all_disk_jars.append(full)
                    if not fl.endswith(".jar"):
                        continue
                    if fl == q_file or fl == q:
                        exact.append(full)
                    elif q_base and q_base in fl:
                        partial.append(full)
                    elif q in fl:
                        partial.append(full)
        except Exception as e:
            logger.warning(f"[CRM] Jar search failed under {folder}: {e}")
            return None
        if exact:
            return sorted(exact, key=lambda p: str(p).lower())[0]
        if partial:
            partial.sort(key=lambda p: (len(p.name), str(p).lower()))
            return partial[0]

        def _probe_priority(p: Path) -> tuple:
            """Try sharedlib and .ear/.war/.zip before scanning every loose .jar (faster resolve)."""
            s = str(p).lower()
            zone = 0 if "sharedlib" in s else 1
            ext = p.suffix.lower()
            if ext == ".ear":
                et = 0
            elif ext in (".war", ".zip"):
                et = 1
            elif ext == ".jar":
                et = 2
            else:
                et = 3
            return (zone, et, len(s), s)

        for jar_path in sorted(all_disk_jars, key=_probe_priority):
            if jar_path.suffix.lower() not in archive_ext:
                continue
            hit = self._find_jar_in_archive_file(jar_path, raw)
            if hit:
                return hit
        return None

    @staticmethod
    def _normalize_class_file_search_query(raw: str) -> str:
        """Normalize user input to a lowercase .class leaf (e.g. createcustomeraccountxb.class)."""
        s = (raw or "").strip().strip('"').strip("'")
        if not s:
            return ""
        s = s.replace("\\", "/").strip("/")
        if "/" in s:
            s = s.rsplit("/", 1)[-1]
        if not s.lower().endswith(".class"):
            s = f"{s}.class"
        return s.lower()

    @staticmethod
    def _jar_basename_from_class_virtual_path(virt_path: str) -> Optional[str]:
        """Innermost .jar basename that contains the class along a virtual !/ path."""
        raw = str(virt_path or "").replace("\\", "/").strip("/")
        if not raw:
            return None
        parts = [p for p in re.split(r"![\\/]", raw) if p.strip()]
        scan = parts[:-1] if parts and parts[-1].lower().endswith(".class") else parts
        for p in reversed(scan):
            bn = os.path.basename(p)
            if bn.lower().endswith(".jar"):
                return bn
        if parts and parts[0].lower().endswith(".jar"):
            return os.path.basename(parts[0])
        return None

    def _jar_label_from_class_hit(self, virt_path: str, container: Path) -> str:
        jar = self._jar_basename_from_class_virtual_path(virt_path)
        if jar:
            return jar
        if container.suffix.lower() == ".jar":
            return container.name
        return container.name

    def _collect_class_search_archive_candidates(self, folder: Path, *, limit: int = 80) -> list[Path]:
        """Ranked archive paths under a build folder for class-to-JAR lookup."""
        if not _path_is_dir(folder):
            return []
        archive_ext = (".jar", ".war", ".ear", ".zip")
        ranked: list[tuple[tuple, Path]] = []
        try:
            for root, _, files in os.walk(str(_walk_base_path(folder))):
                for fn in files:
                    if not fn.lower().endswith(archive_ext):
                        continue
                    full = Path(root) / fn
                    if not _path_exists(full) or not _path_is_file(full):
                        continue
                    fl = fn.lower()
                    pri = (
                        0 if fl.startswith("sdk") else 1,
                        0 if "output" in str(full).lower() else 1,
                        0 if fl.endswith(".jar") else 1,
                        len(str(full)),
                        fl,
                    )
                    ranked.append((pri, full))
        except Exception as e:
            logger.debug(f"[CRM] class search walk failed under {folder}: {e}")
            return []
        ranked.sort(key=lambda x: x[0])
        return [p for _, p in ranked[:limit]]

    def _scan_zipfile_for_class_member(
        self,
        zf: zipfile.ZipFile,
        prefix: str,
        class_leaf: str,
        hits: list[dict],
        container: Path,
        *,
        depth: int = 0,
        max_depth: int = 12,
        stats: Optional[dict] = None,
    ) -> None:
        """Central-directory scan for a .class leaf; recurse into nested archives."""
        if depth >= max_depth:
            return
        st = stats if stats is not None else {}
        nest_exts = _CRM_CLASS_DIFF_NESTED_ARCHIVE_SUFFIXES
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = info.filename.replace("\\", "/").strip("/")
            if not name:
                continue
            nl = name.lower()
            virt = f"{prefix}!/{name}" if prefix else name
            if nl.endswith(class_leaf) or nl.endswith(f"/{class_leaf}"):
                hits.append({"virtual": virt, "container": container})
            if not nl.endswith(nest_exts):
                continue
            st["nested_opened"] = int(st.get("nested_opened", 0)) + 1
            try:
                data = zf.read(info.filename)
            except Exception:
                continue
            try:
                with zipfile.ZipFile(BytesIO(data), "r") as inner:
                    self._scan_zipfile_for_class_member(
                        inner,
                        virt,
                        class_leaf,
                        hits,
                        container,
                        depth=depth + 1,
                        max_depth=max_depth,
                        stats=st,
                    )
            except (zipfile.BadZipFile, OSError, RuntimeError):
                continue

    def _scan_archive_file_for_class(
        self,
        archive_path: Path,
        class_leaf: str,
        hits: list[dict],
        stats: dict,
    ) -> None:
        if not archive_path or not _path_exists(archive_path) or not _path_is_file(archive_path):
            return
        ext = archive_path.suffix.lower()
        if ext not in (".jar", ".war", ".ear", ".zip"):
            return
        scan_path = archive_path
        if self._is_network_path(archive_path):
            local = self._copy_file_to_temp_for_clfy_read(archive_path)
            if local:
                scan_path = local
        stats["archives_scanned"] = int(stats.get("archives_scanned", 0)) + 1
        print(
            f"[CRM] Class lookup: scanning {archive_path.name} for {class_leaf}...",
            flush=True,
        )
        try:
            with zipfile.ZipFile(_fs_path_str(scan_path), "r") as zf:
                self._scan_zipfile_for_class_member(
                    zf, "", class_leaf, hits, archive_path, stats=stats
                )
        except Exception as e:
            logger.debug(f"[CRM] class search failed for {archive_path}: {e}")

    def find_jars_containing_class(
        self,
        root: Path,
        class_query: str,
        *,
        max_results: int = 25,
    ) -> dict:
        """
        Search a build path (new build) for JAR basenames that contain the given .class file.
        Returns jar names suitable for the class-diff JAR text box.
        """
        leaf = self._normalize_class_file_search_query(class_query)
        if not leaf:
            return {
                "ok": False,
                "error": "Enter a class file name (e.g. CreateCustomerAccountXB.class).",
                "query": "",
                "jars": [],
                "suggestions": "",
            }
        hits: list[dict] = []
        stats: dict = {"archives_scanned": 0, "nested_opened": 0}
        shell = self._parse_shell_zip_appended_path(root)
        if shell is not None:
            ap, _ = shell
            if _path_exists(ap) and _path_is_file(ap):
                self._scan_archive_file_for_class(ap, leaf, hits, stats)
        elif _path_is_file(root) and _crm_path_is_archive_file(root):
            self._scan_archive_file_for_class(root, leaf, hits, stats)
        elif _path_is_dir(root):
            for arc in self._collect_class_search_archive_candidates(root):
                if len(hits) >= max_results * 4:
                    break
                self._scan_archive_file_for_class(arc, leaf, hits, stats)
        else:
            return {
                "ok": False,
                "error": f"Build path not found or not accessible: {root}",
                "query": leaf,
                "jars": [],
                "suggestions": "",
            }

        seen_jar: set[str] = set()
        jars_out: list[dict] = []
        for h in hits:
            virt = str(h.get("virtual") or "")
            container = h.get("container")
            if not isinstance(container, Path):
                continue
            jar_name = self._jar_label_from_class_hit(virt, container)
            if not jar_name or jar_name.lower() in seen_jar:
                continue
            seen_jar.add(jar_name.lower())
            member = virt.replace("\\", "/")
            if "!/" in member:
                member = member.rsplit("!/", 1)[-1]
            jars_out.append(
                {
                    "jar": jar_name,
                    "container": str(container),
                    "member": member,
                }
            )
            if len(jars_out) >= max_results:
                break

        suggestions = ", ".join(j["jar"] for j in jars_out)
        if jars_out:
            print(
                f"[CRM] Class lookup {leaf!r}: {len(jars_out)} JAR(s) — {suggestions}",
                flush=True,
            )
        else:
            print(
                f"[CRM] Class lookup {leaf!r}: not found "
                f"({stats.get('archives_scanned', 0)} archive(s) scanned)",
                flush=True,
            )
        return {
            "ok": bool(jars_out),
            "error": None if jars_out else f"No JAR containing {leaf} was found in the build path.",
            "query": leaf,
            "jars": jars_out,
            "suggestions": suggestions,
            "archives_scanned": int(stats.get("archives_scanned", 0)),
            "nested_opened": int(stats.get("nested_opened", 0)),
        }
    
    def _build_one_jar_class_diff(
        self,
        query: str,
        root1: Path,
        root2: Path,
        files1_map: Optional[dict] = None,
        files2_map: Optional[dict] = None,
    ) -> dict:
        """Compare .class members for one JAR query; clears temp extract files in finally."""
        try:
            print(f"[CRM] Building JAR class diff for query: {query!r}")
            jar_old = self._find_jar_by_query(root1, query, files1_map)
            jar_new = self._find_jar_by_query(root2, query, files2_map)
            if not jar_old:
                jar_old = self._resolve_jar_from_clfy_agent(root1, query)
            if not jar_new:
                jar_new = self._resolve_jar_from_clfy_agent(root2, query)
            if not jar_old or not jar_new:
                out_m: dict = {
                    "query": query,
                    "error": (
                        "Could not find matching .jar in one or both builds "
                        "(searched SDK tree first, then ClfyAgent .zip/.ear; includes nested .jar/.war/.ear/.zip)"
                    ),
                    "jar_old": str(jar_old) if jar_old else None,
                    "jar_new": str(jar_new) if jar_new else None,
                    "build_path_old": str(self.folder1_path),
                    "build_path_new": str(self.folder2_path),
                    "jar_old_source": self._logical_jar_source_path(jar_old) if jar_old else "",
                    "jar_new_source": self._logical_jar_source_path(jar_new) if jar_new else "",
                    "only_in_old": [],
                    "only_in_new": [],
                    "modified": [],
                    "modified_detail": [],
                    "java_diff_summary": None,
                    "java_diffs": [],
                }
                self._enrich_jar_entry_path_metadata(
                    out_m, root1, root2, query, files1_map, files2_map
                )
                return out_m
            # Deep nested listing walks every embedded JAR and can take hours on stacks like Tika
            # (thousands of nested archives). Default to shallow: top-level zip entries only.
            class_diff_deep = os.environ.get("CRM_CLASS_DIFF_DEEP_NESTED", "").strip().lower() in (
                "1",
                "true",
                "yes",
                "y",
            )
            print(
                "[CRM] JAR class diff: listing members ("
                f"{'deep nested — slow on fat JARs' if class_diff_deep else 'shallow — recommended'}). "
                "Set env CRM_CLASS_DIFF_DEEP_NESTED=1 if you need classes inside nested archives only."
            )
            sys.stdout.flush()
            try:
                names1, meta1 = self._get_archive_members(jar_old, deep=class_diff_deep)
                names2, meta2 = self._get_archive_members(jar_new, deep=class_diff_deep)
            except Exception as e:
                out_e: dict = {
                    "query": query,
                    "error": str(e),
                    "jar_old": str(jar_old),
                    "jar_new": str(jar_new),
                    "build_path_old": str(self.folder1_path),
                    "build_path_new": str(self.folder2_path),
                    "jar_old_source": self._logical_jar_source_path(jar_old),
                    "jar_new_source": self._logical_jar_source_path(jar_new),
                    "only_in_old": [],
                    "only_in_new": [],
                    "modified": [],
                    "modified_detail": [],
                    "java_diff_summary": None,
                    "java_diffs": [],
                }
                self._enrich_jar_entry_path_metadata(
                    out_e, root1, root2, query, files1_map, files2_map
                )
                return out_e

            def class_keys(names: set) -> set:
                return {n for n in names if n.lower().endswith(".class")}

            c1, c2 = class_keys(names1), class_keys(names2)
            only_old = sorted(c1 - c2)
            only_new = sorted(c2 - c1)
            modified_detail = []
            for p in sorted(c1 & c2):
                m1, m2 = meta1.get(p), meta2.get(p)
                if m1 == m2:
                    continue
                s1, crc1 = m1 if m1 is not None else (None, None)
                s2, crc2 = m2 if m2 is not None else (None, None)
                modified_detail.append(
                    {
                        "path": p,
                        "size_old": s1,
                        "size_new": s2,
                        "crc_old": crc1,
                        "crc_new": crc2,
                    }
                )
            modified = [d["path"] for d in modified_detail]
            decompiler_type, decompiler_path = self._find_preferred_decompiler()
            java_diffs: list[dict] = []
            java_err_count = 0
            if decompiler_type and modified_detail:
                for d in modified_detail[:_CRM_JAVA_DIFF_MAX_CLASSES]:
                    class_path = str(d.get("path") or "")
                    b1 = self._extract_member_bytes_from_archive(jar_old, class_path)
                    b2 = self._extract_member_bytes_from_archive(jar_new, class_path)
                    if not b1 or not b2:
                        java_err_count += 1
                        continue
                    ok1, src1, _ = _decompile_class_file(
                        b1, class_path, decompiler_type=decompiler_type, decompiler_path=decompiler_path
                    )
                    ok2, src2, _ = _decompile_class_file(
                        b2, class_path, decompiler_type=decompiler_type, decompiler_path=decompiler_path
                    )
                    if not (ok1 and ok2):
                        java_err_count += 1
                        continue
                    cmp = _compare_decompiled_sources(src1, src2, class_path, diff_style="p4")
                    if cmp.get("has_diff"):
                        java_diffs.append(
                            {
                                "path": class_path,
                                "changed_lines": int(cmp.get("changed_lines") or 0),
                                "added_lines": int(cmp.get("added_lines") or 0),
                                "removed_lines": int(cmp.get("removed_lines") or 0),
                                "diff_html": cmp.get("diff_html") or "",
                                "decompiler": decompiler_type,
                            }
                        )
            java_summary = {
                "requested": len(modified_detail),
                "decompiler": decompiler_type or "none",
                "decompiler_path": decompiler_path or "",
                "processed": min(len(modified_detail), _CRM_JAVA_DIFF_MAX_CLASSES),
                "with_java_diff": len(java_diffs),
                "errors": java_err_count,
                "truncated": max(0, len(modified_detail) - _CRM_JAVA_DIFF_MAX_CLASSES),
            }
            print(
                f"[CRM] JAR class diff ({query}): {len(only_old)} only old, {len(only_new)} only new, {len(modified)} modified"
            )
            out_ok: dict = {
                "query": query,
                "error": None,
                "jar_old": str(jar_old),
                "jar_new": str(jar_new),
                "build_path_old": str(self.folder1_path),
                "build_path_new": str(self.folder2_path),
                "jar_old_source": self._logical_jar_source_path(jar_old),
                "jar_new_source": self._logical_jar_source_path(jar_new),
                "only_in_old": only_old,
                "only_in_new": only_new,
                "modified": modified,
                "modified_detail": modified_detail,
                "total_old": len(c1),
                "total_new": len(c2),
                "java_diff_summary": java_summary,
                "java_diffs": java_diffs,
            }
            self._enrich_jar_entry_path_metadata(
                out_ok, root1, root2, query, files1_map, files2_map
            )
            return out_ok
        finally:
            for p in self._jar_class_diff_temp_paths:
                try:
                    if p.is_file():
                        p.unlink()
                except OSError:
                    pass
                self._jar_extracted_source_map.pop(str(p), None)
            self._jar_class_diff_temp_paths.clear()

    def _build_jar_class_diff(
        self,
        folder1: Optional[Path] = None,
        folder2: Optional[Path] = None,
        files1_map: Optional[dict] = None,
        files2_map: Optional[dict] = None,
    ) -> None:
        """Populate self.jar_class_diff_list for each comma-separated JAR name."""
        try:
            wildcard_match_limit = int(
                (os.environ.get("CRM_CLASS_DIFF_WILDCARD_LIMIT") or "200").strip()
            )
        except (TypeError, ValueError):
            wildcard_match_limit = 200
        wildcard_match_limit = max(30, min(wildcard_match_limit, 500))

        def _is_wildcard_query(q: str) -> bool:
            return "*" in (q or "") or "?" in (q or "")

        def _pri_merge(pri: dict[str, int], bn: str, p: int) -> None:
            pri[bn] = min(pri.get(bn, 9), p)

        def _collect_disk_jar_names(
            root: Path, raw_query: str, scanned_map: Optional[dict]
        ) -> tuple[set[str], dict[str, int]]:
            out: set[str] = set()
            pri: dict[str, int] = {}
            collect_root = root
            if not _path_exists(root):
                sh = self._parse_shell_zip_appended_path(root)
                if sh and _path_exists(sh[0]) and _path_is_file(sh[0]):
                    collect_root = Path(sh[0])
                else:
                    return out, pri
            q_norm = self._normalize_class_diff_query(raw_query)
            q, q_file, q_base = self._class_diff_jar_query_parts(q_norm)
            wc = _is_wildcard_query(q_norm)
            if isinstance(scanned_map, dict) and scanned_map:
                for rel in scanned_map.keys():
                    rel_sl = str(rel).replace("\\", "/").lower()
                    b = os.path.basename(rel_sl).lower()
                    if b.endswith(".jar") and self._class_diff_jar_name_matches(b, q, q_file, q_base):
                        bn = os.path.basename(str(rel))
                        out.add(bn)
                        _pri_merge(pri, bn, 0 if "sharedlib" in rel_sl else 1)
            if _path_is_dir(collect_root):
                for walk_root, _, files in os.walk(str(_walk_base_path(collect_root))):
                    wr = str(walk_root).lower()
                    for f in files:
                        fl = f.lower()
                        if not fl.endswith(".jar"):
                            continue
                        if self._class_diff_jar_name_matches(fl, q, q_file, q_base):
                            out.add(f)
                            _pri_merge(pri, f, 0 if "sharedlib" in wr else 1)
            elif _path_is_file(collect_root) and str(collect_root).lower().endswith(
                (".jar", ".war", ".ear", ".zip")
            ):
                b = collect_root.name.lower()
                if self._class_diff_jar_name_matches(b, q, q_file, q_base):
                    out.add(collect_root.name)
                    _pri_merge(pri, collect_root.name, 1)
                try:
                    member_map = self._scan_archive_contents(collect_root)
                    for rel in member_map.keys():
                        rel_s = str(rel).replace("\\", "/")
                        rel_sl = rel_s.lower()
                        base = os.path.basename(rel_s.split("!/")[-1]).lower()
                        if not base.endswith(".jar"):
                            continue
                        if self._class_diff_jar_name_matches(base, q, q_file, q_base):
                            bn = os.path.basename(base)
                            out.add(bn)
                            _pri_merge(pri, bn, 0 if "sharedlib" in rel_sl else 1)
                except Exception:
                    pass
                if not out:
                    try:
                        proc = subprocess.run(
                            ["jar", "tf", str(collect_root)],
                            capture_output=True,
                            text=True,
                            timeout=300,
                        )
                        if proc.returncode == 0 and proc.stdout:
                            for ln in proc.stdout.splitlines():
                                rel_s = (ln or "").strip().replace("\\", "/")
                                if not rel_s:
                                    continue
                                base = os.path.basename(rel_s).lower()
                                if not base.endswith(".jar"):
                                    continue
                                if self._class_diff_jar_name_matches(base, q, q_file, q_base):
                                    bn = os.path.basename(base)
                                    out.add(bn)
                                    _pri_merge(pri, bn, 0 if "sharedlib" in rel_s.lower() else 1)
                    except Exception:
                        pass
            if wc:
                try:
                    deep_out, deep_pri = self._deep_collect_matching_jar_basenames_under_root(
                        collect_root, q, q_file, q_base
                    )
                    out |= deep_out
                    for k, v in deep_pri.items():
                        _pri_merge(pri, k, v)
                except Exception as e:
                    logger.debug(f"[CRM] wildcard deep JAR collect failed for {collect_root}: {e}")
            return out, pri

        self.jar_class_diff_list = []
        queries = self._parse_class_diff_jar_names(self.class_diff_jar_name or "")
        if not queries:
            return
        root1 = folder1 if folder1 is not None else self.folder1_path
        root2 = folder2 if folder2 is not None else self.folder2_path
        for query in queries:
            if _is_wildcard_query(query):
                s1, p1 = _collect_disk_jar_names(root1, query, files1_map)
                s2, p2 = _collect_disk_jar_names(root2, query, files2_map)
                merged = s1 | s2

                def _combined_pri(bn: str) -> int:
                    return min(p1.get(bn, 9), p2.get(bn, 9))

                matched = sorted(merged, key=lambda x: (_combined_pri(x), x.lower()))
                if len(matched) > wildcard_match_limit:
                    matched = matched[:wildcard_match_limit]
                if not matched:
                    ewild: dict = {
                        "query": query,
                        "input_query": query,
                        "error": "No JARs matched wildcard input",
                        "jar_old": None,
                        "jar_new": None,
                        "build_path_old": str(self.folder1_path),
                        "build_path_new": str(self.folder2_path),
                        "jar_old_source": "",
                        "jar_new_source": "",
                        "only_in_old": [],
                        "only_in_new": [],
                        "modified": [],
                        "modified_detail": [],
                        "java_diff_summary": None,
                        "java_diffs": [],
                    }
                    self._enrich_jar_entry_path_metadata(
                        ewild, root1, root2, query, files1_map, files2_map
                    )
                    self.jar_class_diff_list.append(ewild)
                    continue
                for resolved in matched:
                    entry = self._build_one_jar_class_diff(
                        resolved, root1, root2, files1_map=files1_map, files2_map=files2_map
                    )
                    entry["input_query"] = query
                    self.jar_class_diff_list.append(entry)
                continue
            entry = self._build_one_jar_class_diff(
                query, root1, root2, files1_map=files1_map, files2_map=files2_map
            )
            entry["input_query"] = query
            self.jar_class_diff_list.append(entry)

    def _build_one_jar_manifest_diff(
        self,
        query: str,
        root1: Path,
        root2: Path,
        files1_map: Optional[dict] = None,
        files2_map: Optional[dict] = None,
    ) -> dict:
        jar_old = self._find_jar_by_query(root1, query, scanned_map=files1_map)
        jar_new = self._find_jar_by_query(root2, query, scanned_map=files2_map)
        if not jar_old:
            jar_old = self._resolve_jar_from_clfy_agent(root1, query)
        if not jar_new:
            jar_new = self._resolve_jar_from_clfy_agent(root2, query)
        if not jar_old and not jar_new:
            out: dict = {
                "query": query,
                "error": "JAR not found in either build",
                "jar_old": None,
                "jar_new": None,
                "build_path_old": str(self.folder1_path),
                "build_path_new": str(self.folder2_path),
                "jar_old_source": "",
                "jar_new_source": "",
                "manifest_impl_version_old": "",
                "manifest_impl_version_new": "",
                "manifest_impl_version_error_old": "JAR not found",
                "manifest_impl_version_error_new": "JAR not found",
                "manifest_impl_version_diff": False,
            }
            self._enrich_jar_entry_path_metadata(
                out, root1, root2, query, files1_map, files2_map
            )
            return out
        mvo, evo = ("", "JAR not found") if not jar_old else self._manifest_implementation_version_from_jar(jar_old)
        mvn, evn = ("", "JAR not found") if not jar_new else self._manifest_implementation_version_from_jar(jar_new)
        out2: dict = {
            "query": query,
            "error": None,
            "jar_old": str(jar_old) if jar_old else None,
            "jar_new": str(jar_new) if jar_new else None,
            "build_path_old": str(self.folder1_path),
            "build_path_new": str(self.folder2_path),
            "jar_old_source": self._logical_jar_source_path(jar_old) if jar_old else "",
            "jar_new_source": self._logical_jar_source_path(jar_new) if jar_new else "",
            "manifest_impl_version_old": mvo,
            "manifest_impl_version_new": mvn,
            "manifest_impl_version_error_old": evo,
            "manifest_impl_version_error_new": evn,
            "manifest_impl_version_diff": (mvo or "") != (mvn or ""),
        }
        self._enrich_jar_entry_path_metadata(
            out2, root1, root2, query, files1_map, files2_map
        )
        return out2

    def _build_jar_manifest_diff(
        self,
        folder1: Optional[Path] = None,
        folder2: Optional[Path] = None,
        files1_map: Optional[dict] = None,
        files2_map: Optional[dict] = None,
    ) -> None:
        try:
            wildcard_match_limit = int(
                (os.environ.get("CRM_MANIFEST_WILDCARD_LIMIT") or "200").strip()
            )
        except (TypeError, ValueError):
            wildcard_match_limit = 200
        wildcard_match_limit = max(30, min(wildcard_match_limit, 500))

        def _is_wildcard_query(q: str) -> bool:
            return "*" in (q or "") or "?" in (q or "")

        def _pri_merge_m(pri: dict[str, int], bn: str, p: int) -> None:
            pri[bn] = min(pri.get(bn, 9), p)

        def _collect_disk_jar_names(
            root: Path, raw_query: str, scanned_map: Optional[dict]
        ) -> tuple[set[str], dict[str, int]]:
            out: set[str] = set()
            pri: dict[str, int] = {}
            collect_root = root
            if not _path_exists(root):
                sh = self._parse_shell_zip_appended_path(root)
                if sh and _path_exists(sh[0]) and _path_is_file(sh[0]):
                    collect_root = Path(sh[0])
                else:
                    return out, pri
            q_norm = self._normalize_class_diff_query(raw_query)
            q, q_file, q_base = self._class_diff_jar_query_parts(q_norm)
            wc = _is_wildcard_query(q_norm)
            if isinstance(scanned_map, dict) and scanned_map:
                for rel in scanned_map.keys():
                    rel_sl = str(rel).replace("\\", "/").lower()
                    b = os.path.basename(rel_sl).lower()
                    if b.endswith(".jar") and self._class_diff_jar_name_matches(b, q, q_file, q_base):
                        bn = os.path.basename(str(rel))
                        out.add(bn)
                        _pri_merge_m(pri, bn, 0 if "sharedlib" in rel_sl else 1)
            if _path_is_dir(collect_root):
                for walk_root, _, files in os.walk(str(_walk_base_path(collect_root))):
                    wr = str(walk_root).lower()
                    for f in files:
                        fl = f.lower()
                        if not fl.endswith(".jar"):
                            continue
                        if self._class_diff_jar_name_matches(fl, q, q_file, q_base):
                            out.add(f)
                            _pri_merge_m(pri, f, 0 if "sharedlib" in wr else 1)
            elif _path_is_file(collect_root) and str(collect_root).lower().endswith(
                (".jar", ".war", ".ear", ".zip")
            ):
                b = collect_root.name.lower()
                if self._class_diff_jar_name_matches(b, q, q_file, q_base):
                    out.add(collect_root.name)
                    _pri_merge_m(pri, collect_root.name, 1)
                try:
                    member_map = self._scan_archive_contents(collect_root)
                    for rel in member_map.keys():
                        rel_s = str(rel).replace("\\", "/")
                        rel_sl = rel_s.lower()
                        base = os.path.basename(rel_s.split("!/")[-1]).lower()
                        if not base.endswith(".jar"):
                            continue
                        if self._class_diff_jar_name_matches(base, q, q_file, q_base):
                            bn = os.path.basename(base)
                            out.add(bn)
                            _pri_merge_m(pri, bn, 0 if "sharedlib" in rel_sl else 1)
                except Exception:
                    pass
                if not out:
                    try:
                        proc = subprocess.run(
                            ["jar", "tf", str(collect_root)],
                            capture_output=True,
                            text=True,
                            timeout=300,
                        )
                        if proc.returncode == 0 and proc.stdout:
                            for ln in proc.stdout.splitlines():
                                rel_s = (ln or "").strip().replace("\\", "/")
                                if not rel_s:
                                    continue
                                base = os.path.basename(rel_s).lower()
                                if not base.endswith(".jar"):
                                    continue
                                if self._class_diff_jar_name_matches(base, q, q_file, q_base):
                                    bn = os.path.basename(base)
                                    out.add(bn)
                                    _pri_merge_m(pri, bn, 0 if "sharedlib" in rel_s.lower() else 1)
                    except Exception:
                        pass
            if wc:
                try:
                    deep_out, deep_pri = self._deep_collect_matching_jar_basenames_under_root(
                        collect_root, q, q_file, q_base
                    )
                    out |= deep_out
                    for k, v in deep_pri.items():
                        _pri_merge_m(pri, k, v)
                except Exception as e:
                    logger.debug(f"[CRM] manifest wildcard deep JAR collect failed for {collect_root}: {e}")
            return out, pri

        self.jar_manifest_diff_list = []
        queries = self._parse_class_diff_jar_names(self.manifest_diff_jar_name or "")
        if not queries:
            return
        root1 = folder1 if folder1 is not None else self.folder1_path
        root2 = folder2 if folder2 is not None else self.folder2_path
        for query in queries:
            if _is_wildcard_query(query):
                s1, p1 = _collect_disk_jar_names(root1, query, files1_map)
                s2, p2 = _collect_disk_jar_names(root2, query, files2_map)
                merged = s1 | s2

                def _combined_pri_m(bn: str) -> int:
                    return min(p1.get(bn, 9), p2.get(bn, 9))

                matched = sorted(merged, key=lambda x: (_combined_pri_m(x), x.lower()))
                if len(matched) > wildcard_match_limit:
                    matched = matched[:wildcard_match_limit]
                if not matched:
                    # Try direct wildcard resolution via archive traversal (sharedlib EAR/WAR/etc).
                    entry = self._build_one_jar_manifest_diff(
                        query, root1, root2, files1_map=files1_map, files2_map=files2_map
                    )
                    entry["input_query"] = query
                    if entry.get("error"):
                        entry["error"] = "No JARs matched wildcard input"
                    self.jar_manifest_diff_list.append(entry)
                    continue
                for resolved in matched:
                    entry = self._build_one_jar_manifest_diff(
                        resolved, root1, root2, files1_map=files1_map, files2_map=files2_map
                    )
                    entry["input_query"] = query
                    self.jar_manifest_diff_list.append(entry)
                continue
            entry = self._build_one_jar_manifest_diff(
                query, root1, root2, files1_map=files1_map, files2_map=files2_map
            )
            entry["input_query"] = query
            self.jar_manifest_diff_list.append(entry)
    
    def _find_member_virtual_by_basename_in_zip(
        self, zf: zipfile.ZipFile, basename_lower: str, prefix: str
    ) -> Optional[str]:
        """DFS: first zip entry whose basename matches (e.g. clfyagent.zip), using !/ for nesting."""
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = info.filename.replace("\\", "/").strip("/")
            if os.path.basename(name).lower() == basename_lower:
                return f"{prefix}!/{name}" if prefix else name
            if name.lower().endswith((".jar", ".war", ".ear", ".zip")):
                try:
                    data = zf.read(info.filename)
                except Exception:
                    continue
                full_key = f"{prefix}!/{name}" if prefix else name
                hit = self._find_basename_inside_archive_bytes(data, basename_lower, full_key)
                if hit:
                    return hit
        return None

    def _find_clfy_agent_bundle_virtual_in_zip(
        self, zf: zipfile.ZipFile, prefix: str, peer_hint: Optional[str] = None
    ) -> Optional[str]:
        """
        Find ClfyAgent.zip or ClfyAgent.ear via shallow scan (no nested .jar reads).
        When multiple copies exist (e.g. lib/ vs sharedlib/), rank by peer hint and lib preference.
        """
        candidates: list[str] = []
        self._collect_clfy_virt_paths_shallow_from_zip(zf, prefix, candidates)
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]
        ranked = self._pick_basename_hits(candidates, peer_hint or "", peer_hint)
        if len(ranked) > 1:
            print(
                f"[CRM] ClfyAgent: {len(ranked)} bundle path(s) in archive; "
                f"using {ranked[0]!r}"
                + (f" (peer hint {peer_hint!r})" if peer_hint else "")
            )
        return ranked[0]

    def _find_basename_inside_archive_bytes(
        self, data: bytes, basename_lower: str, prefix: str
    ) -> Optional[str]:
        """Descend into nested archive bytes; retry from a temp file if BytesIO open fails (some JDK zips)."""
        if not data:
            return None
        try:
            with zipfile.ZipFile(BytesIO(data), "r") as inner:
                return self._find_member_virtual_by_basename_in_zip(inner, basename_lower, prefix)
        except (zipfile.BadZipFile, OSError, RuntimeError):
            pass
        tmp: Optional[str] = None
        try:
            fd, tmp = tempfile.mkstemp(suffix=".zip", prefix="crm_nested_zipdisk_")
            try:
                os.write(fd, data)
            finally:
                os.close(fd)
            with zipfile.ZipFile(tmp, "r") as inner:
                return self._find_member_virtual_by_basename_in_zip(inner, basename_lower, prefix)
        except Exception:
            return None
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    def _zip_read_member_from_nested_bytes(self, blob: bytes, part: str) -> Optional[bytes]:
        """Read one member from nested archive bytes; temp-file retry if BytesIO ZipFile fails."""
        if not blob:
            return None
        try:
            with zipfile.ZipFile(BytesIO(blob), "r") as inner_zf:
                return self._read_member_bytes_from_zip(inner_zf, part)
        except (zipfile.BadZipFile, OSError, RuntimeError):
            pass
        tmp: Optional[str] = None
        try:
            fd, tmp = tempfile.mkstemp(suffix=".zip", prefix="crm_chain_")
            try:
                os.write(fd, blob)
            finally:
                os.close(fd)
            with zipfile.ZipFile(tmp, "r") as inner_zf:
                return self._read_member_bytes_from_zip(inner_zf, part)
        except Exception:
            return None
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    def _extract_member_bytes_jar_tf_xf(
        self,
        outer_path: Path,
        basename_lower: str,
        peer_hint: Optional[str] = None,
    ) -> Optional[bytes]:
        """When ZipFile cannot read the outer archive, locate a member by basename via `jar tf` + `jar xf`."""
        b = (basename_lower or "").strip().lower()
        if not b:
            return None
        outer = outer_path
        local_outer = self._copy_file_to_temp_for_clfy_read(outer_path)
        if local_outer:
            outer = local_outer
        tmp_dir: Optional[str] = None
        try:
            tmp_dir = mkdtemp_compare(prefix="crm_jar_tf_extract_")
            result = subprocess.run(
                [_jar_tool_cmd(), "tf", _fs_path_str(outer)],
                capture_output=True,
                text=True,
                timeout=300,
            )
            if result.returncode != 0 or not result.stdout:
                return None
            rows = [
                ln.strip().replace("\\", "/").strip("/")
                for ln in result.stdout.splitlines()
                if ln.strip()
            ]
            hits = [ln for ln in rows if os.path.basename(ln).lower() == b]
            if not hits:
                return None
            if len(hits) > 1:
                hits = self._pick_basename_hits(hits, peer_hint or b, peer_hint)
            listed_member = hits[0]
            x = subprocess.run(
                [_jar_tool_cmd(), "xf", _fs_path_str(outer), listed_member],
                cwd=tmp_dir,
                capture_output=True,
                text=True,
                timeout=300,
            )
            if x.returncode != 0:
                return None
            segs = [p for p in listed_member.replace("\\", "/").split("/") if p]
            out_file = Path(tmp_dir).joinpath(*segs) if segs else Path(tmp_dir)
            if not out_file.is_file():
                return None
            return out_file.read_bytes()
        except Exception as e:
            logger.debug(f"[CRM] jar tf/xf basename extract failed for {basename_lower} from {outer_path}: {e}")
            return None
        finally:
            if tmp_dir:
                try:
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                except Exception:
                    pass
    
    def _synthetic_clfy_agent_info(
        self, root: Path, peer_hint: Optional[str] = None
    ) -> Optional[dict]:
        """Locate ClfyAgent.zip or ClfyAgent.ear when not in files1/files2 (e.g. fast scan / shallow nesting)."""
        if not _path_exists(root):
            return None
        if _path_is_file(root) and _crm_path_is_archive_file(root):
            virt: Optional[str] = None
            try:
                resolved = self._resolve_outer_archive_for_zip(root) or root
                paths = self._list_clfy_paths_from_outer_archive(resolved)
                if not paths:
                    return None
                virt = self._pick_basename_hits(paths, peer_hint or "", peer_hint)[0]
            except Exception as e:
                logger.warning(
                    f"[CRM] Could not scan {root} for ClfyAgent bundle ({CLFY_AGENT_BUNDLE_REPORT_LABEL}): {e}"
                )
                return None
            if not virt:
                return None
            return {
                "path": resolved,
                "is_nested": True,
                "archive_member": virt,
                "rel_key": virt,
            }
        if _path_is_dir(root):
            found = self._find_sdk_archive_with_clfy_in_folder(root, peer_hint=peer_hint)
            if found:
                return found
            walk_base = _walk_base_path(root)

            def _clfy_bundle_walk_priority(rel_s: str, p: Path) -> tuple:
                """Prefer sharedlib and .ear/.zip like _find_jar_by_query archive ordering (stable across runs)."""
                s = rel_s.lower()
                zone = 0 if "sharedlib" in s else 1
                ext = p.suffix.lower()
                et = 0 if ext == ".ear" else 1 if ext in (".zip", ".war") else 2
                return (zone, et, len(s), s)

            candidates: list[tuple[tuple, Path, str]] = []
            try:
                for r, _, fnames in os.walk(str(walk_base)):
                    for fn in fnames:
                        if fn.lower() not in _CLFY_AGENT_BUNDLE_BASENAMES_LOWER:
                            continue
                        full = Path(r) / fn
                        try:
                            rel = str(full.relative_to(walk_base)).replace("\\", "/")
                        except ValueError:
                            rel = fn
                        candidates.append((_clfy_bundle_walk_priority(rel, full), full, rel))
            except Exception as e:
                logger.warning(
                    f"[CRM] Walk failed looking for ClfyAgent .zip/.ear under {root}: {e}"
                )
                return None
            if not candidates:
                return None
            candidates.sort(key=lambda x: x[0])
            _, full, rel = candidates[0]
            return {
                "path": full,
                "is_nested": False,
                "archive_member": rel,
                "rel_key": rel,
            }
        return None
    
    def _clfyagent_keys_in_files(self, files: dict) -> list[str]:
        return [
            k
            for k in files
            if os.path.basename(k.replace("\\", "/")).lower() in _CLFY_AGENT_BUNDLE_BASENAMES_LOWER
        ]
    
    @staticmethod
    def _pair_clfyagent_rel_keys(keys1: list[str], keys2: list[str]) -> tuple[Optional[str], Optional[str]]:
        if not keys1 or not keys2:
            return None, None
        m = {k.lower(): k for k in keys2}
        for a in sorted(keys1, key=lambda x: (len(x), x.lower())):
            al = a.lower()
            if al in m:
                return a, m[al]
        # Same directory: pair ClfyAgent.zip with ClfyAgent.ear when only one extension exists per build
        def parent_norm(k: str) -> str:
            return os.path.dirname(k.replace("\\", "/").lower()).rstrip("/")

        def is_clfy_bundle_name(name: str) -> bool:
            b = os.path.basename(name.replace("\\", "/")).lower()
            return b in _CLFY_AGENT_BUNDLE_BASENAMES_LOWER

        for a in sorted(keys1, key=lambda x: (len(x), x.lower())):
            if not is_clfy_bundle_name(a):
                continue
            pa, ext_a = parent_norm(a), os.path.basename(a.replace("\\", "/")).lower()
            best_b: Optional[str] = None
            best_rank: tuple[int, int] = (99, 99)
            for b in keys2:
                if not is_clfy_bundle_name(b):
                    continue
                if parent_norm(b) != pa:
                    continue
                ext_b = os.path.basename(b.replace("\\", "/")).lower()
                # Prefer matching extension (.ear/.ear), else any clfyagent pair in same folder
                rank = (0 if ext_a == ext_b else 1, len(b))
                if rank < best_rank:
                    best_rank = rank
                    best_b = b
            if best_b:
                return a, best_b
        return sorted(keys1, key=lambda x: (len(x), x.lower()))[0], sorted(keys2, key=lambda x: (len(x), x.lower()))[0]
    
    def _file_entry_to_clfy_info(self, files: dict, rel: str) -> dict:
        inf = dict(files[rel])
        inf["rel_key"] = rel
        inf.setdefault("is_nested", False)
        if not (inf.get("is_nested") and inf.get("archive_member")):
            inf.setdefault("archive_member", rel.replace("\\", "/"))
        return inf

    def _clfy_info_from_override_path(self, raw_path: str, tag: str = "override") -> Optional[dict]:
        """
        Build ClfyAgent info from explicit override path.
        Supports:
        - direct file path to ClfyAgent.zip or ClfyAgent.ear
        - Explorer-style path inside archive: outer.jar\\...\\ClfyAgent.zip|.ear
        """
        p_raw = (raw_path or "").strip().strip('"').strip("'")
        if not p_raw:
            return None
        p = Path(p_raw)
        shell = self._parse_shell_zip_appended_path(p)
        if shell is not None:
            outer, inner = shell
            inner_norm = inner.replace("\\", "/").strip("/")
            if not inner_norm:
                return None
            if os.path.basename(inner_norm).lower() not in _CLFY_AGENT_BUNDLE_BASENAMES_LOWER:
                # If caller passed a broader path inside SDK archive, discover ClfyAgent .zip/.ear.
                try:
                    with zipfile.ZipFile(_fs_path_str(outer), "r") as zf:
                        virt = self._find_clfy_agent_bundle_virtual_in_zip(zf, "")
                except Exception:
                    virt = None
                if not virt:
                    return None
                inner_norm = virt
            return {
                "path": outer,
                "is_nested": True,
                "archive_member": inner_norm,
                "rel_key": f"{tag}:{inner_norm}",
            }
        if _path_exists(p) and _path_is_file(p):
            return {
                "path": p,
                "is_nested": False,
                "archive_member": p.name,
                "rel_key": f"{tag}:{p.name}",
            }
        return None
    
    @staticmethod
    def _clfy_bundle_parent_key(virtual_path: str) -> str:
        """Parent directory key for pairing ClfyAgent bundles across builds (lib vs sharedlib)."""
        s = str(virtual_path or "").replace("\\", "/").strip("/").lower()
        if "!/" in s:
            s = s.split("!/", 1)[-1]
        return os.path.dirname(s).rstrip("/")

    def _clfy_list_bundle_virtual_paths(self, outer: Path) -> list[str]:
        """All ClfyAgent.zip / .ear virtual paths inside an outer SDK archive (shallow scan)."""
        if not outer or not _path_exists(outer) or not _path_is_file(outer):
            return []
        return self._list_clfy_paths_from_outer_archive(outer)

    @staticmethod
    def _clfy_info_for_virtual_member(outer: Path, member: str, tag: str = "") -> dict:
        member = str(member or "").replace("\\", "/").strip("/")
        return {
            "path": outer,
            "is_nested": True,
            "archive_member": member,
            "rel_key": member if not tag else f"{tag}:{member}",
        }

    def _pair_clfy_bundle_virtual_paths(
        self, paths1: list[str], paths2: list[str]
    ) -> tuple[list[tuple[str, str]], list[str], list[str]]:
        """Pair bundles by exact virtual path, then same parent directory; leave unmatched for solo compare."""
        if not paths1 and not paths2:
            return [], [], []
        map2_exact = {p.lower(): p for p in paths2}
        used2: set[str] = set()
        paired: list[tuple[str, str]] = []
        only1: list[str] = []
        for p1 in paths1:
            hit = map2_exact.get(p1.lower())
            if hit and hit.lower() not in used2:
                paired.append((p1, hit))
                used2.add(hit.lower())
            else:
                only1.append(p1)
        remaining2 = [p for p in paths2 if p.lower() not in used2]
        by_parent2: dict[str, list[str]] = {}
        for p in remaining2:
            by_parent2.setdefault(self._clfy_bundle_parent_key(p), []).append(p)
        still_only1: list[str] = []
        for p1 in only1:
            pk = self._clfy_bundle_parent_key(p1)
            cands = by_parent2.get(pk) or []
            if len(cands) == 1:
                paired.append((p1, cands[0]))
                used2.add(cands[0].lower())
                by_parent2[pk] = []
            elif len(cands) > 1:
                best = self._pick_basename_hits(cands, p1, p1)[0]
                paired.append((p1, best))
                used2.add(best.lower())
                by_parent2[pk] = [c for c in cands if c.lower() != best.lower()]
            else:
                still_only1.append(p1)
        only2 = [p for p in paths2 if p.lower() not in used2]
        return paired, still_only1, only2

    @staticmethod
    def _clfy_prefix_member_path(bundle_member: str, inner_path: str) -> str:
        bundle_member = str(bundle_member or "").replace("\\", "/").strip("/")
        inner_path = str(inner_path or "").replace("\\", "/").strip("/")
        if not inner_path:
            return bundle_member
        if bundle_member and inner_path.lower().startswith(bundle_member.lower() + "!/"):
            return inner_path
        return f"{bundle_member}!/{inner_path}" if bundle_member else inner_path

    def _clfy_prefix_archive_diff(self, diff: dict, old_member: str, new_member: str) -> dict:
        """Prefix inner paths with bundle virtual location so report shows lib/ vs sharedlib/."""
        if not diff:
            return diff or {}
        out = dict(diff)
        label_new = new_member or old_member

        def _pref(p: str, side_member: str) -> str:
            ps = str(p).replace("\\", "/")
            if "!/" in ps or ps.lower().startswith(side_member.lower()):
                return ps
            return self._clfy_prefix_member_path(side_member, ps)

        for key in ("only_in_old", "only_in_new", "container_only_old", "container_only_new", "container_different"):
            vals = diff.get(key)
            if not vals:
                continue
            out[key] = {_pref(v, old_member if "old" in key else label_new) for v in vals}
        out["different_content"] = [
            _pref(v, label_new) for v in (diff.get("different_content") or [])
        ]
        out["class_files_changed"] = [
            _pref(v, label_new) for v in (diff.get("class_files_changed") or [])
        ]
        out["_clfy_old_member"] = old_member
        out["_clfy_new_member"] = new_member
        return out

    def _merge_clfy_archive_diffs(self, diffs: list[dict]) -> dict:
        """Merge multiple ClfyAgent bundle pair diffs (e.g. lib + sharedlib locations)."""
        diffs = [d for d in diffs if d and not d.get("error")]
        if not diffs:
            return {}
        if len(diffs) == 1:
            return diffs[0]
        merged: dict = {
            "only_in_old": set(),
            "only_in_new": set(),
            "different_content": [],
            "class_files_changed": [],
            "config_files_changed": [],
            "config_diff_details": [],
            "class_modified_detail": [],
            "container_only_old": set(),
            "container_only_new": set(),
            "container_different": set(),
            "total_members_old": 0,
            "total_members_new": 0,
            "clfy_pairs_compared": [],
            "all_member_sig_keys_old": set(),
            "all_member_sig_keys_new": set(),
        }
        diff_content_seen: set[str] = set()
        version_seen: set[tuple[str, str]] = set()
        merged["version_mismatch_rows"] = []
        merged["jar_size_diff_rows"] = []
        for d in diffs:
            merged["only_in_old"] |= set(d.get("only_in_old") or [])
            merged["only_in_new"] |= set(d.get("only_in_new") or [])
            merged["container_only_old"] |= set(d.get("container_only_old") or [])
            merged["container_only_new"] |= set(d.get("container_only_new") or [])
            merged["container_different"] |= set(d.get("container_different") or [])
            for vr in d.get("version_mismatch_rows") or []:
                key = (
                    str(vr.get("old_path") or "").lower(),
                    str(vr.get("new_path") or "").lower(),
                )
                if key[0] and key[1] and key not in version_seen:
                    version_seen.add(key)
                    merged["version_mismatch_rows"].append(vr)
            merged["jar_size_diff_rows"].extend(d.get("jar_size_diff_rows") or [])
            for x in d.get("different_content") or []:
                xs = str(x)
                if xs not in diff_content_seen:
                    diff_content_seen.add(xs)
                    merged["different_content"].append(xs)
            merged["class_files_changed"].extend(d.get("class_files_changed") or [])
            merged["config_files_changed"].extend(d.get("config_files_changed") or [])
            merged["config_diff_details"].extend(d.get("config_diff_details") or [])
            merged["class_modified_detail"].extend(d.get("class_modified_detail") or [])
            merged["total_members_old"] += int(d.get("total_members_old") or 0)
            merged["total_members_new"] += int(d.get("total_members_new") or 0)
            merged["all_member_sig_keys_old"] |= set(d.get("all_member_sig_keys_old") or [])
            merged["all_member_sig_keys_new"] |= set(d.get("all_member_sig_keys_new") or [])
            pair = (d.get("_clfy_old_member"), d.get("_clfy_new_member"))
            if pair not in merged["clfy_pairs_compared"]:
                merged["clfy_pairs_compared"].append(pair)
        o, n, _vp = self._reconcile_only_in_member_sets(
            merged.get("only_in_old") or set(),
            merged.get("only_in_new") or set(),
        )
        if _vp:
            merged.setdefault("version_mismatch_rows", [])
            for old_p, new_p in _vp:
                leaf_o = self._path_leaf_name_static(old_p)
                leaf_n = self._path_leaf_name_static(new_p)
                merged["version_mismatch_rows"].append(
                    {
                        "old_path": old_p,
                        "new_path": new_p,
                        "logical_name": self._logical_jar_display_label(leaf_o or leaf_n),
                        "summary": self._format_jar_version_change_summary(
                            {self._extract_jar_version_display_token(leaf_o) or leaf_o},
                            {self._extract_jar_version_display_token(leaf_n) or leaf_n},
                        )
                        or f"{leaf_o} → {leaf_n}",
                    }
                )
        sig_new = merged.get("all_member_sig_keys_new") or set()
        sig_old = merged.get("all_member_sig_keys_old") or set()

        merged["only_in_old"] = self._drop_only_in_cross_build_sigs(o, sig_new)
        merged["only_in_new"] = self._drop_only_in_cross_build_sigs(n, sig_old)
        return merged

    def _clfy_empty_zip_path(self) -> Path:
        fd, tmp = tempfile.mkstemp(suffix=".zip", prefix="crm_clfy_empty_")
        os.close(fd)
        with zipfile.ZipFile(tmp, "w"):
            pass
        return Path(tmp)

    def _compare_clfy_all_bundle_locations(
        self,
        info1: dict,
        info2: dict,
        discovered1: Optional[dict],
        discovered2: Optional[dict],
        extra_paths1: Optional[list[str]] = None,
        extra_paths2: Optional[list[str]] = None,
    ) -> dict:
        """
        Compare every ClfyAgent.zip/.ear under each SDK (lib, sharedlib, etc.).
        Unpaired bundles on one build (e.g. new lib/ClfyAgent.zip) are compared vs empty.
        """
        outer1 = Path(info1["path"])
        outer2 = Path(info2["path"])
        paths1 = self._clfy_list_bundle_virtual_paths(outer1)
        paths2 = self._clfy_list_bundle_virtual_paths(outer2)
        for src in (discovered1, info1):
            if not src:
                continue
            m = str(src.get("archive_member") or "").replace("\\", "/").strip("/")
            if m and m.lower() not in {p.lower() for p in paths1}:
                paths1.append(m)
        for src in (discovered2, info2):
            if not src:
                continue
            m = str(src.get("archive_member") or "").replace("\\", "/").strip("/")
            if m and m.lower() not in {p.lower() for p in paths2}:
                paths2.append(m)
        for k in extra_paths1 or []:
            ks = str(k).replace("\\", "/").strip("/")
            if ks and ks.lower() not in {p.lower() for p in paths1}:
                paths1.append(ks)
        for k in extra_paths2 or []:
            ks = str(k).replace("\\", "/").strip("/")
            if ks and ks.lower() not in {p.lower() for p in paths2}:
                paths2.append(ks)
        paths1 = sorted({p.replace("\\", "/") for p in paths1}, key=lambda x: x.lower())
        paths2 = sorted({p.replace("\\", "/") for p in paths2}, key=lambda x: x.lower())
        if paths1 or paths2:
            print(
                f"[CRM] ClfyAgent bundle paths — old SDK: {len(paths1)}, new SDK: {len(paths2)}"
            )
            for side, paths in (("old", paths1), ("new", paths2)):
                for p in paths[:8]:
                    print(f"[CRM]   {side}: {p}")
                if len(paths) > 8:
                    print(f"[CRM]   {side}: ... and {len(paths) - 8} more")
        if not paths1 and not paths2:
            i1, i2 = self._ensure_local_paths_for_clfy(dict(info1), dict(info2))
            return self._compare_clfy_agent_dispatch(i1, i2) or {}
        paired, only1, only2 = self._pair_clfy_bundle_virtual_paths(paths1, paths2)
        if only1 or only2:
            print(
                f"[CRM] ClfyAgent unpaired bundle location(s): "
                f"only old={len(only1)}, only new={len(only2)}"
            )
        diffs: list[dict] = []
        empty_tmp: Optional[Path] = None
        try:
            for p1, p2 in paired:
                i1 = self._clfy_info_for_virtual_member(outer1, p1)
                i2 = self._clfy_info_for_virtual_member(outer2, p2)
                i1, i2 = self._ensure_local_paths_for_clfy(i1, i2)
                d = self._compare_clfy_agent_dispatch(i1, i2) or {}
                if d.get("error"):
                    print(f"[CRM] ClfyAgent pair failed {p1!r} vs {p2!r}: {d.get('error')}")
                    continue
                diffs.append(self._clfy_prefix_archive_diff(d, p1, p2))
            if only1 or only2:
                if empty_tmp is None:
                    empty_tmp = self._clfy_empty_zip_path()
                info_empty = {
                    "path": empty_tmp,
                    "is_nested": False,
                    "archive_member": empty_tmp.name,
                    "rel_key": "empty",
                }
            for p1 in only1:
                i1 = self._clfy_info_for_virtual_member(outer1, p1)
                i1, _ = self._ensure_local_paths_for_clfy(i1, dict(info_empty))
                d = self._compare_clfy_agent_dispatch(i1, info_empty) or {}
                if not d.get("error"):
                    diffs.append(self._clfy_prefix_archive_diff(d, p1, p1))
            for p2 in only2:
                i2 = self._clfy_info_for_virtual_member(outer2, p2)
                _, i2 = self._ensure_local_paths_for_clfy(dict(info_empty), i2)
                d = self._compare_clfy_agent_dispatch(info_empty, i2) or {}
                if not d.get("error"):
                    diffs.append(self._clfy_prefix_archive_diff(d, p2, p2))
        finally:
            if empty_tmp and empty_tmp.is_file():
                try:
                    empty_tmp.unlink()
                except OSError:
                    pass
        merged = self._merge_clfy_archive_diffs(diffs)
        if not merged and diffs:
            return diffs[0]
        if not merged:
            i1, i2 = self._ensure_local_paths_for_clfy(dict(info1), dict(info2))
            return self._compare_clfy_agent_dispatch(i1, i2) or {}
        return merged

    def _compare_clfy_agent_dispatch(self, info1: dict, info2: dict) -> dict:
        info1 = self._normalize_clfy_dispatch_info(info1, info2)
        info2 = self._normalize_clfy_dispatch_info(info2, info1)
        n1 = info1.get("is_nested", False)
        n2 = info2.get("is_nested", False)
        tmp1: Optional[str] = None
        tmp2: Optional[str] = None
        try:
            p1 = Path(info1["path"])
            p2 = Path(info2["path"])
            if n1 and n2:
                out = self._compare_nested_archive_contents(
                    p1, info1["archive_member"], p2, info2["archive_member"]
                )
                return out or {}
            if not n1 and not n2:
                return self._compare_clfy_agent_archives(p1, p2) or {}
            peer2 = info2.get("archive_member")
            peer1 = info1.get("archive_member")
            if n1:
                blob = self._extract_nested_archive(
                    p1, info1["archive_member"], peer_member_hint=peer2
                )
                if not blob:
                    b1 = os.path.basename(
                        (info1.get("archive_member") or "").replace("\\", "/").strip("/")
                    )
                    if b1:
                        blob = self._extract_member_by_basename_from_outer(
                            p1, b1, peer_hint=peer2
                        )
                if not blob:
                    blob = self._extract_clfyagent_bytes_rediscover(p1, peer2)
                if not blob:
                    return {}
                suf = (
                    ".ear"
                    if str(info1.get("archive_member") or "").lower().rstrip("/").endswith(".ear")
                    else ".zip"
                )
                need = len(blob) if blob else 0
                fd, tmp1 = self._crm_mkstemp_with_space(suf, "crm_clfy_agent_", need)
                os.write(fd, blob)
                os.close(fd)
                return self._compare_clfy_agent_archives(Path(tmp1), p2) or {}
            blob = self._extract_nested_archive(
                p2, info2["archive_member"], peer_member_hint=peer1
            )
            if not blob:
                b2 = os.path.basename(
                    (info2.get("archive_member") or "").replace("\\", "/").strip("/")
                )
                if b2:
                    blob = self._extract_member_by_basename_from_outer(
                        p2, b2, peer_hint=peer1
                    )
            if not blob:
                blob = self._extract_clfyagent_bytes_rediscover(p2, peer1)
            if not blob:
                return {}
            suf2 = (
                ".ear"
                if str(info2.get("archive_member") or "").lower().rstrip("/").endswith(".ear")
                else ".zip"
            )
            need2 = len(blob) if blob else 0
            fd, tmp2 = self._crm_mkstemp_with_space(suf2, "crm_clfy_agent_", need2)
            os.write(fd, blob)
            os.close(fd)
            return self._compare_clfy_agent_archives(p1, Path(tmp2)) or {}
        finally:
            for t in (tmp1, tmp2):
                if t:
                    try:
                        os.unlink(t)
                    except OSError:
                        pass
    
    def _build_clfy_agent_zip_diff(
        self, files1: dict, files2: dict, folder1: Path, folder2: Path
    ) -> None:
        """Size-only compare ClfyAgent.zip / ClfyAgent.ear inside both SDK trees when compare_clfy_agent_zip is True."""
        self.clfy_agent_zip_diff = None
        if not self.compare_clfy_agent_zip:
            return
        print(
            f"[CRM] Looking for {CLFY_AGENT_BUNDLE_REPORT_LABEL} in both builds "
            f"(size-only container compare)...",
            flush=True,
        )
        old_override = (
            self.clfy_agent_zip_override_old
            or os.environ.get("CRM_CLFY_AGENT_ZIP_OLD", "")
            or ""
        )
        new_override = (
            self.clfy_agent_zip_override_new
            or os.environ.get("CRM_CLFY_AGENT_ZIP_NEW", "")
            or ""
        )
        shared_override = os.environ.get("CRM_CLFY_AGENT_ZIP_OVERRIDE", "").strip()
        # Do not probe CLFY_AGENT_ZIP_DEFAULT_FALLBACK_PATH automatically — UNC existence checks
        # can block for minutes on unreachable shares and stall the console right after startup.
        info_old_override = self._clfy_info_from_override_path(old_override, "old-override")
        info_new_override = self._clfy_info_from_override_path(new_override, "new-override")
        info_shared_override = self._clfy_info_from_override_path(shared_override, "shared-override")
        # Keep scan-discovered candidates as secondary fallback when override-first path fails.
        discovered_info1 = None
        discovered_info2 = None

        k1 = self._clfyagent_keys_in_files(files1)
        k2 = self._clfyagent_keys_in_files(files2)
        rel1, rel2 = self._pair_clfyagent_rel_keys(k1, k2)
        if rel1:
            print(f"[CRM] ClfyAgent: using scan-discovered path in old build: {rel1}", flush=True)
            discovered_info1 = self._file_entry_to_clfy_info(files1, rel1)
        else:
            print(f"[CRM] ClfyAgent: discovering bundle in old build ({folder1})...", flush=True)
            discovered_info1 = self._synthetic_clfy_agent_info(folder1)
        if rel2:
            print(f"[CRM] ClfyAgent: using scan-discovered path in new build: {rel2}", flush=True)
            discovered_info2 = self._file_entry_to_clfy_info(files2, rel2)
        else:
            print(f"[CRM] ClfyAgent: discovering bundle in new build ({folder2})...", flush=True)
            discovered_info2 = self._synthetic_clfy_agent_info(folder2)
        # Prefer per-build discovery; explicit old/new overrides win; shared override only if a side is missing.
        info1 = dict(info_old_override or discovered_info1 or {}) or None
        info2 = dict(info_new_override or discovered_info2 or {}) or None
        if not info1 and info_shared_override:
            info1 = dict(info_shared_override)
        if not info2 and info_shared_override:
            info2 = dict(info_shared_override)
        # Re-rank ClfyAgent member path inside each SDK using the peer build's location (lib vs sharedlib).
        if info1 and info2:
            peer1 = str(info2.get("archive_member") or info2.get("rel_key") or "")
            peer2 = str(info1.get("archive_member") or info1.get("rel_key") or "")

            def _rerank_clfy_info(folder: Path, peer: str, current: dict) -> dict:
                if not (
                    _path_is_file(folder)
                    and folder.suffix.lower() in (".jar", ".war", ".ear", ".zip")
                ):
                    return current
                member = str(current.get("archive_member") or current.get("rel_key") or "").strip()
                if member and os.path.basename(member).lower() in _CLFY_AGENT_BUNDLE_BASENAMES_LOWER:
                    return current
                paths = self._list_clfy_paths_from_outer_archive(folder)
                if not paths:
                    return current
                pick = self._pick_basename_hits(paths, peer, peer)[0]
                return self._clfy_info_for_virtual_member(folder, pick)

            info1 = _rerank_clfy_info(folder1, peer1, info1)
            info2 = _rerank_clfy_info(folder2, peer2, info2)
        if not info1 or not info2:
            self.clfy_agent_zip_diff = {
                "label": CLFY_AGENT_BUNDLE_REPORT_LABEL,
                "error": "ClfyAgent .zip / .ear not found in one or both builds (scan may be shallow — try disabling fast mode or point to SDK archive)",
                "rel_old": rel1,
                "rel_new": rel2,
                "zip_old": str(info1.get("path")) if info1 else None,
                "zip_new": str(info2.get("path")) if info2 else None,
                "archive_diff": None,
            }
            print(f"[CRM] ClfyAgent bundle: could not locate in both builds")
            return
        if info1:
            info1 = self._normalize_clfy_dispatch_info(info1, info2)
        if info2:
            info2 = self._normalize_clfy_dispatch_info(info2, info1)
        info1, info2 = self._ensure_local_paths_for_clfy(info1, info2)
        print(
            f"[CRM] ClfyAgent bundle old: {info1.get('rel_key')} | new: {info2.get('rel_key')}"
        )
        try:
            diff = self._compare_clfy_all_bundle_locations(
                info1, info2, discovered_info1, discovered_info2, extra_paths1=k1, extra_paths2=k2
            )
            if diff and diff.get("error"):
                err = str(diff.get("error") or "")
                retry_old = "old_ok=False" in err
                retry_new = "new_ok=False" in err
                retried = False
                retry_info1 = dict(info1)
                retry_info2 = dict(info2)
                if retry_old:
                    # Primary retry: explicit override/shared override, then discovered source.
                    src = info_old_override or info_shared_override or discovered_info1
                    if src:
                        retry_info1 = dict(src)
                        retried = True
                if retry_new:
                    # Primary retry: explicit override/shared override, then discovered source.
                    src = info_new_override or info_shared_override or discovered_info2
                    if src:
                        retry_info2 = dict(src)
                        retried = True
                if retried:
                    print("[CRM] ClfyAgent bundle: retrying comparison with override fallback path(s)")
                    retry_info1, retry_info2 = self._ensure_local_paths_for_clfy(retry_info1, retry_info2)
                    diff_retry = self._compare_clfy_agent_dispatch(retry_info1, retry_info2)
                    if diff_retry and not diff_retry.get("error"):
                        info1, info2, diff = retry_info1, retry_info2, diff_retry
            if diff and diff.get("error"):
                err2 = str(diff.get("error") or "")
                if "Could not extract nested archives" in err2:
                    print("[CRM] ClfyAgent bundle: retrying with jar-tf refresh of archive_member path(s)")
                    rf1 = self._clfy_refresh_member_from_peer_hint(
                        dict(info1), info2.get("archive_member")
                    )
                    rf2 = self._clfy_refresh_member_from_peer_hint(
                        dict(info2), info1.get("archive_member")
                    )
                    rf1, rf2 = self._ensure_local_paths_for_clfy(rf1, rf2)
                    diff_refresh = self._compare_clfy_agent_dispatch(rf1, rf2)
                    if diff_refresh and not diff_refresh.get("error"):
                        info1, info2, diff = rf1, rf2, diff_refresh
            if diff and diff.get("error"):
                err_scan = str(diff.get("error") or "")
                if "Could not extract nested archives" in err_scan and discovered_info1 and discovered_info2:
                    d1, d2 = self._ensure_local_paths_for_clfy(
                        dict(discovered_info1), dict(discovered_info2)
                    )
                    if d1.get("rel_key") != info1.get("rel_key") or d2.get("rel_key") != info2.get("rel_key"):
                        print(
                            "[CRM] ClfyAgent bundle: retrying with scan-discovered bundle path(s) "
                            "(override/stale virtual path recovery)"
                        )
                        diff_scan = self._compare_clfy_agent_dispatch(d1, d2)
                        if diff_scan and not diff_scan.get("error"):
                            info1, info2, diff = d1, d2, diff_scan
            if not diff:
                self.clfy_agent_zip_diff = {
                    "label": CLFY_AGENT_BUNDLE_REPORT_LABEL,
                    "error": "Comparison produced no result (extract or read failed)",
                    "rel_old": info1.get("rel_key"),
                    "rel_new": info2.get("rel_key"),
                    "zip_old": self._clfy_report_outer_path(info1),
                    "zip_new": self._clfy_report_outer_path(info2),
                    "archive_diff": None,
                }
                return
            if diff.get("error"):
                self.clfy_agent_zip_diff = {
                    "label": CLFY_AGENT_BUNDLE_REPORT_LABEL,
                    "error": str(diff.get("error")),
                    "rel_old": info1.get("rel_key"),
                    "rel_new": info2.get("rel_key"),
                    "zip_old": self._clfy_report_outer_path(info1),
                    "zip_new": self._clfy_report_outer_path(info2),
                    "archive_diff": diff,
                }
                return
            oc = diff.get("only_in_old") or set()
            onc = diff.get("only_in_new") or set()
            dc = diff.get("different_content") or []
            c_oc = diff.get("container_only_old") or set()
            c_onc = diff.get("container_only_new") or set()
            c_dc = diff.get("container_different") or set()
            class_detail = diff.get("class_modified_detail") or []
            self.clfy_agent_zip_diff = {
                "label": CLFY_AGENT_BUNDLE_REPORT_LABEL,
                "error": None,
                "rel_old": info1.get("rel_key"),
                "rel_new": info2.get("rel_key"),
                "zip_old": self._clfy_report_outer_path(info1),
                "zip_new": self._clfy_report_outer_path(info2),
                "archive_diff": diff,
                "summary_only_old": len(c_oc),
                "summary_only_new": len(c_onc),
                "summary_different": len(c_dc) if c_dc else len(dc),
                "summary_members_only_old": len(oc),
                "summary_members_only_new": len(onc),
                "summary_members_different": len(dc),
                "summary_class_changed": len(diff.get("class_files_changed") or []),
                "class_modified_detail": class_detail,
            }
            print(
                f"[CRM] ClfyAgent bundle: containers only old {len(c_oc)}, only new {len(c_onc)}, "
                f"container content diff {len(c_dc)} | all members only old {len(oc)}, only new {len(onc)}, "
                f"content diff {len(dc)}, class files changed {len(diff.get('class_files_changed') or [])}"
            )
        except Exception as e:
            logger.warning(f"[CRM] ClfyAgent bundle compare failed: {e}")
            self.clfy_agent_zip_diff = {
                "label": CLFY_AGENT_BUNDLE_REPORT_LABEL,
                "error": str(e),
                "rel_old": info1.get("rel_key"),
                "rel_new": info2.get("rel_key"),
                "zip_old": self._clfy_report_outer_path(info1),
                "zip_new": self._clfy_report_outer_path(info2),
                "archive_diff": None,
            }
    
    def _get_archive_base_name(self, filename: str) -> str:
        """Extract base name from archive filename, removing version/build numbers."""
        base = filename
        # Remove version patterns like _9.22.PB1.HFB11_, _9.22.PB1.HFB12_
        base = re.sub(r'_\d+\.\d+\.PB\d+\.HFB\d+_', '_', base, flags=re.IGNORECASE)
        # Remove date/build patterns like _20251104_1431, _20251210_1232
        base = re.sub(r'_\d{8}_\d{4}', '', base)
        # Remove version patterns like -10.1.0.pb00_hf29.319
        base = re.sub(
            r'-\d+\.\d+\.\d+\.[\w\d_.-]+(?=\.(?:jar|war|ear|zip)(?:\.pack\.gz)?$)',
            "",
            base,
            flags=re.IGNORECASE,
        )
        # Remove simple version numbers at end like -1.0 (before .jar / archive tail or .jar.pack.gz)
        base = re.sub(
            r'-\d+(\.\d+)*(?=\.(?:jar|war|ear|zip)(?:\.pack\.gz)?$)',
            "",
            base,
            flags=re.IGNORECASE,
        )
        # Remove __V patterns like __V9_22_1_12
        base = re.sub(r'__V\d+(?:_\d+)*', '', base, flags=re.IGNORECASE)
        return base.lower()
    
    def compare_folders(self) -> list[CRMComparisonResult]:
        """Perform full content comparison of two CRM folders."""
        print(f"[CRM] Starting full content comparison")
        print(f"[CRM] Old Build: {self.folder1_path}")
        print(f"[CRM] New Build: {self.folder2_path}")
        try:
            from client_temp_paths import (
                cleanup_compare_temp_before_run,
                get_compare_client_context,
            )

            host, _ident, ctx_app = get_compare_client_context()
            app = (
                str(getattr(self, "compare_application", "") or "").strip()
                or ctx_app
                or "CRM"
            )
            pre = cleanup_compare_temp_before_run(host, application=app)
            if pre.get("removed"):
                print(
                    f"[CRM] Temp cleanup before run ({pre.get('application_slug') or app}): "
                    f"removed {pre['removed']} item(s), ~{pre.get('freed_human', '0 B')} freed",
                    flush=True,
                )
            elif pre.get("errors"):
                print(
                    f"[CRM] Temp cleanup: some paths could not be removed "
                    f"({len(pre.get('errors') or [])} error(s)); run continues.",
                    flush=True,
                )
        except Exception as e:
            logger.warning(f"[CRM] Client temp pre-run cleanup skipped: {e}")
        
        self.comparison_start_time = time.time()
        self.results = []
        self.count_unchanged = 0
        self._report_diff_buckets_cache = None
        
        # Check if paths exist (includes Windows Explorer path-into-zip: outer.jar\inner\...)
        def _root_accessible(p: Path) -> bool:
            if self._parse_shell_zip_appended_path(p) is not None:
                return True
            return _path_exists(p)
        
        if not _root_accessible(self.folder1_path):
            self.comparison_error = f"Old build path does not exist: {self.folder1_path}"
            print(f"[CRM] ERROR: {self.comparison_error}")
            return self.results
        
        if not _root_accessible(self.folder2_path):
            self.comparison_error = f"New build path does not exist: {self.folder2_path}"
            print(f"[CRM] ERROR: {self.comparison_error}")
            return self.results
        
        folder1_to_compare = self.folder1_path
        folder2_to_compare = self.folder2_path
        if not getattr(self, "run_full_tree_compare", True):
            print(
                "[CRM] Full build tree compare disabled (Fast CRM/AMSS unchecked). "
                "Optional post-processing only."
            )
            sys.stdout.flush()
            files1: dict = {}
            files2: dict = {}
            if self.compare_clfy_agent_zip:
                print(f"[CRM] ClfyAgent-only mode: {CLFY_AGENT_BUNDLE_REPORT_LABEL} compare...")
                sys.stdout.flush()
                try:
                    self._build_clfy_agent_zip_diff(
                        files1, files2, folder1_to_compare, folder2_to_compare
                    )
                except Exception as e:
                    logger.warning(f"[CRM] {CLFY_AGENT_BUNDLE_REPORT_LABEL} section failed: {e}")
                    self.clfy_agent_zip_diff = {
                        "label": CLFY_AGENT_BUNDLE_REPORT_LABEL,
                        "error": str(e),
                        "rel_old": None,
                        "rel_new": None,
                        "zip_old": None,
                        "zip_new": None,
                        "archive_diff": None,
                    }
            else:
                self.comparison_error = (
                    "No comparison scope selected. Enable "
                    "<strong>Fast CRM/AMSS full compare</strong> and/or "
                    "<strong>Compare ClfyAgent .zip/.ear in SDK</strong> on the form."
                )
                print(f"[CRM] ERROR: {self.comparison_error}")
            for p in self._clfy_temp_copy_paths:
                try:
                    if p.is_file():
                        p.unlink()
                except OSError:
                    pass
            self._clfy_temp_copy_paths.clear()
            self._clfy_outer_read_cache.clear()
            print(f"[CRM] compare_folders() finished ({len(self.results)} result row(s)).")
            sys.stdout.flush()
            return self.results

        local_copy_helper = None
        if self.use_local_copy and (self._is_network_path(self.folder1_path) or self._is_network_path(self.folder2_path)):
            try:
                from folder_compare import ArchiveContentsComparer
                local_copy_helper = ArchiveContentsComparer(str(self.folder1_path), str(self.folder2_path))
                local_copy_helper.max_retries = self.max_retries
                local_copy_helper.retry_delay = self.retry_delay
                if self._is_network_path(self.folder1_path):
                    print("[CRM] Copying old build to local temp for faster scan...")
                    self.temp_folder1 = local_copy_helper._copy_network_folder_to_local(self.folder1_path)
                    if self.temp_folder1:
                        folder1_to_compare = self.temp_folder1
                        print(f"[CRM] Using local old-build copy: {folder1_to_compare}")
                if self._is_network_path(self.folder2_path):
                    print("[CRM] Copying new build to local temp for faster scan...")
                    self.temp_folder2 = local_copy_helper._copy_network_folder_to_local(self.folder2_path)
                    if self.temp_folder2:
                        folder2_to_compare = self.temp_folder2
                        print(f"[CRM] Using local new-build copy: {folder2_to_compare}")
            except Exception as e:
                logger.warning(f"[CRM] Local copy optimization unavailable: {e}")
        
        # Get all files from both folders
        print("[CRM] Scanning old build folder...")
        files1 = self._get_all_files(folder1_to_compare)
        print(f"[CRM] Found {len(files1)} files in old build")
        
        print("[CRM] Scanning new build folder...")
        files2 = self._get_all_files(folder2_to_compare)
        print(f"[CRM] Found {len(files2)} files in new build")

        # Cross-release SDK compare: CRM_10.24/CRM10.24.1_ALL ↔ CRM_10.25/CRM10.25.0_ALL
        self._crm_scan_paths_for_inference = (files1, files2)
        release_pairs = self._crm_infer_release_replacements()
        if release_pairs:
            print(
                "[CRM] Cross-release path mapping: "
                + ", ".join(f"{a}->{b}" for a, b in release_pairs)
            )
        files2_release_index = self._crm_build_files2_release_index(files2)
        files2_jar_key_index = self._crm_build_files2_jar_key_index(files2)
        self._crm_last_files1 = files1
        self._crm_last_files2 = files2
        self._crm_files2_release_index = files2_release_index
        self._crm_files2_jar_key_index = files2_jar_key_index
        self._crm_scan_file_count_old = len(files1)
        self._crm_scan_file_count_new = len(files2)
        
        # Find archives only in folder1 (potential removals or version changes)
        # Archives whose path is missing on the other side *by exact dict key* were paired as
        # "version upgrades". UNC/Windows trees often differ only by path casing, so require a
        # case-insensitive counterpart check. Also require a single unambiguous old/new candidate
        # per (directory, normalized-base) key so unrelated JARs in the same folder are not paired.
        only_in_1_by_key: dict[tuple[str, str], list[str]] = defaultdict(list)
        for rel_path in files1:
            if _crm_find_counterpart_key(files2, rel_path) is not None:
                continue
            if self._crm_find_release_aligned_counterpart(
                files2, files2_release_index, rel_path, jar_key_index=files2_jar_key_index
            ) is not None:
                continue
            if not _crm_rel_path_is_version_pairable_archive(rel_path):
                continue
            dir_part = os.path.dirname(rel_path)
            filename = os.path.basename(rel_path)
            base_name = self._get_archive_base_name(filename)
            key = (
                self._crm_neutralize_path_for_alignment(_crm_norm_rel_path(dir_part)).lower(),
                base_name,
            )
            only_in_1_by_key[key].append(rel_path)

        only_in_2_by_key: dict[tuple[str, str], list[str]] = defaultdict(list)
        for rel_path in files2:
            if _crm_find_counterpart_key(files1, rel_path) is not None:
                continue
            mapped_old = self._crm_map_path_across_release(rel_path, to_new=False)
            if mapped_old and _crm_find_counterpart_key(files1, mapped_old) is not None:
                continue
            if not _crm_rel_path_is_version_pairable_archive(rel_path):
                continue
            dir_part = os.path.dirname(rel_path)
            filename = os.path.basename(rel_path)
            base_name = self._get_archive_base_name(filename)
            key = (
                self._crm_neutralize_path_for_alignment(_crm_norm_rel_path(dir_part)).lower(),
                base_name,
            )
            only_in_2_by_key[key].append(rel_path)

        matched_archive_pairs = {}  # old_path -> new_path
        for key, olds in only_in_1_by_key.items():
            news = only_in_2_by_key.get(key)
            if not news:
                continue
            if len(olds) == 1 and len(news) == 1:
                old_path, new_path = olds[0], news[0]
                matched_archive_pairs[old_path] = new_path
                print(f"[CRM] Matched archives by base name: {os.path.basename(old_path)} <-> {os.path.basename(new_path)}")
            else:
                logger.warning(
                    "[CRM] Skipping ambiguous archive version pairing for %s / %s: "
                    "%d candidate(s) only on old vs %d only on new — listing as removed/added instead",
                    key[0],
                    key[1],
                    len(olds),
                    len(news),
                )
        
        # Track which files have been processed via archive matching
        processed_via_matching = set()
        for old_path, new_path in matched_archive_pairs.items():
            processed_via_matching.add(old_path)
            processed_via_matching.add(new_path)
        
        # Normalize paths to lowercase for case-insensitive matching
        files1_lower = {k.lower(): k for k in files1.keys()}
        files2_lower = {k.lower(): k for k in files2.keys()}

        # Build compare pairs: match across CRM release folders, not only exact paths
        matched_new_paths: set[str] = set()
        compare_pairs: list[tuple[Optional[str], Optional[str]]] = []
        for orig1 in sorted(files1.keys(), key=lambda x: str(x).lower()):
            if orig1 in processed_via_matching:
                continue
            orig2 = files2_lower.get(orig1.lower())
            if not orig2:
                orig2 = self._crm_find_release_aligned_counterpart(
                    files2, files2_release_index, orig1, jar_key_index=files2_jar_key_index
                )
            if orig2:
                matched_new_paths.add(orig2)
            compare_pairs.append((orig1, orig2))
        for orig2 in sorted(files2.keys(), key=lambda x: str(x).lower()):
            if orig2 in processed_via_matching or orig2 in matched_new_paths:
                continue
            compare_pairs.append((None, orig2))

        self.total_files = len(compare_pairs)
        print(f"[CRM] Total file pairs to compare: {self.total_files}")
        print(f"[CRM] Matched {len(matched_archive_pairs)} archive pairs by base name")
        
        # Find common files for debugging (exact path overlap only)
        common_files_lower = set(files1_lower.keys()) & set(files2_lower.keys())
        clfy_common = [f for f in common_files_lower if 'clfycore' in f]
        
        # Show files in common that contain ClfyCore
        if clfy_common:
            print(f"[CRM] Common files with 'ClfyCore': {len(clfy_common)}")
            for f_lower in clfy_common[:5]:
                orig1 = files1_lower.get(f_lower)
                orig2 = files2_lower.get(f_lower)
                info1 = files1.get(orig1, {}) if orig1 else {}
                info2 = files2.get(orig2, {}) if orig2 else {}
                crc1 = info1.get('crc', 0)
                crc2 = info2.get('crc', 0)
                size1 = info1.get('size', 0)
                size2 = info2.get('size', 0)
                diff_marker = "DIFFERENT" if (crc1 != crc2 or size1 != size2) else "same"
                print(f"[CRM]   - {f_lower} [{diff_marker}]")
                print(f"[CRM]       Old: size={size1}, crc={crc1}")
                print(f"[CRM]       New: size={size2}, crc={crc2}")
        
        # Show config files in common
        conf_common = [f for f in common_files_lower if f.endswith(('.conf', '.config'))]
        if conf_common:
            print(f"[CRM] Common config files: {len(conf_common)}")
            for f_lower in conf_common[:5]:
                orig1 = files1_lower.get(f_lower)
                orig2 = files2_lower.get(f_lower)
                info1 = files1.get(orig1, {}) if orig1 else {}
                info2 = files2.get(orig2, {}) if orig2 else {}
                crc1 = info1.get('crc', 0)
                crc2 = info2.get('crc', 0)
                diff_marker = "DIFFERENT" if crc1 != crc2 else "same"
                print(f"[CRM]   - {f_lower} [{diff_marker}]")
        
        # First, process matched archive pairs (archives with different version numbers)
        for old_path, new_path in matched_archive_pairs.items():
            self.processed_files += 1
            self._update_progress(f"Comparing: {os.path.basename(old_path)} <-> {os.path.basename(new_path)}")
            
            info1 = files1[old_path]
            info2 = files2[new_path]
            
            size_diff = info2['size'] - info1['size']
            pct = (size_diff / info1['size'] * 100) if info1['size'] > 0 else 0
            details = f"Version changed: {os.path.basename(old_path)} -> {os.path.basename(new_path)} | Size: {size_diff:+d} bytes ({pct:+.1f}%)"
            
            # Compare archive contents deeply
            archive_diff = None
            if self.compare_archive_contents:
                try:
                    print(f"[CRM] Deep comparing archive contents...")
                    archive_diff = self._compare_archive_contents(info1['path'], info2['path'])
                    if archive_diff['only_in_old'] or archive_diff['only_in_new'] or archive_diff['different_content']:
                        details += f" | Nested: {len(archive_diff['only_in_old'])} removed, {len(archive_diff['only_in_new'])} added, {len(archive_diff['different_content'])} modified"
                        print(f"[CRM] Found nested differences: {len(archive_diff['only_in_old'])} removed, {len(archive_diff['only_in_new'])} added, {len(archive_diff['different_content'])} modified")
                except Exception as e:
                    logger.warning(f"[CRM] Could not compare archive contents: {e}")
                    print(f"[CRM] Error comparing archive contents: {e}")
            
            self.results.append(CRMComparisonResult(
                relative_path=f"{old_path} -> {new_path}",
                status='modified',
                size_folder1=info1['size'],
                size_folder2=info2['size'],
                modified_time_folder1=info1.get('modified_time', ''),
                modified_time_folder2=info2.get('modified_time', ''),
                is_archive=True,
                details=details,
                archive_diff=archive_diff
            ))
        
        # Compare remaining files (not processed via archive matching)
        for orig1, orig2 in compare_pairs:
            if orig1 and orig1 in processed_via_matching:
                continue
            if orig2 and orig2 in processed_via_matching:
                continue

            self.processed_files += 1
            rel_path = self._crm_pair_result_rel_path(orig1, orig2)
            self._update_progress(rel_path)

            if orig1 and not orig2:
                # File only in old build (removed)
                info1 = files1[orig1]
                self.results.append(CRMComparisonResult(
                    relative_path=rel_path,
                    status='removed',
                    size_folder1=info1['size'],
                    modified_time_folder1=info1.get('modified_time', ''),
                    is_archive=info1.get('is_archive', rel_path.lower().endswith(('.jar', '.war', '.ear', '.zip'))),
                    details="File removed in new build"
                ))
            elif not orig1 and orig2:
                # File only in new build (added)
                info2 = files2[orig2]
                self.results.append(CRMComparisonResult(
                    relative_path=rel_path,
                    status='added',
                    size_folder2=info2['size'],
                    modified_time_folder2=info2.get('modified_time', ''),
                    is_archive=info2.get('is_archive', rel_path.lower().endswith(('.jar', '.war', '.ear', '.zip'))),
                    details="File added in new build"
                ))
            else:
                # File in both - compare (possibly under different release folder paths)
                info1 = files1[orig1]
                info2 = files2[orig2]
                member1 = info1.get('archive_member', orig1)
                member2 = info2.get('archive_member', orig2)
                is_archive = info1.get('is_archive', False) or info2.get('is_archive', False) or rel_path.lower().endswith(('.jar', '.war', '.ear', '.zip'))
                is_nested1 = info1.get('is_nested', False)
                is_nested2 = info2.get('is_nested', False)
                mod_time1 = info1.get('modified_time', '')
                mod_time2 = info2.get('modified_time', '')
                path_for_ext = orig1 or orig2 or rel_path
                is_class_file = str(path_for_ext).lower().endswith('.class')
                is_config_file = str(path_for_ext).lower().endswith(('.conf', '.xml', '.properties', '.cfg', '.config'))
                
                # Quick size comparison first
                if info1['size'] != info2['size']:
                    size_diff = info2['size'] - info1['size']
                    pct = (size_diff / info1['size'] * 100) if info1['size'] > 0 else 0
                    details = f"Size changed: {size_diff:+d} bytes ({pct:+.1f}%)"
                    
                    # Log class/config file changes specifically
                    if is_class_file:
                        print(f"[CRM] CLASS FILE CHANGED: {rel_path} (size: {info1['size']}->{info2['size']})")
                    elif is_config_file:
                        print(f"[CRM] CONFIG FILE CHANGED: {rel_path} (size: {info1['size']}->{info2['size']})")
                    
                    # For archives, also compare deep nested contents
                    archive_diff = None
                    # Compare nested archives too (e.g., ClfyCore.jar inside SDK.jar)
                    if is_archive and self.compare_archive_contents:
                        print(f"[CRM] DEBUG: Comparing archive contents for: {rel_path}")
                        print(f"[CRM] DEBUG: is_nested1={is_nested1}, is_nested2={is_nested2}")
                        try:
                            # For nested archives, we need to extract and compare them
                            if is_nested1 and is_nested2:
                                # Both are nested archives - extract from parent and compare
                                print(f"[CRM] DEBUG: Using _compare_nested_archive_contents")
                                archive_diff = self._compare_nested_archive_contents(
                                    info1['path'], member1,
                                    info2['path'], member2
                                )
                            else:
                                print(f"[CRM] DEBUG: Using _compare_archive_contents")
                                archive_diff = self._compare_archive_contents(info1['path'], info2['path'])
                            
                            print(f"[CRM] DEBUG: archive_diff result: only_in_old={len(archive_diff.get('only_in_old', []))}, only_in_new={len(archive_diff.get('only_in_new', []))}, different_content={len(archive_diff.get('different_content', []))}")
                            print(f"[CRM] DEBUG: class_files_changed={len(archive_diff.get('class_files_changed', []))}, config_files_changed={len(archive_diff.get('config_files_changed', []))}")
                            
                            if archive_diff and (archive_diff.get('only_in_old') or archive_diff.get('only_in_new') or archive_diff.get('different_content')):
                                details += f" | Archive: {len(archive_diff.get('only_in_old', []))} removed, {len(archive_diff.get('only_in_new', []))} added, {len(archive_diff.get('different_content', []))} modified"
                                if archive_diff.get('class_files_changed'):
                                    details += f" | {len(archive_diff['class_files_changed'])} class files changed"
                        except Exception as e:
                            print(f"[CRM] DEBUG: Exception in archive comparison: {e}")
                            logger.warning(f"[CRM] Could not compare archive contents: {e}")
                    
                    self.results.append(CRMComparisonResult(
                        relative_path=rel_path,
                        status='modified',
                        size_folder1=info1['size'],
                        size_folder2=info2['size'],
                        modified_time_folder1=mod_time1,
                        modified_time_folder2=mod_time2,
                        is_archive=is_archive,
                        details=details,
                        archive_diff=archive_diff
                    ))
                else:
                    # Same size - check CRC for nested files or hash for regular files
                    if self.fast_mode:
                        if not self.show_missing_only:
                            self.count_unchanged += 1
                        continue
                    if is_nested1 and is_nested2:
                        # Both are nested files - compare CRC
                        crc1 = info1.get('crc', 0)
                        crc2 = info2.get('crc', 0)
                        content_differs = crc1 != crc2
                        hash1, hash2 = str(crc1), str(crc2)
                    else:
                        # Regular files - compute hash
                        hash1 = self._get_file_hash(info1['path'])
                        hash2 = self._get_file_hash(info2['path'])
                        content_differs = hash1 != hash2
                    
                    if content_differs:
                        details = "Content differs (same size, different hash/CRC)"
                        
                        # Log class/config file changes specifically
                        if is_class_file:
                            print(f"[CRM] CLASS FILE CHANGED (same size): {rel_path} (CRC: {hash1}->{hash2})")
                        elif is_config_file:
                            print(f"[CRM] CONFIG FILE CHANGED (same size): {rel_path}")
                        
                        # For archives, also compare deep nested contents
                        archive_diff = None
                        if is_archive and self.compare_archive_contents:
                            try:
                                if is_nested1 and is_nested2:
                                    archive_diff = self._compare_nested_archive_contents(
                                        info1['path'], member1,
                                        info2['path'], member2
                                    )
                                else:
                                    archive_diff = self._compare_archive_contents(info1['path'], info2['path'])
                                if archive_diff and (archive_diff.get('only_in_old') or archive_diff.get('only_in_new') or archive_diff.get('different_content')):
                                    details += f" | Archive: {len(archive_diff.get('only_in_old', []))} removed, {len(archive_diff.get('only_in_new', []))} added, {len(archive_diff.get('different_content', []))} modified"
                            except Exception as e:
                                logger.warning(f"[CRM] Could not compare archive contents: {e}")
                        
                        self.results.append(CRMComparisonResult(
                            relative_path=rel_path,
                            status='modified',
                            size_folder1=info1['size'],
                            size_folder2=info2['size'],
                            hash_folder1=hash1,
                            hash_folder2=hash2,
                            modified_time_folder1=mod_time1,
                            modified_time_folder2=mod_time2,
                            is_archive=is_archive,
                            details=details,
                            archive_diff=archive_diff
                        ))
                    elif not self.show_missing_only:
                        self.count_unchanged += 1
        
        # Summary
        added = len([r for r in self.results if r.status == 'added'])
        removed = len([r for r in self.results if r.status == 'removed'])
        modified = len([r for r in self.results if r.status == 'modified'])
        unchanged = self.count_unchanged
        
        # Count class files and config / text file paths (including in nested archives; optional name filter in pool)
        class_files_changed = []
        for r in self.results:
            if r.status == 'modified':
                rel_path_lower = r.relative_path.lower()
                if rel_path_lower.endswith('.class'):
                    class_files_changed.append(r.relative_path)
                if r.archive_diff:
                    for f in r.archive_diff.get('class_files_changed', []):
                        class_files_changed.append(f"{r.relative_path}!/{f}")
                    for f in r.archive_diff.get('different_content', []):
                        f_lower = f.lower()
                        if f_lower.endswith('.class'):
                            cfp = f"{r.relative_path}!/{f}"
                            if cfp not in class_files_changed:
                                class_files_changed.append(cfp)

        config_files_changed = self._collect_config_text_diff_path_pool()
        
        print(f"[CRM] " + "=" * 60)
        print(f"[CRM] Comparison complete!")
        print(f"[CRM] " + "=" * 60)
        print(f"[CRM] Added: {added}, Removed: {removed}, Modified: {modified}, Unchanged: {unchanged}")
        print(f"[CRM] " + "-" * 60)
        print(f"[CRM] CLASS FILES CHANGED: {len(class_files_changed)}")
        print(f"[CRM] TEXT/CONFIG FILE PATHS (for HTML 'Text and config' section): {len(config_files_changed)}")
        
        if class_files_changed:
            print(f"[CRM] " + "-" * 60)
            print(f"[CRM] Changed class files:")
            for cf in class_files_changed[:20]:
                print(f"[CRM]   - {cf}")
            if len(class_files_changed) > 20:
                print(f"[CRM]   ... and {len(class_files_changed) - 20} more")
        
        if config_files_changed:
            print(f"[CRM] " + "-" * 60)
            print(f"[CRM] Changed config files:")
            for cf in config_files_changed[:20]:
                print(f"[CRM]   - {cf}")
            if len(config_files_changed) > 20:
                print(f"[CRM]   ... and {len(config_files_changed) - 20} more")
        
        print("[CRM] File diff summary complete. Running post-processing (optional JAR / zip steps may take several minutes)...")
        sys.stdout.flush()
        print(f"[CRM] " + "=" * 60)
        sys.stdout.flush()

        if self._parse_class_diff_jar_names(self.class_diff_jar_name or ""):
            print("[CRM] Post-processing: JAR class file diff (decompile / CFR) — please wait...")
            self._update_progress("Post-processing: JAR class diff...")
            sys.stdout.flush()
            try:
                self._build_jar_class_diff(
                    folder1_to_compare,
                    folder2_to_compare,
                    files1_map=files1,
                    files2_map=files2,
                )
            except Exception as e:
                logger.warning(f"[CRM] JAR class diff failed: {e}")
                self.jar_class_diff_list = [
                    {
                        "query": (self.class_diff_jar_name or "").strip(),
                        "error": str(e),
                        "jar_old": None,
                        "jar_new": None,
                        "build_path_old": str(self.folder1_path),
                        "build_path_new": str(self.folder2_path),
                        "jar_old_source": "",
                        "jar_new_source": "",
                        "only_in_old": [],
                        "only_in_new": [],
                        "modified": [],
                        "modified_detail": [],
                    }
                ]
            print("[CRM] Post-processing: JAR class file diff step finished.")
            sys.stdout.flush()
        else:
            print("[CRM] Post-processing: skipping JAR class diff (no JAR names specified).")
            sys.stdout.flush()

        if self._parse_class_diff_jar_names(self.manifest_diff_jar_name or ""):
            print("[CRM] Post-processing: JAR manifest Implementation-Version diff...")
            self._update_progress("Post-processing: JAR manifest diff...")
            sys.stdout.flush()
            try:
                self._build_jar_manifest_diff(
                    folder1_to_compare,
                    folder2_to_compare,
                    files1_map=files1,
                    files2_map=files2,
                )
            except Exception as e:
                logger.warning(f"[CRM] JAR manifest diff failed: {e}")
                self.jar_manifest_diff_list = [
                    {
                        "query": (self.manifest_diff_jar_name or "").strip(),
                        "error": str(e),
                        "jar_old": None,
                        "jar_new": None,
                        "build_path_old": str(self.folder1_path),
                        "build_path_new": str(self.folder2_path),
                        "jar_old_source": "",
                        "jar_new_source": "",
                        "manifest_impl_version_old": "",
                        "manifest_impl_version_new": "",
                        "manifest_impl_version_error_old": "",
                        "manifest_impl_version_error_new": "",
                        "manifest_impl_version_diff": False,
                    }
                ]
            print("[CRM] Post-processing: JAR manifest diff step finished.")
            sys.stdout.flush()
        else:
            print("[CRM] Post-processing: skipping JAR manifest diff (no JAR names specified).")
            sys.stdout.flush()

        if self.compare_clfy_agent_zip:
            print(f"[CRM] Post-processing: {CLFY_AGENT_BUNDLE_REPORT_LABEL} size-only compare...")
            self._update_progress(f"Post-processing: {CLFY_AGENT_BUNDLE_REPORT_LABEL}...")
            sys.stdout.flush()
            try:
                self._build_clfy_agent_zip_diff(files1, files2, folder1_to_compare, folder2_to_compare)
            except Exception as e:
                logger.warning(f"[CRM] {CLFY_AGENT_BUNDLE_REPORT_LABEL} section failed: {e}")
                self.clfy_agent_zip_diff = {
                    "label": CLFY_AGENT_BUNDLE_REPORT_LABEL,
                    "error": str(e),
                    "rel_old": None,
                    "rel_new": None,
                    "zip_old": None,
                    "zip_new": None,
                    "archive_diff": None,
                }
            print(f"[CRM] Post-processing: {CLFY_AGENT_BUNDLE_REPORT_LABEL} step finished.")
            sys.stdout.flush()
        for p in self._clfy_temp_copy_paths:
            try:
                if p.is_file():
                    p.unlink()
            except OSError:
                pass
        self._clfy_temp_copy_paths.clear()
        self._clfy_outer_read_cache.clear()

        if self.should_cleanup_temp_folders:
            for temp in (self.temp_folder1, self.temp_folder2):
                if temp and _path_exists(temp):
                    try:
                        shutil.rmtree(temp)
                    except Exception:
                        pass
            self.temp_folder1 = None
            self.temp_folder2 = None
        print(f"[CRM] compare_folders() finished ({len(self.results)} result row(s)). Saving reports is done by the caller.")
        sys.stdout.flush()
        return self.results

    def _full_path_old_build(self, relative_path: str) -> str:
        """Absolute path under the old (folder1) build root."""
        return str(self._abs_pair_build_paths(relative_path)[0])

    def _full_path_new_build(self, relative_path: str) -> str:
        """Absolute path under the new (folder2) build root."""
        return str(self._abs_pair_build_paths(relative_path)[1])
    
    def _report_diff_buckets(
        self,
    ) -> tuple[list[CRMComparisonResult], list[CRMComparisonResult], list[CRMComparisonResult], int]:
        """Single-pass split: added, removed, modified, unchanged_count (cached)."""
        cached = getattr(self, "_report_diff_buckets_cache", None)
        if cached is not None:
            return cached
        added: list[CRMComparisonResult] = []
        removed: list[CRMComparisonResult] = []
        modified: list[CRMComparisonResult] = []
        legacy_unchanged = 0
        for r in self.results:
            st = r.status
            if st == "added":
                added.append(r)
            elif st == "removed":
                removed.append(r)
            elif st == "modified":
                modified.append(r)
            elif st == "unchanged":
                legacy_unchanged += 1
        unchanged_count = int(getattr(self, "count_unchanged", 0) or 0) + legacy_unchanged
        self._report_diff_buckets_cache = (added, removed, modified, unchanged_count)
        return self._report_diff_buckets_cache

    def generate_report(self) -> str:
        """Generate a text report of the comparison results."""
        from io import StringIO

        buf = StringIO()
        self._write_text_report(buf)
        return buf.getvalue()

    def _write_text_report(self, out) -> None:
        """Stream plain-text report to a file-like object (memory-efficient)."""
        lines = []
        lines.append("=" * 80)
        lines.append("CRM FULL CONTENT COMPARISON REPORT")
        lines.append("=" * 80)
        lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"Old Build Path: {self.folder1_path}")
        lines.append(f"New Build Path: {self.folder2_path}")
        lines.append("")
        
        if self.comparison_error:
            lines.append(f"ERROR: {self.comparison_error}")
            lines.append("=" * 80)
            out.write("\n".join(lines))
            return
        
        added, removed, modified, unchanged_count = self._report_diff_buckets()
        ntot = len(self.results) + unchanged_count
        if ntot >= 50_000:
            print(
                f"[CRM] Text report: assembling {ntot:,} row(s) "
                f"(added {len(added):,}, removed {len(removed):,}, "
                f"modified {len(modified):,}, unchanged {unchanged_count:,})…"
            )
            sys.stdout.flush()
        
        lines.append("-" * 80)
        lines.append("SUMMARY")
        lines.append("-" * 80)
        lines.append(f"Files Added:     {len(added)}")
        lines.append(f"Files Removed:   {len(removed)}")
        lines.append(f"Files Modified:  {len(modified)}")
        lines.append(f"Files Unchanged: {unchanged_count}")
        align_cache = self._crm_align_removed_added_for_report(removed, added)
        c_gl = self._side_by_side_counts(
            added, removed, modified, unchanged_count, align_cache=align_cache
        )
        lines.append("")
        lines.append(
            f"Unmatched old-side paths after alignment: {c_gl['files_only_old']:,}. "
            f"Unmatched JAR / WAR / EAR on old side (incl. nested): {c_gl['jar_removed_old']:,}. "
            f"See AT A GLANCE + side-by-side table below for pairing rules."
        )
        lines.append("")
        lines.extend(
            self._text_side_by_side_summary_block(
                added, removed, modified, unchanged_count, align_cache=align_cache
            )
        )
        lines.extend(self._text_explicit_size_compare_lines())
        lines.extend(self._text_version_changed_jar_summary_lines())
        lines.extend(self._text_high_size_diff_alert_lines(modified))
        ra_delta = len(removed) - len(added)
        align_pairs, unpaired_r, unpaired_a = align_cache
        lines.append("-" * 80)
        lines.append("REMOVED / ADDED BALANCE (Beyond Compare–style)")
        lines.append("-" * 80)
        lines.append(f"Removed-only paths: {len(removed)}")
        lines.append(f"Added-only paths:   {len(added)}")
        lines.append(f"Delta (removed − added): {ra_delta}")
        if ra_delta > 0:
            lines.append(
                f"  → {ra_delta} more removal(s) than addition(s): "
                "those paths have no new-only counterpart at the same cardinality "
                '(see "UNMATCHED — REMOVED" below).'
            )
        elif ra_delta < 0:
            lines.append(
                f"  → {-ra_delta} more addition(s) than removal(s): "
                'see "UNMATCHED — ADDED" below.'
            )
        else:
            lines.append(
                "  → Same count on both sides; alignment still shows probable renames vs strict orphans."
            )
        lines.append("")
        if align_pairs:
            cap_p = min(5_000, _CRM_TXT_REPORT_MAX_PER_SECTION)
            ap_show = align_pairs[:cap_p] if len(align_pairs) > cap_p else align_pairs
            lines.append("-" * 80)
            lines.append(f"PROBABLE RENAME / MOVE PAIRS ({len(align_pairs)} aligned by path similarity)")
            lines.append("-" * 80)
            if len(align_pairs) > cap_p:
                lines.append(f"  (Showing first {cap_p:,} pair(s); listing capped for speed.)")
            for ro, an, sim in ap_show:
                lines.append(f"  Old (removed): {ro.relative_path}")
                lines.append(f"  New (added):   {an.relative_path}")
                lines.append(f"  Match score:   {sim:.0%} (heuristic; not a guarantee of rename)")
                lines.append("")
        if unpaired_r:
            cap = _CRM_TXT_REPORT_MAX_PER_SECTION
            ur_show = unpaired_r[:cap] if len(unpaired_r) > cap else unpaired_r
            lines.append("-" * 80)
            lines.append(
                f"UNMATCHED — REMOVED ({len(unpaired_r)}); no aligned new-only counterpart in pairing"
            )
            lines.append("-" * 80)
            if len(unpaired_r) > cap:
                lines.append(
                    f"  (Listing first {cap:,} of {len(unpaired_r):,} unmatched removed paths.)"
                )
            for r in ur_show:
                sz1 = r.size_folder1 if r.size_folder1 is not None else 0
                lines.append(f"  [-] {r.relative_path} ({sz1:,} bytes)")
                lines.append(f"      Old path: {self._full_path_old_build(r.relative_path)}")
            lines.append("")
        if unpaired_a:
            cap = _CRM_TXT_REPORT_MAX_PER_SECTION
            ua_show = unpaired_a[:cap] if len(unpaired_a) > cap else unpaired_a
            lines.append("-" * 80)
            lines.append(
                f"UNMATCHED — ADDED ({len(unpaired_a)}); no aligned old-only counterpart in pairing"
            )
            lines.append("-" * 80)
            if len(unpaired_a) > cap:
                lines.append(
                    f"  (Listing first {cap:,} of {len(unpaired_a):,} unmatched added paths.)"
                )
            for r in ua_show:
                sz2 = r.size_folder2 if r.size_folder2 is not None else 0
                lines.append(f"  [+] {r.relative_path} ({sz2:,} bytes)")
                lines.append(f"      New path: {self._full_path_new_build(r.relative_path)}")
            lines.append("")
        
        if unchanged_count > _CRM_HTML_OMIT_UNCHANGED_THRESHOLD:
            lines.append("-" * 80)
            lines.append("NOTE — HTML COMPANION REPORT AND UNCHANGED FILES")
            lines.append("-" * 80)
            lines.append(
                f"{unchanged_count:,} unchanged files are not listed in HTML (too many rows)."
            )
            lines.append(
                "Unchanged paths are also omitted from this text report for speed; "
                "only the count appears in the summary above."
            )
            lines.append("")
        
        report_added = unpaired_a
        report_removed = unpaired_r
        if report_added:
            cap = _CRM_TXT_REPORT_MAX_PER_SECTION
            add_show = report_added[:cap] if len(report_added) > cap else report_added
            lines.append("-" * 80)
            lines.append("FILES ADDED IN NEW BUILD (UNMATCHED AFTER ALIGNMENT)")
            lines.append("-" * 80)
            if len(report_added) > cap:
                lines.append(
                    f"  (Listing first {cap:,} of {len(report_added):,} unmatched added files; "
                    "truncate to keep report generation bounded.)"
                )
            lines.append("Tabular (TSV): Relative path | New build path | Size (bytes)")
            lines.append(
                "Relative path\tNew build path\tSize_bytes"
            )
            for r in add_show:
                lines.append(
                    f"{r.relative_path}\t{self._full_path_new_build(r.relative_path)}\t{r.size_folder2:,}"
                )
            lines.append("")
        
        if report_removed:
            cap = _CRM_TXT_REPORT_MAX_PER_SECTION
            rem_show = report_removed[:cap] if len(report_removed) > cap else report_removed
            lines.append("-" * 80)
            lines.append("FILES REMOVED FROM OLD BUILD (UNMATCHED AFTER ALIGNMENT)")
            lines.append("-" * 80)
            if len(report_removed) > cap:
                lines.append(
                    f"  (Listing first {cap:,} of {len(report_removed):,} unmatched removed files; "
                    "truncate to keep report generation bounded.)"
                )
            lines.append("Tabular (TSV): Relative path | Old build path | Size (bytes)")
            lines.append(
                "Relative path\tOld build path\tSize_bytes"
            )
            for r in rem_show:
                lines.append(
                    f"{r.relative_path}\t{self._full_path_old_build(r.relative_path)}\t{r.size_folder1:,}"
                )
            lines.append("")
        
        if modified:
            cap = _CRM_TXT_REPORT_MAX_PER_SECTION
            mod_show = modified[:cap] if len(modified) > cap else modified
            lines.append("-" * 80)
            lines.append("FILES MODIFIED")
            lines.append("-" * 80)
            if len(modified) > cap:
                lines.append(
                    f"  (Listing first {cap:,} of {len(modified):,} modified files; "
                    "truncate to keep report generation bounded.)"
                )
            lines.append("Tabular (TSV): Relative path | Old build path | New build path | Notes")
            lines.append(
                "Relative path\tOld build path\tNew build path\tNotes"
            )
            for r in mod_show:
                det = (r.details or "").replace("\t", " ").replace("\n", " ")
                lines.append(
                    f"{r.relative_path}\t{self._full_path_old_build(r.relative_path)}\t"
                    f"{self._full_path_new_build(r.relative_path)}\t{det}"
                )
            for r in mod_show:
                # Show deep nested archive differences if available
                if r.archive_diff:
                    lines.append(f"  Nested archive detail — {r.relative_path}:")
                    if r.archive_diff.get('only_in_old'):
                        lines.append("      📦 Nested files REMOVED from archive:")
                        for nested_file in sorted(r.archive_diff['only_in_old'])[:50]:  # Limit to 50
                            lines.append(f"         [-] {nested_file}")
                        if len(r.archive_diff['only_in_old']) > 50:
                            lines.append(f"         ... and {len(r.archive_diff['only_in_old']) - 50} more files")
                    if r.archive_diff.get('only_in_new'):
                        lines.append("      📦 Nested files ADDED to archive:")
                        for nested_file in sorted(r.archive_diff['only_in_new'])[:50]:  # Limit to 50
                            lines.append(f"         [+] {nested_file}")
                        if len(r.archive_diff['only_in_new']) > 50:
                            lines.append(f"         ... and {len(r.archive_diff['only_in_new']) - 50} more files")
                    if r.archive_diff.get('different_content'):
                        lines.append("      📦 Nested files with DIFFERENT content:")
                        for nested_file in sorted(r.archive_diff['different_content'])[:50]:  # Limit to 50
                            lines.append(f"         [≠] {nested_file}")
                        if len(r.archive_diff['different_content']) > 50:
                            lines.append(f"         ... and {len(r.archive_diff['different_content']) - 50} more files")
            lines.append("")
        
        if unchanged_count and unchanged_count <= _CRM_HTML_OMIT_UNCHANGED_THRESHOLD:
            lines.append("-" * 80)
            lines.append(f"FILES UNCHANGED IN BOTH BUILDS ({unchanged_count:,})")
            lines.append("-" * 80)
            lines.append(
                "  Unchanged paths are tracked by count only (not enumerated) for report performance."
            )
            lines.append("")
        
        for jd in self.jar_class_diff_list or []:
            lines.append("=" * 80)
            lines.append("JAR CLASS FILE DIFF (optional — saved with this report; reopen from Browse reports)")
            lines.append("=" * 80)
            lines.append(f"JAR name query: {jd.get('query', '')}")
            if jd.get("error"):
                lines.append(f"Status: ERROR — {jd['error']}")
            lines.append(f"Old build path: {jd.get('build_path_old') or self.folder1_path}")
            lines.append(f"New build path: {jd.get('build_path_new') or self.folder2_path}")
            jlo = (jd.get("jar_old_source") or "").strip() or (jd.get("jar_old") or "")
            jln = (jd.get("jar_new_source") or "").strip() or (jd.get("jar_new") or "")
            if jlo:
                lines.append(f"Primary old JAR path: {jlo}")
            if jln:
                lines.append(f"Primary new JAR path: {jln}")
            cjo = (jd.get("clfy_jar_path_old") or "").strip()
            cjn = (jd.get("clfy_jar_path_new") or "").strip()
            if cjo or cjn:
                lines.append(f"Inside ClfyAgent bundle only (old): {cjo or '—'}")
                lines.append(f"Inside ClfyAgent bundle only (new): {cjn or '—'}")
            aol = [str(x) for x in (jd.get("jar_paths_all_old") or []) if x]
            anl = [str(x) for x in (jd.get("jar_paths_all_new") or []) if x]
            if aol:
                lines.append("All matching paths (old):")
                for p in aol:
                    lines.append(f"  - {p}")
            if anl:
                lines.append("All matching paths (new):")
                for p in anl:
                    lines.append(f"  - {p}")
            if not jd.get("error") and jd.get("total_old") is not None:
                lines.append(f"Total .class in old JAR: {jd['total_old']}")
                lines.append(f"Total .class in new JAR: {jd['total_new']}")
            lines.append("")
            lim = 300
            for label, key in (
                (".class only in OLD JAR", "only_in_old"),
                (".class only in NEW JAR", "only_in_new"),
            ):
                items = jd.get(key) or []
                lines.append("-" * 80)
                lines.append(f"{label} ({len(items)})")
                lines.append("-" * 80)
                for p in items[:lim]:
                    lines.append(f"  {p}")
                if len(items) > lim:
                    lines.append(f"  ... and {len(items) - lim} more")
                lines.append("")
            mod_detail = jd.get("modified_detail")
            mod_paths = jd.get("modified") or []
            lines.append("-" * 80)
            lines.append(f".class modified (size/CRC differ) ({len(mod_paths)})")
            lines.append("-" * 80)
            if mod_detail:
                for d in mod_detail[:lim]:
                    p = d.get("path", "")
                    so, sn = d.get("size_old"), d.get("size_new")
                    co, cn = d.get("crc_old"), d.get("crc_new")
                    sz_o = f"{so:,}" if so is not None else "n/a"
                    sz_n = f"{sn:,}" if sn is not None else "n/a"
                    lines.append(f"  {p}")
                    lines.append(
                        f"      old: {sz_o} bytes, CRC {_format_zip_crc_display(co)}"
                        f"  |  new: {sz_n} bytes, CRC {_format_zip_crc_display(cn)}"
                    )
                if len(mod_detail) > lim:
                    lines.append(f"  ... and {len(mod_detail) - lim} more")
            else:
                for p in mod_paths[:lim]:
                    lines.append(f"  {p}")
                if len(mod_paths) > lim:
                    lines.append(f"  ... and {len(mod_paths) - lim} more")
            js = jd.get("java_diff_summary") or {}
            jrows = jd.get("java_diffs") or []
            lines.append("")
            lines.append("-" * 80)
            lines.append("JAVA SOURCE DIFF (CFR/JAVAP)")
            lines.append("-" * 80)
            lines.append(f"Decompiler: {js.get('decompiler', 'none')}")
            if js.get("decompiler_path"):
                lines.append(f"Decompiler path: {js.get('decompiler_path')}")
            lines.append(
                f"Processed classes: {js.get('processed', 0)} / {js.get('requested', 0)}"
                f" | Java diffs: {js.get('with_java_diff', 0)} | Errors: {js.get('errors', 0)}"
            )
            if js.get("truncated"):
                lines.append(f"Truncated: skipped {js.get('truncated')} modified classes due to cap {_CRM_JAVA_DIFF_MAX_CLASSES}")
            for r in jrows[:50]:
                lines.append(
                    f"  {r.get('path')}  (+{r.get('added_lines', 0)} / -{r.get('removed_lines', 0)}"
                    f", changed={r.get('changed_lines', 0)})"
                )
            if len(jrows) > 50:
                lines.append(f"  ... and {len(jrows) - 50} more Java diffs")
            lines.append("")
            lines.append("=" * 80)

        for jd in self.jar_manifest_diff_list or []:
            lines.append("=" * 80)
            lines.append("JAR MANIFEST IMPLEMENTATION-VERSION DIFF")
            lines.append("=" * 80)
            lines.append(f"JAR name query: {jd.get('query', '')}")
            if jd.get("error"):
                lines.append(f"Status: ERROR — {jd['error']}")
            lines.append(f"Old build path: {jd.get('build_path_old') or self.folder1_path}")
            lines.append(f"New build path: {jd.get('build_path_new') or self.folder2_path}")
            jso = (jd.get("jar_old_source") or "").strip() or (jd.get("jar_old") or "") or "—"
            jsn = (jd.get("jar_new_source") or "").strip() or (jd.get("jar_new") or "") or "—"
            lines.append(f"Old JAR (in build / SDK): {jso}")
            lines.append(f"New JAR (in build / SDK): {jsn}")
            cjo = (jd.get("clfy_jar_path_old") or "").strip()
            cjn = (jd.get("clfy_jar_path_new") or "").strip()
            if cjo or cjn:
                lines.append(f"Inside ClfyAgent bundle only (old): {cjo or '—'}")
                lines.append(f"Inside ClfyAgent bundle only (new): {cjn or '—'}")
            aol = [str(x) for x in (jd.get("jar_paths_all_old") or []) if x]
            anl = [str(x) for x in (jd.get("jar_paths_all_new") or []) if x]
            if aol:
                lines.append("All matching paths (old):")
                for p in aol:
                    lines.append(f"  - {p}")
            if anl:
                lines.append("All matching paths (new):")
                for p in anl:
                    lines.append(f"  - {p}")
            mvo = jd.get("manifest_impl_version_old") or ""
            mvn = jd.get("manifest_impl_version_new") or ""
            evo = jd.get("manifest_impl_version_error_old") or ""
            evn = jd.get("manifest_impl_version_error_new") or ""
            lines.append(f"Implementation-Version (old): {mvo or ('n/a' + (f' ({evo})' if evo else ''))}")
            lines.append(f"Implementation-Version (new): {mvn or ('n/a' + (f' ({evn})' if evn else ''))}")
            lines.append("")
            lines.append("=" * 80)
        
        if self.clfy_agent_zip_diff:
            cd = self.clfy_agent_zip_diff
            lines.append("=" * 80)
            lines.append(f"CLFY AGENT BUNDLE — {cd.get('label', CLFY_AGENT_BUNDLE_REPORT_LABEL)} (SDK; saved with this report)")
            lines.append("=" * 80)
            if cd.get("error"):
                lines.append(f"Status: ERROR — {cd['error']}")
            lines.append(f"Old build location: {cd.get('rel_old') or cd.get('zip_old') or '—'}")
            lines.append(f"New build location: {cd.get('rel_new') or cd.get('zip_new') or '—'}")
            lines.append(f"Outer path (old): {cd.get('zip_old') or '—'}")
            lines.append(f"Outer path (new): {cd.get('zip_new') or '—'}")
            if not cd.get("error") and cd.get("archive_diff"):
                ad = cd["archive_diff"]
                lines.append(
                    f"Members: old={ad.get('total_members_old', '?')}, new={ad.get('total_members_new', '?')}"
                )
                lines.append(
                    f"Summary (nested containers): only in old={cd.get('summary_only_old', 0)}, "
                    f"only in new={cd.get('summary_only_new', 0)}, "
                    f".class changed={cd.get('summary_class_changed', 0)}"
                )
                if cd.get("summary_members_only_old") is not None:
                    lines.append(
                        f"Summary (all member paths, reference): only in old={cd.get('summary_members_only_old', 0)}, "
                        f"only in new={cd.get('summary_members_only_new', 0)}"
                    )
                if ad.get("size_compare_mode"):
                    lines.append(
                        "ClfyAgent text: size-only nested container compare (naming-normalized paths; CRC / .class scan skipped)."
                    )
                else:
                    lines.append(
                        "ClfyAgent text: nested .jar/.war/.ear/.zip paths only (not full file tree). "
                        "Containers with same path but different bytes are omitted here — use optional JAR class / manifest in CRM full."
                    )
                size_rows = ad.get("jar_size_diff_rows") or []
                if size_rows:
                    lines.append("-" * 80)
                    lines.append(
                        f"Container size differences ({len(size_rows)}) — old bytes -> new bytes (% change)"
                    )
                    lines.append("-" * 80)
                    for r in size_rows[:200]:
                        lines.append(
                            f"  {r.get('path')}: {int(r.get('size_old') or 0):,} -> "
                            f"{int(r.get('size_new') or 0):,} ({float(r.get('pct_change') or 0):+.1f}%)"
                            + (" [significant]" if r.get("significant") else "")
                        )
                    if len(size_rows) > 200:
                        lines.append(f"  ... and {len(size_rows) - 200} more")
                    lines.append("")
                cidx = self._clfy_archive_diff_container_index(ad)
                m = cidx.get("metrics_ext") or {}
                lines.append("-" * 80)
                lines.append("Nested JAR / WAR / EAR / ZIP (counts) — only in old / only in new")
                lines.append("-" * 80)
                for ex, lab in ((".jar", "JAR"), (".war", "WAR"), (".ear", "EAR"), (".zip", "ZIP")):
                    row = m.get(ex) or {}
                    lines.append(
                        f"  {lab}:  only_old={row.get('oo', 0)}  only_new={row.get('on', 0)}"
                    )
                lim = 200
                bs = cidx.get("by_status") or {}
                for title, key in (
                    ("Container members only in OLD", "only_old"),
                    ("Container members only in NEW", "only_new"),
                ):
                    items = sorted(bs.get(key) or [])
                    lines.append("-" * 80)
                    lines.append(f"{title} ({len(items)})")
                    lines.append("-" * 80)
                    for p in items[:lim]:
                        lines.append(f"  {p}")
                    if len(items) > lim:
                        lines.append(f"  ... and {len(items) - lim} more")
                    lines.append("")
            lines.append("")
            lines.append("=" * 80)
        
        lines.append("=" * 80)
        lines.append("END OF CRM FULL CONTENT COMPARISON REPORT")
        lines.append("=" * 80)

        n_lines = len(lines)
        if n_lines > 200_000:
            print(f"[CRM] Text report: writing {n_lines:,} lines (chunked)…")
            sys.stdout.flush()
            step = 50_000
            for i in range(0, n_lines, step):
                out.write("\n".join(lines[i : i + step]))
                if i + step < n_lines:
                    out.write("\n")
            print("[CRM] Text report: write complete.")
            sys.stdout.flush()
            return
        out.write("\n".join(lines))

    @staticmethod
    def _format_size_bytes_report(size_bytes: Optional[int]) -> str:
        """Human-readable size for HTML/text reports (matches generate_html_report helper)."""
        if size_bytes is None:
            return "N/A"
        if size_bytes < 1024:
            return f"{size_bytes} B"
        if size_bytes < 1024 * 1024:
            return f"{size_bytes / 1024:.1f} KB"
        if size_bytes < 1024 * 1024 * 1024:
            return f"{size_bytes / (1024 * 1024):.2f} MB"
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"

    @staticmethod
    def _is_class_file_path(relative_path: str) -> bool:
        """True for top-level or nested paths ending in .class (e.g. archive!/Foo.class)."""
        return str(relative_path).lower().endswith(".class")

    @staticmethod
    def _path_looks_like_java_archive(rel: str) -> bool:
        """True if path ends in .jar / .war / .ear (nested zip paths like a!/b.jar included)."""
        s = str(rel or "").replace("\\", "/").strip().lower()
        return bool(s) and s.endswith((".jar", ".war", ".ear"))

    @staticmethod
    def _member_path_kind(member_path: str) -> str:
        """Classify a member path inside an archive for reporting."""
        s = str(member_path or "").replace("\\", "/").strip().lower()
        if not s:
            return "file"
        if s.endswith(".class"):
            return "class"
        if s.endswith((".jar", ".war", ".ear", ".zip")):
            return "archive"
        if s.endswith("/") or "/" not in s.rstrip("/"):
            return "folder" if s.endswith("/") else "file"
        return "file"

    @staticmethod
    def _inner_member_row(host: str, member: str, side: str) -> dict:
        host = _crm_norm_rel_path(str(host or "")).strip()
        ms = _crm_norm_rel_path(str(member or "")).lstrip("/")
        path = f"{host}!/{ms}" if host else ms
        return {
            "host": host,
            "member": ms,
            "path": path,
            "name": CRMFolderComparer._path_leaf_name_static(ms),
            "kind": CRMFolderComparer._member_path_kind(ms),
            "side": side,
        }

    @staticmethod
    def _path_leaf_name_static(path_value: str) -> str:
        s = _crm_norm_rel_path(path_value).rstrip("/")
        if "!/" in s:
            s = s.split("!/", 1)[-1]
        return s.rsplit("/", 1)[-1] if "/" in s else s

    @staticmethod
    def _dedupe_inner_member_rows(rows: list[dict]) -> list[dict]:
        seen: set[str] = set()
        out: list[dict] = []
        for r in rows:
            key = str(r.get("path") or "").lower()
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(r)
        return out

    def _inner_member_exists_on_other_build(
        self, ad: dict, member_path: str, side: str
    ) -> bool:
        """True when the same inner-jar file signature exists on the opposite build."""
        sig = self._jar_inner_file_signature(member_path)
        if not sig:
            return False
        if side == "old":
            other = set(ad.get("all_member_sig_keys_new") or [])
        else:
            other = set(ad.get("all_member_sig_keys_old") or [])
        if sig in other:
            return True
        return any(self._nested_path_is_strict_prefix(sig, sk) for sk in other)

    def _collect_inner_jar_member_mismatches(
        self,
        modified: list[CRMComparisonResult],
        *,
        side: str,
    ) -> list[dict]:
        """
        Folder / class / file members inside JARs that differ when the outer archive exists on both builds.
        Excludes whole JAR/WAR/EAR containers (those appear in the archive mismatch section).
        """
        if side not in {"old", "new"}:
            return []
        rows: list[dict] = []
        for r in modified:
            ad = r.archive_diff or {}
            parent = _crm_norm_rel_path(r.relative_path)
            oo, on, _vp = self._reconcile_only_in_member_sets(
                set(ad.get("only_in_old") or []),
                set(ad.get("only_in_new") or []),
            )
            opp_keys = {self._member_match_key(m) for m in (on if side == "old" else oo)}
            for m in sorted(oo if side == "old" else on):
                ms = _crm_norm_rel_path(str(m)).lstrip("/")
                if not ms:
                    continue
                if self._is_phantom_archive_member_path(ms):
                    continue
                if self._clfy_member_is_container_archive(ms):
                    continue
                if self._member_match_key(ms) in opp_keys:
                    continue
                if self._inner_member_exists_on_other_build(ad, ms, side):
                    continue
                if not self._verify_archive_diff_member(r, ms, side):
                    continue
                rows.append(self._inner_member_row(parent, ms, side))
        czd = self.clfy_agent_zip_diff or {}
        if not czd.get("error"):
            cad = (czd.get("archive_diff") or {}) if isinstance(czd.get("archive_diff"), dict) else {}
            container_keys = {
                _crm_norm_rel_path(str(p)).lower()
                for ck in ("container_only_old", "container_only_new")
                for p in (cad.get(ck) or [])
            }
            clfy_anchor = (
                str(czd.get("rel_old" if side == "old" else "rel_new") or "").strip()
                or str(czd.get("zip_old" if side == "old" else "zip_new") or "").strip()
                or str(czd.get("label") or CLFY_AGENT_BUNDLE_REPORT_LABEL)
            )
            clfy_anchor = _crm_norm_rel_path(clfy_anchor)
            oo, on, _vp = self._reconcile_only_in_member_sets(
                set(cad.get("only_in_old") or []),
                set(cad.get("only_in_new") or []),
            )
            opp_keys = {self._member_match_key(m) for m in (on if side == "old" else oo)}
            for m in sorted(oo if side == "old" else on):
                ms = _crm_norm_rel_path(str(m)).lstrip("/")
                if not ms:
                    continue
                if self._is_phantom_archive_member_path(ms):
                    continue
                if ms.lower() in container_keys:
                    continue
                if self._clfy_member_is_container_archive(ms):
                    continue
                if self._member_match_key(ms) in opp_keys:
                    continue
                if self._inner_member_exists_on_other_build(cad, ms, side):
                    continue
                if not self._verify_clfy_archive_diff_member(czd, ms, side):
                    continue
                rows.append(self._inner_member_row(clfy_anchor, ms, side))
        return self._dedupe_inner_member_rows(rows)

    @staticmethod
    def _jar_mismatch_display_rows(jar_pool: list[str]) -> list[dict]:
        """One row per archive path with leaf name, for the container mismatch table."""
        seen: set[str] = set()
        rows: list[dict] = []
        for p in jar_pool:
            norm = _crm_norm_rel_path(str(p)).strip()
            if not norm:
                continue
            key = norm.lower()
            if key in seen:
                continue
            seen.add(key)
            rows.append({"path": norm, "name": CRMFolderComparer._path_leaf_name_static(norm)})
        return rows

    def _crm_collect_release_fragments(self, texts: list[str]) -> tuple[set[str], set[str]]:
        dirs: set[str] = set()
        pkgs: set[str] = set()
        for raw in texts:
            s = _crm_norm_rel_path(str(raw or ""))
            if not s:
                continue
            for m in _CRM_RELEASE_DIR_RE.finditer(s):
                dirs.add(self._crm_normalize_release_dir_token(m.group(0)))
            for m in _CRM_RELEASE_PKG_RE.finditer(s):
                pkgs.add(m.group(0))
        return dirs, pkgs

    @staticmethod
    def _crm_normalize_release_dir_token(token: str) -> str:
        """CRM_10.24.1.0 (SDK filename) → CRM_10.24 to match inner tree folders."""
        t = str(token or "").strip()
        m = re.fullmatch(r"CRM_(\d+)\.(\d+)(?:[._]\d+)*", t, re.IGNORECASE)
        if m:
            return f"CRM_{m.group(1)}.{m.group(2)}"
        return t

    @staticmethod
    def _crm_sdk_release_dir_token(path: str) -> str:
        """
        Infer inner CRM release folder from an SDK .jar path or UNC share segment.
        SDK_CRM_10.24.1.0_linux64… → CRM_10.24; SDK_CRM_10.25_linux64… → CRM_10.25.
        """
        s = str(path or "").replace("\\", "/")
        base = os.path.basename(s)
        m = _CRM_SDK_RELEASE_DIR_RE.search(base)
        if m:
            return f"CRM_{m.group(1)}.{m.group(2)}"
        m = _CRM_SDK_UNC_RELEASE_RE.search(s)
        if m:
            return f"CRM_{m.group(1)}.{m.group(2)}"
        return ""

    @staticmethod
    def _crm_dir_token_from_pkg(pkg: str) -> str:
        """CRM10.24.1_ALL -> CRM_10.24; CRM10.25.0_ALL -> CRM_10.25."""
        m = re.search(r"CRM(\d+)\.(\d+)", str(pkg or ""), re.IGNORECASE)
        if not m:
            return ""
        return f"CRM_{m.group(1)}.{m.group(2)}"

    @staticmethod
    def _crm_sdk_release_pkg_token(path: str) -> str:
        """SDK_CRM_10.24.1.0 → CRM10.24.1_ALL; SDK_CRM_10.25 → CRM10.25.0_ALL."""
        base = os.path.basename(str(path or "").replace("\\", "/"))
        m = re.search(r"SDK_CRM_(\d+)\.(\d+)(?:\.(\d+))?", base, re.IGNORECASE)
        if not m:
            return ""
        major, minor, patch = m.group(1), m.group(2), m.group(3)
        if patch:
            return f"CRM{major}.{minor}.{patch}_ALL"
        return f"CRM{major}.{minor}.0_ALL"

    def _crm_infer_release_replacements(self) -> list[tuple[str, str]]:
        """Map old CRM folder tokens (CRM_10.24, CRM10.24.1_ALL) to new-build equivalents."""
        cached = getattr(self, "_crm_cached_release_replacements", None)
        if cached is not None:
            return cached
        old_texts: list[str] = []
        new_texts: list[str] = []
        for r in self.results or []:
            rp = str(r.relative_path or "")
            if " -> " in rp:
                old_texts.append(rp.split(" -> ", 1)[0].strip())
                new_texts.append(rp.split(" -> ", 1)[1].strip())
            elif r.status == "removed":
                old_texts.append(rp)
            elif r.status == "added":
                new_texts.append(rp)
            elif r.status in ("modified", "unchanged"):
                old_texts.append(rp)

        scan = getattr(self, "_crm_scan_paths_for_inference", None)
        if scan:
            f1_map, f2_map = scan
            for p in list(f1_map.keys())[:800]:
                s = str(p).strip()
                if s:
                    old_texts.append(s)
            for p in list(f2_map.keys())[:800]:
                s = str(p).strip()
                if s:
                    new_texts.append(s)

        old_dirs, old_pkgs = self._crm_collect_release_fragments(old_texts)
        new_dirs, new_pkgs = self._crm_collect_release_fragments(new_texts)
        old_sdk_dir = self._crm_sdk_release_dir_token(str(self.folder1_path))
        new_sdk_dir = self._crm_sdk_release_dir_token(str(self.folder2_path))
        if old_sdk_dir:
            old_dirs.add(old_sdk_dir)
        if new_sdk_dir:
            new_dirs.add(new_sdk_dir)
        old_sdk_pkg = self._crm_sdk_release_pkg_token(str(self.folder1_path))
        new_sdk_pkg = self._crm_sdk_release_pkg_token(str(self.folder2_path))
        if old_sdk_pkg:
            old_pkgs.add(old_sdk_pkg)
        if new_sdk_pkg:
            new_pkgs.add(new_sdk_pkg)
        only_old_dirs = old_dirs - new_dirs
        only_new_dirs = new_dirs - old_dirs
        only_old_pkgs = old_pkgs - new_pkgs
        only_new_pkgs = new_pkgs - old_pkgs

        pairs: list[tuple[str, str]] = []
        if len(only_old_dirs) == 1 and len(only_new_dirs) == 1:
            pairs.append((sorted(only_old_dirs)[0], sorted(only_new_dirs)[0]))
        elif len(old_dirs) == 1 and len(new_dirs) == 1 and old_dirs != new_dirs:
            pairs.append((sorted(old_dirs)[0], sorted(new_dirs)[0]))

        if len(only_old_pkgs) == 1 and len(only_new_pkgs) == 1:
            pairs.append((sorted(only_old_pkgs)[0], sorted(only_new_pkgs)[0]))
        elif len(old_pkgs) == 1 and len(new_pkgs) == 1 and old_pkgs != new_pkgs:
            pairs.append((sorted(old_pkgs)[0], sorted(new_pkgs)[0]))

        # When new-side paths still use the old release dir (CRM_10.24) but the package
        # token changed (CRM10.25.0_ALL), dir pairing above fails — derive dir from pkg pair.
        seen_old_frags = {p[0].lower() for p in pairs}
        for old_frag, new_frag in list(pairs):
            if not _CRM_RELEASE_PKG_RE.fullmatch(old_frag or ""):
                continue
            old_dir = self._crm_dir_token_from_pkg(old_frag)
            new_dir = self._crm_dir_token_from_pkg(new_frag)
            if (
                old_dir
                and new_dir
                and old_dir.lower() != new_dir.lower()
                and old_dir.lower() not in seen_old_frags
            ):
                pairs.append((old_dir, new_dir))
                seen_old_frags.add(old_dir.lower())

        pairs.sort(key=lambda x: len(x[0]), reverse=True)
        self._crm_cached_release_replacements = pairs
        return pairs

    def _crm_build_files2_release_index(self, files_map: dict) -> dict[str, list[str]]:
        """Index new-build paths by CRM release-neutral signature for cross-release pairing."""
        idx: dict[str, list[str]] = defaultdict(list)
        for k in files_map:
            sig = self._crm_neutralize_path_for_alignment(str(k)).lower()
            if sig:
                idx[sig].append(str(k))
        for sig in idx:
            idx[sig].sort(key=lambda x: x.lower())
        return idx

    def _crm_build_files2_jar_key_index(self, files_map: dict) -> dict[str, list[str]]:
        """Index new-build JAR paths by logical aggregate key (version-neutral basename)."""
        idx: dict[str, list[str]] = defaultdict(list)
        for k in files_map:
            leaf = self._path_leaf_name_static(str(k))
            if not self._is_jar_leaf_filename(leaf):
                continue
            jk = self._logical_jar_aggregate_key(leaf)
            if jk:
                idx[jk].append(str(k))
        for jk in idx:
            idx[jk].sort(key=lambda x: x.lower())
        return idx

    def _crm_jar_lib_tail_signature(self, path: str) -> str:
        """
        Pair JARs when only the prefix before lib/ moved (e.g. lib/foo.jar vs modules/crm/lib/foo.jar).
        Uses the last /lib/ segment plus a version-neutral leaf name.
        """
        s = _crm_norm_rel_path(str(path or "")).strip()
        if not s:
            return s
        for old_frag, new_frag in self._crm_infer_release_replacements():
            s = re.sub(re.escape(old_frag), _CRM_RELEASE_NEUTRAL_DIR, s, flags=re.IGNORECASE)
            s = re.sub(re.escape(new_frag), _CRM_RELEASE_NEUTRAL_DIR, s, flags=re.IGNORECASE)
        leaf = self._path_leaf_name_static(s)
        if not self._is_jar_leaf_filename(leaf):
            return s.lower()
        logical_leaf = self._logical_jar_display_label(leaf)
        low = s.lower()
        lib_pos = low.rfind("/lib/")
        if lib_pos >= 0:
            tail = s[lib_pos + 1 :]
            return self._crm_replace_path_leaf(tail, logical_leaf).lower()
        return self._crm_replace_path_leaf(s, logical_leaf).lower()

    def _crm_pick_best_jar_counterpart(
        self, old_path: str, mapped_new: str, candidates: list[str]
    ) -> Optional[str]:
        """Choose one new-build JAR path when several share the same logical basename."""
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]
        old_tail = self._crm_jar_lib_tail_signature(old_path)
        if old_tail:
            tail_hits = [
                c for c in candidates if self._crm_jar_lib_tail_signature(c) == old_tail
            ]
            if len(tail_hits) == 1:
                return tail_hits[0]
        mapped_l = _crm_norm_rel_path(mapped_new or "").lower()
        if mapped_l:
            exact = [c for c in candidates if _crm_norm_rel_path(c).lower() == mapped_l]
            if len(exact) == 1:
                return exact[0]
        return None

    def _crm_find_release_aligned_counterpart(
        self,
        files_map: dict,
        index: dict[str, list[str]],
        old_path: str,
        *,
        jar_key_index: Optional[dict[str, list[str]]] = None,
    ) -> Optional[str]:
        """
        Locate the new-build file matching an old-build path across CRM release folders
        (CRM_10.24/CRM10.24.1_ALL ↔ CRM_10.25/CRM10.25.0_ALL) and version suffixes.
        """
        hit = _crm_find_counterpart_key(files_map, old_path)
        if hit:
            return hit
        mapped = self._crm_map_path_across_release(old_path, to_new=True)
        if mapped and mapped.lower() != str(old_path or "").lower():
            hit = _crm_find_counterpart_key(files_map, mapped)
            if hit:
                return hit
        sig = self._crm_neutralize_path_for_alignment(old_path).lower()
        cands = list(index.get(sig) or [])
        if len(cands) == 1:
            return cands[0]
        if cands and mapped:
            ml = _crm_norm_rel_path(mapped).lower()
            for c in cands:
                if _crm_norm_rel_path(c).lower() == ml:
                    return c
        if cands:
            old_leaf = self._path_leaf_name_static(old_path).lower()
            leaf_hits = [
                c for c in cands if self._path_leaf_name_static(c).lower() == old_leaf
            ]
            if len(leaf_hits) == 1:
                return leaf_hits[0]
        if self._is_jar_leaf_filename(self._path_leaf_name_static(old_path)):
            jk = self._logical_jar_aggregate_key(self._path_leaf_name_static(old_path))
            jar_cands = list((jar_key_index or {}).get(jk) or [])
            if jar_cands:
                picked = self._crm_pick_best_jar_counterpart(old_path, mapped, jar_cands)
                if picked:
                    return picked
        return None

    @staticmethod
    def _crm_pair_result_rel_path(orig1: Optional[str], orig2: Optional[str]) -> str:
        """Result relative_path: single path when identical, else old -> new."""
        if orig1 and orig2:
            if _crm_norm_rel_path(orig1).lower() != _crm_norm_rel_path(orig2).lower():
                return f"{orig1} -> {orig2}"
            return orig1
        return str(orig1 or orig2 or "")

    def _crm_map_path_across_release(self, path: str, *, to_new: bool) -> str:
        s = _crm_norm_rel_path(str(path or "")).strip()
        if not s:
            return s
        for old_frag, new_frag in self._crm_infer_release_replacements():
            if to_new:
                s = re.sub(re.escape(old_frag), new_frag, s, flags=re.IGNORECASE)
            else:
                s = re.sub(re.escape(new_frag), old_frag, s, flags=re.IGNORECASE)
        return s

    @staticmethod
    def _crm_replace_path_leaf(path: str, new_leaf: str) -> str:
        s = _crm_norm_rel_path(str(path or "")).strip()
        if not s:
            return s
        if "!/" in s:
            host, member = s.split("!/", 1)
            parts = [p for p in member.split("/") if p]
            if parts:
                parts[-1] = new_leaf
                return f"{host}!/{'/'.join(parts)}"
            return f"{host}!/{new_leaf}"
        parts = [p for p in s.split("/") if p]
        if parts:
            parts[-1] = new_leaf
            return "/".join(parts)
        return new_leaf

    def _crm_ensure_scan_indexes(self) -> None:
        """Rebuild release/JAR lookup indexes from last scan maps (for report generation)."""
        files2 = getattr(self, "_crm_last_files2", None)
        if isinstance(files2, dict) and files2:
            self._crm_files2_release_index = self._crm_build_files2_release_index(files2)
            self._crm_files2_jar_key_index = self._crm_build_files2_jar_key_index(files2)
        files1 = getattr(self, "_crm_last_files1", None)
        if isinstance(files1, dict) and files1:
            self._crm_files1_release_index = self._crm_build_files2_release_index(files1)
            self._crm_files1_jar_key_index = self._crm_build_files2_jar_key_index(files1)

    @staticmethod
    def _crm_paths_from_result_for_side(r: CRMComparisonResult, side: str) -> list[str]:
        """Old or new relative path(s) represented by one comparison result row."""
        rp = _crm_norm_rel_path(str(r.relative_path or ""))
        if not rp:
            return []
        if " -> " in rp:
            old_p, new_p = (p.strip() for p in rp.split(" -> ", 1))
            if side == "old":
                return [old_p] if old_p else []
            return [new_p] if new_p else []
        if side == "old" and r.status in ("removed", "modified", "unchanged"):
            return [rp]
        if side == "new" and r.status in ("added", "modified", "unchanged"):
            return [rp]
        return []

    def _crm_scanned_tree_file_counts(self) -> tuple[int, int]:
        """Files discovered while scanning each build root (SDK jar or folder)."""
        n_old = int(getattr(self, "_crm_scan_file_count_old", 0) or 0)
        n_new = int(getattr(self, "_crm_scan_file_count_new", 0) or 0)
        if not n_old:
            f1 = getattr(self, "_crm_last_files1", None)
            if isinstance(f1, dict):
                n_old = len(f1)
        if not n_new:
            f2 = getattr(self, "_crm_last_files2", None)
            if isinstance(f2, dict):
                n_new = len(f2)
        return n_old, n_new

    def _crm_jar_exists_on_new_build(self, old_path: str) -> bool:
        found = self._crm_expected_new_path_for_old(old_path)
        return bool(found) and not self._crm_path_is_absent_placeholder(found)

    def _crm_expected_new_path_for_old(self, old_path: str) -> str:
        """New-build path for an old-only path; uses scan index before falling back to …/absent."""
        self._crm_ensure_scan_indexes()
        files2 = getattr(self, "_crm_last_files2", None)
        if isinstance(files2, dict) and files2:
            idx = getattr(self, "_crm_files2_release_index", None) or self._crm_build_files2_release_index(
                files2
            )
            jar_idx = getattr(self, "_crm_files2_jar_key_index", None) or self._crm_build_files2_jar_key_index(
                files2
            )
            found = self._crm_find_release_aligned_counterpart(
                files2, idx, old_path, jar_key_index=jar_idx
            )
            if found:
                return found
        mapped = self._crm_map_path_across_release(old_path, to_new=True)
        return self._crm_replace_path_leaf(mapped, _CRM_ABSENT_NEW_LEAF)

    def _crm_expected_old_path_for_new(self, new_path: str) -> str:
        self._crm_ensure_scan_indexes()
        files1 = getattr(self, "_crm_last_files1", None)
        if isinstance(files1, dict) and files1:
            idx = getattr(self, "_crm_files1_release_index", None) or self._crm_build_files2_release_index(
                files1
            )
            jar_idx = getattr(self, "_crm_files1_jar_key_index", None) or self._crm_build_files2_jar_key_index(
                files1
            )
            mapped_probe = self._crm_map_path_across_release(new_path, to_new=False)
            found = self._crm_find_release_aligned_counterpart(
                files1,
                idx,
                mapped_probe or new_path,
                jar_key_index=jar_idx,
            )
            if found:
                return found
        mapped = self._crm_map_path_across_release(new_path, to_new=False)
        return self._crm_replace_path_leaf(mapped, _CRM_ABSENT_NEW_LEAF)

    @staticmethod
    def _crm_normalize_pkg_layout_tail(s: str) -> str:
        """
        After CRM release tokens are neutralized, align package layout variants:
        lib/… ↔ modules/crm/lib/… (common CRM SDK layout change between releases).
        """
        low = str(s or "").lower()
        marker = f"{_CRM_RELEASE_NEUTRAL_DIR}/{_CRM_RELEASE_NEUTRAL_PKG}/"
        pos = low.find(marker.lower())
        if pos < 0:
            return low
        prefix = s[: pos + len(marker)]
        tail = s[pos + len(marker) :].lstrip("/")
        tl = tail.lower()
        if tl.startswith("modules/crm/lib/"):
            tail = "lib/" + tail[len("modules/crm/lib/") :]
        return (prefix + tail).lower()

    def _crm_neutralize_path_for_alignment(self, path: str) -> str:
        """Alignment key: same logical file across CRM_10.24 vs CRM_10.25 folder layouts."""
        s = _crm_norm_rel_path(str(path or "")).strip()
        if not s:
            return s
        leaf = self._path_leaf_name_static(s)
        if self._is_jar_leaf_filename(leaf):
            return self._crm_jar_lib_tail_signature(s)
        for old_frag, new_frag in self._crm_infer_release_replacements():
            s = re.sub(re.escape(old_frag), _CRM_RELEASE_NEUTRAL_DIR, s, flags=re.IGNORECASE)
            s = re.sub(re.escape(new_frag), _CRM_RELEASE_NEUTRAL_DIR, s, flags=re.IGNORECASE)
        return self._crm_normalize_pkg_layout_tail(s)

    def _crm_align_removed_added_for_report(
        self,
        removed: list[CRMComparisonResult],
        added: list[CRMComparisonResult],
        *,
        min_ratio: float = 0.35,
    ) -> tuple[
        list[tuple[CRMComparisonResult, CRMComparisonResult, float]],
        list[CRMComparisonResult],
        list[CRMComparisonResult],
    ]:
        """Pair removed/added rows using CRM release-neutral path signatures."""
        return _crm_align_removed_added(
            removed,
            added,
            min_ratio=min_ratio,
            signature_fn=self._crm_neutralize_path_for_alignment,
        )

    @staticmethod
    def _crm_path_row_is_true_orphan(row: dict) -> bool:
        """True when exactly one build column is a real path (not a rename/move pair)."""
        op = str(row.get("old_path") or "").strip()
        np = str(row.get("new_path") or "").strip()
        old_miss = not op or CRMFolderComparer._crm_path_is_absent_placeholder(op)
        new_miss = not np or CRMFolderComparer._crm_path_is_absent_placeholder(np)
        return old_miss != new_miss

    @staticmethod
    def _crm_path_rows_side_delta(orphan_rows: list[dict]) -> int:
        only_old = 0
        only_new = 0
        for row in orphan_rows:
            op = str(row.get("old_path") or "").strip()
            np = str(row.get("new_path") or "").strip()
            if op and not CRMFolderComparer._crm_path_is_absent_placeholder(op):
                if not np or CRMFolderComparer._crm_path_is_absent_placeholder(np):
                    only_old += 1
            elif np and not CRMFolderComparer._crm_path_is_absent_placeholder(np):
                only_new += 1
        return only_old - only_new

    def _crm_jar_side_by_side_signature(self, path: str) -> str:
        """Pair JAR/WAR/EAR paths across CRM release folders and version suffixes."""
        leaf = self._path_leaf_name_static(str(path or ""))
        if self._is_jar_leaf_filename(leaf):
            return self._crm_jar_lib_tail_signature(str(path or ""))
        s = _crm_norm_rel_path(str(path or "")).strip()
        if not s:
            return s
        for old_frag, new_frag in self._crm_infer_release_replacements():
            s = re.sub(re.escape(old_frag), _CRM_RELEASE_NEUTRAL_DIR, s, flags=re.IGNORECASE)
            s = re.sub(re.escape(new_frag), _CRM_RELEASE_NEUTRAL_DIR, s, flags=re.IGNORECASE)
        return self._archive_signature_path(s)

    @staticmethod
    def _crm_path_is_absent_placeholder(path: str) -> bool:
        leaf = CRMFolderComparer._path_leaf_name_static(str(path or ""))
        return leaf.lower() == _CRM_ABSENT_NEW_LEAF.lower()

    def _crm_align_for_html_report(
        self,
        removed: list[CRMComparisonResult],
        added: list[CRMComparisonResult],
    ) -> tuple[
        list[tuple[CRMComparisonResult, CRMComparisonResult, float]],
        list[CRMComparisonResult],
        list[CRMComparisonResult],
    ]:
        """Rename alignment for HTML only — tighter budget than text report alignment."""
        n_r, n_a = len(removed), len(added)
        if n_r and n_a and n_r * n_a > _CRM_HTML_ALIGN_MAX_PAIR_PRODUCT:
            logger.warning(
                "[CRM] HTML: greedy removed/added alignment budget exceeded ("
                f"{n_r:,} × {n_a:,} > {_CRM_HTML_ALIGN_MAX_PAIR_PRODUCT:,}); "
                "using release-neutral signature bucket pairing."
            )
            return _crm_align_removed_added_signature_bucket(
                removed,
                added,
                signature_fn=self._crm_neutralize_path_for_alignment,
            )
        return self._crm_align_removed_added_for_report(removed, added, min_ratio=0.35)

    def _crm_fast_expected_new_path_for_old(self, old_path: str) -> str:
        """Map old-only path to new build; use scan index when available."""
        self._crm_ensure_scan_indexes()
        files2 = getattr(self, "_crm_last_files2", None)
        if isinstance(files2, dict) and files2:
            idx = getattr(self, "_crm_files2_release_index", None) or self._crm_build_files2_release_index(
                files2
            )
            jar_idx = getattr(self, "_crm_files2_jar_key_index", None) or self._crm_build_files2_jar_key_index(
                files2
            )
            found = self._crm_find_release_aligned_counterpart(
                files2, idx, old_path, jar_key_index=jar_idx
            )
            if found:
                return found
        mapped = self._crm_map_path_across_release(old_path, to_new=True)
        return self._crm_replace_path_leaf(mapped, _CRM_ABSENT_NEW_LEAF)

    def _crm_fast_expected_old_path_for_new(self, new_path: str) -> str:
        self._crm_ensure_scan_indexes()
        files1 = getattr(self, "_crm_last_files1", None)
        if isinstance(files1, dict) and files1:
            idx = getattr(self, "_crm_files1_release_index", None) or self._crm_build_files2_release_index(
                files1
            )
            jar_idx = getattr(self, "_crm_files1_jar_key_index", None) or self._crm_build_files2_jar_key_index(
                files1
            )
            mapped_probe = self._crm_map_path_across_release(new_path, to_new=False)
            found = self._crm_find_release_aligned_counterpart(
                files1,
                idx,
                mapped_probe or new_path,
                jar_key_index=jar_idx,
            )
            if found:
                return found
        mapped = self._crm_map_path_across_release(new_path, to_new=False)
        return self._crm_replace_path_leaf(mapped, _CRM_ABSENT_NEW_LEAF)

    def _align_side_by_side_path_rows(
        self,
        old_paths: list[str],
        new_paths: list[str],
        *,
        signature_fn: Optional[Callable[[str], str]] = None,
    ) -> list[dict]:
        """
        Build rows with old_path, new_path, and name for side-by-side mismatch tables.
        Pairs by logical signature (CRM release-neutral + JAR version-neutral).
        Old-only rows get an expected new path (…/absent) in the new-build folder layout.
        """
        sig = signature_fn or self._crm_neutralize_path_for_alignment
        fast_paths = len(old_paths) + len(new_paths) >= _CRM_ALIGN_SIDE_BY_SIDE_FAST_MAX_PATHS
        old_map: dict[str, list[str]] = {}
        new_map: dict[str, list[str]] = {}
        for p in old_paths:
            norm = _crm_norm_rel_path(str(p)).strip()
            if not norm:
                continue
            old_map.setdefault(sig(norm), []).append(norm)
        for p in new_paths:
            norm = _crm_norm_rel_path(str(p)).strip()
            if not norm:
                continue
            new_map.setdefault(sig(norm), []).append(norm)
        for dct in (old_map, new_map):
            for k in dct:
                dct[k].sort(key=lambda x: x.lower())
        rows: list[dict] = []
        for k in sorted(set(old_map.keys()) | set(new_map.keys())):
            o = list(old_map.get(k) or [])
            n = list(new_map.get(k) or [])
            while o and n:
                op, np = o.pop(0), n.pop(0)
                np = self._crm_map_path_across_release(np, to_new=True)
                name = self._path_leaf_name_static(op or np)
                rows.append({"old_path": op, "new_path": np, "name": name, "path": op or np})
            for op in o:
                name = self._path_leaf_name_static(op)
                exp_new = (
                    self._crm_fast_expected_new_path_for_old(op)
                    if fast_paths
                    else self._crm_expected_new_path_for_old(op)
                )
                rows.append(
                    {
                        "old_path": op,
                        "new_path": exp_new,
                        "name": name,
                        "path": op,
                    }
                )
            for np in n:
                name = self._path_leaf_name_static(np)
                exp_old = (
                    self._crm_fast_expected_old_path_for_new(np)
                    if fast_paths
                    else self._crm_expected_old_path_for_new(np)
                )
                rows.append(
                    {
                        "old_path": exp_old,
                        "new_path": np,
                        "name": name,
                        "path": np,
                    }
                )
        rows.sort(key=lambda r: (r.get("name") or "").lower())
        return rows

    @staticmethod
    def _html_side_by_side_path_cell(path: str, *, missing: bool) -> str:
        td = (
            "padding:8px 10px;border-bottom:1px solid #e2e8f0;"
            "font-family:Consolas,monospace;font-size:12px;word-break:break-word;"
        )
        if not path:
            return (
                f"<td style='{td}background:{_CRM_MISSING_CELL_BG};color:{_CRM_MISSING_DARK_RED};"
                f"font-weight:700;text-align:center;'>—</td>"
            )
        esc = html.escape(path)
        if missing:
            return (
                f"<td style='{td}color:{_CRM_MISSING_DARK_RED};font-weight:700;"
                f"background:{_CRM_MISSING_CELL_BG};'>{esc}</td>"
            )
        return f"<td style='{td}color:#0f172a;'>{esc}</td>"

    @staticmethod
    def _html_side_by_side_name_cell(name: str, *, highlight: bool) -> str:
        td = "padding:8px 10px;border-bottom:1px solid #e2e8f0;font-size:12px;"
        esc = html.escape(name or "—")
        if highlight:
            return (
                f"<td style='{td}color:{_CRM_MISSING_DARK_RED};font-weight:700;"
                f"background:{_CRM_MISSING_CELL_BG};'>{esc}</td>"
            )
        return f"<td style='{td}color:#334155;font-weight:600;'>{esc}</td>"

    @staticmethod
    def _html_bc_style_path_cell(
        path: str,
        *,
        kind: str,
        missing: bool = False,
    ) -> str:
        """Beyond Compare–style cell: blue=version mismatch, red=missing on old, green=missing on new."""
        td = (
            "padding:8px 10px;border-bottom:1px solid #e2e8f0;"
            "font-family:Consolas,monospace;font-size:12px;word-break:break-word;"
        )
        if not path or CRMFolderComparer._crm_path_is_absent_placeholder(path):
            bg = _CRM_BC_MISSING_OLD_BG if kind == "old" else _CRM_BC_MISSING_NEW_BG
            fg = _CRM_MISSING_DARK_RED
            label = "—" if not path else html.escape(path)
            return (
                f"<td style='{td}background:{bg};color:{fg};font-weight:700;'>{label}</td>"
            )
        esc = html.escape(path)
        if kind == "version":
            return (
                f"<td style='{td}background:{_CRM_BC_VERSION_MISMATCH_BG};"
                f"color:{_CRM_BC_VERSION_MISMATCH_FG};font-weight:600;'>{esc}</td>"
            )
        if missing:
            bg = _CRM_BC_MISSING_OLD_BG if kind == "old" else _CRM_BC_MISSING_NEW_BG
            return f"<td style='{td}background:{bg};color:{_CRM_MISSING_DARK_RED};font-weight:700;'>{esc}</td>"
        return f"<td style='{td}color:#0f172a;'>{esc}</td>"

    @staticmethod
    def _html_bc_style_name_cell(name: str, *, row_kind: str) -> str:
        td = "padding:8px 10px;border-bottom:1px solid #e2e8f0;font-size:12px;"
        esc = html.escape(name or "—")
        if row_kind == "version":
            return (
                f"<td style='{td}background:{_CRM_BC_VERSION_MISMATCH_BG};"
                f"color:{_CRM_BC_VERSION_MISMATCH_FG};font-weight:700;'>{esc}</td>"
            )
        if row_kind == "missing_old":
            return (
                f"<td style='{td}background:{_CRM_BC_MISSING_NEW_BG};"
                f"color:#166534;font-weight:700;'>{esc}</td>"
            )
        if row_kind == "missing_new":
            return (
                f"<td style='{td}background:{_CRM_BC_MISSING_OLD_BG};"
                f"color:{_CRM_MISSING_DARK_RED};font-weight:700;'>{esc}</td>"
            )
        return f"<td style='{td}color:#334155;font-weight:600;'>{esc}</td>"

    @staticmethod
    def _html_bc_style_status_cell(label: str, *, row_kind: str) -> str:
        td = "padding:8px 10px;border-bottom:1px solid #e2e8f0;font-size:11px;font-weight:700;"
        esc = html.escape(label)
        if row_kind == "version":
            bg, fg = _CRM_BC_VERSION_MISMATCH_BG, _CRM_BC_VERSION_MISMATCH_FG
        elif row_kind == "missing_new":
            bg, fg = _CRM_BC_MISSING_OLD_BG, _CRM_MISSING_DARK_RED
        elif row_kind == "missing_old":
            bg, fg = _CRM_BC_MISSING_NEW_BG, "#166534"
        else:
            bg, fg = "#f8fafc", "#475569"
        return f"<td style='{td}background:{bg};color:{fg};'>{esc}</td>"

    def _html_clfy_bc_container_diff_table(self, ad: Optional[dict]) -> str:
        """Beyond Compare–style: version mismatch (blue), missing JARs (red/green)."""
        if not ad:
            return ""
        version_rows = list(ad.get("version_mismatch_rows") or [])
        cidx = self._clfy_archive_diff_container_index(ad)
        only_old_paths = [
            str(r.get("path") or "").strip()
            for r in (cidx.get("rows") or [])
            if r.get("status_key") == "only_old" and r.get("path")
        ]
        only_new_paths = [
            str(r.get("path") or "").strip()
            for r in (cidx.get("rows") or [])
            if r.get("status_key") == "only_new" and r.get("path")
        ]
        ver_member_keys: set[str] = set()
        for vr in version_rows:
            ver_member_keys.add(self._member_match_key(str(vr.get("old_path") or "")))
            ver_member_keys.add(self._member_match_key(str(vr.get("new_path") or "")))
        only_old_paths = [
            p for p in only_old_paths if self._member_match_key(p) not in ver_member_keys
        ]
        only_new_paths = [
            p for p in only_new_paths if self._member_match_key(p) not in ver_member_keys
        ]
        unmatched_old, unmatched_new = self._pair_paths_by_signature(
            only_old_paths,
            only_new_paths,
            signature_fn=self._member_match_key,
        )

        tr: list[str] = []

        for vr in version_rows:
            op = str(vr.get("old_path") or "").strip()
            np = str(vr.get("new_path") or "").strip()
            name = str(vr.get("logical_name") or self._path_leaf_name(op or np))
            summary = str(vr.get("summary") or "")
            tr.append(
                "<tr>"
                f"{self._html_bc_style_path_cell(op, kind='version')}"
                f"{self._html_bc_style_path_cell(np, kind='version')}"
                f"{self._html_bc_style_name_cell(name, row_kind='version')}"
                f"{self._html_bc_style_status_cell('Version mismatch' + (f' ({summary})' if summary else ''), row_kind='version')}"
                "</tr>"
            )

        for raw in unmatched_old:
            op = self._crm_map_path_across_release(raw, to_new=False)
            np = self._crm_expected_new_path_for_old(op)
            name = self._path_leaf_name(op or raw)
            tr.append(
                "<tr>"
                f"{self._html_bc_style_path_cell(op or raw, kind='old')}"
                f"{self._html_bc_style_path_cell(np, kind='new', missing=True)}"
                f"{self._html_bc_style_name_cell(name, row_kind='missing_new')}"
                f"{self._html_bc_style_status_cell('Missing in new build', row_kind='missing_new')}"
                "</tr>"
            )

        for raw in unmatched_new:
            np = self._crm_map_path_across_release(raw, to_new=True)
            op = self._crm_expected_old_path_for_new(np)
            name = self._path_leaf_name(np or raw)
            tr.append(
                "<tr>"
                f"{self._html_bc_style_path_cell(op, kind='old', missing=True)}"
                f"{self._html_bc_style_path_cell(np or raw, kind='new')}"
                f"{self._html_bc_style_name_cell(name, row_kind='missing_old')}"
                f"{self._html_bc_style_status_cell('Missing in old build', row_kind='missing_old')}"
                "</tr>"
            )

        if not tr:
            return (
                "<p style='color:#6b7280;font-size:13px;'>"
                "No nested container version mismatches or missing JAR/WAR/EAR/ZIP entries.</p>"
            )
        lim = 3000
        rest = (
            f"<p style='font-size:12px;color:#6b7280;'>… and {len(tr) - lim} more</p>"
            if len(tr) > lim
            else ""
        )
        legend = (
            "<p style='font-size:12px;color:#64748b;margin:0 0 10px 0;line-height:1.55;'>"
            "<span style='background:#cce8ff;padding:2px 8px;border-radius:4px;color:#003366;'>"
            "Blue</span> = version mismatch (same logical JAR, different version suffix). "
            "<span style='background:#ffe7e7;padding:2px 8px;border-radius:4px;color:#7f1d1d;'>"
            "Red tint</span> = missing in new build. "
            "<span style='background:#e7f7e7;padding:2px 8px;border-radius:4px;color:#166534;'>"
            "Green tint</span> = missing in old build "
            "(Beyond Compare folder-compare style).</p>"
        )
        return f"""
            <div style="margin:16px 0;">
            <h4 style="margin:0 0 8px 0;font-size:15px;color:#0f172a;">
            JAR / WAR / EAR / ZIP — version mismatch &amp; missing (Beyond Compare style)</h4>
            {legend}
            <div style="overflow-x:auto;border:1px solid #e2e8f0;border-radius:10px;background:#fff;">
            <table style="width:100%;border-collapse:collapse;min-width:900px;">
            <thead>
            <tr style="background:linear-gradient(135deg,#1e3a5f 0%,#2d5a87 100%);color:#fff;">
            <th style="padding:10px;text-align:left;">Old build path</th>
            <th style="padding:10px;text-align:left;">New build path</th>
            <th style="padding:10px;text-align:left;">Archive name</th>
            <th style="padding:10px;text-align:left;">Status</th>
            </tr>
            </thead>
            <tbody>{"".join(tr[:lim])}</tbody>
            </table>
            </div>
            {rest}
            </div>"""

    def _html_side_by_side_mismatch_table(
        self,
        rows: list[dict],
        *,
        title: str,
        subtitle: str = "",
        name_header: str = "File name",
        limit: int = 2000,
    ) -> str:
        if not rows:
            return ""
        lim_rows = rows[:limit]
        body = []
        for r in lim_rows:
            op = str(r.get("old_path") or "").strip()
            np = str(r.get("new_path") or "").strip()
            name = str(r.get("name") or self._path_leaf_name(op or np))
            orphan = bool(op) != bool(np) or self._crm_path_is_absent_placeholder(
                op
            ) or self._crm_path_is_absent_placeholder(np)
            missing_old = bool(np) and (
                not op or self._crm_path_is_absent_placeholder(op)
            )
            missing_new = bool(op) and (
                not np or self._crm_path_is_absent_placeholder(np)
            )
            body.append(
                "<tr>"
                f"{self._html_side_by_side_path_cell(op, missing=missing_old)}"
                f"{self._html_side_by_side_path_cell(np, missing=missing_new)}"
                f"{self._html_side_by_side_name_cell(name, highlight=orphan)}"
                "</tr>"
            )
        rest = (
            f"<p style='margin:8px 0 0 0;font-size:12px;color:#64748b;'>"
            f"… and {len(rows) - limit:,} more row(s)</p>"
            if len(rows) > limit
            else ""
        )
        sub = (
            f"<p style='font-size:12px;color:#64748b;margin:0 0 10px 0;line-height:1.5;'>{subtitle}</p>"
            if subtitle
            else ""
        )
        return f"""
            <div style="margin:12px 0 0 0;padding:12px;border-radius:10px;border:1px solid #cbd5e1;background:#fff;">
                <div style="font-size:14px;font-weight:700;color:#0f172a;margin-bottom:8px;">{title}</div>
                {sub}
                <div style="overflow-x:auto;border:1px solid #e2e8f0;border-radius:8px;">
                    <table style="width:100%;border-collapse:collapse;min-width:720px;">
                        <thead><tr style="background:#f1f5f9;color:#0f172a;">
                            <th style="padding:8px 10px;text-align:left;">Old build path</th>
                            <th style="padding:8px 10px;text-align:left;">New build path</th>
                            <th style="padding:8px 10px;text-align:left;">{html.escape(name_header)}</th>
                        </tr></thead>
                        <tbody>{"".join(body)}</tbody>
                    </table>
                </div>
                {rest}
            </div>"""

    @staticmethod
    def _html_report_collapse_extra_css() -> str:
        return report_collapse_extra_css()

    @staticmethod
    def _html_report_collapse_toolbar() -> str:
        return report_collapse_toolbar().replace(
            'class="report-section-toolbar"', 'class="crm-report-toolbar"', 1
        )

    @staticmethod
    def _html_report_collapse_script() -> str:
        return report_collapse_script()

    @staticmethod
    def _html_wrap_report_section(
        section_id: str,
        title_html: str,
        body_html: str,
        *,
        open_default: bool = True,
    ) -> str:
        wrapped = wrap_report_section(
            section_id,
            title_html,
            body_html,
            open_default=open_default,
            extra_classes="crm-report-collapse",
        )
        return wrapped.replace(
            'class="report-collapse-summary"', 'class="crm-collapse-summary"', 1
        ).replace(
            'class="report-collapse-body"', 'class="crm-collapse-body"', 1
        )

    @staticmethod
    def _count_java_archives_in_member_set(members) -> int:
        """Count members that look like deployable Java archives (not only top-level tree rows)."""
        if not members:
            return 0
        n = 0
        for m in members:
            if CRMFolderComparer._path_looks_like_java_archive(m):
                n += 1
        return n

    def _archive_signature_path(self, path_value: str) -> str:
        """
        Normalize archive path for old/new pairing.
        Handles nested 'outer!/inner.jar' chains and strips versionish suffixes from archive names.
        """
        s = _crm_norm_rel_path(path_value).strip().lower()
        if not s:
            return s
        segments = [seg for seg in s.split("!/") if seg]
        normalized_segments: list[str] = []
        for seg in segments:
            parts = [p for p in seg.split("/") if p]
            if not parts:
                continue
            leaf = parts[-1]
            if leaf.endswith((".jar", ".war", ".ear", ".zip")):
                parts[-1] = self._get_archive_base_name(leaf)
            normalized_segments.append("/".join(parts))
        return "!/".join(normalized_segments)

    def _pair_paths_by_signature(
        self,
        old_paths: list[str],
        new_paths: list[str],
        *,
        signature_fn: Optional[Callable[[str], str]] = None,
    ) -> tuple[list[str], list[str]]:
        """Pair old/new paths by signature; return unmatched leftovers (true one-side-only)."""
        sig = signature_fn or self._archive_signature_path
        old_map: dict[str, list[str]] = {}
        new_map: dict[str, list[str]] = {}
        for p in old_paths:
            k = sig(str(p or ""))
            if not k:
                continue
            old_map.setdefault(k, []).append(str(p))
        for p in new_paths:
            k = sig(str(p or ""))
            if not k:
                continue
            new_map.setdefault(k, []).append(str(p))
        for dct in (old_map, new_map):
            for k in dct:
                dct[k].sort(key=lambda x: x.lower())
        unmatched_old: list[str] = []
        unmatched_new: list[str] = []
        for k in sorted(set(old_map.keys()) | set(new_map.keys())):
            o = old_map.get(k) or []
            n = new_map.get(k) or []
            m = min(len(o), len(n))
            if len(o) > m:
                unmatched_old.extend(o[m:])
            if len(n) > m:
                unmatched_new.extend(n[m:])
        unmatched_old.sort(key=lambda x: x.lower())
        unmatched_new.sort(key=lambda x: x.lower())
        return (
            self._dedupe_norm_paths(unmatched_old),
            self._dedupe_norm_paths(unmatched_new),
        )

    def _pair_archive_paths_by_signature(
        self, old_paths: list[str], new_paths: list[str]
    ) -> tuple[list[str], list[str]]:
        """
        Pair old/new archive paths by normalized signature and return unmatched leftovers.
        """
        return self._pair_paths_by_signature(old_paths, new_paths)

    def _collect_unmatched_jar_archive_paths(
        self,
    ) -> tuple[list[str], list[str]]:
        """Whole-container paths on one build only, after CRM-aware signature pairing."""
        added, removed, modified, _ = self._report_diff_buckets()
        jar_old = self._collect_java_archive_one_side_paths(
            added, removed, modified, side="old"
        )
        jar_new = self._collect_java_archive_one_side_paths(
            added, removed, modified, side="new"
        )
        return self._pair_paths_by_signature(
            jar_old,
            jar_new,
            signature_fn=self._crm_jar_side_by_side_signature,
        )

    def _crm_report_version_changed_jar_summaries(self) -> list[dict]:
        """Cached version-changed JAR summaries (one build per HTML report)."""
        cached = getattr(self, "_crm_cached_version_jar_summaries", None)
        if cached is None:
            self._crm_cached_version_jar_summaries = self._collect_version_changed_jar_summaries()
            cached = self._crm_cached_version_jar_summaries
        return cached

    def _crm_report_missing_jar_summaries(self) -> list[dict]:
        """Cached missing JAR summaries with counterpart paths precomputed."""
        cached = getattr(self, "_crm_cached_missing_jar_summaries", None)
        if cached is None:
            self._crm_cached_missing_jar_summaries = self._collect_missing_jar_summaries(
                version_summaries=self._crm_report_version_changed_jar_summaries(),
            )
            cached = self._crm_cached_missing_jar_summaries
        return cached

    def _jar_paths_covered_by_version_summaries(
        self, version_summaries: Optional[list[dict]] = None
    ) -> set[str]:
        """Signature keys for JARs already listed as version-changed (not missing)."""
        covered: set[str] = set()
        summaries = (
            version_summaries
            if version_summaries is not None
            else self._crm_report_version_changed_jar_summaries()
        )
        for item in summaries:
            for p in (item.get("old_path"), item.get("new_path")):
                ps = str(p or "").strip()
                if ps and not self._crm_path_is_absent_placeholder(ps):
                    covered.add(self._crm_jar_side_by_side_signature(ps))
        czd = self.clfy_agent_zip_diff or {}
        if not czd.get("error"):
            cad = (czd.get("archive_diff") or {}) if isinstance(czd.get("archive_diff"), dict) else {}
            for vr in cad.get("version_mismatch_rows") or []:
                for p in (vr.get("old_path"), vr.get("new_path")):
                    ps = str(p or "").strip()
                    if ps:
                        covered.add(self._member_match_key(ps))
                        covered.add(self._crm_jar_side_by_side_signature(ps))
        return covered

    def _side_by_side_counts(
        self,
        added: list[CRMComparisonResult],
        removed: list[CRMComparisonResult],
        modified: list[CRMComparisonResult],
        unchanged_count: int,
        *,
        align_cache: Optional[tuple] = None,
    ) -> dict[str, int]:
        """
        Per-build file counts for summary tables.
        total_old = files present in old tree (removed + modified + unchanged).
        total_new = files present in new tree (added + modified + unchanged).
        files_only_* = unmatched removed/added after rename/move alignment.
        jar_* = unmatched .jar/.war/.ear after signature pairing (top-level + nested archive members).
        """
        if align_cache is not None:
            align_pairs, unpaired_removed, unpaired_added = align_cache
        else:
            align_pairs, unpaired_removed, unpaired_added = self._crm_align_removed_added_for_report(
                removed, added, min_ratio=0.35
            )
        _ = align_pairs  # only unmatched counts are needed here.
        old_archive_paths = self._collect_java_archive_one_side_paths(
            added, removed, modified, side="old"
        )
        new_archive_paths = self._collect_java_archive_one_side_paths(
            added, removed, modified, side="new"
        )
        unmatched_old_arch, unmatched_new_arch = self._pair_archive_paths_by_signature(
            old_archive_paths, new_archive_paths
        )
        scan_old, scan_new = self._crm_scanned_tree_file_counts()
        result_total_old = len(removed) + len(modified) + unchanged_count
        result_total_new = len(added) + len(modified) + unchanged_count
        total_old = scan_old if scan_old else result_total_old
        total_new = scan_new if scan_new else result_total_new
        return {
            "total_old": total_old,
            "total_new": total_new,
            "scan_total_old": scan_old,
            "scan_total_new": scan_new,
            "result_total_old": result_total_old,
            "result_total_new": result_total_new,
            "unchanged": unchanged_count,
            "modified": len(modified),
            "files_only_old": len(unpaired_removed),
            "files_only_new": len(unpaired_added),
            "jar_removed_old": len(unmatched_old_arch),
            "jar_added_new": len(unmatched_new_arch),
            "removed_raw": len(removed),
            "added_raw": len(added),
            "jar_old_raw": len(old_archive_paths),
            "jar_new_raw": len(new_archive_paths),
        }

    @staticmethod
    def _path_leaf_name(path_value: str) -> str:
        """Best-effort filename (leaf) for plain or nested paths."""
        return CRMFolderComparer._path_leaf_name_static(path_value)

    @staticmethod
    def _dedupe_norm_paths(paths: list[str]) -> list[str]:
        """Drop duplicate paths (case-insensitive, normalized), preserving first occurrence."""
        seen: set[str] = set()
        out: list[str] = []
        for p in paths:
            norm = _crm_norm_rel_path(str(p)).strip()
            if not norm:
                continue
            key = norm.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(norm)
        return out

    def _collect_java_archive_one_side_paths(
        self,
        added: list[CRMComparisonResult],
        removed: list[CRMComparisonResult],
        modified: list[CRMComparisonResult],
        *,
        side: str,
    ) -> list[str]:
        """
        Collect whole .jar/.war/.ear containers absent on one build (top-level removed/added + ClfyAgent containers).
        Inner class/folder/file diffs inside modified archives are excluded — see inner-member section.
        """
        if side not in {"old", "new"}:
            return []
        out: list[str] = []
        for r in self.results or []:
            for p in self._crm_paths_from_result_for_side(r, side):
                if self._path_looks_like_java_archive(p):
                    out.append(p)
        czd = self.clfy_agent_zip_diff or {}
        if not czd.get("error"):
            cad = (czd.get("archive_diff") or {}) if isinstance(czd.get("archive_diff"), dict) else {}
            clfy_anchor = (
                str(czd.get("rel_old" if side == "old" else "rel_new") or "").strip()
                or str(czd.get("zip_old" if side == "old" else "zip_new") or "").strip()
                or str(czd.get("label") or CLFY_AGENT_BUNDLE_REPORT_LABEL)
            )
            clfy_anchor = _crm_norm_rel_path(clfy_anchor)
            container_key = "container_only_old" if side == "old" else "container_only_new"
            for m in sorted(cad.get(container_key) or [], key=lambda x: str(x).lower()):
                ms = _crm_norm_rel_path(str(m)).lstrip("/")
                if self._path_looks_like_java_archive(ms):
                    out.append(f"{clfy_anchor}!/{ms}")
        return sorted(self._dedupe_norm_paths(out), key=lambda x: x.lower())

    def _count_delta_details(
        self,
        added: list[CRMComparisonResult],
        removed: list[CRMComparisonResult],
        modified: list[CRMComparisonResult],
        *,
        align_cache: Optional[tuple] = None,
    ) -> dict[str, object]:
        """
        Compute concrete path/file rows that explain count deltas:
        - top-level removed vs added path count delta
        - jar/war/ear removed vs added count delta (including nested archive members)
        """
        c = self._side_by_side_counts(added, removed, modified, 0, align_cache=align_cache)
        # unchanged count omitted — delta metrics do not depend on unchanged files.
        if align_cache is not None:
            align_pairs, unpaired_r, unpaired_a = align_cache
        else:
            align_pairs, unpaired_r, unpaired_a = self._crm_align_removed_added_for_report(
                removed, added, min_ratio=0.35
            )
        _ = align_pairs  # intentionally unused: only unpaired rows matter for delta explanation.
        unpaired_old_paths = self._dedupe_norm_paths(
            sorted(
                (
                    p
                    for r in unpaired_r
                    for p in self._crm_paths_from_result_for_side(r, "old")
                ),
                key=lambda x: x.lower(),
            )
        )
        unpaired_new_paths = self._dedupe_norm_paths(
            sorted(
                (
                    p
                    for r in unpaired_a
                    for p in self._crm_paths_from_result_for_side(r, "new")
                ),
                key=lambda x: x.lower(),
            )
        )
        path_rows_all = self._align_side_by_side_path_rows(unpaired_old_paths, unpaired_new_paths)
        path_rows = [r for r in path_rows_all if self._crm_path_row_is_true_orphan(r)]
        path_delta = self._crm_path_rows_side_delta(path_rows)
        path_side = "old" if path_delta > 0 else ("new" if path_delta < 0 else "balanced")

        jar_old_paths = self._collect_java_archive_one_side_paths(
            added, removed, modified, side="old"
        )
        jar_new_paths = self._collect_java_archive_one_side_paths(
            added, removed, modified, side="new"
        )
        unmatched_old_arch, unmatched_new_arch = self._pair_archive_paths_by_signature(
            jar_old_paths, jar_new_paths
        )
        jar_delta = len(unmatched_old_arch) - len(unmatched_new_arch)
        jar_side = "old" if jar_delta > 0 else ("new" if jar_delta < 0 else "balanced")
        jar_rows = self._align_side_by_side_path_rows(
            unmatched_old_arch,
            unmatched_new_arch,
            signature_fn=self._crm_jar_side_by_side_signature,
        )
        inner_old_rows = self._collect_inner_jar_member_mismatches(modified, side="old")
        inner_new_rows = self._collect_inner_jar_member_mismatches(modified, side="new")
        inner_delta = len(inner_old_rows) - len(inner_new_rows)

        return {
            "path_delta": path_delta,
            "path_side": path_side,
            "path_rows": path_rows,
            "path_total_candidates": len(path_rows),
            "jar_delta": jar_delta,
            "jar_side": jar_side,
            "jar_rows": jar_rows,
            "jar_total_candidates": len(jar_rows),
            "inner_delta": inner_delta,
            "inner_old_rows": inner_old_rows,
            "inner_new_rows": inner_new_rows,
        }

    def _text_side_by_side_summary_block(
        self,
        added: list[CRMComparisonResult],
        removed: list[CRMComparisonResult],
        modified: list[CRMComparisonResult],
        unchanged_count: int,
        *,
        align_cache: Optional[tuple] = None,
    ) -> list[str]:
        """Lines for plain-text report: side-by-side Old vs New summary table."""
        c = self._side_by_side_counts(
            added, removed, modified, unchanged_count, align_cache=align_cache
        )
        delta_details = self._count_delta_details(added, removed, modified, align_cache=align_cache)
        high_diff_count = len(self._compute_high_size_diff_alert(modified))
        lines: list[str] = []
        lines.append("-" * 80)
        lines.append("SIDE-BY-SIDE SUMMARY (Old build vs New build)")
        lines.append("-" * 80)
        lines.append("AT A GLANCE — this comparison run:")
        for _lv, msg in self._build_at_a_glance_items(
            added, removed, modified, unchanged_count,
            as_html=False, counts=c, delta=delta_details,
        ):
            lines.append(f"  • {msg}")
        lines.append("")
        hdr = f"{'Metric':<44} {'Old Build':>16} {'New Build':>16}"
        lines.append(hdr)
        lines.append("-" * 80)
        lines.append(
            f"{'Total files (in tree)':<44} {c['total_old']:>16,} {c['total_new']:>16,}"
        )
        lines.append(
            f"{'Paths unmatched on old side (aligned)':<44} {c['files_only_old']:>16,} {0:>16,}"
        )
        lines.append(
            f"{'Paths unmatched on new side (aligned)':<44} {0:>16,} {c['files_only_new']:>16,}"
        )
        lines.append(
            f"{'Unchanged files':<44} {c['unchanged']:>16,} {c['unchanged']:>16,}"
        )
        lines.append(
            f"{'Modified files':<44} {c['modified']:>16,} {c['modified']:>16,}"
        )
        major_lbl = f"Major size diffs (|change| >= {_CRM_HIGH_SIZE_ALERT_MIN_ABS_PCT:.0f}%)"
        if high_diff_count == 0:
            lines.append(
                f"{major_lbl:<44} {'None found':>16} {'None found':>16}"
            )
        else:
            lines.append(
                f"{major_lbl:<44} {high_diff_count:>16,} {high_diff_count:>16,}"
            )
        lines.append(
            f"{'JAR / WAR / EAR added (whole containers)':<44} {0:>16,} {c['jar_added_new']:>16,}"
        )
        lines.append(
            f"{'JAR / WAR / EAR removed (whole containers)':<44} {c['jar_removed_old']:>16,} {0:>16,}"
        )
        lines.append("-" * 80)
        path_delta = int(delta_details["path_delta"])
        jar_delta = int(delta_details["jar_delta"])
        if path_delta != 0:
            side_label = "old" if path_delta > 0 else "new"
            lines.append(
                f"PATH COUNT DELTA DETAILS ({abs(path_delta):,} more on {side_label} side) — showing exact paths"
            )
            lines.append("-" * 80)
            for row in delta_details["path_rows"]:
                lines.append(f"  - Old path: {row.get('old_path') or '—'}")
                lines.append(f"    New path: {row.get('new_path') or '—'}")
                lines.append(f"    File: {row['name']}")
            shown = len(delta_details["path_rows"])
            if shown < abs(path_delta):
                lines.append(
                    f"  (Listed {shown:,} row(s); expected delta is {abs(path_delta):,}. Alignment candidates were fewer than delta.)"
                )
            lines.append("")
        if jar_delta != 0:
            side_label = "old" if jar_delta > 0 else "new"
            lines.append(
                f"JAR / WAR / EAR CONTAINER MISMATCH ({abs(jar_delta):,} more whole archive(s) on {side_label} side) — path and archive name"
            )
            lines.append("-" * 80)
            for row in delta_details["jar_rows"]:
                lines.append(f"  - Old path: {row.get('old_path') or '—'}")
                lines.append(f"    New path: {row.get('new_path') or '—'}")
                lines.append(f"    Archive: {row['name']}")
            shown = len(delta_details["jar_rows"])
            if shown < abs(jar_delta):
                lines.append(
                    f"  (Listed {shown:,} path(s); expected delta is {abs(jar_delta):,} whole container(s).)"
                )
            lines.append("")
        inner_old_rows = delta_details.get("inner_old_rows") or []
        inner_new_rows = delta_details.get("inner_new_rows") or []
        if inner_old_rows or inner_new_rows:
            lines.append(
                "INSIDE-JAR FOLDER / CLASS / FILE DIFFERENCES (outer archive exists on both builds)"
            )
            lines.append("-" * 80)
            if inner_old_rows:
                lines.append(f"Only in OLD build inside archives ({len(inner_old_rows):,}):")
                lim = 200
                for row in inner_old_rows[:lim]:
                    lines.append(
                        f"  - [{row.get('kind', 'file')}] {row.get('host')}!/{row.get('member')}"
                    )
                if len(inner_old_rows) > lim:
                    lines.append(f"  ... and {len(inner_old_rows) - lim} more")
                lines.append("")
            if inner_new_rows:
                lines.append(f"Only in NEW build inside archives ({len(inner_new_rows):,}):")
                lim = 200
                for row in inner_new_rows[:lim]:
                    lines.append(
                        f"  - [{row.get('kind', 'file')}] {row.get('host')}!/{row.get('member')}"
                    )
                if len(inner_new_rows) > lim:
                    lines.append(f"  ... and {len(inner_new_rows) - lim} more")
                lines.append("")
        lines.append("")
        return lines

    @staticmethod
    def _clfy_container_prefixes(virtual_path: str) -> list[str]:
        """
        Return every .jar/.war/.ear/.zip prefix along a virtual member path (outer → inner).
        Deep scans list inner .class paths; this recovers the containing archive for reporting.
        """
        s = str(virtual_path or "").replace("\\", "/").strip("/")
        if not s:
            return []
        exts = (".jar", ".war", ".ear", ".zip")
        out: list[str] = []
        chunks = s.split("!/")
        built = ""
        for ci, chunk in enumerate(chunks):
            if ci > 0:
                built += "!/"
            parts = [p for p in chunk.split("/") if p]
            for pi in range(len(parts)):
                if pi == 0:
                    if ci == 0:
                        built = parts[0]
                    else:
                        built += parts[0]
                else:
                    built += "/" + parts[pi]
                if parts[pi].lower().endswith(exts):
                    out.append(built)
        return out

    def _clfy_build_container_diff_from_members(
        self,
        names1: set,
        names2: set,
        different_content: list,
    ) -> dict[str, set]:
        """Roll up member-level diff to container (.jar/.war/.ear/.zip) paths for ClfyAgent reporting."""

        def _containers(names: set) -> set[str]:
            found: set[str] = set()
            for n in names:
                for cp in self._clfy_container_prefixes(str(n)):
                    found.add(self._normalize_inner_archive_marker(cp))
            return found

        co = _containers(names1)
        cn = _containers(names2)
        only_old = co - cn
        only_new = cn - co
        both = co & cn
        different: set[str] = set()
        for nm in different_content:
            for cp in self._clfy_container_prefixes(str(nm)):
                norm_cp = self._normalize_inner_archive_marker(cp)
                if norm_cp in both:
                    different.add(norm_cp)
        return {
            "container_only_old": only_old,
            "container_only_new": only_new,
            "container_different": different,
        }

    @staticmethod
    def _clfy_member_is_container_archive(name: str) -> bool:
        n = (name or "").replace("\\", "/").strip()
        if not n:
            return False
        base = n.rsplit("/", 1)[-1].lower()
        return base.endswith((".jar", ".war", ".ear", ".zip"))

    @staticmethod
    def _clfy_container_type_label(name: str) -> str:
        b = (name or "").replace("\\", "/").rsplit("/", 1)[-1].lower()
        for suf, lab in (
            (".ear", "EAR"),
            (".war", "WAR"),
            (".jar", "JAR"),
            (".zip", "ZIP"),
        ):
            if b.endswith(suf):
                return lab
        return "—"

    def _clfy_archive_diff_container_index(
        self, ad: Optional[dict], *, include_content_diff: bool = False
    ) -> dict:
        """
        Restrict ClfyAgent reporting to nested container entries (.jar/.war/.ear/.zip) only.
        Uses precomputed container_only_* sets from archive compare when present (deep-scan safe).
        Returns table rows, per-extension counts, and by-status path lists.
        Content-different containers are omitted from the report by default.
        """
        if not ad:
            return {
                "by_status": {"only_old": [], "only_new": [], "different": []},
                "metrics_ext": {".jar": {"oo": 0, "on": 0, "df": 0}, ".war": {"oo": 0, "on": 0, "df": 0},
                                ".ear": {"oo": 0, "on": 0, "df": 0}, ".zip": {"oo": 0, "on": 0, "df": 0}},
                "rows": [],
            }
        by_status: dict = {"only_old": [], "only_new": [], "different": []}
        metrics: dict = {
            ".jar": {"oo": 0, "on": 0, "df": 0},
            ".war": {"oo": 0, "on": 0, "df": 0},
            ".ear": {"oo": 0, "on": 0, "df": 0},
            ".zip": {"oo": 0, "on": 0, "df": 0},
        }
        rows: list[dict] = []
        seen: set[tuple[str, str]] = set()

        def _ext_of(p: str) -> str:
            b = p.replace("\\", "/").rsplit("/", 1)[-1].lower()
            for s in (".ear", ".war", ".jar", ".zip"):
                if b.endswith(s):
                    return s
            return ""

        def _add(key: str, p: str) -> None:
            p = str(p).strip()
            if not p or not self._clfy_member_is_container_archive(p):
                return
            dedupe = (key, p.lower())
            if dedupe in seen:
                return
            seen.add(dedupe)
            by_status[key].append(p)
            ex = _ext_of(p)
            if ex in metrics:
                sk = "oo" if key == "only_old" else ("on" if key == "only_new" else "df")
                metrics[ex][sk] = metrics[ex].get(sk, 0) + 1
            st_label = {
                "only_old": "Only in old",
                "only_new": "Only in new",
                "different": "Content differs",
            }[key]
            rows.append(
                {
                    "path": p,
                    "status": st_label,
                    "status_key": key,
                    "type": self._clfy_container_type_label(p),
                }
            )

        precomputed = bool(
            ad.get("container_only_old")
            or ad.get("container_only_new")
            or ad.get("container_different")
        )
        if precomputed:
            for p in sorted(ad.get("container_only_old") or [], key=lambda x: str(x).lower()):
                _add("only_old", p)
            for p in sorted(ad.get("container_only_new") or [], key=lambda x: str(x).lower()):
                _add("only_new", p)
            if include_content_diff:
                for p in sorted(ad.get("container_different") or [], key=lambda x: str(x).lower()):
                    _add("different", p)
        else:
            # Fallback: leaf-only filter (legacy paths without container rollup)
            for p in ad.get("only_in_old") or set():
                for cp in self._clfy_container_prefixes(str(p)):
                    _add("only_old", self._normalize_inner_archive_marker(cp))
            for p in ad.get("only_in_new") or set():
                for cp in self._clfy_container_prefixes(str(p)):
                    _add("only_new", self._normalize_inner_archive_marker(cp))
            if include_content_diff:
                for p in ad.get("different_content") or []:
                    for cp in self._clfy_container_prefixes(str(p)):
                        _add("different", self._normalize_inner_archive_marker(cp))

        rows.sort(key=lambda r: (r["status_key"], r["path"].lower()))
        return {"by_status": by_status, "metrics_ext": metrics, "rows": rows}

    @staticmethod
    def _clfy_jar_entry_is_clfy_agent_bundle(
        jentry: dict, rel_old: str, rel_new: str
    ) -> bool:
        """True when optional JAR class/manifest row resolves inside a ClfyAgent .zip/.ear path."""
        jso = f"{jentry.get('jar_old_source') or ''} {jentry.get('jar_old') or ''}"
        jsn = f"{jentry.get('jar_new_source') or ''} {jentry.get('jar_new') or ''}"
        comb = f"{jso} {jsn} {rel_old or ''} {rel_new or ''}".lower()
        return "clfyagent" in comb

    def _html_clfy_nested_containers_summary_table(self, cidx: dict) -> str:
        """ClfyAgent: Old vs New style counts for JAR/WAR/EAR/ZIP (container entries only)."""
        m = cidx.get("metrics_ext") or {}
        th = "padding:10px 12px;text-align:left;border-bottom:1px solid #e2e8f0;"
        tr = []
        for lab, ex in (("JAR (nested .jar entries)", ".jar"), ("WAR (nested .war entries)", ".war"), ("EAR (nested .ear entries)", ".ear"), ("ZIP (nested .zip entries)", ".zip")):
            row = m.get(ex) or {"oo": 0, "on": 0}
            tr.append(
                f"<tr><td style='{th}'>{html.escape(lab)}</td>"
                f"<td style='{th}text-align:right;'>{row.get('oo', 0):,}</td>"
                f"<td style='{th}text-align:right;'>{row.get('on', 0):,}</td></tr>"
            )
        return f"""
            <div class="summary-side-by-side" style="margin:0 0 18px 0;padding:0;">
            <h4 style="margin:0 0 8px 0;font-size:15px;color:#0f172a;">📊 ClfyAgent bundle — nested containers (summary, Old vs New)</h4>
            <p style="font-size:12px;color:#64748b;margin:0 0 10px 0;line-height:1.5;">
            Size-only compare of nested <code>.jar</code> / <code>.war</code> / <code>.ear</code> / <code>.zip</code> entries (naming-normalized paths, no CRC / no <code>.class</code> scan).
            Missing containers and size mismatches are listed in the tables below.
            </p>
            <div style="overflow-x:auto;">
            <table class="summary-compare-table">
            <thead>
            <tr>
                <th>Container type</th>
                <th class="right">Only in old</th>
                <th class="right">Only in new</th>
            </tr>
            </thead>
            <tbody>{"".join(tr)}</tbody>
            </table>
            </div>
            </div>"""

    def _html_clfy_nested_containers_path_table(self, cidx: dict) -> str:
        """Side-by-side only-in-old vs only-in-new nested container member paths."""
        only_old = [
            r for r in (cidx.get("rows") or []) if r.get("status_key") == "only_old"
        ]
        only_new = [
            r for r in (cidx.get("rows") or []) if r.get("status_key") == "only_new"
        ]
        if not only_old and not only_new:
            return (
                "<p style='color:#6b7280;font-size:13px;'>No nested .jar / .war / .ear / .zip member differences in this pair.</p>"
            )

        def _clfy_container_sig(path: str) -> str:
            neutral = self._crm_neutralize_path_for_alignment(str(path or ""))
            return self._normalize_inner_archive_marker(neutral).lower()

        old_paths = [str(r.get("path") or "") for r in only_old if r.get("path")]
        new_paths = [str(r.get("path") or "") for r in only_new if r.get("path")]
        row_pairs = self._align_side_by_side_path_rows(
            old_paths,
            new_paths,
            signature_fn=_clfy_container_sig,
        )
        path_to_old = {str(r.get("path") or ""): r for r in only_old}
        path_to_new = {str(r.get("path") or ""): r for r in only_new}
        aligned: list[dict] = []
        for pair in row_pairs:
            ro = path_to_old.get(pair.get("old_path") or "") if pair.get("old_path") else None
            rn = path_to_new.get(pair.get("new_path") or "") if pair.get("new_path") else None
            if not ro and pair.get("old_path") and not self._crm_path_is_absent_placeholder(
                pair.get("old_path") or ""
            ):
                ro = {"path": pair.get("old_path")}
            if not rn and pair.get("new_path") and not self._crm_path_is_absent_placeholder(
                pair.get("new_path") or ""
            ):
                rn = {"path": pair.get("new_path")}
            aligned.append({"old": ro or {}, "new": rn or {}, "pair": pair})

        lim = 2000
        tr = []
        th = "padding:8px 10px;border-bottom:1px solid #e2e8f0;font-size:12px;"
        for pair in aligned[:lim]:
            row = pair.get("pair") or {}
            ro = pair.get("old") or {}
            rn = pair.get("new") or {}
            op = str(row.get("old_path") or ro.get("path") or "").strip()
            np = str(row.get("new_path") or rn.get("path") or "").strip()
            typ = str(ro.get("type") or rn.get("type") or "")
            name = self._path_leaf_name(op or np)
            orphan = bool(op) != bool(np) or self._crm_path_is_absent_placeholder(
                op
            ) or self._crm_path_is_absent_placeholder(np)
            missing_old = bool(np) and (
                not op or self._crm_path_is_absent_placeholder(op)
            )
            missing_new = bool(op) and (
                not np or self._crm_path_is_absent_placeholder(np)
            )
            tr.append(
                "<tr>"
                f"{self._html_side_by_side_path_cell(op, missing=missing_old)}"
                f"{self._html_side_by_side_path_cell(np, missing=missing_new)}"
                f"<td style='{th}color:#334155;font-weight:600;'>{html.escape(typ)}</td>"
                f"{self._html_side_by_side_name_cell(name, highlight=orphan)}"
                "</tr>"
            )
        rest = (
            f"<p style='font-size:12px;color:#6b7280;'>… and {len(aligned) - lim} more</p>"
            if len(aligned) > lim
            else ""
        )
        return f"""
            <div style="margin:16px 0;">
            <h4 style="margin:0 0 8px 0;font-size:15px;color:#0f172a;">Nested JAR / WAR / EAR / ZIP — only in old vs only in new (side by side)</h4>
            <p style="font-size:12px;color:#64748b;margin:0 0 10px 0;line-height:1.5;">
            Member paths inside the ClfyAgent bundle aligned by logical archive name across
            CRM release folders (<code>CRM_10.24/…</code> ↔ <code>CRM_10.25/…</code>).
            Missing counterparts show <code>…/absent</code> in the new-build layout.
            </p>
            <div style="overflow-x:auto;border:1px solid #e2e8f0;border-radius:10px;background:#fff;">
            <table style="width:100%;border-collapse:collapse;min-width:720px;">
            <thead>
            <tr style="background:linear-gradient(135deg,#1e3a5f 0%,#2d5a87 100%);color:#fff;">
            <th style="padding:10px;text-align:left;">Only in old — member path</th>
            <th style="padding:10px;text-align:left;">Only in new — member path</th>
            <th style="padding:10px;text-align:left;">Type</th>
            <th style="padding:10px;text-align:left;">Archive name</th>
            </tr>
            </thead>
            <tbody>{"".join(tr)}</tbody>
            </table>
            </div>
            {rest}
            </div>"""

    def _html_clfy_jar_size_diff_table(self, ad: Optional[dict]) -> str:
        """Tabular report: nested containers with different zip entry sizes (old vs new)."""
        if not ad:
            return ""
        rows = list(ad.get("jar_size_diff_rows") or [])
        if not rows:
            return (
                "<p style='color:#6b7280;font-size:13px;margin:8px 0 0 0;'>"
                "No nested container size differences between builds.</p>"
            )
        min_pct = self._clfy_size_diff_min_pct()
        lim = 2000
        th = "padding:8px 10px;border-bottom:1px solid #e2e8f0;font-size:12px;"
        minor_decrease_rows = [
            r
            for r in rows
            if int(r.get("size_new") or 0) < int(r.get("size_old") or 0)
            and 0 < abs(float(r.get("pct_change") or 0.0)) < _CRM_MINOR_SIZE_REPORT_MAX_PCT
        ]
        tr = []
        for r in rows[:lim]:
            raw_path = str(r.get("path") or "")
            path_esc = html.escape(raw_path)
            archive_name = html.escape(self._path_leaf_name(raw_path))
            so = int(r.get("size_old") or 0)
            sn = int(r.get("size_new") or 0)
            pct = float(r.get("pct_change") or 0.0)
            sig = bool(r.get("significant"))
            minor_dec = (
                sn < so and 0 < abs(pct) < _CRM_MINOR_SIZE_REPORT_MAX_PCT
            )
            name_style = (
                f"color:{_CRM_MISSING_DARK_RED};font-weight:700;background:{_CRM_MISSING_CELL_BG};"
                if minor_dec
                else "color:#0f172a;font-weight:600;"
            )
            badge = (
                "<span style='background:#fef3c7;color:#92400e;padding:2px 8px;border-radius:6px;font-size:11px;font-weight:600;'>"
                f"≥{min_pct:.0f}%</span>"
                if sig
                else (
                    "<span style='background:#fee2e2;color:#7f1d1d;padding:2px 8px;border-radius:6px;font-size:11px;font-weight:600;'>"
                    f"&lt;{_CRM_MINOR_SIZE_REPORT_MAX_PCT:.0f}% shrink</span>"
                    if minor_dec
                    else "<span style='color:#94a3b8;font-size:11px;'>minor</span>"
                )
            )
            tr.append(
                f"<tr><td style='{th}font-family:Consolas,monospace;word-break:break-all;'>{path_esc}</td>"
                f"<td style='{th}{name_style}'>{archive_name}</td>"
                f"<td style='{th}text-align:right;white-space:nowrap;'>{so:,}</td>"
                f"<td style='{th}text-align:right;white-space:nowrap;'>{sn:,}</td>"
                f"<td style='{th}text-align:right;white-space:nowrap;'>{pct:+.1f}%</td>"
                f"<td style='{th}'>{badge}</td></tr>"
            )
        rest = (
            f"<p style='font-size:12px;color:#6b7280;'>… and {len(rows) - lim} more size change(s)</p>"
            if len(rows) > lim
            else ""
        )
        minor_note = ""
        if minor_decrease_rows:
            minor_note = (
                f"<p style='font-size:12px;color:#7f1d1d;margin:0 0 10px 0;font-weight:600;'>"
                f"{len(minor_decrease_rows):,} nested container(s) shrank in the new build by less than "
                f"{_CRM_MINOR_SIZE_REPORT_MAX_PCT:.0f}% (archive names highlighted in dark red).</p>"
            )
        return f"""
            <div style="margin:16px 0;">
            <h4 style="margin:0 0 8px 0;font-size:15px;color:#0f172a;">Container size differences (old vs new)</h4>
            <p style="font-size:12px;color:#64748b;margin:0 0 10px 0;">
            Compared by zip entry <strong>size</strong> only (CRC ignored). Version tokens in archive names are normalized before matching.
            Highlighted rows meet the significant change threshold (≥{min_pct:.0f}% size change).
            </p>
            {minor_note}
            <div style="overflow-x:auto;border:1px solid #e2e8f0;border-radius:10px;background:#fff;">
            <table style="width:100%;border-collapse:collapse;min-width:720px;">
            <thead>
            <tr style="background:linear-gradient(135deg,#1e3a5f 0%,#2d5a87 100%);color:#fff;">
            <th style="padding:10px;text-align:left;">Normalized path</th>
            <th style="padding:10px;text-align:left;">Archive name</th>
            <th style="padding:10px;text-align:right;">Old size (bytes)</th>
            <th style="padding:10px;text-align:right;">New size (bytes)</th>
            <th style="padding:10px;text-align:right;">Change</th>
            <th style="padding:10px;text-align:left;">Significant</th>
            </tr>
            </thead>
            <tbody>{"".join(tr)}</tbody>
            </table>
            </div>
            {rest}
            </div>"""

    def _html_clfy_optional_manifest_subtable(
        self, clfy_rel_old: str, clfy_rel_new: str
    ) -> str:
        if not self._parse_class_diff_jar_names(self.manifest_diff_jar_name or ""):
            return ""
        jdl = [j for j in (self.jar_manifest_diff_list or []) if self._clfy_jar_entry_is_clfy_agent_bundle(j, clfy_rel_old, clfy_rel_new)]
        if not jdl:
            return f"""
            <p style="font-size:12px;color:#6b7280;margin:0 0 8px 0;">No JAR from <strong>manifest</strong> optional list resolved inside the ClfyAgent bundle path. Check names or the global 
            <a href="#class-level-details" style="color:#2563eb;">JAR manifest diff (optional) — summary</a>.</p>"""
        rows: list[str] = []
        for jd in jdl:
            q = html.escape(str(jd.get("query") or ""))
            err = jd.get("error")
            mvo = jd.get("manifest_impl_version_old")
            mvn = jd.get("manifest_impl_version_new")
            evo = jd.get("manifest_impl_version_error_old") or ""
            evn = jd.get("manifest_impl_version_error_new") or ""
            vo = html.escape(str(mvo) if mvo else (f"n/a ({evo})" if evo else "n/a"))
            vn = html.escape(str(mvn) if mvn else (f"n/a ({evn})" if evn else "n/a"))
            clfo = (jd.get("clfy_jar_path_old") or "").strip()
            clfn = (jd.get("clfy_jar_path_new") or "").strip()
            cpo = (
                f"<span style='font-size:11px;word-break:break-all;color:#0f172a;'>{html.escape(clfo)}</span>"
                if clfo
                else "<span style=\"color:#94a3b8;\">—</span>"
            )
            cpn = (
                f"<span style='font-size:11px;word-break:break-all;color:#0f172a;'>{html.escape(clfn)}</span>"
                if clfn
                else "<span style=\"color:#94a3b8;\">—</span>"
            )
            pao = _crm_html_jar_paths_list_cell(jd.get("jar_paths_all_old"))
            pan = _crm_html_jar_paths_list_cell(jd.get("jar_paths_all_new"))
            jpath_old = (jd.get("jar_old") or "") or None
            jpath_new = (jd.get("jar_new") or "") or None
            st = _crm_jar_pair_downloads_html(
                str(jpath_old) if jpath_old else None,
                str(jpath_new) if jpath_new else None,
                err,
                str(jd.get("jar_old_source") or "") or None,
                str(jd.get("jar_new_source") or "") or None,
            )
            ccell = "padding:8px 10px;border-bottom:1px solid #e2e8f0;vertical-align:top;"
            rows.append(
                f"<tr><td style='{ccell}font-weight:600;word-break:break-word;'>{q}</td>"
                f"<td style='{ccell}font-family:Consolas,monospace;font-size:12px;word-break:break-word;'>{vo}</td>"
                f"<td style='{ccell}font-family:Consolas,monospace;font-size:12px;word-break:break-word;'>{vn}</td>"
                f"<td style='{ccell}word-break:break-word;'>{cpo}</td>"
                f"<td style='{ccell}word-break:break-word;'>{cpn}</td>"
                f"<td style='{ccell}word-break:break-word;'>{pao}</td>"
                f"<td style='{ccell}word-break:break-word;'>{pan}</td>"
                f"<td style='{ccell}font-size:11px;white-space:normal;'>{st}</td></tr>"
            )
        th_clfy_m = (
            "padding:10px;text-align:left;vertical-align:top;font-weight:600;"
            "white-space:normal;word-break:break-word;"
        )
        colgroup_clfy_manifest = (
            "<colgroup>"
            "<col style='width:12%' />"
            "<col style='width:8%' />"
            "<col style='width:8%' />"
            "<col style='width:15%' />"
            "<col style='width:15%' />"
            "<col style='width:16%' />"
            "<col style='width:16%' />"
            "<col style='width:10%' />"
            "</colgroup>"
        )
        return (
            f"""
            <div style="margin:16px 0 8px 0;">
                <h4 style="margin:0 0 8px 0;font-size:15px;color:#0f172a;">JAR manifest diff (optional) — ClfyAgent bundle (same inputs as CRM full)</h4>
                <p style="font-size:12px;color:#64748b;margin:0 0 10px 0;line-height:1.5;">
                <strong>In ClfyAgent bundle</strong> shows the <code>outer!/…!/nested.jar</code> path from the
                JAR as resolved <em>inside</em> the ClfyAgent <code>.zip</code> / <code>.ear</code> only.
                <strong>All paths found</strong> lists every relative path in the scan (and the same Clfy path) where this basename matched.
                The global summary table still shows the primary on-disk / logical path used for that report row.
                </p>
                <div style="overflow-x:auto;border:1px solid #cbd5e1;border-radius:10px;background:#fff;">
                <table style="width:100%;table-layout:fixed;border-collapse:collapse;font-size:13px;min-width:1200px;">
                {colgroup_clfy_manifest}
                <thead>
                <tr style="background:linear-gradient(135deg,#1e3a5f 0%,#2d5a87 100%);color:#fff;">
                    <th style="{th_clfy_m}">JAR name / query</th>
                    <th style="{th_clfy_m}">Impl-Version (old)</th>
                    <th style="{th_clfy_m}">Impl-Version (new)</th>
                    <th style="{th_clfy_m}">In ClfyAgent bundle (old)</th>
                    <th style="{th_clfy_m}">In ClfyAgent bundle (new)</th>
                    <th style="{th_clfy_m}">All matching paths (old)</th>
                    <th style="{th_clfy_m}">All matching paths (new)</th>
                    <th style="{th_clfy_m}">Download JARs</th>
                </tr>
                </thead>
                <tbody>{"".join(rows)}</tbody>
                </table>
                </div>"""
            + _crm_jar_download_footnote_html()
            + """
            </div>"""
        )

    def _html_clfy_optional_class_subtable(
        self, clfy_rel_old: str, clfy_rel_new: str
    ) -> str:
        jcl = self.jar_class_diff_list or []
        in_clfy = [j for j in jcl if self._clfy_jar_entry_is_clfy_agent_bundle(j, clfy_rel_old, clfy_rel_new)]
        if not in_clfy:
            if not self._parse_class_diff_jar_names(self.class_diff_jar_name or ""):
                return ""
            return """
            <p style="font-size:12px;color:#6b7280;margin:0 0 8px 0;">No JAR from <strong>class</strong> optional list resolved inside the ClfyAgent bundle. See the
            <a href="#class-level-details" style="color:#2563eb;">JAR class file diff (optional) — summary</a> for the full list.</p>"""
        rows: list[str] = []
        for gidx, jd in enumerate(jcl):
            if not self._clfy_jar_entry_is_clfy_agent_bundle(jd, clfy_rel_old, clfy_rel_new):
                continue
            q = html.escape(str(jd.get("query") or ""))
            iq = html.escape(str(jd.get("input_query") or jd.get("query") or ""))
            err = jd.get("error")
            mod_n = len(jd.get("modified") or [])
            jn = len(jd.get("java_diffs") or [])
            clfo = (jd.get("clfy_jar_path_old") or "").strip()
            clfn = (jd.get("clfy_jar_path_new") or "").strip()
            cfo = (
                f"<span style='font-size:11px;word-break:break-all;color:#0f172a'>{html.escape(clfo)}</span>"
                if clfo
                else "<span style=\"color:#94a3b8;\">—</span>"
            )
            cfn = (
                f"<span style='font-size:11px;word-break:break-all;color:#0f172a'>{html.escape(clfn)}</span>"
                if clfn
                else "<span style=\"color:#94a3b8;\">—</span>"
            )
            pao = _crm_html_jar_paths_list_cell(jd.get("jar_paths_all_old"))
            pan = _crm_html_jar_paths_list_cell(jd.get("jar_paths_all_new"))
            jpath_old = (jd.get("jar_old") or "") or None
            jpath_new = (jd.get("jar_new") or "") or None
            st = _crm_jar_pair_downloads_html(
                str(jpath_old) if jpath_old else None,
                str(jpath_new) if jpath_new else None,
                err,
                str(jd.get("jar_old_source") or "") or None,
                str(jd.get("jar_new_source") or "") or None,
            )
            rows.append(
                f"<tr><td style='padding:8px 10px;border-bottom:1px solid #e2e8f0;font-weight:600;vertical-align:top;'>{q}</td>"
                f"<td style='padding:8px 10px;border-bottom:1px solid #e2e8f0;font-size:11px;color:#64748b;vertical-align:top;'>{iq}</td>"
                f"<td style='padding:8px 10px;border-bottom:1px solid #e2e8f0;vertical-align:top;'>{cfo}</td>"
                f"<td style='padding:8px 10px;border-bottom:1px solid #e2e8f0;vertical-align:top;'>{cfn}</td>"
                f"<td style='padding:8px 10px;border-bottom:1px solid #e2e8f0;vertical-align:top;'>{pao}</td>"
                f"<td style='padding:8px 10px;border-bottom:1px solid #e2e8f0;vertical-align:top;'>{pan}</td>"
                f"<td style='padding:8px 10px;border-bottom:1px solid #e2e8f0;text-align:right;vertical-align:top;'>{mod_n}</td>"
                f"<td style='padding:8px 10px;border-bottom:1px solid #e2e8f0;text-align:right;vertical-align:top;'>{jn}</td>"
                f"<td style='padding:8px 10px;border-bottom:1px solid #e2e8f0;font-size:11px;vertical-align:top;'>{st}</td>"
                f"<td style='padding:8px 10px;border-bottom:1px solid #e2e8f0;text-align:center;vertical-align:top;'>"
                f"<a href='#jar-class-diff-{gidx}' style='color:#2563eb;font-weight:600;'>Open</a></td></tr>"
            )
        return (
            f"""
            <div style="margin:16px 0 8px 0;">
                <h4 style="margin:0 0 8px 0;font-size:15px;color:#0f172a;">JAR class file diff (optional) — ClfyAgent bundle (same inputs as CRM full)</h4>
                <p style="font-size:12px;color:#64748b;margin:0 0 10px 0;line-height:1.5;">
                Paths under <strong>In ClfyAgent bundle</strong> are the virtual <code>outer!/…!/file.jar</code> locations used when resolving the JAR
                <em>inside</em> the ClfyAgent <code>.ear</code>/<code>.zip</code> only. <strong>All matching paths</strong> includes the full scan. The global class-diff summary
                also lists primary and all paths for the same query.
                </p>
                <div style="overflow-x:auto;border:1px solid #bfdbfe;border-radius:10px;background:#fff;">
                <table style="width:100%;border-collapse:collapse;font-size:13px;min-width:1100px;">
                <thead>
                <tr style="background:linear-gradient(135deg,#1e40af 0%,#3b82f6 100%);color:#fff;">
                    <th style="padding:10px;text-align:left;">Resolved JAR / query</th>
                    <th style="padding:10px;text-align:left;">Input query</th>
                    <th style="padding:10px;text-align:left;">In ClfyAgent bundle (old)</th>
                    <th style="padding:10px;text-align:left;">In ClfyAgent bundle (new)</th>
                    <th style="padding:10px;text-align:left;">All matching paths (old)</th>
                    <th style="padding:10px;text-align:left;">All matching paths (new)</th>
                    <th style="padding:10px;text-align:right;">Modified .class</th>
                    <th style="padding:10px;text-align:right;">Java diffs</th>
                    <th style="padding:10px;text-align:left;">Download JARs</th>
                    <th style="padding:10px;text-align:center;">Global section</th>
                </tr>
                </thead>
                <tbody>{"".join(rows)}</tbody>
                </table>
                </div>
                <p style="font-size:12px;color:#64748b;margin:8px 0 0 0;">Decompiled / unified diffs: use <strong>Global section</strong> (same row as the full report).</p>"""
            + _crm_jar_download_footnote_html()
            + """
            </div>"""
        )

    def _html_clfy_config_filter_paths_only(
        self, ad: dict, config_filter_query: str
    ) -> str:
        """
        In ClfyAgent block: only paths matching the CRM full config file filter; no full unified diffs.
        """
        names = [str(x) for x in (ad.get("config_files_changed") or [])]
        diff_details = ad.get("config_diff_details") or []
        if not diff_details and not names:
            return (
                "<p style='color:#6b7280;font-size:12px;'>No config-like member changes (by extension) in this diff.</p>"
            )
        raw_f = (config_filter_query or "").strip()
        if not raw_f:
            return f"""
            <p style="font-size:12px;color:#92400e;margin:0 0 6px 0;padding:10px 12px;background:#fffbeb;border:1px solid #fde68a;border-radius:8px;">
            <strong>Optional file pattern (same as CRM full, comma-separated):</strong> not set. Set it to list
            <em>relevant</em> non-archive members here (unified diffs are not shown in this ClfyAgent block; use the main <strong>Text and config files changed</strong> section for full diffs when needed).
            </p>"""
        flt = _crm_parse_config_file_filters(raw_f)
        matched: list[dict] = []
        for row in diff_details:
            memb = str(row.get("member") or "")
            if not memb or not _crm_config_path_matches_any_filter(memb, flt):
                continue
            uhtml = row.get("unified_html") or ""
            matched.append({"member": memb, "unified_html": uhtml})
        matched_names = [n for n in names if _crm_config_path_matches_any_filter(n, flt)]
        for n in matched_names:
            if not any(x.get("member") == n for x in matched):
                matched.append({"member": n, "unified_html": ""})
        if not matched:
            return f"""
            <p style="font-size:12px;color:#6b7280;">No ClfyAgent members matched the filter: <code>{html.escape(raw_f)}</code>.</p>"""
        cap = 120
        lines = [
            f"<tr><td style='padding:8px 10px;border-bottom:1px solid #e2e8f0;font-family:Consolas,monospace;font-size:12px;'>{html.escape(str(m['member']))}</td>"
            f"<td style='padding:8px 10px;border-bottom:1px solid #e2e8f0;font-size:11px;'>"
            f"{'changed (size/CRC) — see main “Text and config files changed” for text diff' if m.get('unified_html') else 'listed by filter'}"
            f"</td></tr>"
            for m in matched[:cap]
        ]
        more = f"<p style='font-size:12px;color:#6b7280;'>… and {len(matched) - cap} more</p>" if len(matched) > cap else ""
        return f"""
            <div style="margin:12px 0 0 0;">
            <h4 style="margin:0 0 6px 0;font-size:14px;color:#4338ca;">Text / config members (filter only — {len(matched)} path(s))</h4>
            <p style="font-size:12px;color:#374151;margin:0 0 8px 0;">Filter: <code style='background:#eef2ff;padding:2px 6px;border-radius:4px'>{html.escape(raw_f)}</code>
            — text diffs are <strong>not</strong> repeated here; open the <strong>Text and config files changed</strong> section for unified diffs.</p>
            <div style="overflow-x:auto;border:1px solid #c7d2fe;border-radius:8px;">
            <table style="width:100%;border-collapse:collapse;">
            <thead>
            <tr style="background:#e0e7ff;color:#1e1b4b;">
            <th style="padding:8px 10px;text-align:left;">Member</th>
            <th style="padding:8px 10px;text-align:left;">Note</th>
            </tr>
            </thead>
            <tbody>{"".join(lines)}</tbody>
            </table>
            </div>
            {more}
            </div>"""

    @staticmethod
    def _group_inner_rows_by_host(rows: list[dict]) -> dict[str, list[dict]]:
        grouped: dict[str, list[dict]] = {}
        for r in rows:
            host = str(r.get("host") or "").strip() or "(unknown)"
            grouped.setdefault(host, []).append(r)
        return grouped

    @staticmethod
    def _short_archive_display(host: str, max_len: int = 64) -> str:
        s = _crm_norm_rel_path(str(host or "")).strip()
        if len(s) <= max_len:
            return s
        return "…" + s[-(max_len - 1) :]

    def _format_inner_host_gap_snippet(
        self, grouped: dict[str, list[dict]], *, as_html: bool, max_hosts: int = 6
    ) -> str:
        if not grouped:
            return ""
        ranked = sorted(grouped.items(), key=lambda kv: (-len(kv[1]), kv[0].lower()))
        parts: list[str] = []
        for host, host_rows in ranked[:max_hosts]:
            label = self._short_archive_display(host)
            if as_html:
                parts.append(f"<code>{html.escape(label)}</code> ({len(host_rows):,})")
            else:
                parts.append(f"{label} ({len(host_rows):,})")
        rest = len(ranked) - max_hosts
        snippet = ", ".join(parts)
        if rest > 0:
            snippet += f", +{rest} more archive(s)"
        return snippet

    @staticmethod
    def _inner_kind_breakdown(rows: list[dict]) -> str:
        if not rows:
            return ""
        counts = Counter(str(r.get("kind") or "file") for r in rows)
        labels = {
            "class": "class",
            "file": "file",
            "folder": "folder",
            "archive": "nested archive",
        }
        bits = [f"{counts[k]:,} {labels.get(k, k)}" for k in sorted(counts.keys())]
        return ", ".join(bits)

    @staticmethod
    def _format_path_list_snippet(
        paths: list[str], *, as_html: bool, max_show: int = 5
    ) -> str:
        if not paths:
            return ""
        shown = paths[:max_show]
        parts: list[str] = []
        for p in shown:
            label = CRMFolderComparer._short_archive_display(p)
            if as_html:
                parts.append(f"<code>{html.escape(label)}</code>")
            else:
                parts.append(label)
        snippet = ", ".join(parts)
        rest = len(paths) - max_show
        if rest > 0:
            snippet += f", +{rest} more"
        return snippet

    def _build_at_a_glance_items(
        self,
        added: list[CRMComparisonResult],
        removed: list[CRMComparisonResult],
        modified: list[CRMComparisonResult],
        unchanged_count: int,
        *,
        as_html: bool = False,
        counts: Optional[dict] = None,
        delta: Optional[dict] = None,
        align_cache: Optional[tuple] = None,
    ) -> list[tuple[str, str]]:
        """Ordered (severity, message) bullets for the At a glance summary."""
        c = counts or self._side_by_side_counts(added, removed, modified, unchanged_count)
        d = delta or self._count_delta_details(added, removed, modified)
        items: list[tuple[str, str]] = []

        def _b(level: str, text: str) -> None:
            items.append((level, text))

        if getattr(self, "comparison_error", None):
            err = html.escape(str(self.comparison_error)) if as_html else str(self.comparison_error)
            _b("alert", f"Comparison issue: {err}")

        _b(
            "info",
            (
                f"<strong>{c['total_old']:,}</strong> file(s) in old build vs "
                f"<strong>{c['total_new']:,}</strong> in new; "
                f"<strong>{c['unchanged']:,}</strong> unchanged, "
                f"<strong>{c['modified']:,}</strong> modified."
                if as_html
                else (
                    f"{c['total_old']:,} file(s) in old build vs {c['total_new']:,} in new; "
                    f"{c['unchanged']:,} unchanged, {c['modified']:,} modified."
                )
            ),
        )

        jar_old_paths = self._collect_java_archive_one_side_paths(
            added, removed, modified, side="old"
        )
        jar_new_paths = self._collect_java_archive_one_side_paths(
            added, removed, modified, side="new"
        )
        unmatched_old_arch, unmatched_new_arch = self._pair_archive_paths_by_signature(
            jar_old_paths, jar_new_paths
        )

        if c["jar_removed_old"]:
            snippet = self._format_path_list_snippet(unmatched_old_arch, as_html=as_html)
            extra = f" Examples: {snippet}." if snippet else ""
            _b(
                "alert",
                (
                    f"<strong>{c['jar_removed_old']:,}</strong> whole "
                    f"<strong>JAR / WAR / EAR / ZIP</strong> container(s) absent on the "
                    f"<strong>new</strong> build (not inner classes — see details below).{extra}"
                    if as_html
                    else (
                        f"{c['jar_removed_old']:,} whole JAR/WAR/EAR/ZIP container(s) absent on "
                        f"the new build (not inner classes — see details below).{extra}"
                    )
                ),
            )
        if c["jar_added_new"]:
            snippet = self._format_path_list_snippet(unmatched_new_arch, as_html=as_html)
            extra = f" Examples: {snippet}." if snippet else ""
            _b(
                "warn",
                (
                    f"<strong>{c['jar_added_new']:,}</strong> whole "
                    f"<strong>JAR / WAR / EAR / ZIP</strong> container(s) present only on the "
                    f"<strong>new</strong> build.{extra}"
                    if as_html
                    else (
                        f"{c['jar_added_new']:,} whole JAR/WAR/EAR/ZIP container(s) present only "
                        f"on the new build.{extra}"
                    )
                ),
            )

        inner_old_rows = list(d.get("inner_old_rows") or [])
        inner_new_rows = list(d.get("inner_new_rows") or [])
        if inner_old_rows:
            grouped = self._group_inner_rows_by_host(inner_old_rows)
            snippet = self._format_inner_host_gap_snippet(grouped, as_html=as_html)
            kinds = self._inner_kind_breakdown(inner_old_rows)
            _b(
                "alert",
                (
                    f"<strong>{len(inner_old_rows):,}</strong> member(s) missing from the "
                    f"<strong>new</strong> build inside <strong>{len(grouped):,}</strong> host "
                    f"archive(s) (.jar/.war/.ear/.zip) that exist on both sides "
                    f"({kinds}): {snippet}."
                    if as_html
                    else (
                        f"{len(inner_old_rows):,} member(s) missing from the new build inside "
                        f"{len(grouped):,} host archive(s) (.jar/.war/.ear/.zip) on both sides "
                        f"({kinds}): {snippet}."
                    )
                ),
            )
        if inner_new_rows:
            grouped = self._group_inner_rows_by_host(inner_new_rows)
            snippet = self._format_inner_host_gap_snippet(grouped, as_html=as_html)
            kinds = self._inner_kind_breakdown(inner_new_rows)
            _b(
                "warn",
                (
                    f"<strong>{len(inner_new_rows):,}</strong> member(s) present only in the "
                    f"<strong>new</strong> build inside <strong>{len(grouped):,}</strong> host "
                    f"archive(s) ({kinds}): {snippet}."
                    if as_html
                    else (
                        f"{len(inner_new_rows):,} member(s) present only in the new build inside "
                        f"{len(grouped):,} host archive(s) ({kinds}): {snippet}."
                    )
                ),
            )

        high_diff = self._compute_high_size_diff_alert(modified)
        if high_diff:
            top_bits: list[str] = []
            for r, _diff, pct in high_diff[:4]:
                leaf = self._path_leaf_name_static(r.relative_path)
                if as_html:
                    top_bits.append(f"<code>{html.escape(leaf)}</code> ({pct:.1f}%)")
                else:
                    top_bits.append(f"{leaf} ({pct:.1f}%)")
            top_txt = "; ".join(top_bits)
            more = len(high_diff) - len(top_bits)
            if more > 0:
                top_txt += f"; +{more} more"
            _b(
                "alert",
                (
                    f"<strong>{len(high_diff):,}</strong> file(s) with major size difference "
                    f"(|change| ≥ {_CRM_HIGH_SIZE_ALERT_MIN_ABS_PCT:.0f}%): {top_txt}."
                    if as_html
                    else (
                        f"{len(high_diff):,} file(s) with major size difference "
                        f"(|change| >= {_CRM_HIGH_SIZE_ALERT_MIN_ABS_PCT:.0f}%): {top_txt}."
                    )
                ),
            )
        else:
            _b(
                "ok",
                (
                    f"No major size differences — no file changed by "
                    f"{_CRM_HIGH_SIZE_ALERT_MIN_ABS_PCT:.0f}% or more (increase or decrease)."
                ),
            )

        if c["files_only_old"]:
            _b(
                "warn",
                (
                    f"<strong>{c['files_only_old']:,}</strong> top-level path(s) unmatched on the "
                    f"<strong>old</strong> side after rename/move alignment."
                    if as_html
                    else (
                        f"{c['files_only_old']:,} top-level path(s) unmatched on the old side "
                        f"after rename/move alignment."
                    )
                ),
            )
        if c["files_only_new"]:
            _b(
                "warn",
                (
                    f"<strong>{c['files_only_new']:,}</strong> top-level path(s) unmatched on the "
                    f"<strong>new</strong> side after rename/move alignment."
                    if as_html
                    else (
                        f"{c['files_only_new']:,} top-level path(s) unmatched on the new side "
                        f"after rename/move alignment."
                    )
                ),
            )

        if self.compare_clfy_agent_zip and self.clfy_agent_zip_diff:
            cd = self.clfy_agent_zip_diff
            label = str(cd.get("label") or CLFY_AGENT_BUNDLE_REPORT_LABEL)
            if cd.get("error"):
                err = html.escape(str(cd["error"])) if as_html else str(cd["error"])
                _b("alert", f"{label}: compare failed — {err}")
            else:
                mo = int(cd.get("summary_members_only_old") or 0)
                mn = int(cd.get("summary_members_only_new") or 0)
                md = int(cd.get("summary_members_different") or 0)
                mc = int(cd.get("summary_class_changed") or 0)
                co = int(cd.get("summary_only_old") or 0)
                cn = int(cd.get("summary_only_new") or 0)
                if mo or mn or md or mc or co or cn:
                    lbl = html.escape(label) if as_html else label
                    _b(
                        "warn",
                        (
                            f"{lbl}: {mo:,} inner member(s) only in old, {mn:,} only in new, "
                            f"{md:,} content diff, {mc:,} .class change(s)"
                            + (
                                f"; {co:,} nested container(s) only in old, {cn:,} only in new."
                                if (co or cn)
                                else "."
                            )
                        ),
                    )
                else:
                    _b("ok", f"{label}: no inner member differences detected.")

        manifest_diffs = [
            j
            for j in (self.jar_manifest_diff_list or [])
            if j.get("manifest_impl_version_diff")
        ]
        if manifest_diffs:
            names = [
                html.escape(str(j.get("query") or "?")) if as_html else str(j.get("query") or "?")
                for j in manifest_diffs[:5]
            ]
            rest = len(manifest_diffs) - len(names)
            name_txt = ", ".join(names)
            if rest > 0:
                name_txt += f", +{rest} more"
            _b(
                "warn",
                (
                    f"Manifest <strong>Implementation-Version</strong> differs for "
                    f"<strong>{len(manifest_diffs):,}</strong> queried JAR(s): {name_txt}."
                    if as_html
                    else (
                        f"Manifest Implementation-Version differs for {len(manifest_diffs):,} "
                        f"queried JAR(s): {name_txt}."
                    )
                ),
            )

        for jd in self.jar_class_diff_list or []:
            q = str(jd.get("query") or "?")
            q_disp = html.escape(q) if as_html else q
            if jd.get("error"):
                err = html.escape(str(jd["error"])) if as_html else str(jd["error"])
                _b("warn", f"JAR class diff ({q_disp}): {err}")
                continue
            oio = len(jd.get("only_in_old") or [])
            oin = len(jd.get("only_in_new") or [])
            mod_n = len(jd.get("modified") or [])
            if oio or oin or mod_n:
                _b(
                    "warn",
                    (
                        f"JAR class diff <strong>{q_disp}</strong>: {oio:,} .class removed, "
                        f"{oin:,} added, {mod_n:,} modified."
                        if as_html
                        else (
                            f"JAR class diff «{q}»: {oio:,} .class removed, {oin:,} added, "
                            f"{mod_n:,} modified."
                        )
                    ),
                )

        if removed or added:
            if align_cache is not None:
                align_pairs, unpaired_r, unpaired_a = align_cache
            elif as_html:
                align_pairs, unpaired_r, unpaired_a = self._crm_align_for_html_report(
                    removed, added
                )
            else:
                align_pairs, unpaired_r, unpaired_a = self._crm_align_removed_added_for_report(
                    removed, added, min_ratio=0.35
                )
            _b(
                "info",
                (
                    f"Removed/added alignment: <strong>{len(align_pairs):,}</strong> probable "
                    f"rename/move pair(s); <strong>{len(unpaired_r):,}</strong> unmatched removed, "
                    f"<strong>{len(unpaired_a):,}</strong> unmatched added."
                    if as_html
                    else (
                        f"Removed/added alignment: {len(align_pairs):,} probable rename/move "
                        f"pair(s); {len(unpaired_r):,} unmatched removed, {len(unpaired_a):,} "
                        f"unmatched added."
                    )
                ),
            )

        if not any(l in ("alert", "warn") for l, _ in items):
            _b(
                "ok",
                (
                    "No whole-archive removals, no inner member gaps in shared archives, and no "
                    f"major (>={_CRM_HIGH_SIZE_ALERT_MIN_ABS_PCT:.0f}%) size differences detected in this run."
                    if as_html
                    else (
                        "No whole-archive removals, no inner member gaps in shared archives, and "
                        f"no major (>={_CRM_HIGH_SIZE_ALERT_MIN_ABS_PCT:.0f}%) size differences detected in this run."
                    )
                ),
            )

        return items

    def _html_at_a_glance_panel(
        self,
        added: list[CRMComparisonResult],
        removed: list[CRMComparisonResult],
        modified: list[CRMComparisonResult],
        unchanged_count: int,
        *,
        counts: Optional[dict] = None,
        delta: Optional[dict] = None,
        align_cache: Optional[tuple] = None,
    ) -> str:
        items = self._build_at_a_glance_items(
            added,
            removed,
            modified,
            unchanged_count,
            as_html=True,
            counts=counts,
            delta=delta,
            align_cache=align_cache,
        )
        levels = [lv for lv, _ in items]
        if "alert" in levels:
            glance_bg, glance_border, title_color = "#fef2f2", "#fca5a5", "#991b1b"
        elif "warn" in levels:
            glance_bg, glance_border, title_color = "#fff7ed", "#fdba74", "#9a3412"
        elif levels and all(lv == "ok" for lv in levels):
            glance_bg, glance_border, title_color = "#ecfdf5", "#6ee7b7", "#047857"
        else:
            glance_bg, glance_border, title_color = "#f8fafc", "#e2e8f0", "#0f172a"
        li_colors = {
            "alert": "#991b1b",
            "warn": "#92400e",
            "info": "#334155",
            "ok": "#047857",
        }
        markers = {"alert": "⚠️", "warn": "●", "info": "○", "ok": "✓"}
        lis = "".join(
            f'<li style="color:{li_colors.get(lv, "#334155")};margin-bottom:6px;">'
            f'{markers.get(lv, "•")} {text}</li>'
            for lv, text in items
        )
        return f"""
            <div style="margin:0 0 14px 0;padding:12px 14px;border-radius:10px;border:1px solid {glance_border};background:{glance_bg};font-size:14px;color:#0f172a;line-height:1.55;">
                <strong style="display:block;margin-bottom:8px;color:{title_color};">At a glance — this comparison run</strong>
                <ul style="margin:0;padding-left:1.25rem;">{lis}</ul>
                <p style="margin:10px 0 0 0;font-size:11px;color:#64748b;line-height:1.45;">
                    Inner gaps = files, classes, folders, or nested archives missing inside a host
                    .jar/.war/.ear/.zip that exists on both builds. Whole missing containers are listed separately.
                    Major size = |% change| ≥ {_CRM_HIGH_SIZE_ALERT_MIN_ABS_PCT:.0f}%.
                </p>
            </div>"""
    def _html_side_by_side_summary_table(
        self,
        added: list[CRMComparisonResult],
        removed: list[CRMComparisonResult],
        modified: list[CRMComparisonResult],
        unchanged_count: int,
        *,
        compact: bool = False,
        align_cache: Optional[tuple] = None,
    ) -> str:
        """Single HTML table: Old build vs New build counts (full + compact HTML reports)."""
        c = self._side_by_side_counts(
            added, removed, modified, unchanged_count, align_cache=align_cache
        )
        high_diff_count = len(self._compute_high_size_diff_alert(modified))
        d = self._count_delta_details(added, removed, modified, align_cache=align_cache)
        wrap = "margin:0 0 20px 0;padding:0;" if compact else "margin:0 0 28px 0;padding:0;"
        title = (
            "<h2 class='section-title' style='margin-top:0;font-size:18px;'>📊 Build comparison summary (Old vs New)</h2>"
            if not compact
            else "<h2 style='margin:0 0 12px 0;color:#0f172a;font-size:17px;'>📊 Build comparison summary (Old vs New)</h2>"
        )
        tbl_cls = "summary-compare-table" + (" compact" if compact else "")
        glance = self._html_at_a_glance_panel(
            added,
            removed,
            modified,
            unchanged_count,
            counts=c,
            delta=d,
            align_cache=align_cache,
        )
        path_delta = int(d["path_delta"])
        jar_delta = int(d["jar_delta"])
        inner_old_rows = d.get("inner_old_rows") or []
        inner_new_rows = d.get("inner_new_rows") or []
        inner_rows_html = ""
        if inner_old_rows or inner_new_rows:
            def _inner_table(title: str, rows: list) -> str:
                if not rows:
                    return ""
                lim = 500
                body = "".join(
                    f"<tr>"
                    f"<td style='padding:8px 10px;border-bottom:1px solid #e2e8f0;font-family:Consolas,monospace;font-size:11px;word-break:break-all;'>{html.escape(str(r.get('host') or ''))}</td>"
                    f"<td style='padding:8px 10px;border-bottom:1px solid #e2e8f0;font-family:Consolas,monospace;font-size:11px;word-break:break-all;'>{html.escape(str(r.get('member') or ''))}</td>"
                    f"<td style='padding:8px 10px;border-bottom:1px solid #e2e8f0;font-size:11px;'>{html.escape(str(r.get('kind') or 'file'))}</td>"
                    f"</tr>"
                    for r in rows[:lim]
                )
                rest = (
                    f"<p style='margin:8px 0 0 0;font-size:12px;color:#64748b;'>… and {len(rows) - lim} more</p>"
                    if len(rows) > lim
                    else ""
                )
                return f"""
                <div style="margin:12px 0 0 0;">
                    <div style="font-size:13px;font-weight:600;color:#0f172a;margin-bottom:6px;">{html.escape(title)} ({len(rows):,})</div>
                    <div style="overflow-x:auto;border:1px solid #e2e8f0;border-radius:8px;">
                        <table style="width:100%;border-collapse:collapse;min-width:640px;">
                            <thead><tr style="background:#f8fafc;color:#0f172a;">
                                <th style="padding:8px 10px;text-align:left;">Host archive</th>
                                <th style="padding:8px 10px;text-align:left;">Member path inside archive</th>
                                <th style="padding:8px 10px;text-align:left;">Kind</th>
                            </tr></thead>
                            <tbody>{body}</tbody>
                        </table>
                    </div>
                    {rest}
                </div>"""

            inner_rows_html = f"""
            <div style="margin:12px 0 0 0;padding:12px;border-radius:10px;border:1px solid #cbd5e1;background:#fff;">
                <div style="font-size:14px;font-weight:700;color:#0f172a;margin-bottom:8px;">
                    Inside-JAR folder / class / file differences
                </div>
                <p style="font-size:12px;color:#64748b;margin:0 0 10px 0;line-height:1.5;">
                    Members below differ inside archives that exist on <strong>both</strong> builds (e.g. missing <code>META-INF/maven</code> tree, classes, or other files).
                    Whole missing JAR/WAR/EAR containers are listed separately above.
                </p>
                {_inner_table("Only in OLD build (inside archives)", inner_old_rows)}
                {_inner_table("Only in NEW build (inside archives)", inner_new_rows)}
            </div>"""
        delta_detail_html = ""
        if path_delta != 0:
            side_label = "old" if path_delta > 0 else "new"
            path_note = ""
            if len(d["path_rows"]) < abs(path_delta):
                path_note = (
                    f"<p style='margin:8px 0 0 0;font-size:12px;color:#92400e;'>"
                    f"Listed {len(d['path_rows']):,} row(s); expected delta is {abs(path_delta):,}."
                    f"</p>"
                )
            path_tbl = self._html_side_by_side_mismatch_table(
                d["path_rows"],
                title=(
                    f"Paths count mismatch details: {abs(path_delta):,} more path(s) on "
                    f"<span style=\"text-transform:uppercase;\">{side_label}</span> side"
                ),
                subtitle=(
                    "Side-by-side confirmation after rename/move alignment. "
                    f"<span style=\"color:{_CRM_MISSING_DARK_RED};font-weight:700;\">Dark red</span> "
                    "marks missing paths and file names on one build only."
                ),
                name_header="File name",
            ) + path_note
            delta_detail_html += self._html_wrap_report_section(
                "path-mismatch-details",
                (
                    f"Paths count mismatch details: {abs(path_delta):,} more path(s) on "
                    f"<span style=\"text-transform:uppercase;\">{side_label}</span> side"
                ),
                path_tbl,
                open_default=True,
            )
        if jar_delta != 0:
            side_label = "old" if jar_delta > 0 else "new"
            jar_note = ""
            if len(d["jar_rows"]) < abs(jar_delta):
                jar_note = (
                    f"<p style='margin:8px 0 0 0;font-size:12px;color:#92400e;'>"
                    f"Listed {len(d['jar_rows']):,} row(s); expected delta is {abs(jar_delta):,} whole container(s)."
                    f"</p>"
                )
            jar_tbl = self._html_side_by_side_mismatch_table(
                d["jar_rows"],
                title=(
                    f"JAR / WAR / EAR count mismatch details: {abs(jar_delta):,} more whole archive(s) on "
                    f"<span style=\"text-transform:uppercase;\">{side_label}</span> side"
                ),
                subtitle=(
                    "Whole container archives only (top-level and nested paths such as "
                    "<code>host.jar!/inner.war</code>). "
                    f"<span style=\"color:{_CRM_MISSING_DARK_RED};font-weight:700;\">Dark red</span> "
                    "highlights missing archive paths and names."
                ),
                name_header="Archive name (JAR / WAR / EAR)",
            ) + jar_note
            delta_detail_html += self._html_wrap_report_section(
                "jar-mismatch-details",
                (
                    f"JAR / WAR / EAR count mismatch details: {abs(jar_delta):,} more whole archive(s) on "
                    f"<span style=\"text-transform:uppercase;\">{side_label}</span> side"
                ),
                jar_tbl,
                open_default=True,
            )
        if inner_rows_html:
            delta_detail_html += self._html_wrap_report_section(
                "inner-jar-member-diffs",
                "Inside-JAR folder / class / file differences",
                inner_rows_html,
                open_default=False,
            )
        summary_inner = f"""
            {glance}
            {delta_detail_html}
            <div style="overflow-x:auto;">
            <table class="{tbl_cls}">
            <thead><tr><th>Metric</th><th class="right">Old Build</th><th class="right">New Build</th></tr></thead>
            <tbody>
            <tr><td>Total files (in tree)</td><td class="right">{c["total_old"]:,}</td><td class="right">{c["total_new"]:,}</td></tr>
            <tr><td>Paths unmatched on old side (aligned)</td><td class="right">{c["files_only_old"]:,}</td><td class="right">0</td></tr>
            <tr><td>Paths unmatched on new side (aligned)</td><td class="right">0</td><td class="right">{c["files_only_new"]:,}</td></tr>
            <tr><td>Unchanged files</td><td class="right">{c["unchanged"]:,}</td><td class="right">{c["unchanged"]:,}</td></tr>
            <tr><td>Modified files</td><td class="right">{c["modified"]:,}</td><td class="right">{c["modified"]:,}</td></tr>
            <tr><td>Major size differences (|change| ≥ {_CRM_HIGH_SIZE_ALERT_MIN_ABS_PCT:.0f}%)</td>{(
                f'<td class="right" colspan="2" style="color:#047857;font-size:13px;font-style:italic;text-align:left;">'
                f'Not found any files with a size change of {_CRM_HIGH_SIZE_ALERT_MIN_ABS_PCT:.0f}% or more '
                f'(increase or decrease).</td>'
                if high_diff_count == 0
                else f'<td class="right">{high_diff_count:,}</td><td class="right">{high_diff_count:,}</td>'
            )}</tr>
            <tr><td>JAR / WAR / EAR added</td><td class="right">0</td><td class="right">{c["jar_added_new"]:,}</td></tr>
            <tr><td>JAR / WAR / EAR removed</td><td class="right">{c["jar_removed_old"]:,}</td><td class="right">0</td></tr>
            </tbody>
            </table>
            </div>
            <p style="font-size:12px;color:#64748b;margin:10px 0 0 0;line-height:1.5;">
            <strong>Total files</strong> = one row per path in this scan (old = removed + modified + unchanged; new = added + modified + unchanged).
            <strong>Unmatched path rows</strong> are computed after removed/added rename/move alignment (path similarity heuristic), so paths that likely moved/renamed are not counted as missing.
            <strong>JAR / WAR / EAR</strong> counts whole container archives removed/added on one build (top-level + ClfyAgent containers). Class/folder/file diffs inside modified archives are listed separately.
            </p>"""
        return (
            f'<div class="summary-side-by-side" style="{wrap}">'
            + self._html_wrap_report_section(
                "build-comparison-summary",
                "📊 Build comparison summary (Old vs New)",
                summary_inner,
                open_default=True,
            )
            + "</div>"
        )

    def _html_nav_manifest_summary_table(self) -> str:
        """Top-of-report table: manifest diff queries with versions and paths (replaces per-JAR nav links)."""
        jdl = self.jar_manifest_diff_list or []
        if not jdl:
            return ""
        rows: list[str] = []
        for jd in jdl:
            q = html.escape(str(jd.get("query") or ""))
            err = jd.get("error")
            jso = (jd.get("jar_old_source") or "").strip() or (jd.get("jar_old") or "") or "—"
            jsn = (jd.get("jar_new_source") or "").strip() or (jd.get("jar_new") or "") or "—"
            mvo = jd.get("manifest_impl_version_old")
            mvn = jd.get("manifest_impl_version_new")
            evo = jd.get("manifest_impl_version_error_old") or ""
            evn = jd.get("manifest_impl_version_error_new") or ""
            vo = html.escape(str(mvo) if mvo else (f"n/a ({evo})" if evo else "n/a"))
            vn = html.escape(str(mvn) if mvn else (f"n/a ({evn})" if evn else "n/a"))
            po = html.escape(str(jso))
            pn = html.escape(str(jsn))
            pao = _crm_html_jar_paths_list_cell(jd.get("jar_paths_all_old"))
            pan = _crm_html_jar_paths_list_cell(jd.get("jar_paths_all_new"))
            clfo = (jd.get("clfy_jar_path_old") or "").strip()
            clfn = (jd.get("clfy_jar_path_new") or "").strip()
            cfo = (
                f"<span style='font-size:11px;word-break:break-all;color:#0f172a'>{html.escape(clfo)}</span>"
                if clfo
                else "<span style=\"color:#94a3b8;\">—</span>"
            )
            cfn = (
                f"<span style='font-size:11px;word-break:break-all;color:#0f172a'>{html.escape(clfn)}</span>"
                if clfn
                else "<span style=\"color:#94a3b8;\">—</span>"
            )
            jpath_old = (jd.get("jar_old") or "") or None
            jpath_new = (jd.get("jar_new") or "") or None
            st = _crm_jar_pair_downloads_html(
                str(jpath_old) if jpath_old else None,
                str(jpath_new) if jpath_new else None,
                err,
                str(jd.get("jar_old_source") or "") or None,
                str(jd.get("jar_new_source") or "") or None,
            )
            mcell = "padding:8px 10px;border-bottom:1px solid #e2e8f0;vertical-align:top;"
            rows.append(
                f"<tr><td style='{mcell}font-weight:600;word-break:break-word;'>{q}</td>"
                f"<td style='{mcell}font-family:Consolas,monospace;font-size:12px;word-break:break-word;'>{vo}</td>"
                f"<td style='{mcell}font-family:Consolas,monospace;font-size:12px;word-break:break-word;'>{vn}</td>"
                f"<td style='{mcell}font-size:11px;word-break:break-all;color:#475569;'>{po}</td>"
                f"<td style='{mcell}font-size:11px;word-break:break-all;color:#475569;'>{pn}</td>"
                f"<td style='{mcell}word-break:break-word;'>{pao}</td>"
                f"<td style='{mcell}word-break:break-word;'>{pan}</td>"
                f"<td style='{mcell}word-break:break-word;'>{cfo}</td>"
                f"<td style='{mcell}word-break:break-word;'>{cfn}</td>"
                f"<td style='{mcell}font-size:11px;white-space:normal;'>{st}</td></tr>"
            )
        thm = (
            "padding:10px;text-align:left;vertical-align:top;font-weight:600;"
            "white-space:normal;word-break:break-word;"
        )
        colgroup_manifest = (
            "<colgroup>"
            "<col style='width:9%' />"
            "<col style='width:6%' />"
            "<col style='width:6%' />"
            "<col style='width:13%' />"
            "<col style='width:13%' />"
            "<col style='width:13%' />"
            "<col style='width:13%' />"
            "<col style='width:9%' />"
            "<col style='width:9%' />"
            "<col style='width:9%' />"
            "</colgroup>"
        )
        return (
            f"""
            <div style="margin:16px 0 8px 0;">
                <h4 style="margin:0 0 8px 0;font-size:15px;color:#0f172a;">📋 JAR manifest diff (optional) — summary</h4>
                <p style="font-size:12px;color:#64748b;margin:0 0 10px 0;line-height:1.5;">
                <strong>Primary JAR path</strong> is the on-disk or logical path used to read the manifest (first match in the build tree). 
                <strong>All matching paths</strong> lists every relative path in the scan where the JAR name matched, plus the path inside
                the ClfyAgent bundle when that exists. <strong>Under ClfyAgent.ear (zip) only</strong> is the virtual path to the JAR
                <em>inside</em> the ClfyAgent container without relying on a duplicate match elsewhere in the tree.
                </p>
                <div style="overflow-x:auto;border:1px solid #cbd5e1;border-radius:10px;background:#fff;">
                <table style="width:100%;table-layout:fixed;border-collapse:collapse;font-size:13px;min-width:1400px;">
                {colgroup_manifest}
                <thead>
                <tr style="background:linear-gradient(135deg,#1e3a5f 0%,#2d5a87 100%);color:#fff;">
                    <th style="{thm}">JAR name / query</th>
                    <th style="{thm}">MANIFEST.MF Impl-Version (old)</th>
                    <th style="{thm}">MANIFEST.MF Impl-Version (new)</th>
                    <th style="{thm}">Primary JAR path (old)</th>
                    <th style="{thm}">Primary JAR path (new)</th>
                    <th style="{thm}">All matching paths (old)</th>
                    <th style="{thm}">All matching paths (new)</th>
                    <th style="{thm}">In ClfyAgent bundle only (old)</th>
                    <th style="{thm}">In ClfyAgent bundle only (new)</th>
                    <th style="{thm}">Download JARs</th>
                </tr>
                </thead>
                <tbody>{"".join(rows)}</tbody>
                </table>
                </div>"""
            + _crm_jar_download_footnote_html()
            + """
            </div>"""
        )

    def _html_nav_class_diff_summary_table(self) -> str:
        """Top-of-report table: class-diff JAR queries with counts, build paths, and link (replaces per-JAR nav links)."""
        jdl = self.jar_class_diff_list or []
        if not jdl:
            return ""
        rows: list[str] = []
        total_java_diffs = 0
        for idx, jd in enumerate(jdl):
            q = html.escape(str(jd.get("query") or ""))
            iq = html.escape(str(jd.get("input_query") or jd.get("query") or ""))
            err = jd.get("error")
            mod_n = len(jd.get("modified") or [])
            jn = len(jd.get("java_diffs") or [])
            total_java_diffs += jn
            jso = (jd.get("jar_old_source") or "").strip() or (jd.get("jar_old") or "") or "—"
            jsn = (jd.get("jar_new_source") or "").strip() or (jd.get("jar_new") or "") or "—"
            po = html.escape(str(jso))
            pn = html.escape(str(jsn))
            pao = _crm_html_jar_paths_list_cell(jd.get("jar_paths_all_old"))
            pan = _crm_html_jar_paths_list_cell(jd.get("jar_paths_all_new"))
            clfo = (jd.get("clfy_jar_path_old") or "").strip()
            clfn = (jd.get("clfy_jar_path_new") or "").strip()
            cfo = (
                f"<span style='font-size:11px;word-break:break-all;color:#0f172a'>{html.escape(clfo)}</span>"
                if clfo
                else "<span style=\"color:#94a3b8;\">—</span>"
            )
            cfn = (
                f"<span style='font-size:11px;word-break:break-all;color:#0f172a'>{html.escape(clfn)}</span>"
                if clfn
                else "<span style=\"color:#94a3b8;\">—</span>"
            )
            jpath_old = (jd.get("jar_old") or "") or None
            jpath_new = (jd.get("jar_new") or "") or None
            st = _crm_jar_pair_downloads_html(
                str(jpath_old) if jpath_old else None,
                str(jpath_new) if jpath_new else None,
                err,
                str(jd.get("jar_old_source") or "") or None,
                str(jd.get("jar_new_source") or "") or None,
            )
            rows.append(
                f"<tr><td style='padding:8px 10px;border-bottom:1px solid #e2e8f0;font-weight:600;vertical-align:top;'>{q}</td>"
                f"<td style='padding:8px 10px;border-bottom:1px solid #e2e8f0;font-size:11px;color:#64748b;vertical-align:top;'>{iq}</td>"
                f"<td style='padding:8px 10px;border-bottom:1px solid #e2e8f0;font-size:11px;word-break:break-all;color:#475569;vertical-align:top;'>{po}</td>"
                f"<td style='padding:8px 10px;border-bottom:1px solid #e2e8f0;font-size:11px;word-break:break-all;color:#475569;vertical-align:top;'>{pn}</td>"
                f"<td style='padding:8px 10px;border-bottom:1px solid #e2e8f0;vertical-align:top;'>{pao}</td>"
                f"<td style='padding:8px 10px;border-bottom:1px solid #e2e8f0;vertical-align:top;'>{pan}</td>"
                f"<td style='padding:8px 10px;border-bottom:1px solid #e2e8f0;vertical-align:top;'>{cfo}</td>"
                f"<td style='padding:8px 10px;border-bottom:1px solid #e2e8f0;vertical-align:top;'>{cfn}</td>"
                f"<td style='padding:8px 10px;border-bottom:1px solid #e2e8f0;text-align:right;vertical-align:top;'>{mod_n}</td>"
                f"<td style='padding:8px 10px;border-bottom:1px solid #e2e8f0;text-align:right;vertical-align:top;'>{jn}</td>"
                f"<td style='padding:8px 10px;border-bottom:1px solid #e2e8f0;font-size:11px;vertical-align:top;'>{st}</td>"
                f"<td style='padding:8px 10px;border-bottom:1px solid #e2e8f0;text-align:center;vertical-align:top;'>"
                f"<a href='#jar-class-diff-{idx}' style='color:#2563eb;font-weight:600;'>Open</a></td></tr>"
            )
        return (
            f"""
            <div style="margin:16px 0 8px 0;">
                <h4 style="margin:0 0 8px 0;font-size:15px;color:#0f172a;">☕ JAR class file diff (optional) — summary</h4>
                <p style="font-size:12px;color:#64748b;margin:0 0 10px 0;">Same path columns as the manifest summary: <strong>primary</strong> resolution, <strong>all matching</strong> relative paths, and
                <strong>ClfyAgent bundle only</strong> when the JAR is present inside that container.</p>
                <div style="overflow-x:auto;border:1px solid #bfdbfe;border-radius:10px;background:#fff;">
                <table style="width:100%;border-collapse:collapse;font-size:13px;min-width:1180px;">
                <thead>
                <tr style="background:linear-gradient(135deg,#1e40af 0%,#3b82f6 100%);color:#fff;">
                    <th style="padding:10px;text-align:left;">Resolved JAR / query</th>
                    <th style="padding:10px;text-align:left;">Input query</th>
                    <th style="padding:10px;text-align:left;">Primary JAR path (old)</th>
                    <th style="padding:10px;text-align:left;">Primary JAR path (new)</th>
                    <th style="padding:10px;text-align:left;">All matching paths (old)</th>
                    <th style="padding:10px;text-align:left;">All matching paths (new)</th>
                    <th style="padding:10px;text-align:left;">In ClfyAgent bundle (old)</th>
                    <th style="padding:10px;text-align:left;">In ClfyAgent bundle (new)</th>
                    <th style="padding:10px;text-align:right;">Modified .class</th>
                    <th style="padding:10px;text-align:right;">Java diffs</th>
                    <th style="padding:10px;text-align:left;">Download JARs</th>
                    <th style="padding:10px;text-align:center;">Section</th>
                </tr>
                </thead>
                <tbody>{"".join(rows)}</tbody>
                </table>
                </div>
                <p style="margin:10px 0 0 0;font-size:12px;color:#334155;line-height:1.55;">
                    <strong>Java source differences — Perforce-style unified diff</strong> ({total_java_diffs} total).
                    Use <strong>Open</strong> above for decompiled old vs new source diffs per JAR.
                </p>"""
            + _crm_jar_download_footnote_html()
            + """
            </div>"""
        )

    def _extract_jar_version_display_token(self, filename: str) -> str:
        """Short version token from a JAR filename (e.g. __V10_24_1_0)."""
        base = os.path.basename(str(filename or ""))
        if not base:
            return ""
        m = re.search(r"(__V\d+(?:_\d+)*)", base, re.IGNORECASE)
        if m:
            return m.group(1)
        for pat in (
            r"[-_](\d+\.\d+(?:\.\d+)*(?:\.\d+)*)",
            r"[-_](\d+\.\d+(?:\.\d+)*)",
        ):
            m = re.search(pat, base)
            if m:
                return m.group(1)
        norm = self._get_archive_base_name(base)
        if norm and base.lower() != norm.lower():
            stem, _ext = os.path.splitext(base)
            norm_stem = os.path.splitext(norm)[0]
            if stem.lower().startswith(norm_stem.lower()) and len(stem) > len(norm_stem):
                tail = stem[len(norm_stem) :].lstrip("_-")
                if tail:
                    return tail
        return ""

    def _logical_jar_display_label(self, filename: str) -> str:
        """Human-readable logical JAR name without version tokens."""
        base = os.path.basename(str(filename or ""))
        stem, ext = os.path.splitext(base)
        if not stem:
            return base
        label_stem = re.sub(r"__V\d+(?:_\d+)*", "", stem, flags=re.IGNORECASE)
        label_stem = re.sub(r"[-_]unsigned$", "", label_stem, flags=re.IGNORECASE)
        label_stem = re.sub(
            r"[-_]\d+\.\d+(?:\.\d+)*(?:\.\d+)*(?=[-_]|$)", "", label_stem
        )
        label_stem = re.sub(r"[-_]\d{8}_\d{4}$", "", label_stem)
        label_stem = re.sub(r"_+", "_", label_stem).strip("_-")
        return (label_stem or stem) + (ext.lower() if ext else ".jar")

    def _logical_jar_aggregate_key(self, filename: str) -> str:
        return self._get_archive_base_name(os.path.basename(str(filename or ""))).lower()

    @staticmethod
    def _is_jar_leaf_filename(filename: str) -> bool:
        return str(filename or "").lower().endswith(".jar")

    def _merge_jar_version_tokens(
        self,
        agg: dict,
        old_leaf: str,
        new_leaf: str,
        *,
        old_full: str = "",
        new_full: str = "",
    ) -> None:
        key = self._logical_jar_aggregate_key(old_leaf or new_leaf)
        if not key:
            return
        if key not in agg:
            agg[key] = {
                "label": self._logical_jar_display_label(old_leaf or new_leaf),
                "old": set(),
                "new": set(),
                "old_paths": set(),
                "new_paths": set(),
            }
        o_tok = self._extract_jar_version_display_token(old_leaf)
        n_tok = self._extract_jar_version_display_token(new_leaf)
        if old_leaf:
            agg[key]["old"].add(o_tok or os.path.basename(old_leaf))
        if new_leaf:
            agg[key]["new"].add(n_tok or os.path.basename(new_leaf))
        if old_full:
            agg[key]["old_paths"].add(_crm_norm_rel_path(old_full))
        if new_full:
            agg[key]["new_paths"].add(_crm_norm_rel_path(new_full))

    def _pick_version_changed_path_examples(self, entry: dict) -> tuple[str, str]:
        old_paths = sorted(entry.get("old_paths") or [], key=lambda x: (len(x), x.lower()))
        new_paths = sorted(entry.get("new_paths") or [], key=lambda x: (len(x), x.lower()))
        old_ex = old_paths[0] if old_paths else ""
        new_ex = new_paths[0] if new_paths else ""
        if old_ex and not new_ex:
            new_ex = self._crm_expected_new_path_for_old(old_ex)
        elif new_ex and not old_ex:
            old_ex = self._crm_expected_old_path_for_new(new_ex)
        else:
            if new_ex:
                new_ex = self._crm_map_path_across_release(new_ex, to_new=True)
            if old_ex:
                old_ex = self._crm_map_path_across_release(old_ex, to_new=False)
        return old_ex, new_ex

    def _accumulate_jar_upgrades_from_member_sets(
        self,
        agg: dict,
        only_old: set,
        only_new: set,
    ) -> None:
        """Pair nested JAR members that share a normalized path key but differ in version suffix."""
        old_by_mk: dict[str, set[str]] = defaultdict(set)
        new_by_mk: dict[str, set[str]] = defaultdict(set)
        for m in only_old or set():
            ms = str(m).strip()
            leaf = self._path_leaf_name_static(ms)
            if not self._is_jar_leaf_filename(leaf):
                continue
            old_by_mk[self._member_match_key(ms)].add(ms)
        for m in only_new or set():
            ms = str(m).strip()
            leaf = self._path_leaf_name_static(ms)
            if not self._is_jar_leaf_filename(leaf):
                continue
            new_by_mk[self._member_match_key(ms)].add(ms)
        for mk in set(old_by_mk.keys()) & set(new_by_mk.keys()):
            olds = old_by_mk[mk]
            news = new_by_mk[mk]
            if olds == news:
                continue
            by_lo: dict[str, set[str]] = defaultdict(set)
            by_ln: dict[str, set[str]] = defaultdict(set)
            for o in olds:
                by_lo[self._logical_jar_aggregate_key(self._path_leaf_name_static(o))].add(o)
            for n in news:
                by_ln[self._logical_jar_aggregate_key(self._path_leaf_name_static(n))].add(n)
            for lk in set(by_lo.keys()) & set(by_ln.keys()):
                for o in by_lo[lk]:
                    for n in by_ln[lk]:
                        if o.lower() != n.lower():
                            self._merge_jar_version_tokens(
                                agg,
                                self._path_leaf_name_static(o),
                                self._path_leaf_name_static(n),
                                old_full=o,
                                new_full=n,
                            )

    def _format_jar_version_change_summary(self, old_tokens: set, new_tokens: set) -> str:
        olds = sorted((t for t in old_tokens if t), key=lambda x: x.lower())
        news = sorted((t for t in new_tokens if t), key=lambda x: x.lower())
        if not olds and not news:
            return ""
        old_part = ", ".join(olds) if len(olds) > 1 else (olds[0] if olds else "—")
        new_part = ", ".join(news) if len(news) > 1 else (news[0] if news else "—")
        if old_part == new_part:
            return ""
        return f"{old_part} → {new_part}"

    def _collect_version_changed_jar_summaries(self) -> list[dict]:
        """
        One summary row per logical JAR (.jar) whose version suffix changed between builds.
        Includes top-level renames and nested upgrades inside WAR/EAR/SDK (e.g. __V10_24_1_0 → __V10_25).
        """
        agg: dict[str, dict] = {}

        for r in self.results:
            if r.status != "modified":
                continue
            rp = str(r.relative_path or "")
            if " -> " in rp:
                old_p, new_p = (p.strip() for p in rp.split(" -> ", 1))
                old_leaf = self._path_leaf_name_static(old_p)
                new_leaf = self._path_leaf_name_static(new_p)
                if not (
                    self._is_jar_leaf_filename(old_leaf)
                    and self._is_jar_leaf_filename(new_leaf)
                ):
                    continue
                if self._logical_jar_aggregate_key(old_leaf) != self._logical_jar_aggregate_key(
                    new_leaf
                ):
                    continue
                if old_leaf.lower() == new_leaf.lower():
                    continue
                self._merge_jar_version_tokens(
                    agg, old_leaf, new_leaf, old_full=old_p, new_full=new_p
                )
            ad = r.archive_diff or {}
            if ad:
                self._accumulate_jar_upgrades_from_member_sets(
                    agg,
                    set(ad.get("only_in_old") or []),
                    set(ad.get("only_in_new") or []),
                )

        czd = self.clfy_agent_zip_diff or {}
        if not czd.get("error"):
            cad = (czd.get("archive_diff") or {}) if isinstance(czd.get("archive_diff"), dict) else {}
            self._accumulate_jar_upgrades_from_member_sets(
                agg,
                set(cad.get("only_in_old") or []),
                set(cad.get("only_in_new") or []),
            )

        removed_by_logical: dict[str, set[str]] = defaultdict(set)
        added_by_logical: dict[str, set[str]] = defaultdict(set)
        for r in self.results:
            rp = _crm_norm_rel_path(str(r.relative_path or ""))
            if " -> " in rp:
                continue
            leaf = self._path_leaf_name_static(rp)
            if not self._is_jar_leaf_filename(leaf):
                continue
            lk = self._logical_jar_aggregate_key(leaf)
            if r.status == "removed":
                removed_by_logical[lk].add(rp)
            elif r.status == "added":
                added_by_logical[lk].add(rp)
        for lk in set(removed_by_logical.keys()) & set(added_by_logical.keys()):
            olds = removed_by_logical[lk]
            news = added_by_logical[lk]
            if len(olds) == 1 and len(news) == 1:
                o, n = next(iter(olds)), next(iter(news))
                if o.lower() != n.lower():
                    self._merge_jar_version_tokens(
                        agg,
                        self._path_leaf_name_static(o),
                        self._path_leaf_name_static(n),
                        old_full=o,
                        new_full=n,
                    )

        out: list[dict] = []
        for _key in sorted(agg.keys(), key=lambda k: agg[k]["label"].lower()):
            entry = agg[_key]
            summary = self._format_jar_version_change_summary(entry["old"], entry["new"])
            if not summary:
                continue
            old_ex, new_ex = self._pick_version_changed_path_examples(entry)
            out.append(
                {
                    "logical_name": entry["label"],
                    "old_versions": sorted(entry["old"], key=lambda x: x.lower()),
                    "new_versions": sorted(entry["new"], key=lambda x: x.lower()),
                    "summary": summary,
                    "old_path": old_ex,
                    "new_path": new_ex,
                }
            )
        return out

    def _collect_missing_jar_summaries(
        self, *, version_summaries: Optional[list[dict]] = None
    ) -> list[dict]:
        """JAR/WAR/EAR containers present on only one build after signature pairing."""
        covered = self._jar_paths_covered_by_version_summaries(version_summaries)
        unmatched_old, unmatched_new = self._collect_unmatched_jar_archive_paths()
        fast_paths = len(unmatched_old) + len(unmatched_new) > _CRM_ALIGN_SIDE_BY_SIDE_FAST_MAX_PATHS
        out: list[dict] = []
        seen: set[tuple[str, str]] = set()

        def _add(status: str, path: str) -> None:
            path = _crm_norm_rel_path(str(path or "").strip())
            if not path:
                return
            counterpart = ""
            if status == "missing_in_new":
                if self._crm_jar_exists_on_new_build(path):
                    return
                counterpart = (
                    self._crm_fast_expected_new_path_for_old(path)
                    if fast_paths
                    else self._crm_expected_new_path_for_old(path)
                )
            elif status == "missing_in_old":
                counterpart = (
                    self._crm_fast_expected_old_path_for_new(path)
                    if fast_paths
                    else self._crm_expected_old_path_for_new(path)
                )
                if counterpart and not self._crm_path_is_absent_placeholder(counterpart):
                    return
            sig = self._crm_jar_side_by_side_signature(path)
            if sig in covered:
                return
            leaf = self._path_leaf_name_static(path)
            if not self._path_looks_like_java_archive(path):
                return
            logical = self._logical_jar_display_label(leaf)
            key = (status, sig)
            if key in seen:
                return
            seen.add(key)
            out.append(
                {
                    "logical_name": logical,
                    "status": status,
                    "path": path,
                    "counterpart_path": counterpart,
                }
            )

        for p in unmatched_old:
            _add("missing_in_new", p)
        for p in unmatched_new:
            _add("missing_in_old", p)

        out.sort(key=lambda x: (x["status"], x["logical_name"].lower()))
        return out

    def _text_version_changed_jar_summary_lines(self) -> list[str]:
        summaries = self._collect_version_changed_jar_summaries()
        if not summaries:
            return []
        lines = [
            "-" * 80,
            f"VERSION-CHANGED JARs (logical summary, {len(summaries)} component(s))",
            "-" * 80,
        ]
        for item in summaries:
            lines.append(f"  {item['logical_name']}: {item['summary']}")
        lines.append("")
        return lines

    def _html_jar_archive_focus_tables(
        self,
        modified: list[CRMComparisonResult],
    ) -> str:
        """
        Summary table: one row per logical JAR whose version suffix changed (top-level or nested).
        """
        _ = modified  # summaries are built from full results + nested archive diffs
        ver_rows: list[str] = []
        summaries = self._crm_report_version_changed_jar_summaries()
        for item in summaries:
            old_ex = str(item.get("old_path") or "")
            new_ex = str(item.get("new_path") or "")
            name = str(item.get("logical_name") or "")
            summary = str(item.get("summary") or "")
            ver_rows.append(
                "<tr>"
                f"{self._html_bc_style_path_cell(old_ex, kind='version')}"
                f"{self._html_bc_style_path_cell(new_ex, kind='version')}"
                f"{self._html_bc_style_name_cell(name, row_kind='version')}"
                f"{self._html_bc_style_status_cell(summary or 'Version mismatch', row_kind='version')}"
                "</tr>"
            )

        miss_rows: list[str] = []
        missing_summaries = self._crm_report_missing_jar_summaries()
        miss_cap = _CRM_HTML_JAR_MISSING_ROW_CAP
        for item in missing_summaries[:miss_cap]:
            path = str(item.get("path") or "")
            name = str(item.get("logical_name") or "")
            counterpart = str(item.get("counterpart_path") or "")
            if item.get("status") == "missing_in_new":
                new_display = counterpart or path
                is_absent = self._crm_path_is_absent_placeholder(new_display)
                miss_rows.append(
                    "<tr>"
                    f"{self._html_bc_style_path_cell(path, kind='old')}"
                    f"{self._html_bc_style_path_cell(new_display, kind='new', missing=is_absent)}"
                    f"{self._html_bc_style_name_cell(name, row_kind='missing_new' if is_absent else 'version')}"
                    f"{self._html_bc_style_status_cell('Missing in new build' if is_absent else 'Relocated / renamed path', row_kind='missing_new' if is_absent else 'version')}"
                    "</tr>"
                )
            else:
                old_display = counterpart or self._crm_replace_path_leaf(
                    self._crm_map_path_across_release(path, to_new=False),
                    _CRM_ABSENT_NEW_LEAF,
                )
                miss_rows.append(
                    "<tr>"
                    f"{self._html_bc_style_path_cell(old_display, kind='old', missing=True)}"
                    f"{self._html_bc_style_path_cell(path, kind='new')}"
                    f"{self._html_bc_style_name_cell(name, row_kind='missing_old')}"
                    f"{self._html_bc_style_status_cell('Missing in old build', row_kind='missing_old')}"
                    "</tr>"
                )
        if len(missing_summaries) > miss_cap:
            miss_rows.append(
                f"<tr><td colspan='4' style='padding:12px;color:#64748b;font-style:italic;'>"
                f"… and {len(missing_summaries) - miss_cap:,} more missing JAR/archive row(s) not shown.</td></tr>"
            )

        def _tbl(
            title: str,
            subtitle: str,
            thead_html: str,
            body_rows: list[str],
            empty_msg: str,
            colspan: int = 4,
            table_min_width: str = "680px",
            colgroup_html: str = "",
            table_layout_fixed: bool = False,
        ) -> str:
            bod = "".join(body_rows) if body_rows else (
                f"<tr><td colspan='{colspan}' style='padding:12px;color:#64748b;font-style:italic;'>{empty_msg}</td></tr>"
            )
            layout = "table-layout:fixed;" if table_layout_fixed else ""
            return f"""
            <div style="margin:20px 0 12px 0;">
                <h3 style="margin:0 0 6px 0;font-size:17px;color:#0f172a;">{title}</h3>
                <p style="margin:0 0 10px 0;font-size:12px;color:#64748b;line-height:1.5;">{subtitle}</p>
                <div style="overflow-x:auto;border:1px solid #cbd5e1;border-radius:10px;background:#fff;">
                <table style="width:100%;{layout}border-collapse:collapse;font-size:13px;min-width:{table_min_width};">
                {colgroup_html}
                <thead><tr style="background:linear-gradient(135deg,#1e3a5f 0%,#2d5a87 100%);color:#fff;">{thead_html}</tr></thead>
                <tbody>{bod}</tbody>
                </table>
                </div>
            </div>"""

        th_base = "padding:10px;text-align:left;vertical-align:top;font-weight:600;white-space:normal;word-break:break-word;"
        t1h = (
            f"<th style='{th_base}'>Old build path</th>"
            f"<th style='{th_base}'>New build path</th>"
            f"<th style='{th_base}'>Archive name</th>"
            f"<th style='{th_base}'>Version change</th>"
        )
        t2h = (
            f"<th style='{th_base}'>Old build path</th>"
            f"<th style='{th_base}'>New build path</th>"
            f"<th style='{th_base}'>Archive name</th>"
            f"<th style='{th_base}'>Status</th>"
        )
        colgroup_vc = (
            "<colgroup>"
            "<col style='width:32%' />"
            "<col style='width:32%' />"
            "<col style='width:18%' />"
            "<col style='width:18%' />"
            "</colgroup>"
        )
        intro = (
            "<p style='font-size:12px;color:#475569;margin:0 0 16px 0;line-height:1.55;'>"
            "Beyond Compare–style layout: "
            "<span style='background:#cce8ff;padding:2px 8px;border-radius:4px;color:#003366;'>"
            "Blue</span> rows = version mismatch (same logical JAR, different version suffix). "
            "<span style='background:#ffe7e7;padding:2px 8px;border-radius:4px;color:#7f1d1d;'>"
            "Red tint</span> = missing in new build; "
            "<span style='background:#e7f7e7;padding:2px 8px;border-radius:4px;color:#166534;'>"
            "green tint</span> = missing in old build. "
            "Paths use CRM release folder mapping "
            "(<code>CRM_10.24/…</code> ↔ <code>CRM_10.25/…</code>).</p>"
        )
        return (
            intro
            + _tbl(
                "1) Version-changed JARs (blue)",
                "Logical JARs whose version token changed between old and new build "
                "(includes nested upgrades such as <code>__V10_24_1_0 → __V10_25</code>).",
                t1h,
                ver_rows,
                "No version-changed JAR rows in this run.",
                colspan=4,
                table_min_width="1100px",
                colgroup_html=colgroup_vc,
                table_layout_fixed=True,
            )
            + _tbl(
                "2) Missing JARs (one side only)",
                "Whole JAR/WAR/EAR containers present on only one build — not version renames.",
                t2h,
                miss_rows,
                "No missing JAR rows in this run.",
                colspan=4,
                table_min_width="1100px",
                colgroup_html=colgroup_vc,
                table_layout_fixed=True,
            )
        )

    def _crm_path_matches_explicit_token(self, path: str, token: str) -> bool:
        """Match a relative or virtual path to one explicit-size query token."""
        pl = token.strip()
        if not pl or not path:
            return False
        rp = _crm_norm_rel_path(path)
        pln = pl.replace("\\", "/").lstrip("/")
        base = os.path.basename(rp)
        if any(ch in pl for ch in "*?["):
            b_l = base.lower()
            rp_l = rp.lower()
            pl_l = pln.lower()
            return fnmatch.fnmatch(b_l, pl_l) or fnmatch.fnmatch(rp_l, pl_l)
        if "!/" in pln:
            parent_t, _, memb_t = pln.partition("!/")
            if "!/" in rp:
                parent_p, _, memb_p = rp.partition("!/")
                parent_ok = (
                    parent_p.lower() == parent_t.lower()
                    or parent_p.lower().endswith("/" + parent_t.lower())
                    or os.path.basename(parent_p).lower() == parent_t.lower()
                )
                if not parent_ok:
                    return False
                memb_pl = memb_t.lower()
                if "/" in memb_t:
                    mp = memb_p.lower()
                    return mp.endswith(memb_pl) or mp == memb_pl
                return os.path.basename(memb_p).lower() == memb_pl
            return False
        if "/" in pln:
            rpl = rp.lower()
            plnl = pln.lower()
            return rpl.endswith(plnl) or rpl == plnl
        return base.lower() == pln.lower()

    def _crm_row_matches_explicit_size_token(self, r: CRMComparisonResult, token: str) -> bool:
        """Match a result row to one user token (basename, relative suffix, or glob)."""
        rp_full = _crm_norm_rel_path(r.relative_path)
        candidates = [rp_full]
        if " -> " in rp_full:
            a, b = rp_full.split(" -> ", 1)
            candidates = [a.strip(), b.strip()]
        for rp in candidates:
            if rp and self._crm_path_matches_explicit_token(rp, token):
                return True
        return False

    @staticmethod
    def _crm_parse_explicit_token_archive_parts(token: str) -> tuple[Optional[str], str]:
        pln = token.strip().replace("\\", "/").lstrip("/")
        if "!/" in pln:
            parent, _, member = pln.partition("!/")
            return (parent.strip() or None), (member.strip() or pln)
        return None, pln

    @staticmethod
    def _crm_archive_display_prefix(relative_path: str) -> str:
        rp = _crm_norm_rel_path(relative_path)
        if " -> " in rp:
            return rp.split(" -> ", 1)[0].strip()
        return rp

    def _cached_get_archive_members(self, archive_path: Path, *, deep: bool = False) -> tuple[set, dict]:
        if not archive_path or not _path_exists(archive_path) or not _path_is_file(archive_path):
            return set(), {}
        try:
            key = (str(archive_path.resolve()), deep)
        except OSError:
            key = (str(archive_path), deep)
        if key in self._archive_members_lookup_cache:
            return self._archive_members_lookup_cache[key]
        try:
            names, meta = self._get_archive_members(archive_path, deep=deep)
        except Exception as e:
            logger.debug(f"[CRM] explicit size nested lookup failed for {archive_path}: {e}")
            names, meta = set(), {}
        self._archive_members_lookup_cache[key] = (names, meta)
        return names, meta

    def _crm_find_nested_explicit_size_hits(self, token: str) -> list[_ExplicitSizeHit]:
        """Find explicit-size matches inside archive members (e.g. Crm.jnlp inside Crm.war)."""
        parent_filter, member_filter = self._crm_parse_explicit_token_archive_parts(token)
        mf = member_filter or token.strip()
        need_deep = "!/" in mf or ("/" not in mf.replace("\\", "/").lstrip("/") and not parent_filter)
        hits: list[_ExplicitSizeHit] = []

        for display_prefix, p1, p2 in self._crm_iter_explicit_size_archive_pairs():
            prefix_parts = (
                [p.strip() for p in display_prefix.split(" -> ", 1)]
                if " -> " in display_prefix
                else [display_prefix]
            )
            if parent_filter and not any(
                self._crm_path_matches_explicit_token(p, parent_filter) for p in prefix_parts
            ):
                continue

            names1, meta1 = self._cached_get_archive_members(p1, deep=need_deep)
            names2, meta2 = self._cached_get_archive_members(p2, deep=need_deep)
            if not names1 and not names2:
                continue

            for memb in sorted(names1 | names2):
                memb_n = memb.replace("\\", "/")
                virt = f"{display_prefix}!/{memb_n}"
                if not (
                    self._crm_path_matches_explicit_token(virt, token)
                    or self._crm_path_matches_explicit_token(memb_n, member_filter)
                ):
                    continue
                in1 = memb in names1 or memb_n in names1
                in2 = memb in names2 or memb_n in names2
                s1 = meta1.get(memb, meta1.get(memb_n, (None,)))[0] if in1 else None
                s2 = meta2.get(memb, meta2.get(memb_n, (None,)))[0] if in2 else None
                if not in1 and in2:
                    st = "added"
                elif in1 and not in2:
                    st = "removed"
                elif s1 != s2:
                    st = "modified"
                else:
                    st = "unchanged"
                hits.append(
                    _ExplicitSizeHit(
                        relative_path=virt,
                        status=st,
                        size_folder1=s1 if in1 else None,
                        size_folder2=s2 if in2 else None,
                    )
                )
        return hits

    def _crm_explicit_token_needs_nested_lookup(self, token: str, top_level_hits: list[_ExplicitSizeHit]) -> bool:
        """Avoid scanning every archive interior when the query already matched outer archives."""
        if "!/" in token:
            return True
        if not top_level_hits:
            return True
        bare = token.strip().replace("\\", "/").split("/")[-1].lower()
        if bare.endswith((".jar", ".war", ".ear", ".zip")):
            return False
        if any(ch in token for ch in "*?["):
            globs = (".jar", ".war", ".ear", ".zip")
            if any(ext in bare for ext in globs):
                return False
        return True

    def _crm_collect_explicit_size_hits(self, token: str) -> list[_ExplicitSizeHit]:
        """Top-level scan rows plus nested archive members for one query token."""
        hits: list[_ExplicitSizeHit] = []
        seen: set[str] = set()
        for r in self.results:
            if self._crm_row_matches_explicit_size_token(r, token):
                rp = r.relative_path
                if (
                    self._build_root_is_archive()
                    and "!/" not in rp
                    and not (r.is_archive or rp.lower().endswith(CRMFolderComparer._ARCHIVE_ROOT_SUFFIXES))
                ):
                    rp = self._virtual_display_path(rp)
                k = _crm_norm_rel_path(rp).lower()
                if k not in seen:
                    seen.add(k)
                    hits.append(
                        _ExplicitSizeHit(
                            relative_path=rp,
                            status=r.status,
                            size_folder1=r.size_folder1,
                            size_folder2=r.size_folder2,
                        )
                    )
        if self._crm_explicit_token_needs_nested_lookup(token, hits):
            for nh in self._crm_find_nested_explicit_size_hits(token):
                k = _crm_norm_rel_path(nh.relative_path).lower()
                if k not in seen:
                    seen.add(k)
                    hits.append(nh)
        hits.sort(key=lambda x: _crm_norm_rel_path(x.relative_path))
        return hits

    def _explicit_size_hit_abs_paths(self, hit: _ExplicitSizeHit) -> tuple[Path, Path]:
        rp = hit.relative_path.replace("\\", "/")
        o1, m1 = self._resolve_build_file_target(1, rp)
        o2, m2 = self._resolve_build_file_target(2, rp)
        if m1 or m2:
            m = (m1 or m2 or rp.lstrip("/")).strip("/")
            return Path(f"{o1}!/{m}"), Path(f"{o2}!/{m}")
        return o1, o2

    def _html_explicit_file_size_focus(self) -> str:
        """Optional table: user-listed files / globs with old vs new sizes from the scan."""
        raw = (self.explicit_size_compare_query or "").strip()
        tokens = _crm_parse_explicit_size_tokens(raw)
        if not tokens:
            return ""
        fsz = self._format_size_bytes_report
        all_rows: list[str] = []
        for tok in tokens:
            hits = self._crm_collect_explicit_size_hits(tok)
            if not hits:
                all_rows.append(
                    "<tr>"
                    f"<td style='padding:8px 10px;border-bottom:1px solid #e2e8f0;font-family:Consolas,monospace;font-size:12px;'>{html.escape(tok)}</td>"
                    "<td colspan='5' style='padding:8px 10px;border-bottom:1px solid #e2e8f0;color:#92400e;font-size:12px;'>"
                    "No matching path in this scan (check spelling, build roots, or use "
                    "<code>Archive.war!/path/inside</code> for nested members).</td>"
                    "</tr>"
                )
                continue
            for h in hits:
                s1, s2 = h.size_folder1, h.size_folder2
                sz1 = fsz(s1) if s1 is not None else "n/a"
                sz2 = fsz(s2) if s2 is not None else "n/a"
                pct_cell = "n/a"
                if s1 is not None and s2 is not None and s1 > 0:
                    pct_cell = f"{(s2 - s1) / s1 * 100:+.1f}%"
                elif s1 == 0 and s2 and s2 > 0:
                    pct_cell = "+∞ (old empty)"
                p1, p2 = self._explicit_size_hit_abs_paths(h)
                po = html.escape(str(p1))
                pn = html.escape(str(p2))
                all_rows.append(
                    f"<tr>"
                    f"<td style='padding:8px 10px;border-bottom:1px solid #e2e8f0;font-family:Consolas,monospace;font-size:12px;'>{html.escape(tok)}</td>"
                    f"<td style='padding:8px 10px;border-bottom:1px solid #e2e8f0;font-size:12px;color:#334155;'>{html.escape(h.status)}</td>"
                    f"<td style='padding:8px 10px;border-bottom:1px solid #e2e8f0;font-family:Consolas,monospace;font-size:11px;'>{html.escape(h.relative_path)}</td>"
                    f"<td style='padding:8px 10px;border-bottom:1px solid #e2e8f0;font-size:11px;word-break:break-all;color:#475569;'>{po}</td>"
                    f"<td style='padding:8px 10px;border-bottom:1px solid #e2e8f0;font-size:11px;word-break:break-all;color:#475569;'>{pn}</td>"
                    f"<td style='padding:8px 10px;border-bottom:1px solid #e2e8f0;text-align:right;white-space:nowrap;'>{html.escape(sz1)}</td>"
                    f"<td style='padding:8px 10px;border-bottom:1px solid #e2e8f0;text-align:right;white-space:nowrap;'>{html.escape(sz2)}</td>"
                    f"<td style='padding:8px 10px;border-bottom:1px solid #e2e8f0;text-align:right;font-weight:600;'>{html.escape(pct_cell)}</td>"
                    f"</tr>"
                )
        thead = (
            "<th style='padding:10px;text-align:left;'>Query</th>"
            "<th style='padding:10px;text-align:left;'>Status</th>"
            "<th style='padding:10px;text-align:left;'>Relative path</th>"
            "<th style='padding:10px;text-align:left;'>Old build path</th>"
            "<th style='padding:10px;text-align:left;'>New build path</th>"
            "<th style='padding:10px;text-align:right;'>Old size</th>"
            "<th style='padding:10px;text-align:right;'>New size</th>"
            "<th style='padding:10px;text-align:right;'>Δ%</th>"
        )
        return f"""
            <div style="margin:20px 0 12px 0;">
                <h3 style="margin:0 0 6px 0;font-size:17px;color:#0f172a;">2) Explicit file sizes (from your list)</h3>
                <p style="margin:0 0 10px 0;font-size:12px;color:#64748b;line-height:1.5;">
                  Comma-separated <strong>file names</strong>, <strong>relative path suffixes</strong>, <strong>nested archive paths</strong>, or <strong>globs</strong> (e.g.
                  <code>ClfyAgent.ear</code>, <code>Crm.jnlp</code>, <code>Crm.war!/WEB-INF/Crm.jnlp</code>, <code>*.war</code>).
                  Nested members inside WAR/JAR/EAR are looked up on demand when not listed as top-level files.
                </p>
                <div style="overflow-x:auto;border:1px solid #93c5fd;border-radius:10px;background:#fff;">
                <table style="width:100%;border-collapse:collapse;font-size:13px;min-width:960px;">
                <thead><tr style="background:linear-gradient(135deg,#0369a1 0%,#1d4ed8 100%);color:#fff;">{thead}</tr></thead>
                <tbody>{"".join(all_rows)}</tbody>
                </table>
                </div>
            </div>"""

    def _text_explicit_size_compare_lines(self) -> list[str]:
        raw = (self.explicit_size_compare_query or "").strip()
        tokens = _crm_parse_explicit_size_tokens(raw)
        if not tokens:
            return []
        lines: list[str] = []
        lines.append("-" * 80)
        lines.append("EXPLICIT FILE SIZE COMPARE (user list)")
        lines.append("-" * 80)
        for tok in tokens:
            hits = self._crm_collect_explicit_size_hits(tok)
            lines.append(f"Query: {tok}")
            if not hits:
                lines.append("  (no matching path in this scan)")
                lines.append("")
                continue
            for h in hits:
                s1, s2 = h.size_folder1, h.size_folder2
                pct = ""
                if s1 is not None and s2 is not None and s1 > 0:
                    pct = f"  delta%: {(s2 - s1) / s1 * 100:+.2f}%"
                lines.append(f"  [{h.status}] {h.relative_path}")
                lines.append(f"    old: {s1} B  new: {s2} B{pct}")
            lines.append("")
        return lines

    def _text_high_size_diff_alert_lines(
        self, modified: list[CRMComparisonResult]
    ) -> list[str]:
        high_diff = self._compute_high_size_diff_alert(modified)
        lines: list[str] = []
        lines.append("-" * 80)
        lines.append(
            f"HIGH SIZE DIFFERENCE ALERT (|change| >= {_CRM_HIGH_SIZE_ALERT_MIN_ABS_PCT:.0f}%)"
        )
        if not high_diff:
            lines.append(
                f"No files found with a size change of {_CRM_HIGH_SIZE_ALERT_MIN_ABS_PCT:.0f}% "
                f"or more in either direction (increase or decrease)."
            )
            lines.append("")
            return lines
        fs = self._format_size_bytes_report
        lines.append(f"({len(high_diff)} file(s))")
        lines.append("-" * 80)
        cap = 200
        for r, diff, pct in high_diff[:cap]:
            sign = "+" if (r.size_folder2 or 0) > (r.size_folder1 or 0) else "-"
            lines.append(
                f"  {r.relative_path}: old={fs(r.size_folder1)} new={fs(r.size_folder2)} "
                f"delta={sign}{fs(int(diff))} ({sign}{pct:.1f}%)"
            )
        if len(high_diff) > cap:
            lines.append(f"  ... and {len(high_diff) - cap} more")
        lines.append("")
        return lines

    def _html_unpaired_removed_added_side_by_side(
        self,
        unpaired_r: list,
        unpaired_a: list,
        *,
        format_size_fn,
        limit: int = 2000,
    ) -> str:
        """Single table: unmatched removed (old) vs unmatched added (new) in parallel columns."""
        if not unpaired_r and not unpaired_a:
            return ""
        old_paths = [_crm_norm_rel_path(r.relative_path) for r in unpaired_r]
        new_paths = [_crm_norm_rel_path(r.relative_path) for r in unpaired_a]
        rows = self._align_side_by_side_path_rows(old_paths, new_paths)
        title = (
            f"Unmatched removed ({len(unpaired_r):,}) vs unmatched added ({len(unpaired_a):,}) "
            f"— side by side"
        )
        subtitle = (
            "Paths aligned by logical file name across CRM release folders "
            "(<code>CRM_10.24/CRM10.24.1_ALL/…</code> ↔ "
            "<code>CRM_10.25/CRM10.25.0_ALL/…</code>). "
            "Missing counterparts show <code>…/absent</code> on the new-build side. "
            f"<span style=\"color:{_CRM_MISSING_DARK_RED};font-weight:700;\">Dark red</span> "
            "marks the missing side and file/archive name."
        )
        _ = format_size_fn  # reserved for callers / future per-row size column
        table = self._html_side_by_side_mismatch_table(
            rows, title=title, subtitle=subtitle, name_header="File / archive name", limit=limit
        )
        if not table:
            return ""
        size_note = (
            "<p style='font-size:11px;color:#64748b;margin:8px 0 0 0;line-height:1.45;'>"
            "Rows are paired by logical JAR/path signature (version-neutral), not by row index."
            "</p>"
        )
        return table + size_note

    def _compute_minor_size_diff_alert(
        self, modified: list[CRMComparisonResult]
    ) -> list[tuple[CRMComparisonResult, float, float]]:
        """
        Modified files where new build is smaller than old and |% decrease| is below 20%.
        """
        rows: list[tuple[CRMComparisonResult, float, float]] = []
        for r in modified:
            if not (r.size_folder1 and r.size_folder2 and r.size_folder1 > 0):
                continue
            if r.size_folder2 >= r.size_folder1:
                continue
            diff = float(r.size_folder1 - r.size_folder2)
            pct = abs((r.size_folder2 - r.size_folder1) / r.size_folder1 * 100)
            if 0 < pct < _CRM_MINOR_SIZE_REPORT_MAX_PCT:
                rows.append((r, diff, pct))
        rows.sort(key=lambda x: x[2], reverse=True)
        return rows

    def _html_fragment_minor_size_diff_alert(
        self, minor_files: list[tuple[CRMComparisonResult, float, float]]
    ) -> str:
        if not minor_files:
            return ""
        fs = self._format_size_bytes_report
        lines_tb: list[str] = []
        max_rows = 50
        for r, diff, pct in minor_files[:max_rows]:
            file_name = self._path_leaf_name(r.relative_path)
            lines_tb.append(
                f"""
                        <tr>
                            <td class="mono" style="color:{_CRM_MISSING_DARK_RED};font-weight:700;">{html.escape(file_name)}</td>
                            <td class="mono" style="font-size:11px;word-break:break-all;">{html.escape(r.relative_path)}</td>
                            <td class="right">{fs(r.size_folder1)}</td>
                            <td class="right">{fs(r.size_folder2)}</td>
                            <td class="right" style="color:#059669;font-weight:700;">-{fs(int(diff))}</td>
                            <td class="right" style="color:#059669;font-weight:700;">-{pct:.1f}%</td>
                        </tr>"""
            )
        more = ""
        if len(minor_files) > max_rows:
            more = f"""
                        <tr>
                            <td colspan="6" style="text-align:center;color:#64748b;font-style:italic;">
                                … and {len(minor_files) - max_rows:,} more minor size decrease(s)
                            </td>
                        </tr>"""
        return f"""
            <div class="high-diff-alert" style="background:linear-gradient(135deg,#fff7ed,#ffedd5);border-color:#fdba74;">
                <h3 style="color:#9a3412;">📉 Minor size decreases ({len(minor_files):,} files, new &lt; old and &lt; {_CRM_MINOR_SIZE_REPORT_MAX_PCT:.0f}%)</h3>
                <p style="margin:0 0 12px 0;font-size:12px;color:#64748b;">
                    New build is smaller than old, but the decrease is under {_CRM_MINOR_SIZE_REPORT_MAX_PCT:.0f}%
                    (not shown in the major ≥{_CRM_HIGH_SIZE_ALERT_MIN_ABS_PCT:.0f}% alert).
                </p>
                <table class="high-diff-table" style="border:1px solid #fdba74;">
                    <thead>
                        <tr style="background:#fed7aa;color:#9a3412;">
                            <th>File name</th>
                            <th>Path</th>
                            <th class="right">Old size</th>
                            <th class="right">New size</th>
                            <th class="right">Decrease</th>
                            <th class="right">% change</th>
                        </tr>
                    </thead>
                    <tbody>{"".join(lines_tb)}{more}
                    </tbody>
                </table>
            </div>"""

    def _compute_high_size_diff_alert(
        self, modified: list[CRMComparisonResult]
    ) -> list[tuple[CRMComparisonResult, float, float]]:
        """
        Top-level modified files where |% size change| >= _CRM_HIGH_SIZE_ALERT_MIN_ABS_PCT.
        Tuple: (result, abs_byte_diff, abs_pct).
        """
        rows: list[tuple[CRMComparisonResult, float, float]] = []
        for r in modified:
            if not (r.size_folder1 and r.size_folder2 and r.size_folder1 > 0):
                continue
            diff = abs(r.size_folder2 - r.size_folder1)
            abs_pct = abs((r.size_folder2 - r.size_folder1) / r.size_folder1 * 100)
            if abs_pct >= _CRM_HIGH_SIZE_ALERT_MIN_ABS_PCT:
                rows.append((r, float(diff), float(abs_pct)))
        rows.sort(key=lambda x: x[2], reverse=True)
        return rows

    def _html_fragment_high_size_diff_alert(
        self, high_diff_files: list[tuple[CRMComparisonResult, float, float]]
    ) -> str:
        """HTML block for High Size Difference Alert (full + compact reports)."""
        if not high_diff_files:
            return f"""
            <div class="high-diff-alert high-diff-alert-empty" style="background:linear-gradient(135deg,#ecfdf5,#d1fae5);border-color:#6ee7b7;">
                <h3 style="color:#047857;">✓ Major size differences (|change| ≥ {_CRM_HIGH_SIZE_ALERT_MIN_ABS_PCT:.0f}%)</h3>
                <p style="margin:0;font-size:13px;color:#065f46;line-height:1.55;">
                    Not found any files with a size change of {_CRM_HIGH_SIZE_ALERT_MIN_ABS_PCT:.0f}% or more
                    in either direction (increase or decrease).
                </p>
            </div>"""
        fs = self._format_size_bytes_report
        non_class: list[tuple[CRMComparisonResult, float, float]] = []
        class_hd: list[tuple[CRMComparisonResult, float, float]] = []
        for row in high_diff_files:
            r, diff, pct = row
            if self._is_class_file_path(str(r.relative_path)):
                class_hd.append(row)
            else:
                non_class.append(row)

        lines_tb: list[str] = []
        max_non_class_rows = 15
        for r, diff, pct in non_class[:max_non_class_rows]:
            file_name = r.relative_path.split("/")[-1].split("\\")[-1]
            if " -> " in file_name:
                file_name = file_name.split(" -> ")[-1]
            diff_sign = "+" if r.size_folder2 > r.size_folder1 else "-"
            diff_color = "#dc2626" if r.size_folder2 > r.size_folder1 else "#059669"
            lines_tb.append(
                f"""
                        <tr>
                            <td class="mono">{html.escape(file_name)}</td>
                            <td class="right">{fs(r.size_folder1)}</td>
                            <td class="right">{fs(r.size_folder2)}</td>
                            <td class="right" style="color:{diff_color};font-weight:700;">{diff_sign}{fs(int(diff))}</td>
                            <td class="right" style="color:{diff_color};font-weight:700;">{diff_sign}{pct:.1f}%</td>
                        </tr>"""
            )
        if class_hd:
            n_cls = len(class_hd)
            lines_tb.append(
                f"""
                        <tr>
                            <td colspan="5" style="text-align:left;padding:12px 10px;background:#fff7ed;border-bottom:1px solid #fecaca;font-family:Consolas,monospace;font-size:12px;color:#9a3412;">
                                <strong>.class files:</strong> {n_cls} file(s) with high size difference — shown once here (per-row &quot;HIGH DIFF&quot; badges are suppressed for .class in the main table). See <strong>Modified files</strong> for paths.
                            </td>
                        </tr>"""
            )
        more = ""
        remainder_non_class = len(non_class) - min(len(non_class), max_non_class_rows)
        if remainder_non_class > 0:
            more = f"""
                        <tr>
                            <td colspan="5" style="text-align:center;color:#991b1b;font-style:italic;">
                                ... and {remainder_non_class} more non-.class file(s) with high size differences
                            </td>
                        </tr>"""
        return f"""
            <div class="high-diff-alert">
                <h3>⚠️ Major size differences ({len(high_diff_files)} files, |change| ≥ {_CRM_HIGH_SIZE_ALERT_MIN_ABS_PCT:.0f}%)</h3>
                <p style="margin:0 0 12px 0;font-size:12px;color:#64748b;">
                    Files where size changed by at least {_CRM_HIGH_SIZE_ALERT_MIN_ABS_PCT:.0f}% in either direction
                    (growth or shrink). <strong>.class</strong> entries are summarized in a single row to avoid repetition.
                </p>
                <table class="high-diff-table">
                    <thead>
                        <tr>
                            <th>File Name</th>
                            <th class="right">Old Size</th>
                            <th class="right">New Size</th>
                            <th class="right">Difference</th>
                            <th class="right">% Change</th>
                        </tr>
                    </thead>
                    <tbody>{"".join(lines_tb)}{more}
                    </tbody>
                </table>
            </div>"""

    def _html_fragment_jar_class_diff_sharedlib_scala_notice(self) -> str:
        """
        Nested JAR class diffs (e.g. scala-library under sharedlib/CrmSharedLib.ear) are not
        top-level modified files, so they do not appear in High Size Difference Alert. Surface
        a short summary when class-diff entries target sharedlib or scala*.jar.
        """
        items: list[str] = []
        for jd in self.jar_class_diff_list or []:
            if jd.get("error"):
                continue
            q = str(jd.get("query") or "")
            iq = str(jd.get("input_query") or q)
            old_src = (jd.get("jar_old_source") or "") + " " + (jd.get("jar_new_source") or "")
            low = old_src.lower() + " " + q.lower() + " " + iq.lower()
            base_q = os.path.basename(q.replace("\\", "/")).lower()
            is_scala_q = base_q.startswith("scala") or fnmatch.fnmatch(base_q, "scala*.jar")
            if "sharedlib" not in low and not is_scala_q:
                continue
            mo = len(jd.get("modified") or [])
            oo = len(jd.get("only_in_old") or [])
            on = len(jd.get("only_in_new") or [])
            items.append(
                f"<li style='margin:6px 0;font-family:Consolas,monospace;font-size:12px;'>"
                f"<strong>{html.escape(q)}</strong>"
                f" <span style='color:#64748b;'>(input: {html.escape(iq)})</span> — "
                f".class modified: {mo}, only old: {oo}, only new: {on}</li>"
            )
        if not items:
            return ""
        return f"""
            <div style="background:linear-gradient(135deg,#ecfdf5,#d1fae5);padding:16px 18px;border-radius:12px;margin-bottom:18px;border:2px solid #34d399;">
                <h3 style="margin:0 0 10px 0;color:#065f46;font-size:16px;">☕ Class file diff — sharedlib / scala JARs (nested)</h3>
                <p style="margin:0 0 10px 0;font-size:12px;color:#047857;line-height:1.5;">
                  These entries come from optional JAR class compare (nested paths under <code>sharedlib</code> or <code>scala*.jar</code>).
                  They are not duplicated as top-level file rows in the main comparison table.
                </p>
                <ul style="margin:0;padding:0 0 0 18px;">{"".join(items)}</ul>
            </div>"""

    def _generate_html_report_compact(
        self,
        added: list[CRMComparisonResult],
        removed: list[CRMComparisonResult],
        modified: list[CRMComparisonResult],
        unchanged_count: int,
        *,
        large_comparison: bool = False,
        align_cache: Optional[tuple] = None,
    ) -> str:
        """Compact report: counts first, all lists collapsed by default."""
        list_cap = _CRM_HTML_LARGE_LIST_CAP if large_comparison else None

        def _render_file_list(
            title: str,
            items: list[CRMComparisonResult],
            path_kind: str,
            *,
            max_items: Optional[int] = None,
        ) -> str:
            cell = "padding:8px 10px;border-bottom:1px solid #e2e8f0;font-size:12px;vertical-align:top;"
            cell_mono = cell + "font-family:Consolas,monospace;font-weight:600;color:#0f172a;word-break:break-word;"
            cell_path = cell + "font-size:11px;color:#475569;word-break:break-all;"
            total_n = len(items)
            shown = items if max_items is None else items[: max(0, max_items)]
            fast_abs = max_items is not None
            rows_list: list[str] = []
            thead = ""
            if path_kind == "added":
                thead = f"<thead><tr><th style='{cell}'>Relative path</th><th style='{cell}'>New build path</th><th style='{cell}text-align:right;'>Size</th></tr></thead>"
                for r in shown:
                    rel = html.escape(str(r.relative_path))
                    if fast_abs:
                        pn = html.escape(str(self.folder2_path / Path(r.relative_path)))
                    else:
                        pn = html.escape(str(self._abs_pair_build_paths(r.relative_path)[1]))
                    sz = r.size_folder2 if r.size_folder2 is not None else 0
                    rows_list.append(
                        f"<tr><td style='{cell_mono}'>{rel}</td><td style='{cell_path}'>{pn}</td>"
                        f"<td style='{cell}text-align:right;white-space:nowrap;'>{sz:,}</td></tr>"
                    )
            elif path_kind == "removed":
                thead = f"<thead><tr><th style='{cell}'>Relative path</th><th style='{cell}'>Old build path</th><th style='{cell}text-align:right;'>Size</th></tr></thead>"
                for r in shown:
                    rel = html.escape(str(r.relative_path))
                    if fast_abs:
                        po = html.escape(str(self.folder1_path / Path(r.relative_path)))
                    else:
                        po = html.escape(str(self._abs_pair_build_paths(r.relative_path)[0]))
                    sz = r.size_folder1 if r.size_folder1 is not None else 0
                    rows_list.append(
                        f"<tr><td style='{cell_mono}'>{rel}</td><td style='{cell_path}'>{po}</td>"
                        f"<td style='{cell}text-align:right;white-space:nowrap;'>{sz:,}</td></tr>"
                    )
            else:
                thead = f"<thead><tr><th style='{cell}'>Relative path</th><th style='{cell}'>Old build path</th><th style='{cell}'>New build path</th></tr></thead>"
                for r in shown:
                    rel = html.escape(str(r.relative_path))
                    if fast_abs:
                        rp = str(r.relative_path or "")
                        if " -> " in rp:
                            old_r, new_r = (p.strip() for p in rp.split(" -> ", 1))
                            po = html.escape(str(self.folder1_path / Path(old_r)))
                            pn = html.escape(str(self.folder2_path / Path(new_r)))
                        else:
                            po = html.escape(str(self.folder1_path / Path(rp)))
                            pn = html.escape(str(self.folder2_path / Path(rp)))
                    else:
                        p_old, p_new = self._abs_pair_build_paths(r.relative_path)
                        po = html.escape(str(p_old))
                        pn = html.escape(str(p_new))
                    rows_list.append(
                        f"<tr><td style='{cell_mono}'>{rel}</td><td style='{cell_path}'>{po}</td><td style='{cell_path}'>{pn}</td></tr>"
                    )
            rows = "".join(rows_list) or f"<tr><td colspan='3' style='{cell}color:#6b7280;font-style:italic;'>No entries</td></tr>"
            more = ""
            if max_items is not None and total_n > len(shown):
                remainder = total_n - len(shown)
                more = f"""<div style="margin:10px 0 0 0;padding:10px 12px;background:#f1f5f9;border-radius:8px;border:1px solid #e2e8f0;font-size:12px;color:#334155;line-height:1.55;">
<strong>… and {remainder:,} more</strong> in this category are not shown above (this HTML view lists only the first {len(shown):,} for speed and size).
<p style="margin:8px 0 0 0;"><strong>To see the full list:</strong></p>
<ul style="margin:6px 0 0 18px;padding:0;">
<li>Open the <strong>companion plain-text report</strong> with the <code>.txt</code> extension. It uses the <strong>same base name</strong> as this HTML file—swap <code>.html</code> for <code>.txt</code> (same folder where reports are saved).</li>
<li>Or, in the comparison app, use <strong>Browse comparison reports</strong> on the home page and select the matching <code>.txt</code> file for this run.</li>
</ul>
{self._companion_txt_report_link_html()}
<p style="margin:8px 0 0 0;color:#475569;">The <code>.txt</code> report contains every path and detail for this comparison; search inside it for specific files or copy sections as needed.</p>
</div>"""
            summary_count = f"{total_n:,}" if total_n != len(shown) else str(total_n)
            return f"""
            <details class="crm-report-collapse" style="margin:12px 0;">
              <summary class="crm-collapse-summary">{html.escape(title)} ({summary_count})</summary>
              <div style="margin-top:8px;max-height:320px;overflow:auto;border:1px solid #e2e8f0;border-radius:8px;background:#fff;">
              <table style="width:100%;border-collapse:collapse;">{thead}<tbody>{rows}</tbody></table>
              </div>
              {more}
            </details>"""

        class_sections = ""
        for jd in self.jar_class_diff_list or []:
            q = html.escape(str(jd.get("query") or ""))
            jrows = jd.get("java_diffs") or []
            js = jd.get("java_diff_summary") or {}
            err = jd.get("error")
            presence_diff = jd.get("presence_diff")
            bpo = html.escape(str(jd.get("build_path_old") or self.folder1_path))
            bpn = html.escape(str(jd.get("build_path_new") or self.folder2_path))
            java_diff_blocks = "".join(
                f"""
                <details style="margin:8px 0;background:#f8fafc;border:1px solid #e5e7eb;border-radius:8px;padding:8px;">
                  <summary style="cursor:pointer;font-family:Consolas,monospace;font-size:12px;color:#1f2937;">
                    {html.escape(str(r.get("path") or ""))} (+{int(r.get("added_lines") or 0)} / -{int(r.get("removed_lines") or 0)}, changed={int(r.get("changed_lines") or 0)})
                  </summary>
                  <div style="margin-top:8px;">{r.get("diff_html") or ""}</div>
                </details>
                """
                for r in jrows[:30]
            )
            class_sections += f"""
            <div style="border:1px solid #bfdbfe;background:#eff6ff;border-radius:12px;padding:12px;margin-top:12px;box-shadow:0 1px 2px rgba(0,0,0,0.04);">
              <div style="font-weight:700;">☕ {q}</div>
              {f"<div style='margin-top:6px;color:#991b1b;font-size:13px;'>Error: {html.escape(str(err))}</div>" if err else ""}
              {f"<div style='margin-top:6px;color:#9a3412;font-size:13px;'>Difference: {html.escape(str(presence_diff))}</div>" if (presence_diff and not err) else ""}
              <div style="font-size:12px;color:#475569;margin-top:6px;word-break:break-all;">Old build path: {bpo}</div>
              <div style="font-size:12px;color:#475569;margin-top:4px;word-break:break-all;">New build path: {bpn}</div>
              <div style="font-size:12px;color:#64748b;margin-top:8px;">
                Decompiler: <strong>{html.escape(str(js.get("decompiler", "none")).upper())}</strong>
                {('(' + html.escape(str(js.get("decompiler_path") or "")) + ')') if js.get("decompiler_path") else ''}
                | Processed {int(js.get("processed", 0) or 0)} / {int(js.get("requested", 0) or 0)}
                | Errors {int(js.get("errors", 0) or 0)} | Java diffs: {len(jrows)}
              </div>
              <details style="margin-top:8px;">
                <summary style="cursor:pointer;color:#2563eb;">Java source differences — Perforce-style unified diff</summary>
                <div style="margin-top:8px;">
                  {(java_diff_blocks or "<div style='font-size:12px;color:#6b7280;'>No Java-level source differences were produced for modified classes.</div>")}
                  {f"<div style='font-style:italic;color:#6b7280;margin-top:6px;'>... and {len(jrows) - 30} more Java diffs.</div>" if len(jrows) > 30 else ""}
                </div>
              </details>
            </div>"""

        manifest_sections = ""
        for idx, jd in enumerate(self.jar_manifest_diff_list or []):
            q = html.escape(str(jd.get("query") or ""))
            mvo = html.escape(str(jd.get("manifest_impl_version_old") or "n/a"))
            mvn = html.escape(str(jd.get("manifest_impl_version_new") or "n/a"))
            bpoc = html.escape(str(jd.get("build_path_old") or self.folder1_path))
            bpnc = html.escape(str(jd.get("build_path_new") or self.folder2_path))
            jso = (jd.get("jar_old_source") or "").strip() or (jd.get("jar_old") or "") or "—"
            jsn = (jd.get("jar_new_source") or "").strip() or (jd.get("jar_new") or "") or "—"
            jsoe = html.escape(str(jso))
            jsne = html.escape(str(jsn))
            cjlo = (jd.get("clfy_jar_path_old") or "").strip()
            cjln = (jd.get("clfy_jar_path_new") or "").strip()
            cjloe = html.escape(cjlo) if cjlo else "—"
            cjlne = html.escape(cjln) if cjln else "—"
            aol = [str(x) for x in (jd.get("jar_paths_all_old") or []) if x]
            anl = [str(x) for x in (jd.get("jar_paths_all_new") or []) if x]
            alo = "<br/>".join(html.escape(x) for x in aol[:24]) or "—"
            aln = "<br/>".join(html.escape(x) for x in anl[:24]) or "—"
            if len(aol) > 24:
                alo += f"<br/><em style='color:#64748b;'>… and {len(aol) - 24} more</em>"
            if len(anl) > 24:
                aln += f"<br/><em style='color:#64748b;'>… and {len(anl) - 24} more</em>"
            evo = html.escape(str(jd.get("manifest_impl_version_error_old") or ""))
            evn = html.escape(str(jd.get("manifest_impl_version_error_new") or ""))
            err = jd.get("error")
            manifest_sections += f"""
            <div style="border:1px solid #bbf7d0;background:#f0fdf4;border-radius:12px;padding:12px;margin-top:12px;box-shadow:0 1px 2px rgba(0,0,0,0.04);">
              <div style="font-weight:700;">📋 {q}</div>
              {f"<div style='margin-top:6px;color:#991b1b;font-size:13px;'>Error: {html.escape(str(err))}</div>" if err else ""}
              <div style="font-size:13px;color:#334155;margin-top:6px;">
                Implementation-Version differs: {'Yes' if jd.get('manifest_impl_version_diff') else 'No'}
              </div>
              <div style="font-size:12px;color:#475569;margin-top:4px;word-break:break-all;">Old build path: {bpoc}</div>
              <div style="font-size:12px;color:#475569;margin-top:2px;word-break:break-all;">New build path: {bpnc}</div>
              <div style="font-size:12px;color:#4338ca;margin-top:8px;word-break:break-all;"><strong>Inside ClfyAgent.ear/.zip (old):</strong> {cjloe}</div>
              <div style="font-size:12px;color:#4338ca;margin-top:2px;word-break:break-all;"><strong>Inside ClfyAgent.ear/.zip (new):</strong> {cjlne}</div>
              <div style="font-size:12px;color:#0f172a;margin-top:8px;word-break:break-all;"><strong>Old JAR (in build / SDK) — primary:</strong> {jsoe}</div>
              <div style="font-size:12px;color:#0f172a;margin-top:4px;word-break:break-all;"><strong>New JAR (in build / SDK) — primary:</strong> {jsne}</div>
              <details style="margin-top:6px;"><summary style="cursor:pointer;font-size:12px;color:#2563eb;">All matching paths (old / new)</summary>
              <div style="font-size:11px;color:#334155;margin-top:6px;">Old:<br/>{alo}</div>
              <div style="font-size:11px;color:#334155;margin-top:6px;">New:<br/>{aln}</div>
              </details>
              <div style="font-size:12px;color:#475569;margin-top:4px;">old: {mvo}{(' (' + evo + ')') if (not jd.get('manifest_impl_version_old') and evo) else ''}</div>
              <div style="font-size:12px;color:#475569;margin-top:2px;">new: {mvn}{(' (' + evn + ')') if (not jd.get('manifest_impl_version_new') and evn) else ''}</div>
              <details style="margin-top:8px;">
                <summary style="cursor:pointer;color:#2563eb;">Show manifest versions</summary>
                <div style="margin-top:8px;font-family:Consolas,monospace;font-size:12px;">old: {mvo}<br/>new: {mvn}</div>
              </details>
            </div>"""

        high_diff_alert_html = self._html_fragment_high_size_diff_alert(
            self._compute_high_size_diff_alert(modified)
        )
        minor_diff_alert_html = self._html_fragment_minor_size_diff_alert(
            self._compute_minor_size_diff_alert(modified)
        )
        nested_scala_class_html = self._html_fragment_jar_class_diff_sharedlib_scala_notice()

        if align_cache is not None:
            c_apairs, c_up_r, c_up_a = align_cache
        else:
            c_apairs, c_up_r, c_up_a = self._crm_align_removed_added_for_report(removed, added)
        c_ra_d = len(removed) - len(added)
        ra_compact_panel = ""
        if removed or added:
            imb_txt = (
                f"Δ (removed − added) = {c_ra_d}. "
                f"Unmatched after path-similarity pairing: {len(c_up_r)} removed, {len(c_up_a)} added. "
                "Open the companion .txt for the full alignment write-up."
            )
            pr = min(25, len(c_apairs))
            pair_lines = "".join(
                f"<li style='font-size:11px;margin:4px 0;'><span style='color:#991b1b;'>{html.escape(ro.relative_path)}</span> "
                f"→ <span style='color:#166534;'>{html.escape(an.relative_path)}</span> "
                f"<span style='color:#64748b;'>({sim:.0%})</span></li>"
                for ro, an, sim in c_apairs[:pr]
            )
            if len(c_apairs) > pr:
                pair_lines += (
                    f"<li style='font-style:italic;color:#64748b;'>… {len(c_apairs) - pr} more pair(s)</li>"
                )
            if len(c_up_r) + len(c_up_a) > _CRM_ALIGN_SIDE_BY_SIDE_FAST_MAX_PATHS:
                unpaired_side_by_side = (
                    "<p style='margin:8px 0 0 0;font-size:12px;color:#64748b;line-height:1.45;'>"
                    f"Unmatched removed ({len(c_up_r):,}) and added ({len(c_up_a):,}) paths are too numerous "
                    "to render side-by-side here; see the summary counts and file lists below.</p>"
                )
            else:
                unpaired_side_by_side = self._html_unpaired_removed_added_side_by_side(
                    c_up_r,
                    c_up_a,
                    format_size_fn=self._format_size_bytes_report,
                    limit=500,
                )
            ra_compact_panel = f"""
  <div class="panel" style="background:#faf5ff;border-color:#a78bfa;">
    <p style="margin:8px 0;font-size:13px;color:#4c1d95;line-height:1.5;">{html.escape(imb_txt)}</p>
    <details><summary style="cursor:pointer;color:#6d28d9;font-weight:600;">Probable rename pairs ({len(c_apairs)})</summary>
    <ul style="margin:8px 0;padding-left:18px;">{pair_lines or "<li style='color:#64748b;'>None</li>"}</ul></details>
    {unpaired_side_by_side}
  </div>"""

        return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>CRM Comparison Report</title>
<style>
  .app {{
    font-family: Segoe UI, system-ui, sans-serif;
    background: #e0ecff;
    padding: 20px;
    max-width: 1200px;
    margin: 0 auto;
  }}
  .panel {{
    background: #eef4ff;
    border: 1px solid #bfd4ff;
    border-radius: 12px;
    padding: 14px;
    margin-top: 14px;
    box-shadow: 0 2px 8px rgba(15, 23, 42, 0.05);
  }}
  .metric-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
    gap: 10px;
  }}
  .metric-card {{
    border-radius: 12px;
    padding: 12px;
    border: 1px solid transparent;
    font-weight: 600;
  }}
  .metric-total {{ background: #dbeafe; border-color: #93c5fd; color: #1e3a8a; }}
  .metric-unchanged {{ background: #dcfce7; border-color: #86efac; color: #166534; }}
  .metric-modified {{ background: #ffedd5; border-color: #fdba74; color: #9a3412; }}
  .metric-added {{ background: #e0f2fe; border-color: #7dd3fc; color: #0c4a6e; }}
  .metric-removed {{ background: #fee2e2; border-color: #fca5a5; color: #991b1b; }}
  .section-title {{
    margin-top: 16px;
    padding: 10px 12px;
    border-radius: 10px;
    background: #e2e8f0;
    color: #0f172a;
  }}
  details.crm-report-collapse > summary.crm-collapse-summary {{
    list-style: none; cursor: pointer; font-weight: 600; color: #0f172a;
    padding: 10px 12px; border-radius: 10px; background: #e2e8f0;
    display: flex; align-items: center; gap: 10px; user-select: none;
  }}
  details.crm-report-collapse > summary.crm-collapse-summary::-webkit-details-marker {{ display: none; }}
  {self._html_report_collapse_extra_css()}
  .high-diff-alert {{ background: linear-gradient(135deg,#fef2f2,#fee2e2); padding: 18px; border-radius: 12px; margin: 16px 0; border: 2px solid #f87171; }}
  .high-diff-table {{ width: 100%; border-collapse: collapse; font-size: 13px; background: white; border-radius: 8px; overflow: hidden; }}
  .high-diff-table th, .high-diff-table td {{ padding: 10px; border-bottom: 1px solid #fecaca; }}
  .high-diff-table thead tr {{ background: #fecaca; color: #991b1b; }}
  .high-diff-table .right {{ text-align: right; }}
  .high-diff-table .mono {{ font-family: Consolas, monospace; font-weight: 600; }}
  .summary-side-by-side .summary-compare-table {{ width: 100%; border-collapse: collapse; background: white; border-radius: 10px; overflow: hidden; box-shadow: 0 2px 8px rgba(15,23,42,0.08); font-size: 13px; }}
  .summary-compare-table th, .summary-compare-table td {{ padding: 10px 12px; border-bottom: 1px solid #e2e8f0; text-align: left; }}
  .summary-compare-table thead {{ background: #1e3a5f; color: white; }}
  .summary-compare-table th.right, .summary-compare-table td.right {{ text-align: right; font-variant-numeric: tabular-nums; }}
  .summary-compare-table tbody tr:nth-child(even) {{ background: #f8fafc; }}
</style>
</head>
<body class="app">
  <h1 style="margin:0 0 12px 0;color:#0f172a;">CRM Comparison Report</h1>
  {self._companion_txt_report_link_html()}
  {self._html_report_collapse_toolbar()}
  {self._html_side_by_side_summary_table(added, removed, modified, unchanged_count, compact=True, align_cache=align_cache)}
  {self._html_wrap_report_section("removed-added-alignment", "⚖️ Removed vs Added alignment", ra_compact_panel, open_default=False) if ra_compact_panel else ""}
  {self._html_wrap_report_section("major-size-diffs", "⚠️ Major size differences", high_diff_alert_html, open_default=True) if high_diff_alert_html else ""}
  {self._html_wrap_report_section("minor-size-diffs", "📉 Minor size decreases (&lt;20%)", minor_diff_alert_html, open_default=False) if minor_diff_alert_html else ""}
  {self._html_wrap_report_section("scala-class-notice", "Scala / sharedlib class notice", nested_scala_class_html, open_default=False) if nested_scala_class_html else ""}
  {(
    f'''<div style="margin-top:14px;padding:12px 14px;background:#fef9c3;border:1px solid #eab308;border-radius:8px;font-size:13px;color:#713f12;line-height:1.55;">
    <strong>{unchanged_count:,} unchanged files</strong> are not listed in HTML (too many rows).
    See the summary counts above; only differences are listed below.
    {self._companion_txt_report_link_html()}
    </div>'''
    if large_comparison and unchanged_count > _CRM_HTML_OMIT_UNCHANGED_THRESHOLD
    else ""
  )}
  {self._html_wrap_report_section("modified-files", f"Modified files ({len(modified):,})", f'<div class="panel">{_render_file_list("Modified files", modified, "modified", max_items=list_cap)}</div>', open_default=False)}
  {self._html_wrap_report_section("added-files", f"Added files ({len(added):,})", f'<div class="panel">{_render_file_list("Added files", added, "added", max_items=list_cap)}</div>', open_default=False)}
  {self._html_wrap_report_section("removed-files", f"Removed files ({len(removed):,})", f'<div class="panel">{_render_file_list("Removed files", removed, "removed", max_items=list_cap)}</div>', open_default=False)}
  {(
    f'''<div class="panel" style="padding:14px 16px;background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;">
    <p style="margin:0;color:#1e293b;line-height:1.55;font-size:13px;">
    <strong>{unchanged_count:,} unchanged files</strong> are not listed in this HTML page (performance).
    </p>
    {self._companion_txt_report_link_html()}
    </div>'''
    if unchanged_count > _CRM_HTML_OMIT_UNCHANGED_THRESHOLD
    else (
        f'''<div class="panel" style="padding:14px 16px;background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;">
    <p style="margin:0;color:#1e293b;line-height:1.55;font-size:13px;">
    <strong>{unchanged_count:,} unchanged files</strong> — paths omitted for speed; see summary counts.
    </p></div>'''
        if unchanged_count
        else ""
    )
  )}
  {self._html_wrap_report_section("jar-class-diffs", "Entered JAR class file differences", class_sections or "<p style='color:#6b7280;'>No class diff entries.</p>", open_default=False)}
  {self._html_wrap_report_section("jar-manifest-diffs", "Entered JAR manifest differences", manifest_sections or "<p style='color:#6b7280;'>No manifest diff entries.</p>", open_default=False)}
  <p><a href="/" style="color:#2563eb;">← Back to Home</a></p>
{self._html_report_collapse_script()}
{_crm_html_jar_manual_helpers_script()}
</body></html>"""
    
    def generate_html_report(self) -> str:
        """Generate a comprehensive HTML report matching ASCF style with full comparison table."""
        self._crm_ensure_scan_indexes()
        added, removed, modified, unchanged_count = self._report_diff_buckets()
        align_cache = self._crm_align_for_html_report(removed, added)
        align_pairs, unpaired_r, unpaired_a = align_cache
        unpaired_removed_paths = {_crm_norm_rel_path(r.relative_path) for r in unpaired_r}
        unpaired_added_paths = {_crm_norm_rel_path(r.relative_path) for r in unpaired_a}
        ra_delta = len(removed) - len(added)

        n_diffs = len(self.results)
        if n_diffs > _CRM_HTML_FULL_REPORT_MAX_FILES:
            print(
                f"[CRM] HTML: {n_diffs:,} diff rows exceed full-report limit "
                f"({_CRM_HTML_FULL_REPORT_MAX_FILES:,}); generating bounded compact HTML."
            )
            sys.stdout.flush()
            return self._generate_html_report_compact(
                added, removed, modified, unchanged_count, large_comparison=True,
                align_cache=align_cache,
            )

        # Helper function to format file size
        def format_size(size_bytes):
            if size_bytes is None:
                return "N/A"
            if size_bytes < 1024:
                return f"{size_bytes} B"
            elif size_bytes < 1024 * 1024:
                return f"{size_bytes / 1024:.1f} KB"
            elif size_bytes < 1024 * 1024 * 1024:
                return f"{size_bytes / (1024 * 1024):.2f} MB"
            else:
                return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"
        
        high_diff_files = self._compute_high_size_diff_alert(modified)
        
        # Text + config file diffs — HTML section only when optional file-pattern field was filled in
        config_filter_raw = (self.config_files_filter_query or "").strip()
        config_files_changed: list[str] = []
        config_files_changed_total = 0
        config_diff_lookup: dict[str, str] = {}
        if config_filter_raw:
            config_files_changed = self._collect_config_text_diff_path_pool()
            config_files_changed_total = len(config_files_changed)
            config_files_changed, _config_ftoks = _crm_filter_config_paths(
                config_files_changed, config_filter_raw
            )
            for r in modified:
                if not r.archive_diff:
                    continue
                base = r.relative_path
                for row in r.archive_diff.get("config_diff_details") or []:
                    mkey = row.get("member") or ""
                    if mkey:
                        config_diff_lookup[f"{base}!/{mkey}"] = row.get("unified_html") or ""
            self._config_diff_html_lookup_ensure(
                config_files_changed,
                config_diff_lookup,
                max_paths=_CRM_HTML_CONFIG_DIFF_ENSURE_CAP,
            )
            _czd = self.clfy_agent_zip_diff or {}
            _czd_ad = _czd.get("archive_diff") or {}
            _rel_base = str(_czd.get("rel_old") or _czd.get("rel_new") or "").strip()
            if _rel_base:
                for row in _czd_ad.get("config_diff_details") or []:
                    mkey = row.get("member") or ""
                    if mkey:
                        _full = f"{_rel_base}!/{mkey}"
                        config_diff_lookup.setdefault(_full, row.get("unified_html") or "")
        
        style = """
        <style>
        * { box-sizing: border-box; }
        body { font-family: 'Segoe UI', system-ui, -apple-system, sans-serif; margin: 0; padding: 20px; background: linear-gradient(135deg, #1e3a5f 0%, #2d5a87 100%); min-height: 100vh; }
        .container { max-width: 1400px; margin: 0 auto; background: white; border-radius: 16px; box-shadow: 0 20px 60px rgba(0,0,0,0.3); overflow: hidden; }
        .header { background: linear-gradient(135deg, #1e3a5f 0%, #2d5a87 100%); color: white; padding: 30px; }
        .header h1 { margin: 0 0 10px 0; font-size: 28px; font-weight: 600; }
        .header .subtitle { opacity: 0.9; font-size: 14px; }
        .header .paths { margin-top: 15px; padding-top: 15px; border-top: 1px solid rgba(255,255,255,0.3); }
        .header .path-label { font-weight: 600; font-size: 12px; text-transform: uppercase; opacity: 0.8; margin-top: 8px; }
        .header .path-value { font-family: 'Consolas', monospace; font-size: 13px; margin-top: 4px; word-break: break-all; }
        .content { padding: 30px; }
        .summary { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 20px; margin-bottom: 30px; }
        .stat-card { background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%); color: white; padding: 20px; border-radius: 12px; box-shadow: 0 4px 15px rgba(0,0,0,0.1); text-align: center; }
        .stat-card.success { background: linear-gradient(135deg, #4facfe 0%, #00f2fe 100%); }
        .stat-card.warning { background: linear-gradient(135deg, #fa709a 0%, #fee140 100%); }
        .stat-card.info { background: linear-gradient(135deg, #30cfd0 0%, #330867 100%); }
        .stat-card.danger { background: linear-gradient(135deg, #f5576c 0%, #f093fb 100%); }
        .stat-card.new { background: linear-gradient(135deg, #48bb78 0%, #38a169 100%); }
        .stat-card .label { font-size: 12px; font-weight: 500; opacity: 0.9; text-transform: uppercase; letter-spacing: 0.5px; }
        .stat-card .value { font-size: 32px; font-weight: 700; margin-top: 8px; }
        .nav-menu { background: #f7fafc; padding: 20px; border-radius: 8px; margin-bottom: 30px; }
        .nav-menu h3 { margin: 0 0 15px 0; font-size: 18px; color: #2d3748; }
        .nav-links { display: flex; flex-wrap: wrap; gap: 12px; }
        .nav-link { display: inline-block; padding: 10px 16px; background: white; border-left: 4px solid #1e3a5f; border-radius: 6px; text-decoration: none; color: #2d3748; font-weight: 600; transition: all 0.2s; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
        .nav-link:hover { transform: translateX(4px); border-left-width: 6px; box-shadow: 0 2px 8px rgba(0,0,0,0.15); }
        .nav-link.missing-left { border-left-color: #f56565; }
        .nav-link.missing-right { border-left-color: #48bb78; }
        .nav-link.different { border-left-color: #ed8936; }
        .nav-link.identical { border-left-color: #4299e1; }
        .section { margin-bottom: 40px; }
        .section-title { font-size: 22px; font-weight: 600; color: #1a202c; margin-bottom: 20px; padding-bottom: 10px; border-bottom: 3px solid #1e3a5f; }
        .table-container { background: #f7fafc; border-radius: 8px; padding: 20px; margin-bottom: 20px; overflow-x: auto; }
        .comparison-table { width: 100%; border-collapse: separate; border-spacing: 0; background: white; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.1); min-width: 900px; }
        .comparison-table thead { background: linear-gradient(135deg, #1e3a5f 0%, #2d5a87 100%); color: white; }
        .comparison-table th { padding: 14px 12px; text-align: left; font-weight: 600; font-size: 13px; text-transform: uppercase; letter-spacing: 0.5px; border-bottom: 2px solid #1e3a5f; white-space: nowrap; }
        .comparison-table th.center { text-align: center; }
        .comparison-table tbody tr { transition: all 0.2s; }
        .comparison-table tbody tr:hover { filter: brightness(0.95); }
        .comparison-table td { padding: 12px; border-bottom: 1px solid #e2e8f0; font-size: 13px; vertical-align: middle; }
        .comparison-table td.filename { font-family: 'Consolas', monospace; font-weight: 600; color: #2d3748; max-width: 350px; word-break: break-word; }
        .comparison-table td.info { font-family: 'Consolas', monospace; font-size: 11px; color: #718096; max-width: 200px; word-break: break-all; }
        .comparison-table td.status { text-align: center; font-weight: 600; width: 140px; }
        .comparison-table tr.identical { background: #dbeafe !important; border-left: 4px solid #3b82f6; }
        .comparison-table tr.missing-left { background: #fee2e2 !important; border-left: 4px solid #ef4444; }
        .comparison-table tr.missing-right { background: #dcfce7 !important; border-left: 4px solid #22c55e; }
        .comparison-table tr.different { background: #fef3c7 !important; border-left: 4px solid #f59e0b; }
        .status-badge { display: inline-block; padding: 5px 12px; border-radius: 16px; font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px; white-space: nowrap; }
        .status-badge.identical { background: #dbeafe; color: #1e40af; }
        .status-badge.missing-left { background: #fee2e2; color: #991b1b; }
        .status-badge.missing-right { background: #dcfce7; color: #166534; }
        .status-badge.different { background: #fef3c7; color: #92400e; }
        .empty-cell { color: #cbd5e0; text-align: center; font-style: italic; }
        .back-link { display: inline-block; margin-top: 20px; padding: 12px 24px; background: linear-gradient(135deg, #1e3a5f 0%, #2d5a87 100%); color: white; text-decoration: none; border-radius: 8px; font-weight: 600; transition: transform 0.2s; }
        .back-link:hover { transform: translateY(-2px); }
        .legend { background: #e0e7ff; padding: 15px; border-radius: 8px; margin-bottom: 20px; border-left: 4px solid #6366f1; }
        .legend-title { font-weight: 600; color: #3730a3; margin-bottom: 8px; }
        .legend-items { display: flex; flex-wrap: wrap; gap: 15px; font-size: 13px; color: #4338ca; }
        .legend-item { display: flex; align-items: center; gap: 6px; }
        .legend-color { width: 20px; height: 20px; border-radius: 4px; }
        .archive-details { margin-top: 8px; padding: 10px; background: #f9fafb; border-radius: 6px; font-size: 12px; }
        .archive-details summary { cursor: pointer; color: #d97706; font-weight: 600; }
        .archive-details ul { margin: 8px 0 0 20px; padding: 0; }
        .archive-details li { margin: 4px 0; color: #4b5563; }
        .high-diff-alert { background: linear-gradient(135deg, #fef2f2, #fee2e2); padding: 20px; border-radius: 12px; margin-bottom: 20px; border: 2px solid #f87171; }
        .high-diff-alert h3 { margin: 0 0 15px 0; color: #dc2626; display: flex; align-items: center; gap: 10px; }
        .high-diff-table { width: 100%; border-collapse: collapse; font-size: 13px; background: white; border-radius: 8px; overflow: hidden; }
        .high-diff-table thead { background: #fecaca; }
        .high-diff-table th { padding: 10px; text-align: left; color: #991b1b; }
        .high-diff-table th.right { text-align: right; }
        .high-diff-table td { padding: 10px; border-bottom: 1px solid #fecaca; }
        .high-diff-table td.right { text-align: right; }
        .high-diff-table td.mono { font-family: 'Consolas', monospace; font-weight: 600; }
        .comparison-table tr.row-unmatched-orphan { background: #f5f3ff !important; border-left: 6px solid #7c3aed !important; box-shadow: inset 0 0 0 1px #c4b5fd; }
        .comparison-table tr.row-unmatched-orphan .status-badge { box-shadow: 0 0 0 2px #7c3aed; }
        .ra-align-panel { background: linear-gradient(135deg, #faf5ff 0%, #ede9fe 100%); border: 2px solid #8b5cf6; border-radius: 12px; padding: 20px; margin-bottom: 24px; }
        .ra-align-panel h3 { margin: 0 0 12px 0; color: #5b21b6; font-size: 18px; }
        .ra-align-table { width: 100%; border-collapse: collapse; font-size: 12px; background: white; border-radius: 8px; overflow: hidden; margin-top: 12px; }
        .ra-align-table th { background: #7c3aed; color: white; padding: 10px; text-align: left; }
        .ra-align-table td { padding: 8px 10px; border-bottom: 1px solid #e9d5ff; font-family: Consolas, monospace; vertical-align: top; word-break: break-word; }
        .ra-align-table tr:nth-child(even) { background: #faf5ff; }
        .summary-side-by-side .summary-compare-table { width: 100%; border-collapse: collapse; background: white; border-radius: 10px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.08); font-size: 14px; }
        .summary-compare-table th, .summary-compare-table td { padding: 12px 14px; border-bottom: 1px solid #e2e8f0; text-align: left; }
        .summary-compare-table thead { background: linear-gradient(135deg, #1e3a5f 0%, #2d5a87 100%); color: white; }
        .summary-compare-table th.right, .summary-compare-table td.right { text-align: right; font-variant-numeric: tabular-nums; }
        .summary-compare-table tbody tr:nth-child(even) { background: #f8fafc; }
        .summary-compare-table tbody tr:last-child td { border-bottom: none; }
        @media (max-width: 1024px) { .summary { grid-template-columns: repeat(2, 1fr); } .comparison-table { font-size: 11px; } }
        @media (max-width: 768px) { .summary { grid-template-columns: 1fr; } .nav-links { flex-direction: column; } }
        details.crm-report-collapse { margin-bottom: 24px; border-radius: 10px; }
        details.crm-report-collapse > summary.crm-collapse-summary {
            list-style: none; cursor: pointer; font-size: 22px; font-weight: 600; color: #1a202c;
            margin: 0; padding: 12px 16px 14px 12px; border-bottom: 3px solid #1e3a5f;
            display: flex; align-items: center; gap: 12px; user-select: none;
            background: linear-gradient(180deg, #f8fafc 0%, #f1f5f9 100%);
            border-radius: 10px 10px 0 0;
        }
        details.crm-report-collapse:not([open]) > summary.crm-collapse-summary { border-radius: 10px; border-bottom: 3px solid #1e3a5f; }
        details.crm-report-collapse > summary.crm-collapse-summary::-webkit-details-marker { display: none; }
        details.crm-report-collapse > summary.crm-collapse-summary::marker { content: ''; }
        details.crm-report-collapse > summary.crm-collapse-summary::before {
            content: '▸'; font-size: 20px; line-height: 1; color: #1e3a5f; transition: transform 0.2s ease; flex-shrink: 0;
        }
        details.crm-report-collapse .table-container { margin-top: 0; }
        """ + self._html_report_collapse_extra_css() + """
        </style>
        """
        
        extra_ra_stat_cards = ""
        ra_alignment_block = ""
        if removed or added:
            extra_ra_stat_cards = f"""
                <div class="stat-card" style="background: linear-gradient(135deg, #c084fc 0%, #6b21a8 100%);">
                    <div class="label">Unmatched removed†</div>
                    <div class="value">{len(unpaired_r)}</div>
                </div>
                <div class="stat-card" style="background: linear-gradient(135deg, #a78bfa 0%, #4c1d95 100%);">
                    <div class="label">Unmatched added†</div>
                    <div class="value">{len(unpaired_a)}</div>
                </div>"""
            if ra_delta > 0:
                imb_msg = (
                    f"<p style='margin:0 0 12px 0;color:#5b21b6;font-size:14px;line-height:1.55;'>"
                    f"<strong>Cardinality mismatch:</strong> <strong>{len(removed)}</strong> removed-only vs "
                    f"<strong>{len(added)}</strong> added-only — <strong>{ra_delta}</strong> more removal(s) than "
                    f"addition(s) at the path level. After best-effort rename pairing, "
                    f"<strong>{len(unpaired_r)}</strong> removed path(s) and <strong>{len(unpaired_a)}</strong> "
                    f"added path(s) are still <em>unmatched</em> (orphans on that side).</p>"
                )
            elif ra_delta < 0:
                imb_msg = (
                    f"<p style='margin:0 0 12px 0;color:#5b21b6;font-size:14px;line-height:1.55;'>"
                    f"<strong>Cardinality mismatch:</strong> <strong>{-ra_delta}</strong> more addition(s) than "
                    f"removal(s). Unmatched removed: <strong>{len(unpaired_r)}</strong>, "
                    f"unmatched added: <strong>{len(unpaired_a)}</strong>.</p>"
                )
            else:
                imb_msg = (
                    f"<p style='margin:0 0 12px 0;color:#5b21b6;font-size:13px;line-height:1.55;'>"
                    f"Removed-only and added-only counts match (<strong>{len(removed)}</strong> vs "
                    f"<strong>{len(added)}</strong>). The table below still splits "
                    f"<strong>probable renames</strong> from <strong>strict orphans</strong> using path similarity.</p>"
                )
            foot = (
                "<p style='margin:12px 0 0 0;font-size:11px;color:#6b21a8;line-height:1.45;'>"
                "† <strong>Unmatched</strong> = no pair found above the similarity threshold in greedy rename alignment "
                "(same idea as Beyond Compare’s orphan edges). Those rows also use a <strong>purple highlight</strong> "
                "in the full comparison table.</p>"
            )
            pair_cap = _CRM_HTML_ALIGN_PAIR_ROW_CAP
            pair_rows = "".join(
                f"<tr><td>{html.escape(ro.relative_path)}</td>"
                f"<td>{html.escape(an.relative_path)}</td>"
                f"<td style='text-align:right;white-space:nowrap;'>{sim:.0%}</td></tr>"
                for ro, an, sim in align_pairs[:pair_cap]
            )
            if len(align_pairs) > pair_cap:
                pair_rows += (
                    f"<tr><td colspan='3' style='color:#64748b;font-style:italic;'>"
                    f"… and {len(align_pairs) - pair_cap:,} more rename/move pair(s) not shown.</td></tr>"
                )
            if not pair_rows:
                pair_rows = (
                    "<tr><td colspan='3' style='color:#64748b;font-style:italic;'>"
                    "No pairs above similarity threshold.</td></tr>"
                )
            unpaired_side_by_side = self._html_unpaired_removed_added_side_by_side(
                unpaired_r,
                unpaired_a,
                format_size_fn=format_size,
                limit=2000,
            )
            ra_alignment_block = f"""
            <div class="ra-align-panel">
                <h3>⚖️ Removed vs Added — Beyond Compare–style alignment</h3>
                {imb_msg}
                <h4 style="margin:16px 0 8px 0;color:#6d28d9;font-size:15px;">Probable rename / move pairs ({len(align_pairs)})</h4>
                <table class="ra-align-table">
                <thead><tr><th>Old build (removed)</th><th>New build (added)</th><th>Similarity</th></tr></thead>
                <tbody>{pair_rows}</tbody></table>
                {unpaired_side_by_side}
                {foot}
            </div>"""
        skip_dup_added_removed_lists = bool(removed or added)
        nav_manifest_tbl = self._html_nav_manifest_summary_table()
        nav_class_tbl = self._html_nav_class_diff_summary_table()

        html_content = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>CRM Build Comparison Report - Old vs New</title>
    {style}
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🏢 CRM Build Comparison Report</h1>
            <div class="subtitle">Comparing Old Build vs New Build (Perforce-style diff) • {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</div>
            <div class="paths">
                <div class="path-label">📁 OLD BUILD PATH</div>
                <div class="path-value">{html.escape(str(self.folder1_path))}</div>
                <div class="path-label">📁 NEW BUILD PATH</div>
                <div class="path-value">{html.escape(str(self.folder2_path))}</div>
            </div>
            {self._companion_txt_report_link_html(for_dark_header=True)}
        </div>
        
        <div class="content">
            {self._html_report_collapse_toolbar()}
            {self._html_side_by_side_summary_table(added, removed, modified, unchanged_count, align_cache=align_cache)}
            <div class="summary">
                {extra_ra_stat_cards}
            </div>
            
            <!-- Navigation Menu -->
            <div class="nav-menu">
                <h3>📑 Quick Navigation</h3>
                <div class="nav-links">
                    <a href="#comparison-table" class="nav-link">📊 JAR / archive change summary</a>"""
        
        if removed or added:
            html_content += """
                    <a href="#removed-added-alignment" class="nav-link" style="border-left-color:#7c3aed;">⚖️ Removed vs Added alignment</a>"""
        if removed and not skip_dup_added_removed_lists:
            html_content += f"""
                    <a href="#removed-files" class="nav-link missing-left">🗑️ Removed Files ({len(removed)})</a>"""
        if added and not skip_dup_added_removed_lists:
            html_content += f"""
                    <a href="#added-files" class="nav-link missing-right">✨ Added Files ({len(added)})</a>"""
        if unchanged_count:
            html_content += f"""
                    <a href="#identical-files" class="nav-link identical">✓ Unchanged Files ({unchanged_count:,})</a>"""
        if self.clfy_agent_zip_diff:
            _clfy_lbl = html.escape(CLFY_AGENT_BUNDLE_REPORT_LABEL)
            html_content += f"""
                    <a href="#clfy-agent-zip" class="nav-link different">📦 {_clfy_lbl} (SDK)</a>"""
        html_content += """
                </div>
"""
        html_content += nav_manifest_tbl + nav_class_tbl
        html_content += """
            </div>
"""
        if self.comparison_error:
            html_content += f"""
            <div style="background:#fef2f2;border:2px solid #f87171;border-radius:12px;padding:18px;margin-bottom:24px;color:#991b1b;">
                <strong>Comparison issue:</strong> {html.escape(str(self.comparison_error))}
                <div style="margin-top:8px;font-size:13px;color:#7f1d1d;">The table below may be empty if the comparison could not scan both builds.</div>
            </div>"""
        
        html_content += """
            <!-- Legend -->
            <div class="legend">
                <div class="legend-title">💡 Color Legend (Perforce-style Build Comparison)</div>
                <div class="legend-items">
                    <div class="legend-item"><span class="legend-color" style="background: #dbeafe; border: 2px solid #3b82f6;"></span> Unchanged</div>
                    <div class="legend-item"><span class="legend-color" style="background: #fef3c7; border: 2px solid #f59e0b;"></span> Modified</div>
                    <div class="legend-item"><span class="legend-color" style="background: #fee2e2; border: 2px solid #ef4444;"></span> Removed in New Build</div>
                    <div class="legend-item"><span class="legend-color" style="background: #dcfce7; border: 2px solid #22c55e;"></span> Added in New Build</div>
                    <div class="legend-item"><span class="legend-color" style="background: #f5f3ff; border: 2px solid #7c3aed;"></span> Unmatched orphan (after pairing)</div>
                </div>
            </div>"""
        
        if ra_alignment_block:
            html_content += self._html_wrap_report_section(
                "removed-added-alignment",
                "⚖️ Removed vs Added — Beyond Compare–style alignment",
                ra_alignment_block,
                open_default=False,
            )
        _major_sz = self._html_fragment_high_size_diff_alert(high_diff_files)
        if _major_sz:
            html_content += self._html_wrap_report_section(
                "major-size-diffs",
                "⚠️ Major size differences",
                _major_sz,
                open_default=True,
            )
        _minor_sz = self._html_fragment_minor_size_diff_alert(
            self._compute_minor_size_diff_alert(modified)
        )
        if _minor_sz:
            html_content += self._html_wrap_report_section(
                "minor-size-diffs",
                "📉 Minor size decreases (&lt;20%, new smaller than old)",
                _minor_sz,
                open_default=False,
            )
        
        # Text and config file diffs (shown only when file patterns were entered for this run)
        if config_filter_raw:
            cf_title_count = len(config_files_changed)
            cf_heading = (
                f"Text and config files changed ({cf_title_count} of {config_files_changed_total} files)"
            )
            cf_note = (
                f"<p style=\"margin:0 0 12px 0;font-size:12px;color:#3730a3;line-height:1.5;\">"
                f"Filtered by the <strong>file pattern</strong> from this run: "
                f"<code style=\"background:#eef2ff;padding:2px 6px;border-radius:4px;\">"
                f"{html.escape(config_filter_raw)}</code>. "
                f"Matches are non–archive (no <code>.jar</code>/<code>.war</code>/<code>.ear</code>/<code>.zip</code> leaf) "
                f"under the two builds. "
                f"{'No paths matched; try other names or <code>*.ext</code>-style patterns.' if cf_title_count == 0 else 'Unmatched paths in the candidate list are omitted here.'}"
                f"</p>"
            )
            if config_files_changed:
                list_body = ""
                for cf in config_files_changed[:50]:
                    diff_html = config_diff_lookup.get(cf, "")
                    if diff_html:
                        list_body += (
                            '<li style="margin:10px 0;color:#4338ca;">'
                            '<details style="margin-top:4px;">'
                            '<summary style="cursor:pointer;font-family:Consolas,monospace;font-size:12px;">'
                            f"{html.escape(cf)}"
                            "</summary>"
                            '<div style="margin-top:8px;">'
                            f"{diff_html}"
                            "</div></details></li>"
                        )
                    else:
                        list_body += (
                            f'<li style="margin:4px 0;color:#4338ca;">{html.escape(cf)}</li>'
                        )
                if len(config_files_changed) > 50:
                    list_body += f'<li style="font-style:italic;color:#666;">... and {len(config_files_changed) - 50} more file(s)</li>'
            else:
                list_body = '<li style="font-style:italic;color:#64748b;">No changed config paths match the filter.</li>'
            _cfg_body = f"""
                {cf_note}
                <div style="background:white;border-radius:8px;padding:15px;max-height:560px;overflow-y:auto;">
                    <ul style="margin:0;padding:0 0 0 20px;font-family:'Consolas',monospace;font-size:12px;">{list_body}</ul>
                </div>"""
            html_content += self._html_wrap_report_section(
                "config-files-changed",
                f"⚙️ {cf_heading}",
                _cfg_body,
                open_default=False,
            )

        _scala_notice = self._html_fragment_jar_class_diff_sharedlib_scala_notice()
        if _scala_notice:
            html_content += self._html_wrap_report_section(
                "scala-sharedlib-notice",
                "Scala / sharedlib class diff notice",
                _scala_notice,
                open_default=False,
            )

        # JAR / archive table summary only (full per-file table removed — use .txt report)
        jar_focus_html = self._html_jar_archive_focus_tables(modified)
        explicit_sizes_html = self._html_explicit_file_size_focus()
        _jar_summary_body = f"""
                <div id="different-files" style="height:0;margin:0;padding:0;overflow:hidden;"></div>
                {jar_focus_html}
                {explicit_sizes_html}"""
        html_content += self._html_wrap_report_section(
            "comparison-table",
            "📊 JAR / archive change summary",
            _jar_summary_body,
            open_default=True,
        )
        
        # Removed Files Section (skipped when Beyond Compare–style alignment lists removals/additions)
        if removed and not skip_dup_added_removed_lists:
            rem_path_sub = "font-size:10px;font-weight:400;color:#64748b;margin-top:4px;word-break:break-all;"
            rem_tr: list[str] = []
            for r in removed:
                rpo = html.escape(str(self.folder1_path / Path(r.relative_path)))
                rem_paths_html = (
                    f"<div style='{rem_path_sub}'>Old path: {rpo}</div>"
                    f"<div style='{rem_path_sub}'>New path: <em>Not found</em> (removed from new build)</div>"
                )
                rem_orphan = (
                    " row-unmatched-orphan"
                    if _crm_norm_rel_path(r.relative_path) in unpaired_removed_paths
                    else ""
                )
                rem_tr.append(
                    f"""
                            <tr class="missing-left{rem_orphan}">
                                <td class="filename">{html.escape(r.relative_path)}{rem_paths_html}</td>
                                <td class="info">{format_size(r.size_folder1)}</td>
                                <td class="info">{html.escape(r.modified_time_folder1 or 'N/A')}</td>
                            </tr>"""
                )
            rem_body = f"""
                <div class="table-container">
                    <table class="comparison-table">
                        <thead>
                            <tr>
                                <th>File Path</th>
                                <th>Size</th>
                                <th>Modified Time</th>
                            </tr>
                        </thead>
                        <tbody>{"".join(rem_tr)}</tbody>
                    </table>
                </div>"""
            html_content += self._html_wrap_report_section(
                "removed-files",
                f"🗑️ Files Removed ({len(removed):,})",
                rem_body,
                open_default=False,
            )

        # Added Files Section (skipped when Beyond Compare–style alignment lists removals/additions)
        if added and not skip_dup_added_removed_lists:
            add_path_sub = "font-size:10px;font-weight:400;color:#64748b;margin-top:4px;word-break:break-all;"
            add_tr: list[str] = []
            for r in added:
                apn = html.escape(str(self.folder2_path / Path(r.relative_path)))
                add_paths_html = (
                    f"<div style='{add_path_sub}'>Old path: <em>Not found</em> (exists only in new build)</div>"
                    f"<div style='{add_path_sub}'>New path: {apn}</div>"
                )
                add_orphan = (
                    " row-unmatched-orphan"
                    if _crm_norm_rel_path(r.relative_path) in unpaired_added_paths
                    else ""
                )
                add_tr.append(
                    f"""
                            <tr class="missing-right{add_orphan}">
                                <td class="filename">{html.escape(r.relative_path)}{add_paths_html}</td>
                                <td class="info">{format_size(r.size_folder2)}</td>
                                <td class="info">{html.escape(r.modified_time_folder2 or 'N/A')}</td>
                            </tr>"""
                )
            add_body = f"""
                <div class="table-container">
                    <table class="comparison-table">
                        <thead>
                            <tr>
                                <th>File Path</th>
                                <th>Size</th>
                                <th>Modified Time</th>
                            </tr>
                        </thead>
                        <tbody>{"".join(add_tr)}</tbody>
                    </table>
                </div>"""
            html_content += self._html_wrap_report_section(
                "added-files",
                f"✨ Files Added ({len(added):,})",
                add_body,
                open_default=False,
            )
        
        # Unchanged Files Section (count only — paths omitted for performance)
        if unchanged_count:
            unch_body = f"""
                <div class="table-container" style="padding:16px;">
                    <p style="margin:0;color:#334155;line-height:1.55;">
                        <strong>{unchanged_count:,}</strong> unchanged file(s). Individual paths are omitted from HTML
                        and text reports for performance; see summary cards above.
                    </p>
                </div>"""
            html_content += self._html_wrap_report_section(
                "identical-files",
                f"✓ Unchanged Files ({unchanged_count:,})",
                unch_body,
                open_default=False,
            )
        
        if self.jar_class_diff_list or self.jar_manifest_diff_list or self.clfy_agent_zip_diff:
            html_content += """
            <div id="class-level-details" style="scroll-margin-top:1.25rem;"></div>"""
        if self.jar_class_diff_list:
            wildcard_summary: dict[str, int] = {}
            for jentry in self.jar_class_diff_list:
                iq_raw = str(jentry.get("input_query") or "").strip()
                if not iq_raw or ("*" not in iq_raw and "?" not in iq_raw):
                    continue
                if jentry.get("error"):
                    wildcard_summary.setdefault(iq_raw, 0)
                    continue
                wildcard_summary[iq_raw] = wildcard_summary.get(iq_raw, 0) + 1
            if wildcard_summary:
                html_content += """
            <div class="legend" style="margin-bottom:16px;">
                <div class="legend-title">Wildcard query matches</div>"""
                for qraw, cnt in sorted(wildcard_summary.items(), key=lambda x: x[0].lower()):
                    html_content += f"""
                <div style="font-family:Consolas,monospace;font-size:13px;">{html.escape(qraw)} -> {cnt} JAR(s)</div>"""
                html_content += """
            </div>"""

        if self.jar_class_diff_list:
            for idx, jd in enumerate(self.jar_class_diff_list):
                q = html.escape(str(jd.get("query") or ""))
                iq = html.escape(str(jd.get("input_query") or jd.get("query") or ""))
                err = jd.get("error")
                intro = ""
                if idx == 0:
                    intro = """
                <p style="color:#4b5563;font-size:14px;margin:0 0 20px 0;">
                    <strong>JAR class file diff (optional):</strong> one block per name from the run (comma-separated). Old/new build paths are in the <strong>summary table</strong> at the top; use <strong>Open</strong> there or
                    <a href="#class-level-details" style="color:#2563eb;">this anchor</a>. Below: <strong>Perforce-style unified diff</strong> of decompiled sources (old vs new) only.
                </p>"""
                html_content += f"""
            <div class="section" id="jar-class-diff-{idx}">
                <div class="section-title" style="font-size:18px;">☕ {q}</div>
{intro}                <div class="legend" style="margin-bottom:16px;">
                    <div class="legend-title">Query</div>
                    <div style="font-family:Consolas,monospace;font-size:13px;">Resolved JAR: {q}</div>
                    <div style="font-family:Consolas,monospace;font-size:12px;color:#6b7280;margin-top:4px;">Input: {iq}</div>
                </div>"""
                if err:
                    html_content += f"""
                <div style="background:#fee2e2;border:2px solid #fca5a5;border-radius:12px;padding:16px;margin-bottom:16px;color:#991b1b;">
                    <strong>Error:</strong> {html.escape(str(err))}
                </div>"""
                if not err and jd.get("total_old") is not None:
                    html_content += f"""
                <p style="margin:0 0 16px 0;font-size:13px;color:#4b5563;">.class entries in JAR: <strong>{jd["total_old"]}</strong> (old) vs <strong>{jd["total_new"]}</strong> (new).</p>"""
                if not err:
                    js = jd.get("java_diff_summary") or {}
                    jrows = jd.get("java_diffs") or []
                    html_content += f"""
                <div style="margin-bottom:20px;">
                    <h4 style="margin:0 0 8px 0;color:#1d4ed8;">Java source differences — Perforce-style unified diff ({len(jrows)})</h4>
                    <p style="margin:0 0 10px 0;font-size:13px;color:#6b7280;">
                        Decompiler: <strong>{html.escape(str(js.get("decompiler", "none")).upper())}</strong>
                        {'(' + html.escape(str(js.get("decompiler_path") or '')) + ')' if js.get("decompiler_path") else ''}
                        &nbsp;|&nbsp; Processed {js.get("processed", 0)} / {js.get("requested", 0)}
                        &nbsp;|&nbsp; Errors {js.get("errors", 0)}
                    </p>"""
                    if js.get("truncated"):
                        html_content += f"""
                    <p style="margin:0 0 10px 0;font-size:12px;color:#92400e;">
                        Processing capped at {_CRM_JAVA_DIFF_MAX_CLASSES} modified classes; skipped {js.get("truncated")} additional entries.
                    </p>"""
                    if jrows:
                        for r in jrows[:30]:
                            html_content += f"""
                    <details style="margin:8px 0;background:#f8fafc;border:1px solid #e5e7eb;border-radius:8px;padding:8px;">
                        <summary style="cursor:pointer;font-family:Consolas,monospace;font-size:12px;color:#1f2937;">
                            {html.escape(str(r.get("path") or ""))} — old vs new Java (unified diff) (+{r.get("added_lines", 0)} / -{r.get("removed_lines", 0)})
                        </summary>
                        <div style="margin-top:8px;">{r.get("diff_html") or ""}</div>
                    </details>"""
                        if len(jrows) > 30:
                            html_content += f"""
                    <p style="font-style:italic;color:#6b7280;margin:6px 0 0 0;">... and {len(jrows) - 30} more Java diffs in text report summary.</p>"""
                    else:
                        html_content += """
                    <p style="margin:0;font-size:12px;color:#6b7280;">No Java-level source differences were produced for modified classes.</p>"""
                    html_content += """
                </div>"""
                html_content += """
            </div>"""

        if self.clfy_agent_zip_diff:
            cd = self.clfy_agent_zip_diff
            lbl = html.escape(str(cd.get("label") or CLFY_AGENT_BUNDLE_REPORT_LABEL))
            err = cd.get("error")
            ro = html.escape(str(cd.get("rel_old") or "—"))
            rn = html.escape(str(cd.get("rel_new") or "—"))
            zo = html.escape(str(cd.get("zip_old") or "—"))
            zn = html.escape(str(cd.get("zip_new") or "—"))
            _clfy_hdr = html.escape(CLFY_AGENT_BUNDLE_REPORT_LABEL)
            clfy_rel_o = str(cd.get("rel_old") or "")
            clfy_rel_n = str(cd.get("rel_new") or "")
            html_content += f"""
            <div class="section">
                <details class="crm-report-collapse" id="clfy-agent-zip">
                <summary class="crm-collapse-summary">📦 {_clfy_hdr} (SDK — tree + optional JARs)</summary>
                <div>
                <p style="color:#4b5563;font-size:14px;margin:0 0 12px 0;">
                    <strong>{lbl}</strong> — fast <strong>size-only</strong> compare of nested <code>.jar</code> / <code>.war</code> / <code>.ear</code> / <code>.zip</code>
                    (naming-normalized paths; CRC and <code>.class</code> scan skipped). Tables list missing containers and size mismatches.
                    Optional JAR class / manifest queries from CRM full are repeated below when they resolve under ClfyAgent.
                    <a href="#clfy-agent-zip" style="color:#2563eb;">Jump here</a> from the top.
                </p>"""
            if err:
                html_content += f"""
                <div style="background:#fee2e2;border:2px solid #fca5a5;border-radius:12px;padding:16px;margin-bottom:16px;color:#991b1b;">
                    <strong>Error:</strong> {html.escape(str(err))}
                </div>"""
            html_content += f"""
                <div class="table-container" style="margin-bottom:16px;">
                    <table class="comparison-table">
                        <tbody>
                            <tr><td class="filename" style="width:220px;">Path in old SDK</td><td class="info">{ro}</td></tr>
                            <tr><td class="filename">Path in new SDK</td><td class="info">{rn}</td></tr>
                            <tr><td class="filename">Outer/old file</td><td class="info">{zo}</td></tr>
                            <tr><td class="filename">Outer/new file</td><td class="info">{zn}</td></tr>"""
            ad = cd.get("archive_diff") if not err else None
            if ad:
                html_content += f"""
                            <tr><td class="filename">Zip members (old / new)</td><td class="info">{ad.get('total_members_old', '—')} / {ad.get('total_members_new', '—')}</td></tr>
                            <tr><td class="filename">Nested containers: only in old / only in new</td><td class="info">{cd.get('summary_only_old', 0)} / {cd.get('summary_only_new', 0)}</td></tr>
                            <tr><td class="filename">All member paths (reference): only in old / only in new</td><td class="info">{cd.get('summary_members_only_old', cd.get('summary_only_old', 0))} / {cd.get('summary_members_only_new', cd.get('summary_only_new', 0))}</td></tr>
                            <tr><td class="filename">Container size differences</td><td class="info">{len(ad.get('jar_size_diff_rows') or [])} row(s) in size table below</td></tr>"""
            html_content += """
                        </tbody>
                    </table>
                </div>"""
            if ad:
                cidx = self._clfy_archive_diff_container_index(ad)
                html_content += self._html_clfy_nested_containers_summary_table(cidx)
                html_content += self._html_clfy_bc_container_diff_table(ad)
                html_content += self._html_clfy_jar_size_diff_table(ad)
            html_content += self._html_clfy_optional_manifest_subtable(clfy_rel_o, clfy_rel_n)
            html_content += self._html_clfy_optional_class_subtable(clfy_rel_o, clfy_rel_n)
            if ad:
                html_content += self._html_clfy_config_filter_paths_only(
                    ad, self.config_files_filter_query or ""
                )
            html_content += """
                </div>
            </details>
            </div>"""
        
        html_content += """
            <a href="/" class="back-link">← Back to Home</a>
        </div>
    </div>
    <script>
    (function () {
      function scrollToHash() {
        var h = location.hash;
        if (!h || h.length < 2) return;
        var id = h.slice(1);
        try { id = decodeURIComponent(id); } catch (e) {}
        var el = document.getElementById(id);
        if (el) {
          if (el.tagName === 'DETAILS') el.open = true;
          el.scrollIntoView({ block: "start", behavior: "auto" });
        }
      }
      if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", scrollToHash);
      } else {
        scrollToHash();
      }
      window.addEventListener("pageshow", function (ev) {
        if (ev.persisted) scrollToHash();
      });
    })();
    </script>
"""
        html_content += self._html_report_collapse_script()
        html_content += _crm_html_jar_manual_helpers_script()
        html_content += """
</body>
</html>"""
        
        return html_content
    
    def _fallback_html_report_error(self, err_msg: str) -> str:
        """Minimal HTML if full report generation fails (avoids empty/truncated files)."""
        return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>CRM report — HTML generation failed</title>
</head>
<body style="font-family:Segoe UI,system-ui,sans-serif;padding:28px;background:#f8fafc;max-width:800px;margin:0 auto;">
    <h1 style="color:#b91c1c;">CRM HTML report could not be generated</h1>
    <p>The text report (<code>.txt</code>) was saved first and should contain the full comparison. Details:</p>
    {self._companion_txt_report_link_html()}
    <pre style="background:#1e293b;color:#e2e8f0;padding:16px;border-radius:8px;overflow:auto;font-size:13px;">{html.escape(err_msg)}</pre>
    <p><a href="/" style="color:#2563eb;">← Back to Home</a></p>
</body>
</html>"""

    def save_report(self, filepath: str):
        """Save HTML report only (no companion .txt)."""
        html_filepath = _crm_html_report_path(filepath)
        self.report_txt_basename = None
        added, removed, modified, unchanged_count = self._report_diff_buckets()
        print(
            f"[CRM] Generating HTML report ({len(self.results):,} diff row(s); "
            f"{unchanged_count:,} unchanged by count)..."
        )
        self._update_progress("Saving: generating HTML report...")
        sys.stdout.flush()
        try:
            html_body = self.generate_html_report()
        except Exception as e:
            logger.exception("[CRM] HTML report generation failed")
            html_body = self._fallback_html_report_error(f"{type(e).__name__}: {e}")
        print(f"[CRM] HTML body built ({len(html_body):,} characters), writing file...")
        sys.stdout.flush()
        try:
            _crm_write_html_file(html_filepath, html_body)
        finally:
            self._cleanup_temp_artifacts()
        print(f"[CRM] HTML report saved: {html_filepath}")
        sys.stdout.flush()


# ============================================================================
# CRM NAMING CONVENTION COMPARER
# ============================================================================

class CRMNamingComparer:
    """
    CRM-specific naming convention comparer.
    Compares JAR file naming patterns between two CRM builds.
    """
    
    def __init__(self, folder1: str, folder2: str):
        """Initialize with two folder paths."""
        self.folder1_path = Path(folder1)
        self.folder2_path = Path(folder2)
        self.jar_files_folder1: dict = {}
        self.jar_files_folder2: dict = {}
        self.results: list = []
        self.suppress_warnings = True
        self.deep_nested_archives = True  # Scan JARs inside archives
        # Progress tracking
        self.total_files = 0
        self.processed_files = 0
        self.current_file = ""
        self.progress_percentage = 0.0
        self.estimated_time_remaining = 0
        self.comparison_start_time = None
        self.comparison_error = None
    
    def _update_progress(self, current_file=""):
        """Update progress tracking information."""
        if self.total_files > 0:
            self.progress_percentage = (self.processed_files / self.total_files) * 100
            if self.comparison_start_time and self.processed_files > 0:
                elapsed_time = time.time() - self.comparison_start_time
                avg_time_per_file = elapsed_time / self.processed_files
                remaining_files = self.total_files - self.processed_files
                self.estimated_time_remaining = int(avg_time_per_file * remaining_files)
        self.current_file = current_file
    
    def _normalize_jar_name(self, filename: str) -> str:
        """
        Normalize JAR filename by removing version/build/timestamp patterns.
        CRM-specific patterns:
        - SDK_CRM_10.0.1.22_linux64_20251120_2043.jar -> SDK_CRM_linux64.jar
        - winpack__V10_0_1_22.jar -> winpack.jar
        - module_unsigned.jar -> module.jar
        """
        base, ext = os.path.splitext(filename)
        
        # Strip "unsigned" suffix
        base = re.sub(r'[-_]unsigned$', '', base, flags=re.IGNORECASE)
        
        # Strip CRM-specific version patterns: __V10_0_1_22, __V10_0_1_21
        base = re.sub(r'__+V\d+(?:[_\.]\d+)*', '', base, flags=re.IGNORECASE)
        
        # Strip version patterns: _10.0.1.22_, -10.0.1.22-
        base = re.sub(r'[-_]\d+\.\d+(?:\.\d+)*(?:\.\d+)*[-_]', '_', base)
        
        # Strip trailing version: -1.2.3, _1.2.3
        base = re.sub(r'[-_]\d+\.\d+(?:\.\d+)*(?:-[A-Za-z0-9]+)*$', '', base)
        
        # Strip timestamps: _20251120_2043, -20240101, _build123
        base = re.sub(r'[-_](?:b?\d{3,}|build-?\d+|\d{8}(?:_\d{4})?|\d{14})$', '', base)
        
        # Clean up double underscores
        base = re.sub(r'_+', '_', base)
        base = base.strip('_-')
        
        return f"{base}{ext.lower()}"
    
    def _relative_dir_key(self, rel_path: str, original_filename: str) -> str:
        """Directory prefix for a file's relative path (for pairing JARs across builds)."""
        p = rel_path.replace("\\", "/")
        fname = original_filename.replace("\\", "/")
        if p.endswith(fname):
            prefix = p[: -len(fname)].strip("/")
        else:
            prefix = os.path.dirname(p).replace("\\", "/").strip("/")
        return prefix
    
    def _pairing_paths_match(self, old_info: dict, new_info: dict) -> bool:
        """True if both JARs sit in the same relative directory inside compared roots."""
        return self._relative_dir_key(old_info["rel_path"], old_info["original"]) == self._relative_dir_key(
            new_info["rel_path"], new_info["original"]
        )
    
    def _extract_version(self, filename: str) -> str:
        """Extract version information from a JAR filename."""
        # Try various version patterns
        patterns = [
            r'__V(\d+(?:[_\.]\d+)+)',  # __V10_0_1_22
            r'[-_](\d+\.\d+(?:\.\d+)*(?:\.\d+)*)',  # -10.0.1.22 or _10.0.1.22
            r'[-_](\d+\.\d+(?:\.\d+)*)',  # -1.2.3
        ]
        
        for pattern in patterns:
            match = re.search(pattern, filename)
            if match:
                return match.group(1).replace('_', '.')
        
        return ""
    
    def _scan_archive_for_jars(self, archive_data, archive_rel_path: str, jars: dict, depth: int = 0, max_depth: int = 5):
        """Recursively scan an archive for JAR files."""
        if depth >= max_depth:
            return
        try:
            with zipfile.ZipFile(BytesIO(archive_data), 'r') as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    name = info.filename.replace('\\', '/').strip('/')
                    filename = os.path.basename(name)
                    lower = filename.lower()
                    
                    if lower.endswith('.jar'):
                        # Found a JAR inside the archive
                        full_rel_path = f"{archive_rel_path}!/{name}"
                        normalized = self._normalize_jar_name(filename)
                        version = self._extract_version(filename)
                        
                        if normalized not in jars:
                            jars[normalized] = []
                        jars[normalized].append({
                            'original': filename,
                            'rel_path': full_rel_path,
                            'version': version,
                            'nested': True
                        })
                        
                        # Recursively scan this JAR for nested JARs
                        if self.deep_nested_archives:
                            try:
                                nested_data = zf.read(info.filename)
                                self._scan_archive_for_jars(nested_data, full_rel_path, jars, depth + 1, max_depth)
                            except Exception:
                                pass
                    
                    elif lower.endswith(('.war', '.ear', '.zip')):
                        # Nested archive - scan it recursively
                        if self.deep_nested_archives:
                            try:
                                nested_data = zf.read(info.filename)
                                nested_rel_path = f"{archive_rel_path}!/{name}"
                                self._scan_archive_for_jars(nested_data, nested_rel_path, jars, depth + 1, max_depth)
                            except Exception:
                                pass
        except Exception as e:
            logger.warning(f"[CRM] Could not scan archive {archive_rel_path}: {e}")
    
    def _collect_jars(self, folder: Path) -> dict:
        """Collect all JAR files from a folder, grouped by normalized name. Includes JARs inside archives."""
        jars = {}
        try:
            if not _path_exists(folder):
                return jars
            # Single archive/JAR passed as the comparison root (same idea as ASCF zip/jar inputs)
            if _path_is_file(folder):
                lower = folder.name.lower()
                if lower.endswith(".jar"):
                    f = folder.name
                    rel_path = f
                    normalized = self._normalize_jar_name(f)
                    version = self._extract_version(f)
                    jars.setdefault(normalized, []).append(
                        {
                            "original": f,
                            "rel_path": rel_path,
                            "version": version,
                            "nested": False,
                        }
                    )
                    if self.deep_nested_archives:
                        try:
                            with open(_fs_path_str(folder), "rb") as archive_file:
                                archive_data = archive_file.read()
                            self._scan_archive_for_jars(archive_data, rel_path, jars, depth=1, max_depth=5)
                        except Exception:
                            pass
                    return jars
                if lower.endswith((".war", ".ear", ".zip")) and self.deep_nested_archives:
                    try:
                        with open(_fs_path_str(folder), "rb") as archive_file:
                            archive_data = archive_file.read()
                        self._scan_archive_for_jars(archive_data, folder.name, jars, depth=0, max_depth=5)
                    except Exception as e:
                        logger.warning(f"[CRM] Could not read archive root {folder}: {e}")
                    return jars
                return jars
            if not _path_is_dir(folder):
                return jars
            walk_base = _walk_base_path(folder)
            for root, _, files in os.walk(str(walk_base)):
                for f in files:
                    full_path = Path(root) / f
                    rel_path = str(full_path.relative_to(walk_base))
                    lower = f.lower()
                    
                    if lower.endswith('.jar'):
                        # Regular JAR file
                        normalized = self._normalize_jar_name(f)
                        version = self._extract_version(f)
                        
                        if normalized not in jars:
                            jars[normalized] = []
                        jars[normalized].append({
                            'original': f,
                            'rel_path': rel_path,
                            'version': version,
                            'nested': False
                        })
                        
                        # Scan inside JAR for nested JARs
                        if self.deep_nested_archives:
                            try:
                                with open(_fs_path_str(full_path), 'rb') as archive_file:
                                    archive_data = archive_file.read()
                                self._scan_archive_for_jars(archive_data, rel_path, jars, depth=1, max_depth=5)
                            except Exception:
                                pass
                    
                    elif lower.endswith(('.war', '.ear', '.zip')) and self.deep_nested_archives:
                        # Archive file - scan for JARs inside
                        try:
                            with open(_fs_path_str(full_path), 'rb') as archive_file:
                                archive_data = archive_file.read()
                            self._scan_archive_for_jars(archive_data, rel_path, jars, depth=0, max_depth=5)
                        except Exception as e:
                            logger.warning(f"[CRM] Could not read archive {full_path}: {e}")
        except Exception as e:
            logger.error(f"[CRM] Error collecting JARs from {folder}: {e}")
        return jars
    
    def compare_naming(self) -> list:
        """Compare JAR naming conventions between two folders."""
        print(f"[CRM] Starting naming convention comparison")
        print(f"[CRM] Old Build: {self.folder1_path}")
        print(f"[CRM] New Build: {self.folder2_path}")
        
        self.comparison_start_time = time.time()
        self.results = []
        
        # Check paths
        if not _path_exists(self.folder1_path) or not _path_exists(self.folder2_path):
            self.comparison_error = "One or both paths do not exist"
            return self.results
        
        # Collect JARs
        print("[CRM] Scanning old build for JAR files...")
        self.jar_files_folder1 = self._collect_jars(self.folder1_path)
        print(f"[CRM] Found {sum(len(v) for v in self.jar_files_folder1.values())} JAR files in old build")
        
        print("[CRM] Scanning new build for JAR files...")
        self.jar_files_folder2 = self._collect_jars(self.folder2_path)
        print(f"[CRM] Found {sum(len(v) for v in self.jar_files_folder2.values())} JAR files in new build")
        
        all_normalized = set(self.jar_files_folder1.keys()) | set(self.jar_files_folder2.keys())
        self.total_files = len(all_normalized)
        
        # Compare
        for normalized in sorted(all_normalized):
            self.processed_files += 1
            self._update_progress(normalized)
            
            in_old = self.jar_files_folder1.get(normalized, [])
            in_new = self.jar_files_folder2.get(normalized, [])
            
            if in_old and not in_new:
                for jar_info in in_old:
                    self.results.append({
                        'normalized': normalized,
                        'status': 'removed',
                        'old_name': jar_info['original'],
                        'old_version': jar_info['version'],
                        'old_path': jar_info['rel_path'],
                        'new_name': None,
                        'new_version': None,
                        'new_path': None
                    })
            elif not in_old and in_new:
                for jar_info in in_new:
                    self.results.append({
                        'normalized': normalized,
                        'status': 'added',
                        'old_name': None,
                        'old_version': None,
                        'old_path': None,
                        'new_name': jar_info['original'],
                        'new_version': jar_info['version'],
                        'new_path': jar_info['rel_path']
                    })
            else:
                # Both have this logical JAR — surface version and filename/timestamp drift (ASCF-style visibility)
                used_new = set()
                for old_info in in_old:
                    best_match = None
                    for idx, new_info in enumerate(in_new):
                        if idx in used_new:
                            continue
                        if self._pairing_paths_match(old_info, new_info):
                            best_match = new_info
                            used_new.add(idx)
                            break
                    if best_match is None and in_new:
                        for idx, new_info in enumerate(in_new):
                            if idx not in used_new:
                                best_match = new_info
                                used_new.add(idx)
                                break
                    if best_match is None and in_new:
                        best_match = in_new[0]
                    if best_match:
                        ver_old, ver_new = old_info["version"], best_match["version"]
                        name_old, name_new = old_info["original"], best_match["original"]
                        if ver_old != ver_new:
                            self.results.append({
                                'normalized': normalized,
                                'status': 'version_changed',
                                'old_name': name_old,
                                'old_version': ver_old,
                                'old_path': old_info['rel_path'],
                                'new_name': name_new,
                                'new_version': ver_new,
                                'new_path': best_match['rel_path']
                            })
                        elif name_old != name_new:
                            # Same semantic version in filename (e.g. SDK CRM) but timestamp/build suffix differs
                            self.results.append({
                                'normalized': normalized,
                                'status': 'naming_variant',
                                'old_name': name_old,
                                'old_version': ver_old,
                                'old_path': old_info['rel_path'],
                                'new_name': name_new,
                                'new_version': ver_new,
                                'new_path': best_match['rel_path']
                            })
        
        print(f"[CRM] Naming comparison complete! Found {len(self.results)} differences")
        return self.results
    
    def generate_report(self) -> str:
        """Generate text report."""
        lines = []
        lines.append("=" * 80)
        lines.append("CRM JAR NAMING CONVENTION COMPARISON REPORT")
        lines.append("=" * 80)
        lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"Old Build Path: {self.folder1_path}")
        lines.append(f"New Build Path: {self.folder2_path}")
        lines.append("")
        
        added = [r for r in self.results if r['status'] == 'added']
        removed = [r for r in self.results if r['status'] == 'removed']
        version_changed = [r for r in self.results if r['status'] == 'version_changed']
        naming_variant = [r for r in self.results if r['status'] == 'naming_variant']
        
        lines.append("-" * 80)
        lines.append("SUMMARY")
        lines.append("-" * 80)
        lines.append(f"JARs Added:             {len(added)}")
        lines.append(f"JARs Removed:           {len(removed)}")
        lines.append(f"JARs Version Changed:   {len(version_changed)}")
        lines.append(f"JARs Naming / Timestamp Drift (same logical JAR): {len(naming_variant)}")
        lines.append("")
        
        if naming_variant:
            lines.append("-" * 80)
            lines.append("NAMING / TIMESTAMP DRIFT (logical JAR matches, filename differs)")
            lines.append("-" * 80)
            for r in naming_variant:
                lines.append(f"  {r['normalized']}")
                lines.append(f"    Old: {r['old_name']} (v{r['old_version'] or '—'})")
                lines.append(f"    New: {r['new_name']} (v{r['new_version'] or '—'})")
            lines.append("")
        
        if version_changed:
            lines.append("-" * 80)
            lines.append("VERSION CHANGES")
            lines.append("-" * 80)
            for r in version_changed:
                lines.append(f"  {r['normalized']}")
                lines.append(f"    Old: {r['old_name']} (v{r['old_version']})")
                lines.append(f"    New: {r['new_name']} (v{r['new_version']})")
            lines.append("")
        
        if added:
            lines.append("-" * 80)
            lines.append("NEW JARS")
            lines.append("-" * 80)
            for r in added:
                lines.append(f"  [+] {r['new_name']}")
            lines.append("")
        
        if removed:
            lines.append("-" * 80)
            lines.append("REMOVED JARS")
            lines.append("-" * 80)
            for r in removed:
                lines.append(f"  [-] {r['old_name']}")
            lines.append("")
        
        lines.append("=" * 80)
        return "\n".join(lines)
    
    def generate_html_report(self) -> str:
        """Generate HTML report."""
        added = [r for r in self.results if r['status'] == 'added']
        removed = [r for r in self.results if r['status'] == 'removed']
        version_changed = [r for r in self.results if r['status'] == 'version_changed']
        naming_variant = [r for r in self.results if r['status'] == 'naming_variant']
        
        html_content = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>CRM JAR Naming Convention Report</title>
    <style>
        body {{ font-family: 'Segoe UI', system-ui, sans-serif; margin: 0; padding: 20px; background: linear-gradient(135deg, #1e3a5f 0%, #2d5a87 100%); min-height: 100vh; }}
        .container {{ max-width: 1200px; margin: 0 auto; background: white; border-radius: 16px; box-shadow: 0 20px 60px rgba(0,0,0,0.3); overflow: hidden; }}
        .header {{ background: linear-gradient(135deg, #1e3a5f 0%, #2d5a87 100%); color: white; padding: 30px; }}
        .header h1 {{ margin: 0; font-size: 24px; }}
        .content {{ padding: 30px; }}
        .summary {{ display: flex; gap: 20px; margin-bottom: 30px; }}
        .summary-card {{ flex: 1; padding: 20px; border-radius: 12px; text-align: center; }}
        .summary-card.added {{ background: #dcfce7; border: 2px solid #22c55e; }}
        .summary-card.removed {{ background: #fee2e2; border: 2px solid #ef4444; }}
        .summary-card.changed {{ background: #fef3c7; border: 2px solid #f59e0b; }}
        .summary-card.naming {{ background: #e0f2fe; border: 2px solid #0ea5e9; }}
        .summary-card .count {{ font-size: 32px; font-weight: bold; }}
        table {{ width: 100%; border-collapse: collapse; margin-top: 20px; }}
        th, td {{ padding: 12px; text-align: left; border-bottom: 1px solid #e5e7eb; }}
        th {{ background: #f9fafb; font-weight: 600; }}
        .version-old {{ color: #ef4444; }}
        .version-new {{ color: #22c55e; }}
        a {{ color: #2563eb; text-decoration: none; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🏢 CRM JAR Naming Convention Report</h1>
            <p style="opacity:0.8;margin:10px 0 0 0;">Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
        </div>
        <div class="content">
            <div class="summary">
                <div class="summary-card added"><div class="count">{len(added)}</div><div>Added</div></div>
                <div class="summary-card removed"><div class="count">{len(removed)}</div><div>Removed</div></div>
                <div class="summary-card changed"><div class="count">{len(version_changed)}</div><div>Version Changed</div></div>
                <div class="summary-card naming"><div class="count">{len(naming_variant)}</div><div>Naming / Timestamp Drift</div></div>
            </div>
"""
        
        if naming_variant:
            html_content += """
            <div class="section" id="naming-variant">
            <h2>📛 Naming / timestamp drift (same logical JAR)</h2>
            <p style="color:#64748b;font-size:14px;">Filenames differ (often build timestamp) while normalized identity and extracted version match.</p>
            <table>
                <tr><th>Logical name</th><th>Old file</th><th>New file</th><th>Version</th></tr>
"""
            for r in naming_variant:
                html_content += (
                    f'                <tr><td>{html.escape(r["normalized"])}</td>'
                    f'<td class="version-old">{html.escape(r["old_name"] or "")}</td>'
                    f'<td class="version-new">{html.escape(r["new_name"] or "")}</td>'
                    f'<td>{html.escape(r["old_version"] or "—")}</td></tr>\n'
                )
            html_content += "            </table>\n            </div>\n"
        
        if version_changed:
            html_content += """
            <div class="section" id="version-changed">
            <h2>🔄 Version Changes</h2>
            <table>
                <tr><th>JAR</th><th>Old Version</th><th>New Version</th></tr>
"""
            for r in version_changed:
                html_content += f'                <tr><td>{html.escape(r["normalized"])}</td><td class="version-old">{html.escape(r["old_version"] or "N/A")}</td><td class="version-new">{html.escape(r["new_version"] or "N/A")}</td></tr>\n'
            html_content += "            </table>\n            </div>\n"
        
        html_content += """
            <p><a href="/">← Back to Home</a></p>
        </div>
    </div>
</body>
</html>"""
        
        return inject_report_section_controls(html_content)
    
    def save_report(self, filepath: str):
        """Save HTML report only."""
        html_filepath = _crm_html_report_path(filepath)
        _crm_write_html_file(html_filepath, self.generate_html_report())


# ============================================================================
# CRM ARCHIVE CONTENTS COMPARER
# ============================================================================

class CRMArchiveComparer:
    """
    CRM-specific archive contents comparer.
    Deep comparison of archive files (JAR/WAR/EAR/ZIP/RPM).
    """
    
    def __init__(self, folder1: str, folder2: str):
        """Initialize with two folder paths."""
        self.folder1_path = Path(folder1)
        self.folder2_path = Path(folder2)
        self.results: list[CRMArchiveEntry] = []
        self.deep_nested_archives = True
        self.suppress_warnings = True
        # Progress tracking
        self.total_files = 0
        self.processed_files = 0
        self.current_file = ""
        self.progress_percentage = 0.0
        self.estimated_time_remaining = 0
        self.comparison_start_time = None
        self.comparison_error = None
        # Temp folders
        self.temp_folder1 = None
        self.temp_folder2 = None
        self.should_cleanup_temp_folders = True
        self.use_local_copy = False
        self.max_retries = 3
        self.retry_delay = 1.0
    
    def _list_archive_members_ascf(self, archive_path: Path) -> tuple[set, dict, str]:
        """List archive members using ASCF logic (ZipFile + jar tf fallback, RPM payload)."""
        from folder_compare import ArchiveContentsComparer
        h = ArchiveContentsComparer(str(archive_path), str(archive_path))
        h.deep_nested_archives = self.deep_nested_archives
        h.suppress_warnings = self.suppress_warnings
        h.max_retries = self.max_retries
        h.retry_delay = self.retry_delay
        ext = archive_path.suffix.lower()
        if ext in (".ear", ".war", ".zip", ".jar"):
            return h._list_zip_members(archive_path)
        if ext == ".rpm":
            return h._list_rpm_members(archive_path)
        logger.warning(f"[CRM] Unsupported archive type for listing: {archive_path}")
        return set(), {}, f"unsupported extension: {ext}"
    
    def _convert_ascf_archive_results(self, asc_results) -> list[CRMArchiveEntry]:
        """Turn ASCF ArchiveEntryComparison rows into flat CRMArchiveEntry list."""
        out: list[CRMArchiveEntry] = []
        for entry in asc_results:
            label = entry.archive_relative_path
            if entry.exists_in_folder1 and not entry.exists_in_folder2:
                out.append(CRMArchiveEntry(archive_name=label, member_path="", status="only_in_old"))
                continue
            if entry.exists_in_folder2 and not entry.exists_in_folder1:
                out.append(CRMArchiveEntry(archive_name=label, member_path="", status="only_in_new"))
                continue
            s1, s2 = entry.members_folder1, entry.members_folder2
            meta1, meta2 = entry.members_meta_folder1, entry.members_meta_folder2
            for m in sorted(s1 - s2):
                sz, crc = meta1.get(m, (0, 0))
                out.append(
                    CRMArchiveEntry(
                        archive_name=label,
                        member_path=m,
                        status="only_in_old",
                        size_old=sz,
                        crc_old=crc,
                    )
                )
            for m in sorted(s2 - s1):
                sz, crc = meta2.get(m, (0, 0))
                out.append(
                    CRMArchiveEntry(
                        archive_name=label,
                        member_path=m,
                        status="only_in_new",
                        size_new=sz,
                        crc_new=crc,
                    )
                )
            for m in sorted(s1 & s2):
                meta_a, meta_b = meta1.get(m), meta2.get(m)
                if meta_a and meta_b and meta_a != meta_b:
                    out.append(
                        CRMArchiveEntry(
                            archive_name=label,
                            member_path=m,
                            status="modified",
                            size_old=meta_a[0],
                            size_new=meta_b[0],
                            crc_old=meta_a[1],
                            crc_new=meta_b[1],
                        )
                    )
        return out
    
    def _sync_progress_from_asc(self, asc):
        self.processed_files = asc.processed_files
        self.total_files = asc.total_files
        self.current_file = asc.current_file
        self.progress_percentage = asc.progress_percentage
        self.estimated_time_remaining = asc.estimated_time_remaining
        self.temp_folder1 = asc.temp_folder1
        self.temp_folder2 = asc.temp_folder2
    
    def _update_progress(self, current_file=""):
        """Update progress tracking."""
        if self.total_files > 0:
            self.progress_percentage = (self.processed_files / self.total_files) * 100
            if self.comparison_start_time and self.processed_files > 0:
                elapsed_time = time.time() - self.comparison_start_time
                avg_time_per_file = elapsed_time / self.processed_files
                remaining_files = self.total_files - self.processed_files
                self.estimated_time_remaining = int(avg_time_per_file * remaining_files)
        self.current_file = current_file
    
    def _normalize_archive_filename(self, filename: str) -> str:
        """Normalize archive filename for comparison."""
        base, ext = os.path.splitext(filename)
        
        # CRM-specific normalization
        base = re.sub(r'[-_]unsigned$', '', base, flags=re.IGNORECASE)
        base = re.sub(r'__+V\d+(?:[_\.]\d+)*', '', base, flags=re.IGNORECASE)
        base = re.sub(r'_\d+\.\d+(?:\.\d+)*(?:\.\d+)*_', '_', base)
        base = re.sub(r'[-_]\d+\.\d+(?:\.\d+)*(?:-[A-Za-z0-9]+)*$', '', base)
        base = re.sub(r'[-_](?:b?\d{3,}|build-?\d+|\d{8}(?:_\d{4})?|\d{14})$', '', base)
        base = re.sub(r'_+', '_', base)
        base = base.strip('_-')
        
        return f"{base}{ext.lower()}"
    
    def _normalize_inner_archive_marker(self, member_path: str) -> str:
        """Normalize paths within archives for proper comparison.
        
        This handles nested archive paths like:
        - lib/ClfyCore-1.2.3.jar!/com/xyz/MyClass.class
        - SDK-1.0.0.jar!/lib/Common-2.1.jar!/pkg/File.class
        
        All archive names (including those with !/ suffix) are normalized
        to strip version numbers for matching across builds.
        """
        parts = member_path.replace('\\', '/').split('/')
        if not parts:
            return member_path
        
        # Normalize ALL segments, not just the last one
        # This handles nested archive paths with version numbers
        normalized_parts = []
        for part in parts:
            lower = part.lower()
            # Check for archive names with !/ marker (e.g., "ClfyCore-1.2.3.jar!")
            if lower.endswith(('.jar!', '.war!', '.ear!', '.zip!')):
                # Remove trailing ! for normalization, then add it back
                base = part[:-1]  # Remove !
                normalized = self._normalize_archive_filename(base)
                normalized_parts.append(normalized + '!')
            elif lower.endswith('.pack.gz'):
                inner_base = part[:-8]
                normalized_inner = self._normalize_archive_filename(inner_base)
                normalized_parts.append(normalized_inner + '.pack.gz')
            elif lower.endswith(('.jar', '.war', '.ear', '.zip')):
                normalized_parts.append(self._normalize_archive_filename(part))
            else:
                normalized_parts.append(part)
        
        return '/'.join(normalized_parts)
    
    def _normalize_member_set(self, members: set) -> set:
        """
        Normalize all member names in a set by applying normalization to archive filenames.
        This ensures version-specific JAR names are treated as identical.
        """
        normalized = set()
        for member in members:
            normalized_member = self._normalize_inner_archive_marker(member)
            normalized.add(normalized_member)
        return normalized
    
    def _find_archives(self, folder: Path) -> list:
        """Find all archive files in a folder."""
        archives = []
        archive_extensions = ('.jar', '.war', '.ear', '.zip', '.rpm')
        
        if _path_is_file(folder):
            # Single archive file
            if folder.suffix.lower() in archive_extensions:
                archives.append(folder)
        else:
            # Directory
            try:
                walk_base = _walk_base_path(folder)
                for root, _, files in os.walk(str(walk_base)):
                    for f in files:
                        if f.lower().endswith(archive_extensions):
                            archives.append(Path(root) / f)
            except Exception as e:
                logger.error(f"[CRM] Error scanning folder {folder}: {e}")
        
        return archives
    
    def compare_archives(self) -> list[CRMArchiveEntry]:
        """Compare archive contents between two folders."""
        print(f"[CRM] Starting archive contents comparison")
        print(f"[CRM] Old Build: {self.folder1_path}")
        print(f"[CRM] New Build: {self.folder2_path}")
        
        self.comparison_start_time = time.time()
        self.results = []
        
        # Check paths
        if not _path_exists(self.folder1_path):
            self.comparison_error = f"Old build path does not exist: {self.folder1_path}"
            return self.results
        
        if not _path_exists(self.folder2_path):
            self.comparison_error = f"New build path does not exist: {self.folder2_path}"
            return self.results
        
        # Two archive files: use ASCF ArchiveContentsComparer (zip integrity fallback, jar tf, RPM, UNC copy)
        supported_exts = (".ear", ".war", ".zip", ".rpm", ".jar")
        if _path_is_file(self.folder1_path) and _path_is_file(self.folder2_path):
            e1, e2 = self.folder1_path.suffix.lower(), self.folder2_path.suffix.lower()
            if e1 in supported_exts and e2 in supported_exts:
                from folder_compare import ArchiveContentsComparer
                asc = ArchiveContentsComparer(str(self.folder1_path), str(self.folder2_path))
                asc.deep_nested_archives = self.deep_nested_archives
                asc.suppress_warnings = self.suppress_warnings
                asc.use_local_copy = self.use_local_copy
                asc.max_retries = self.max_retries
                asc.retry_delay = self.retry_delay
                asc.should_cleanup_temp_folders = self.should_cleanup_temp_folders
                raw = asc.compare_archives()
                self.comparison_error = asc.comparison_error
                self._sync_progress_from_asc(asc)
                self.results = self._convert_ascf_archive_results(raw)
                print(
                    f"[CRM] Archive comparison complete (two-file ASCF engine). "
                    f"{len(self.results)} difference row(s)."
                )
                return self.results
        
        # Find archives
        print("[CRM] Scanning for archive files...")
        archives1 = self._find_archives(self.folder1_path)
        archives2 = self._find_archives(self.folder2_path)
        print(f"[CRM] Found {len(archives1)} archives in old build, {len(archives2)} in new build")
        
        # Build normalized name mapping
        norm_to_arch1 = {}
        norm_to_arch2 = {}
        
        for arch in archives1:
            norm = self._normalize_archive_filename(arch.name)
            if norm not in norm_to_arch1:
                norm_to_arch1[norm] = arch
        
        for arch in archives2:
            norm = self._normalize_archive_filename(arch.name)
            if norm not in norm_to_arch2:
                norm_to_arch2[norm] = arch
        
        all_normalized = set(norm_to_arch1.keys()) | set(norm_to_arch2.keys())
        self.total_files = len(all_normalized)
        
        # Compare each normalized archive
        for norm_name in sorted(all_normalized):
            self.processed_files += 1
            self._update_progress(norm_name)
            
            arch1 = norm_to_arch1.get(norm_name)
            arch2 = norm_to_arch2.get(norm_name)
            
            if arch1 and not arch2:
                # Archive only in old build
                self.results.append(CRMArchiveEntry(
                    archive_name=arch1.name,
                    member_path="",
                    status="only_in_old"
                ))
            elif not arch1 and arch2:
                # Archive only in new build
                self.results.append(CRMArchiveEntry(
                    archive_name=arch2.name,
                    member_path="",
                    status="only_in_new"
                ))
            else:
                # Compare archive contents with normalization for nested archives
                print(f"[CRM] Comparing: {arch1.name} vs {arch2.name}")
                
                names1, meta1, err1 = self._list_archive_members_ascf(arch1)
                names2, meta2, err2 = self._list_archive_members_ascf(arch2)
                if err1:
                    logger.warning(f"[CRM] Listing old archive {arch1}: {err1}")
                if err2:
                    logger.warning(f"[CRM] Listing new archive {arch2}: {err2}")
                
                # Normalize member names for comparison (handles version differences in nested JARs)
                normalized1 = self._normalize_member_set(names1)
                normalized2 = self._normalize_member_set(names2)
                
                # Build reverse mapping: normalized -> original name(s)
                norm_to_orig1 = {}
                norm_to_orig2 = {}
                for m in names1:
                    nm = self._normalize_inner_archive_marker(m)
                    if nm not in norm_to_orig1:
                        norm_to_orig1[nm] = m
                for m in names2:
                    nm = self._normalize_inner_archive_marker(m)
                    if nm not in norm_to_orig2:
                        norm_to_orig2[nm] = m
                
                # Files only in old (by normalized name)
                for norm_member in normalized1 - normalized2:
                    orig_member = norm_to_orig1.get(norm_member, norm_member)
                    size, crc = meta1.get(orig_member, (0, 0))
                    self.results.append(CRMArchiveEntry(
                        archive_name=norm_name,
                        member_path=norm_member,
                        status="only_in_old",
                        size_old=size,
                        crc_old=crc
                    ))
                
                # Files only in new (by normalized name)
                for norm_member in normalized2 - normalized1:
                    orig_member = norm_to_orig2.get(norm_member, norm_member)
                    size, crc = meta2.get(orig_member, (0, 0))
                    self.results.append(CRMArchiveEntry(
                        archive_name=norm_name,
                        member_path=norm_member,
                        status="only_in_new",
                        size_new=size,
                        crc_new=crc
                    ))
                
                # Files in both - check for modifications (using normalized names)
                for norm_member in normalized1 & normalized2:
                    orig1 = norm_to_orig1.get(norm_member, norm_member)
                    orig2 = norm_to_orig2.get(norm_member, norm_member)
                    size1, crc1 = meta1.get(orig1, (0, 0))
                    size2, crc2 = meta2.get(orig2, (0, 0))
                    
                    if size1 != size2 or crc1 != crc2:
                        self.results.append(CRMArchiveEntry(
                            archive_name=norm_name,
                            member_path=norm_member,
                            status="modified",
                            size_old=size1,
                            size_new=size2,
                            crc_old=crc1,
                            crc_new=crc2
                        ))
        
        print(f"[CRM] Archive comparison complete! Found {len(self.results)} differences")
        return self.results
    
    def generate_archive_report(self) -> str:
        """Generate text report."""
        lines = []
        lines.append("=" * 80)
        lines.append("CRM ARCHIVE CONTENTS COMPARISON REPORT")
        lines.append("=" * 80)
        lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"Old Build Path: {self.folder1_path}")
        lines.append(f"New Build Path: {self.folder2_path}")
        lines.append("")
        
        only_old = [r for r in self.results if r.status == 'only_in_old']
        only_new = [r for r in self.results if r.status == 'only_in_new']
        modified = [r for r in self.results if r.status == 'modified']
        
        lines.append("-" * 80)
        lines.append("SUMMARY")
        lines.append("-" * 80)
        lines.append(f"Only in Old Build: {len(only_old)}")
        lines.append(f"Only in New Build: {len(only_new)}")
        lines.append(f"Modified:          {len(modified)}")
        lines.append("")
        
        # Group by archive
        by_archive = {}
        for r in self.results:
            if r.archive_name not in by_archive:
                by_archive[r.archive_name] = []
            by_archive[r.archive_name].append(r)
        
        for archive_name in sorted(by_archive.keys()):
            entries = by_archive[archive_name]
            lines.append("-" * 80)
            lines.append(f"ARCHIVE: {archive_name}")
            lines.append("-" * 80)
            
            for entry in entries:
                if entry.member_path:
                    status_symbol = {
                        'only_in_old': '[-]',
                        'only_in_new': '[+]',
                        'modified': '[~]'
                    }.get(entry.status, '[?]')
                    lines.append(f"  {status_symbol} {entry.member_path}")
            lines.append("")
        
        lines.append("=" * 80)
        return "\n".join(lines)
    
    def generate_archive_html_report(self) -> str:
        """Generate HTML report."""
        only_old = [r for r in self.results if r.status == 'only_in_old']
        only_new = [r for r in self.results if r.status == 'only_in_new']
        modified = [r for r in self.results if r.status == 'modified']
        
        html_content = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>CRM Archive Contents Report</title>
    <style>
        body {{ font-family: 'Segoe UI', system-ui, sans-serif; margin: 0; padding: 20px; background: linear-gradient(135deg, #1e3a5f 0%, #2d5a87 100%); min-height: 100vh; }}
        .container {{ max-width: 1400px; margin: 0 auto; background: white; border-radius: 16px; box-shadow: 0 20px 60px rgba(0,0,0,0.3); overflow: hidden; }}
        .header {{ background: linear-gradient(135deg, #1e3a5f 0%, #2d5a87 100%); color: white; padding: 30px; }}
        .header h1 {{ margin: 0; font-size: 24px; }}
        .content {{ padding: 30px; }}
        .summary {{ display: flex; gap: 20px; margin-bottom: 30px; }}
        .summary-card {{ flex: 1; padding: 20px; border-radius: 12px; text-align: center; }}
        .summary-card.old {{ background: #fee2e2; border: 2px solid #ef4444; }}
        .summary-card.new {{ background: #dcfce7; border: 2px solid #22c55e; }}
        .summary-card.modified {{ background: #fef3c7; border: 2px solid #f59e0b; }}
        .summary-card .count {{ font-size: 32px; font-weight: bold; }}
        .archive-section {{ margin: 20px 0; border: 1px solid #e5e7eb; border-radius: 8px; overflow: hidden; }}
        .archive-header {{ background: #f9fafb; padding: 15px; font-weight: 600; cursor: pointer; }}
        .archive-content {{ padding: 15px; }}
        .entry {{ padding: 8px; font-family: monospace; font-size: 13px; border-bottom: 1px solid #f3f4f6; }}
        .entry.old {{ color: #ef4444; }}
        .entry.new {{ color: #22c55e; }}
        .entry.modified {{ color: #f59e0b; }}
        a {{ color: #2563eb; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🏢 CRM Archive Contents Comparison Report</h1>
            <p style="opacity:0.8;margin:10px 0 0 0;">Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
        </div>
        <div class="content">
            <div class="summary">
                <div class="summary-card old"><div class="count">{len(only_old)}</div><div>Only in Old</div></div>
                <div class="summary-card new"><div class="count">{len(only_new)}</div><div>Only in New</div></div>
                <div class="summary-card modified"><div class="count">{len(modified)}</div><div>Modified</div></div>
            </div>
"""
        
        # Group by archive
        by_archive = {}
        for r in self.results:
            if r.archive_name not in by_archive:
                by_archive[r.archive_name] = []
            by_archive[r.archive_name].append(r)
        
        for archive_name in sorted(by_archive.keys()):
            entries = by_archive[archive_name]
            html_content += f"""
            <div class="archive-section">
                <div class="archive-header">📦 {html.escape(archive_name)} ({len(entries)} differences)</div>
                <div class="archive-content">
"""
            # Show all entries so the HTML report includes the complete archive diff.
            for entry in entries:
                if entry.member_path:
                    css_class = {'only_in_old': 'old', 'only_in_new': 'new', 'modified': 'modified'}.get(entry.status, '')
                    symbol = {'only_in_old': '[-]', 'only_in_new': '[+]', 'modified': '[~]'}.get(entry.status, '')
                    html_content += f'                    <div class="entry {css_class}">{symbol} {html.escape(entry.member_path)}</div>\n'
            
            html_content += """                </div>
            </div>
"""
        
        html_content += """
            <p><a href="/">← Back to Home</a></p>
        </div>
    </div>
</body>
</html>"""
        
        return inject_report_section_controls(html_content)
    
    def save_archive_report(self, filepath: str):
        """Save HTML archive report only."""
        html_filepath = _crm_html_report_path(filepath)
        _crm_write_html_file(html_filepath, self.generate_archive_html_report())
    
    def cleanup_temp_folders(self):
        """Clean up temporary folders."""
        if self.should_cleanup_temp_folders:
            for temp in [self.temp_folder1, self.temp_folder2]:
                if temp:
                    try:
                        shutil.rmtree(temp)
                    except Exception:
                        pass


# ============================================================================
# CRM JAR MANIFEST COMPARER (FOSS/Foundation)
# ============================================================================

class CRMJarManifestComparer:
    """
    CRM-specific JAR manifest comparer.
    Compares Implementation-Version from MANIFEST.MF files.
    """
    
    def __init__(self, folder1: str, folder2: str, jar_name: str):
        """Initialize with folder paths and target JAR name."""
        self.folder1_path = Path(folder1)
        self.folder2_path = Path(folder2)
        self.jar_name = jar_name.strip()
        self.result = CRMJarManifestResult(self.jar_name)
        # Progress tracking
        self.total_files = 0
        self.processed_files = 0
        self.current_file = ""
        self.progress_percentage = 0.0
        self.estimated_time_remaining = 0
        self.comparison_start_time = None
        self.comparison_error = None
    
    def _update_progress(self, current_file=""):
        """Update progress tracking."""
        if self.total_files > 0:
            self.progress_percentage = (self.processed_files / self.total_files) * 100
        self.current_file = current_file
    
    def _find_jar(self, base: Path) -> str:
        """Find a JAR file by name (exact or partial match)."""
        target = self.jar_name.lower()
        exact_match = None
        partial_match = None
        
        try:
            for root, _, files in os.walk(str(_walk_base_path(base))):
                for f in files:
                    if not f.lower().endswith('.jar'):
                        continue
                    if f.lower() == target:
                        return str(Path(root) / f)
                    if target in f.lower() and partial_match is None:
                        partial_match = str(Path(root) / f)
        except Exception as e:
            logger.warning(f"[CRM] Error searching for JAR {self.jar_name}: {e}")
        
        return exact_match or partial_match or ""
    
    def _read_manifest_version(self, jar_path: str) -> tuple:
        """Read Implementation-Version from JAR's MANIFEST.MF."""
        if not jar_path:
            return "", "JAR not found"
        
        try:
            with zipfile.ZipFile(_fs_path_str(Path(jar_path)), 'r') as zf:
                try:
                    data = zf.read('META-INF/MANIFEST.MF')
                except KeyError:
                    return "", "MANIFEST.MF not found"
                
                text = data.decode('utf-8', errors='replace')
                for line in text.splitlines():
                    if line.lower().startswith('implementation-version:'):
                        return line.split(':', 1)[1].strip(), ""
                
                return "", "Implementation-Version not found in manifest"
        except Exception as e:
            return "", f"Error reading JAR: {e}"
    
    def compare_versions(self) -> CRMJarManifestResult:
        """Compare Implementation-Version between folders."""
        print(f"[CRM] Starting FOSS/Foundation comparison for: {self.jar_name}")
        print(f"[CRM] Old Build: {self.folder1_path}")
        print(f"[CRM] New Build: {self.folder2_path}")
        
        self.comparison_start_time = time.time()
        self.total_files = 1
        self.current_file = self.jar_name
        self._update_progress(self.jar_name)
        
        # Find JARs
        jar1 = self._find_jar(self.folder1_path)
        jar2 = self._find_jar(self.folder2_path)
        
        self.result.path_folder1 = jar1
        self.result.path_folder2 = jar2
        
        # Read versions
        v1, e1 = self._read_manifest_version(jar1)
        v2, e2 = self._read_manifest_version(jar2)
        
        self.result.version_folder1 = v1
        self.result.version_folder2 = v2
        self.result.error_folder1 = e1
        self.result.error_folder2 = e2
        
        self.processed_files = 1
        self._update_progress(self.jar_name)
        
        print(f"[CRM] Old version: {v1 or 'N/A'} ({e1 or 'OK'})")
        print(f"[CRM] New version: {v2 or 'N/A'} ({e2 or 'OK'})")
        
        return self.result
    
    def generate_report(self) -> str:
        """Generate text report."""
        r = self.result
        lines = []
        lines.append("=" * 80)
        lines.append("CRM FOSS/FOUNDATION JAR VERSION COMPARISON REPORT")
        lines.append("=" * 80)
        lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"Old Build Path: {self.folder1_path}")
        lines.append(f"New Build Path: {self.folder2_path}")
        lines.append(f"Target JAR: {r.jar_name}")
        lines.append("")
        lines.append("-" * 80)
        lines.append("RESULTS")
        lines.append("-" * 80)
        lines.append(f"Old Build JAR: {r.path_folder1 or 'NOT FOUND'}")
        if r.error_folder1:
            lines.append(f"  Note: {r.error_folder1}")
        lines.append(f"Implementation-Version (Old): {r.version_folder1 or 'N/A'}")
        lines.append("")
        lines.append(f"New Build JAR: {r.path_folder2 or 'NOT FOUND'}")
        if r.error_folder2:
            lines.append(f"  Note: {r.error_folder2}")
        lines.append(f"Implementation-Version (New): {r.version_folder2 or 'N/A'}")
        lines.append("")
        
        if r.version_folder1 and r.version_folder2:
            if r.version_folder1 == r.version_folder2:
                lines.append("SUMMARY: Versions match - no difference found.")
            else:
                lines.append("SUMMARY: Versions differ!")
        else:
            lines.append("SUMMARY: Could not compare (missing JAR or version).")
        
        lines.append("")
        lines.append("=" * 80)
        return "\n".join(lines)
    
    def generate_html_report(self) -> str:
        """Generate HTML report."""
        r = self.result
        versions_match = r.version_folder1 and r.version_folder2 and r.version_folder1 == r.version_folder2
        
        html_content = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>CRM FOSS/Foundation Report</title>
    <style>
        body {{ font-family: 'Segoe UI', system-ui, sans-serif; margin: 0; padding: 20px; background: linear-gradient(135deg, #1e3a5f 0%, #2d5a87 100%); min-height: 100vh; }}
        .container {{ max-width: 800px; margin: 0 auto; background: white; border-radius: 16px; box-shadow: 0 20px 60px rgba(0,0,0,0.3); overflow: hidden; }}
        .header {{ background: linear-gradient(135deg, #1e3a5f 0%, #2d5a87 100%); color: white; padding: 30px; }}
        .header h1 {{ margin: 0; font-size: 24px; }}
        .content {{ padding: 30px; }}
        .jar-info {{ background: #f9fafb; padding: 20px; border-radius: 8px; margin: 15px 0; }}
        .jar-info h3 {{ margin: 0 0 10px 0; color: #374151; }}
        .version {{ font-size: 24px; font-weight: bold; font-family: monospace; }}
        .version.old {{ color: #ef4444; }}
        .version.new {{ color: #22c55e; }}
        .summary {{ padding: 20px; border-radius: 8px; margin-top: 20px; text-align: center; font-size: 18px; }}
        .summary.match {{ background: #dcfce7; border: 2px solid #22c55e; color: #166534; }}
        .summary.differ {{ background: #fee2e2; border: 2px solid #ef4444; color: #991b1b; }}
        a {{ color: #2563eb; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🏢 CRM FOSS/Foundation JAR Version Report</h1>
            <p style="opacity:0.8;margin:10px 0 0 0;">Target JAR: {html.escape(r.jar_name)}</p>
        </div>
        <div class="content">
            <div class="section" id="old-build">
            <h2>📁 Old Build</h2>
            <div class="jar-info">
                <h3>Jar location &amp; version</h3>
                <p>{html.escape(r.path_folder1 or 'JAR not found')}</p>
                <div class="version old">{html.escape(r.version_folder1 or 'N/A')}</div>
                {f'<p style="color:#6b7280;font-size:12px;">{html.escape(r.error_folder1)}</p>' if r.error_folder1 else ''}
            </div>
            </div>
            <div class="section" id="new-build">
            <h2>📁 New Build</h2>
            <div class="jar-info">
                <h3>Jar location &amp; version</h3>
                <p>{html.escape(r.path_folder2 or 'JAR not found')}</p>
                <div class="version new">{html.escape(r.version_folder2 or 'N/A')}</div>
                {f'<p style="color:#6b7280;font-size:12px;">{html.escape(r.error_folder2)}</p>' if r.error_folder2 else ''}
            </div>
            </div>
            <div class="section" id="summary">
            <h2>Summary</h2>
            <div class="summary {'match' if versions_match else 'differ'}">
                {'✅ Versions Match' if versions_match else '⚠️ Versions Differ' if r.version_folder1 and r.version_folder2 else '❓ Could not compare'}
            </div>
            </div>
            <p style="margin-top:20px;"><a href="/">← Back to Home</a></p>
        </div>
    </div>
</body>
</html>"""
        
        return inject_report_section_controls(html_content)
    
    def save_report(self, filepath: str):
        """Save HTML report only."""
        html_filepath = _crm_html_report_path(filepath)
        _crm_write_html_file(html_filepath, self.generate_html_report())


# ============================================================================
# LEGACY COMPATIBILITY - CRMComparer class
# ============================================================================

class CRMComparer:
    """
    Legacy CRM comparer for backward compatibility.
    Routes to appropriate comparer based on mode.
    """
    
    def __init__(self, folder1: str, folder2: str):
        self.folder1 = folder1
        self.folder2 = folder2
        self.results = []
        self.comparison_error = None
        self.suppress_warnings = True
        self.show_missing_only = False
        self.ignore_timestamp_only_differences = True
        self._internal_comparer = None
    
    def compare_folders(self):
        """Default comparison using full content comparer."""
        comparer = CRMFolderComparer(self.folder1, self.folder2)
        comparer.suppress_warnings = self.suppress_warnings
        comparer.show_missing_only = self.show_missing_only
        self._internal_comparer = comparer
        self.results = comparer.compare_folders()
        self.comparison_error = comparer.comparison_error
        return self.results
    
    def generate_report(self) -> str:
        if self._internal_comparer:
            return self._internal_comparer.generate_report()
        return "No comparison performed."
    
    def generate_html_report(self) -> str:
        if self._internal_comparer:
            return self._internal_comparer.generate_html_report()
        return "<html><body>No comparison performed.</body></html>"
    
    def save_report(self, filepath: str):
        if self._internal_comparer:
            self._internal_comparer.report_txt_basename = None
        html_filepath = _crm_html_report_path(filepath)
        _crm_write_html_file(html_filepath, self.generate_html_report())


# ============================================================================
# MAIN ENTRY POINTS
# ============================================================================

def _bind_comparer_progress(comparer, options: dict) -> None:
    """Expose the live comparer on session/global progress for /progress polling."""
    session_progress = options.get("session_progress")
    if isinstance(session_progress, dict):
        session_progress["comparer"] = comparer
    comparison_progress = options.get("comparison_progress")
    if isinstance(comparison_progress, dict):
        comparison_progress["comparer"] = comparer


def run_crm_comparison(folder1: str, folder2: str, mode: str = "full", jar_name: str = "", **options) -> tuple:
    """
    Main entry point for CRM comparison.

    Args:
        folder1: Path to old CRM build
        folder2: Path to new CRM build
        mode: Comparison mode - 'full', 'naming', 'archives', 'foss'
        jar_name: JAR name for FOSS/Foundation mode
        **options: Additional options, including:
            class_diff_jar_name / crm_class_diff_jar: comma-separated basenames for optional per-JAR .class/CFR diffs (full mode).
            manifest_diff_jar_name / crm_manifest_diff_jar: comma-separated basenames for optional per-JAR MANIFEST Implementation-Version diff (full mode).
            compare_clfy_agent_zip: if True, size-only compare ClfyAgent .zip/.ear under each SDK (full mode).
            config_files_filter / crm_config_files_filter: comma-separated names or glob patterns; required to
            show the "Text and config files changed" block in the HTML report; also matches non–.jar/.war/.ear/.zip files under the build.
            explicit_size_compare_query / crm_explicit_size_paths: optional comma-separated basenames, path suffixes, or globs; adds a focused old/new size table in HTML/TXT (replaces the removed bulk archive list).

    Returns:
        Tuple of (comparer, results, output_filename)
    """
    from report_naming import build_report_filename

    report_user = (
        options.get("report_user")
        or options.get("client_identity")
        or "unknown"
    )
    _prefix = {
        "full": "crm_full_comparison",
        "naming": "crm_naming_comparison",
        "archives": "crm_archives_comparison",
        "foss": "crm_foss_comparison",
    }

    if mode == "full":
        comparer = CRMFolderComparer(folder1, folder2)
        comparer.suppress_warnings = options.get('suppress_warnings', True)
        comparer.show_missing_only = options.get('show_missing_only', False)
        comparer.fast_mode = options.get('fast_mode', True)
        comparer.deep_nested_archives = options.get('deep_nested_archives', False)
        comparer.compare_archive_contents = options.get('compare_archive_contents', False)
        comparer.use_local_copy = options.get('use_local_copy', False)
        comparer.should_cleanup_temp_folders = not options.get('keep_temp_folders', False)
        try:
            comparer.max_retries = int(options.get('max_retries', 3))
        except (TypeError, ValueError):
            comparer.max_retries = 3
        try:
            comparer.retry_delay = float(options.get('retry_delay', 1.0))
        except (TypeError, ValueError):
            comparer.retry_delay = 1.0
        comparer.class_diff_jar_name = (
            options.get("class_diff_jar_name") or options.get("crm_class_diff_jar") or ""
        ).strip()
        comparer.manifest_diff_jar_name = (
            options.get("manifest_diff_jar_name") or options.get("crm_manifest_diff_jar") or ""
        ).strip()
        comparer.compare_clfy_agent_zip = bool(options.get("compare_clfy_agent_zip", False))
        comparer.run_full_tree_compare = bool(
            options.get("run_full_tree_compare", options.get("fast_mode", True))
        )
        comparer.compare_application = str(
            options.get("compare_application") or options.get("application") or "CRM"
        ).strip()
        comparer.config_files_filter_query = (
            options.get("config_files_filter") or options.get("crm_config_files_filter") or ""
        ).strip()
        comparer.explicit_size_compare_query = (
            options.get("explicit_size_compare_query")
            or options.get("crm_explicit_size_paths")
            or ""
        ).strip()
        _bind_comparer_progress(comparer, options)
        results = comparer.compare_folders()
        _rd = Path(str(options.get("report_dir") or "."))
        output_filename = build_report_filename(
            _prefix["full"], folder1, folder2, report_user, report_dir=_rd, suffix=_CRM_REPORT_HTML_SUFFIX
        )

    elif mode == "naming":
        comparer = CRMNamingComparer(folder1, folder2)
        comparer.suppress_warnings = options.get('suppress_warnings', True)
        comparer.deep_nested_archives = options.get('deep_nested_archives', True)
        _bind_comparer_progress(comparer, options)
        results = comparer.compare_naming()
        _rd = Path(str(options.get("report_dir") or "."))
        output_filename = build_report_filename(
            _prefix["naming"], folder1, folder2, report_user, report_dir=_rd, suffix=_CRM_REPORT_HTML_SUFFIX
        )

    elif mode == "archives":
        comparer = CRMArchiveComparer(folder1, folder2)
        comparer.suppress_warnings = options.get('suppress_warnings', True)
        comparer.deep_nested_archives = options.get('deep_nested_archives', True)
        comparer.use_local_copy = options.get('use_local_copy', False)
        comparer.should_cleanup_temp_folders = not options.get('keep_temp_folders', False)
        try:
            comparer.max_retries = int(options.get('max_retries', 3))
        except (TypeError, ValueError):
            comparer.max_retries = 3
        try:
            comparer.retry_delay = float(options.get('retry_delay', 1.0))
        except (TypeError, ValueError):
            comparer.retry_delay = 1.0
        _bind_comparer_progress(comparer, options)
        results = comparer.compare_archives()
        _rd = Path(str(options.get("report_dir") or "."))
        output_filename = build_report_filename(
            _prefix["archives"], folder1, folder2, report_user, report_dir=_rd, suffix=_CRM_REPORT_HTML_SUFFIX
        )

    elif mode == "foss":
        if not jar_name:
            raise ValueError("JAR name is required for FOSS/Foundation mode")
        comparer = CRMJarManifestComparer(folder1, folder2, jar_name)
        _bind_comparer_progress(comparer, options)
        results = comparer.compare_versions()
        _rd = Path(str(options.get("report_dir") or "."))
        output_filename = build_report_filename(
            _prefix["foss"], folder1, folder2, report_user, report_dir=_rd, suffix=_CRM_REPORT_HTML_SUFFIX
        )

    else:
        # Default to full comparison
        comparer = CRMFolderComparer(folder1, folder2)
        comparer.deep_nested_archives = options.get('deep_nested_archives', True)
        _bind_comparer_progress(comparer, options)
        results = comparer.compare_folders()
        _rd = Path(str(options.get("report_dir") or "."))
        output_filename = build_report_filename(
            "crm_comparison", folder1, folder2, report_user, report_dir=_rd, suffix=_CRM_REPORT_HTML_SUFFIX
        )

    return comparer, results, output_filename


if __name__ == "__main__":
    # Test the module
    print("CRM Comparison Module loaded successfully!")
    print("Available comparers:")
    print("  - CRMFolderComparer (full content)")
    print("  - CRMNamingComparer (naming conventions)")
    print("  - CRMArchiveComparer (archive contents)")
    print("  - CRMJarManifestComparer (FOSS/Foundation)")