#!/usr/bin/env python3
"""
CRM crm-playbooks Nexus verification: download TAR artifacts from Nexus and
compare .yaml files under the amdocs-version folder (previous vs latest).
"""

from __future__ import annotations

import difflib
import hashlib
import html
import os
import re
import shutil
import ssl
import tarfile
import tempfile
import urllib.request
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

CRM_PLAYBOOKS_NEXUS_BASE = (
    "https://indlin2410:28081/nexus/content/repositories"
)
_CRM_PLAYBOOKS_VERSION_RE = re.compile(r"^\d+(?:\.\d+)+(?:-\d+)?$")
_AMDOCS_VERSION_FOLDER = "amdocs-version"


def parse_crm_playbooks_nexus_version(version: str) -> tuple[str, str]:
    """Return (release_train, full_version) e.g. ('10.24.1', '10.24.1.7-4410')."""
    version = (version or "").strip()
    if not version or not _CRM_PLAYBOOKS_VERSION_RE.match(version):
        raise ValueError(
            f"Invalid crm-playbooks Nexus version '{version}'. "
            "Use e.g. 10.24.1.7-4410 or 10.24.1.6-4368"
        )
    parts = version.split(".")
    if len(parts) < 3:
        raise ValueError(f"Version must have at least three segments: {version}")
    release_train = ".".join(parts[:3])
    return release_train, version


def build_crm_playbooks_tar_url(version: str) -> Dict[str, str]:
    """Build Nexus TAR URL for one crm-playbooks version."""
    release_train, full_version = parse_crm_playbooks_nexus_version(version)
    repo = f"crm-v{release_train}-Releases"
    base = (
        f"{CRM_PLAYBOOKS_NEXUS_BASE}/{repo}/com/amdocs/core/crm/crm-playbooks/"
        f"{full_version}/crm-playbooks-{full_version}"
    )
    return {
        "release_train": release_train,
        "full_version": full_version,
        "repo": repo,
        "tar": f"{base}.tar",
    }


def _insecure_ssl_context():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def download_crm_playbooks_tar(
    version: str,
    dest_dir: Path,
    log_callback: Optional[Callable[[str, str], None]] = None,
) -> Path:
    """Download crm-playbooks-{version}.tar into dest_dir."""
    urls = build_crm_playbooks_tar_url(version)
    url = urls["tar"]
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"crm-playbooks-{urls['full_version']}.tar"
    if log_callback:
        log_callback(f"Downloading {url}", "INFO")
    req = urllib.request.Request(url, headers={"User-Agent": "CRM-Playbooks-Nexus/1.0"})
    errors: list[Exception] = []
    for ctx in (ssl.create_default_context(), _insecure_ssl_context()):
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=600) as resp:
                with open(dest, "wb") as out:
                    shutil.copyfileobj(resp, out)
            if log_callback:
                log_callback(f"Saved {dest.name} ({dest.stat().st_size:,} bytes)", "INFO")
            return dest
        except Exception as e:
            errors.append(e)
    if errors:
        raise errors[-1]
    raise RuntimeError(f"Failed to download {url}")


def _normalize_tar_member_path(name: str) -> str:
    return (name or "").replace("\\", "/").strip("/")


def _in_amdocs_version_folder(member_name: str) -> bool:
    """True when a tar member path lies under an amdocs-version directory."""
    n = _normalize_tar_member_path(member_name).lower()
    if not n:
        return False
    parts = n.split("/")
    return _AMDOCS_VERSION_FOLDER in parts


def _relative_amdocs_version_path(member_name: str) -> str:
    """Path relative to amdocs-version/ for stable compare keys across tar roots."""
    n = _normalize_tar_member_path(member_name)
    parts = n.split("/")
    for i, p in enumerate(parts):
        if p.lower() == _AMDOCS_VERSION_FOLDER:
            return "/".join(parts[i + 1 :])
    return n


def _is_yaml_member(name: str) -> bool:
    lower = name.lower()
    return lower.endswith(".yml") or lower.endswith(".yaml")


def _read_tar_member_bytes(tar_path: Path, member_name: str) -> bytes:
    with tarfile.open(tar_path, "r:*") as tf:
        member = tf.getmember(member_name)
        extracted = tf.extractfile(member)
        if extracted is None:
            return b""
        return extracted.read()


