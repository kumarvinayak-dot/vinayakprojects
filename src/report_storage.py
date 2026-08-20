"""
Persist comparison reports on the client workstation (browser user's machine).

Reports stay until the user deletes them — no automatic cleanup.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

MAX_BROWSE_REPORTS = 30

DEFAULT_CLIENT_REPORT_ROOT = r"\\{client}\c$\crm_compare_reports"

REPORT_LIST_PATTERNS = (
    "*_report.txt",
    "*_report.html",
    "ascf_patch_comparison_*.txt",
    "ascf_simple_names_report_*.txt",
    "ascf_naming_convention_report_*.txt",
    "ascf_archives_report_*.txt",
    "ascf_foss_foundation_report_*.txt",
    "crm_patch_comparison_*.txt",
    "crm_full_comparison_*.txt",
    "crm_full_comparison_*.html",
    "crm_naming_comparison_*.txt",
    "crm_archives_comparison_*.txt",
    "crm_archives_report_*.txt",
    "crm_foss_comparison_*.txt",
    "crm_comparison_*.txt",
    "uxf_patch_comparison_*.txt",
    "dex_patch_comparison_*.txt",
    "environment_jar_validation_*.html",
)

# Legacy alias — listing uses text (or html-only) patterns so pairs are not duplicated.
REPORT_GLOB_PATTERNS = REPORT_LIST_PATTERNS

ALLOWED_REPORT_PREFIXES = (
    "ascf_patch_comparison_",
    "ascf_simple_names_report_",
    "ascf_naming_convention_report_",
    "ascf_archives_report_",
    "ascf_foss_foundation_report_",
    "crm_patch_comparison_",
    "crm_full_comparison_",
    "crm_naming_comparison_",
    "crm_archives_comparison_",
    "crm_archives_report_",
    "crm_foss_comparison_",
    "crm_comparison_",
    "uxf_patch_comparison_",
    "dex_patch_comparison_",
    "environment_jar_validation_",
)

_NEW_REPORT_RE = re.compile(r"_.+_report\.(txt|html)$", re.IGNORECASE)


def _short_hostname(name: str) -> str:
    return str(name or "").strip().split(".")[0]


def client_report_dir_candidates(machine_host: str) -> list[Path]:
    host = _short_hostname(machine_host)
    if not host or host in ("localhost", "client", "127.0.0.1"):
        return []

    tpl = (os.environ.get("CRM_CLIENT_REPORT_ROOT") or DEFAULT_CLIENT_REPORT_ROOT).strip()
    paths: list[Path] = []
    if "{client}" in tpl:
        paths.append(Path(tpl.replace("{client}", host)))
    elif tpl:
        paths.append(Path(tpl))

    paths.extend(
        [
            Path(rf"\\{host}\c$\crm_compare_reports"),
            Path(rf"\\{host}\c$\Users\Public\crm_compare_reports"),
            Path(rf"\\{host}\c$\Documents\crm_compare_reports"),
        ]
    )
    seen: set[str] = set()
    out: list[Path] = []
    for p in paths:
        key = str(p).lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def resolve_report_save_dir(machine_host: str, project_root: Path) -> Path:
    """Prefer client workstation folder; fall back to project root if UNC is unavailable."""
    for candidate in client_report_dir_candidates(machine_host):
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / ".write_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            logger.info("Using client report directory: %s", candidate)
            return candidate
        except OSError as e:
            logger.debug("Client report dir unavailable %s: %s", candidate, e)
            continue
    logger.info("Falling back to server report directory: %s", project_root)
    return Path(project_root)


def is_allowed_report_name(name: str) -> bool:
    name_l = str(name or "").strip().lower()
    if not name_l or name_l != os.path.basename(name_l):
        return False
    if not name_l.endswith((".txt", ".html")):
        return False
    if _NEW_REPORT_RE.search(name_l):
        return True
    return any(name_l.startswith(pref) for pref in ALLOWED_REPORT_PREFIXES)


def list_report_files(
    report_dirs: Iterable[Path],
    *,
    limit: Optional[int] = MAX_BROWSE_REPORTS,
) -> list[Path]:
    """Newest comparison reports — one row per run (txt index; html-only when no txt)."""
    seen_names: set[str] = set()
    by_stem: dict[str, Path] = {}
    for root in report_dirs:
        root = Path(root)
        if not root.is_dir():
            continue
        for pat in REPORT_LIST_PATTERNS:
            for p in root.glob(pat):
                if not p.is_file():
                    continue
                key = p.name.lower()
                if key in seen_names:
                    continue
                seen_names.add(key)
                stem_key = p.with_suffix("").name.lower()
                prev = by_stem.get(stem_key)
                if prev is None:
                    by_stem[stem_key] = p
                    continue
                # Prefer .txt as browse index when both txt and html exist for same stem.
                if p.suffix.lower() == ".txt" and prev.suffix.lower() != ".txt":
                    by_stem[stem_key] = p
    files = list(by_stem.values())
    files.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    if limit is None:
        return files
    return files[:limit]


def resolve_report_path(
    name: str,
    machine_host: str,
    project_root: Path,
) -> Optional[Path]:
    """Locate a report file on the client workstation or legacy server folder."""
    if not is_allowed_report_name(name):
        return None
    dirs: list[Path] = []
    dirs.extend(client_report_dir_candidates(machine_host))
    dirs.append(Path(project_root))
    seen: set[str] = set()
    for d in dirs:
        key = str(d).lower()
        if key in seen:
            continue
        seen.add(key)
        candidate = (d / name).resolve()
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None