def _yaml_map_in_amdocs_version(tar_path: Path) -> Dict[str, str]:
    """Map relative amdocs-version path -> absolute tar member name."""
    out: Dict[str, str] = {}
    with tarfile.open(tar_path, "r:*") as tf:
        for m in tf.getmembers():
            if not m.isfile():
                continue
            if not _is_yaml_member(m.name):
                continue
            if not _in_amdocs_version_folder(m.name):
                continue
            rel = _relative_amdocs_version_path(m.name)
            if rel:
                out[rel] = m.name
    return out


def _build_yaml_content_diff_rows(b1: bytes, b2: bytes) -> List[dict]:
    try:
        t1 = b1.decode("utf-8", errors="replace").splitlines()
        t2 = b2.decode("utf-8", errors="replace").splitlines()
    except Exception:
        return [
            {
                "change": "binary",
                "line_prev": "",
                "line_latest": "",
                "previous": "(binary or non-text content)",
                "latest": "(binary or non-text content)",
            }
        ]
    rows: List[dict] = []
    matcher = difflib.SequenceMatcher(None, t1, t2)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag == "replace":
            prev_block = t1[i1:i2]
            latest_block = t2[j1:j2]
            max_len = max(len(prev_block), len(latest_block))
            for idx in range(max_len):
                rows.append(
                    {
                        "change": "modified",
                        "line_prev": str(i1 + idx + 1) if idx < len(prev_block) else "",
                        "line_latest": str(j1 + idx + 1) if idx < len(latest_block) else "",
                        "previous": prev_block[idx] if idx < len(prev_block) else "",
                        "latest": latest_block[idx] if idx < len(latest_block) else "",
                    }
                )
        elif tag == "delete":
            for idx, line in enumerate(t1[i1:i2]):
                rows.append(
                    {
                        "change": "removed",
                        "line_prev": str(i1 + idx + 1),
                        "line_latest": "",
                        "previous": line,
                        "latest": "",
                    }
                )
        elif tag == "insert":
            for idx, line in enumerate(t2[j1:j2]):
                rows.append(
                    {
                        "change": "added",
                        "line_prev": "",
                        "line_latest": str(j1 + idx + 1),
                        "previous": "",
                        "latest": line,
                    }
                )
    if not rows and b1 != b2:
        rows.append(
            {
                "change": "modified",
                "line_prev": "1",
                "line_latest": "1",
                "previous": "(content differs)",
                "latest": "(content differs)",
            }
        )
    return rows[:200]


def compare_crm_playbooks_yaml_in_amdocs_version(
    previous_tar: Path,
    latest_tar: Path,
    *,
    log_callback: Optional[Callable[[str, str], None]] = None,
) -> Dict[str, Any]:
    """Compare .yaml/.yml files under amdocs-version/ in two crm-playbooks TARs."""
    if log_callback:
        log_callback(
            f"Comparing YAML under {_AMDOCS_VERSION_FOLDER}/ in "
            f"{previous_tar.name} vs {latest_tar.name}",
            "INFO",
        )
    map1 = _yaml_map_in_amdocs_version(previous_tar)
    map2 = _yaml_map_in_amdocs_version(latest_tar)
    keys1, keys2 = set(map1.keys()), set(map2.keys())
    removed = sorted(keys1 - keys2)
    added = sorted(keys2 - keys1)
    modified: List[dict] = []

    for rel in sorted(keys1 & keys2):
        m1 = map1[rel]
        m2 = map2[rel]
        b1 = _read_tar_member_bytes(previous_tar, m1)
        b2 = _read_tar_member_bytes(latest_tar, m2)
        if hashlib.sha256(b1).digest() == hashlib.sha256(b2).digest():
            continue
        modified.append(
            {
                "name": rel,
                "path_previous": m1,
                "path_latest": m2,
                "old_size": len(b1),
                "new_size": len(b2),
                "size_delta": len(b2) - len(b1),
                "content_rows": _build_yaml_content_diff_rows(b1, b2),
            }
        )

    has_changes = bool(added or removed or modified)
    return {
        "added": added,
        "removed": removed,
        "modified": modified,
        "total_previous": len(keys1),
        "total_latest": len(keys2),
        "has_changes": has_changes,
        "previous_tar": str(previous_tar),
        "latest_tar": str(latest_tar),
    }


def _content_rows_html(content_rows: List[dict]) -> str:
    if not content_rows:
        return ""
    styles = {
        "added": ("#166534", "#f0fdf4"),
        "removed": ("#b91c1c", "#fef2f2"),
        "modified": ("#b45309", "#fffbeb"),
        "binary": ("#7c2d12", "#fff7ed"),
    }
    body = []
    for row in content_rows:
        change = row.get("change", "modified")
        color, bg = styles.get(change, ("#334155", "#f8fafc"))
        body.append(
            f"<tr style='background:{bg};'>"
            f"<td style='color:{color};font-weight:600;'>{html.escape(change)}</td>"
            f"<td style='text-align:center;font-family:Consolas,monospace;'>{html.escape(str(row.get('line_prev', '')))}</td>"
            f"<td style='font-family:Consolas,monospace;font-size:12px;white-space:pre-wrap;'>{html.escape(row.get('previous', ''))}</td>"
            f"<td style='text-align:center;font-family:Consolas,monospace;'>{html.escape(str(row.get('line_latest', '')))}</td>"
            f"<td style='font-family:Consolas,monospace;font-size:12px;white-space:pre-wrap;'>{html.escape(row.get('latest', ''))}</td>"
            f"</tr>"
        )
    truncated = ""
    if len(content_rows) >= 200:
        truncated = "<p style='font-size:12px;color:#64748b;'>Showing first 200 changed lines.</p>"
    return (
        truncated
        + "<table class='comparison-table' style='width:100%;border-collapse:collapse;margin:12px 0;'>"
        + "<thead><tr><th>Change</th><th>Line (prev)</th><th>Previous</th><th>Line (latest)</th><th>Latest</th></tr></thead>"
        + f"<tbody>{''.join(body)}</tbody></table>"
    )


def render_crm_playbooks_nexus_html(meta: Dict[str, Any], yaml_diff: Dict[str, Any]) -> str:
    """HTML report body for crm-playbooks Nexus YAML verification."""
    prev_urls = meta.get("previous_urls") or {}
    latest_urls = meta.get("latest_urls") or {}
    prev_v = html.escape(str(meta.get("previous_version", "")))
    latest_v = html.escape(str(meta.get("latest_version", "")))
    parts = [
        "<!DOCTYPE html><html><head><meta charset='utf-8'>",
        "<title>CRM crm-playbooks Nexus YAML verification</title>",
        "<style>",
        "body{font-family:Segoe UI,Arial,sans-serif;margin:24px;background:#f8fafc;color:#1e293b;}",
        ".section{background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:20px;margin-bottom:20px;}",
        ".success-box{background:#ecfdf5;border:1px solid #86efac;border-radius:8px;padding:14px;}",
        "code{background:#f1f5f9;padding:2px 6px;border-radius:4px;font-size:12px;}",
        "a{color:#2563eb;word-break:break-all;}",
        "h1{color:#1e40af;} h2{color:#9a3412;}",
        "</style></head><body>",
        "<h1>CRM crm-playbooks Nexus verification</h1>",
        f"<p>Generated: {html.escape(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))}</p>",
        "<div class='section'>",
        f"<p><strong>Previous:</strong> {prev_v} | <strong>Latest:</strong> {latest_v}</p>",
        f"<p><strong>Previous TAR:</strong> <a href='{html.escape(prev_urls.get('tar', ''))}' target='_blank' rel='noopener'>"
        f"{html.escape(prev_urls.get('tar', ''))}</a></p>",
        f"<p><strong>Latest TAR:</strong> <a href='{html.escape(latest_urls.get('tar', ''))}' target='_blank' rel='noopener'>"
        f"{html.escape(latest_urls.get('tar', ''))}</a></p>",
        f"<p>Scope: <code>{_AMDOCS_VERSION_FOLDER}/</code> folder — <strong>.yaml</strong> / <strong>.yml</strong> files only.</p>",
        f"<p>Previous YAML count: <strong>{yaml_diff.get('total_previous', 0)}</strong> | "
        f"Latest YAML count: <strong>{yaml_diff.get('total_latest', 0)}</strong></p>",
        "</div>",
    ]

    if not yaml_diff.get("has_changes"):
        parts.append(
            "<div class='section success-box'><p>All YAML files under "
            f"<code>{_AMDOCS_VERSION_FOLDER}/</code> are identical between versions.</p></div>"
        )
    else:
        if yaml_diff.get("removed"):
            rows = "".join(
                f"<tr><td>{i}</td><td><code>{html.escape(p)}</code></td></tr>"
                for i, p in enumerate(yaml_diff["removed"][:100], 1)
            )
            parts.append(
                f"<div class='section'><h2>Removed in latest ({len(yaml_diff['removed'])})</h2>"
                f"<table class='comparison-table'><thead><tr><th>#</th><th>Path (under amdocs-version)</th></tr></thead>"
                f"<tbody>{rows}</tbody></table></div>"
            )
        if yaml_diff.get("added"):
            rows = "".join(
                f"<tr><td>{i}</td><td><code>{html.escape(p)}</code></td></tr>"
                for i, p in enumerate(yaml_diff["added"][:100], 1)
            )
            parts.append(
                f"<div class='section'><h2>Added in latest ({len(yaml_diff['added'])})</h2>"
                f"<table class='comparison-table'><thead><tr><th>#</th><th>Path (under amdocs-version)</th></tr></thead>"
                f"<tbody>{rows}</tbody></table></div>"
            )
        for idx, m in enumerate(yaml_diff.get("modified") or [], 1):
            delta = m.get("size_delta", 0)
            delta_s = f"+{delta:,}" if delta > 0 else f"{delta:,}"
            parts.append(
                f"<div class='section'><h2>Modified #{idx}: <code>{html.escape(m.get('name', ''))}</code></h2>"
                f"<p>Size: {m.get('old_size', 0):,} → {m.get('new_size', 0):,} ({delta_s} bytes)</p>"
                f"{_content_rows_html(m.get('content_rows') or [])}</div>"
            )

    parts.append("</body></html>")
    return "".join(parts)


def run_crm_playbooks_nexus_comparison(
    previous_version: str,
    latest_version: str,
    *,
    log_callback: Optional[Callable[[str, str], None]] = None,
    progress_callback: Optional[Callable[[str, int, int], None]] = None,
    report_user: str = "unknown",
    report_dir: Optional[str] = None,
) -> tuple[Dict[str, Any], Dict[str, Any], str]:
    """
    Download both TARs, compare YAML under amdocs-version/, write HTML report.
    Returns (meta, yaml_diff, saved_html_basename).
    """
    from report_naming import build_report_filename

    prev_v = (previous_version or "").strip()
    latest_v = (latest_version or "").strip()
    if not prev_v or not latest_v:
        raise ValueError("Enter both previous and latest crm-playbooks Nexus versions.")
    if prev_v == latest_v:
        raise ValueError("Previous and latest crm-playbooks versions must differ.")

    prev_urls = build_crm_playbooks_tar_url(prev_v)
    latest_urls = build_crm_playbooks_tar_url(latest_v)
    work_root = Path(tempfile.mkdtemp(prefix="crm_playbooks_nexus_"))
    try:
        if progress_callback:
            progress_callback("Downloading previous crm-playbooks TAR...", 1, 4)
        prev_tar = download_crm_playbooks_tar(prev_v, work_root / "previous", log_callback)
        if progress_callback:
            progress_callback("Downloading latest crm-playbooks TAR...", 2, 4)
        latest_tar = download_crm_playbooks_tar(latest_v, work_root / "latest", log_callback)
        if progress_callback:
            progress_callback("Comparing YAML under amdocs-version...", 3, 4)
        yaml_diff = compare_crm_playbooks_yaml_in_amdocs_version(
            prev_tar, latest_tar, log_callback=log_callback
        )
        meta = {
            "previous_version": prev_v,
            "latest_version": latest_v,
            "previous_urls": prev_urls,
            "latest_urls": latest_urls,
            "work_root": str(work_root),
        }
        if progress_callback:
            progress_callback("Writing report...", 4, 4)
        html_body = render_crm_playbooks_nexus_html(meta, yaml_diff)
        _rd = Path(str(report_dir or "."))
        path_hint = f"crm-playbooks_{prev_v}_vs_{latest_v}"
        base_name = build_report_filename(
            "crm_playbooks_nexus",
            path_hint,
            report_user,
            _rd,
            suffix=".html",
        )
        out_path = _rd / base_name
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(html_body, encoding="utf-8")
        if log_callback:
            log_callback(f"Report saved: {base_name}", "INFO")
        return meta, yaml_diff, base_name
    finally:
        try:
            shutil.rmtree(work_root, ignore_errors=True)
        except Exception:
            pass
