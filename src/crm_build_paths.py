"""
Configurable CRM version / build base paths for the web UI (copy-paste helpers).

Paths are **UNC** (``\\\\server\\share\\...``) with a trailing ``\\`` for folders so
copy-paste into Explorer and CLI works. Pasted ``//server/share/...`` URLs are
converted to UNC. Legacy ``file://`` values in ``CRM_BUILD_BASE_PATHS_JSON`` are
normalized when served via :func:`get_crm_build_base_paths`.
"""

from __future__ import annotations

import json
import os
import re
from urllib.parse import unquote, urlparse

# Official CRM version → build location under \\ntnet\indiafilestore1\cm_share\... (trailing \ for copy/cmd)
DEFAULT_CRM_BUILD_BASE_PATHS: list[dict[str, str]] = [
    {"label": "9.1.1", "path": "\\\\ntnet\\indiafilestore1\\cm_share\\CRM9_SP1\\9.1\\"},
    {"label": "9.2.0", "path": "\\\\ntnet\\indiafilestore1\\cm_share\\CRM9_SP2\\9.2\\"},
    {"label": "9.3.0", "path": "\\\\ntnet\\indiafilestore1\\cm_share\\CRM9_SP3\\9.3\\"},
    {"label": "9.3.2", "path": "\\\\ntnet\\indiafilestore1\\cm_share\\CRM9_SP3\\9.3.2\\staging\\"},
    {"label": "9.22.0", "path": "\\\\ntnet\\indiafilestore1\\cm_share\\CRM9_SP22\\9.22\\"},
    {"label": "9.22.1", "path": "\\\\ntnet\\indiafilestore1\\cm_share\\CRM9_SP22\\9.22.1\\"},
    {"label": "10.0.1", "path": "\\\\ntnet\\indiafilestore1\\cm_share\\10.0.1\\"},
    {"label": "10.1.0", "path": "\\\\ntnet\\indiafilestore1\\cm_share\\10.1\\staging\\"},
    {"label": "10.2.0", "path": "\\\\ntnet\\indiafilestore1\\cm_share\\v10.2\\"},
    {"label": "10.24.0", "path": "\\\\ntnet\\indiafilestore1\\cm_share\\v10.24\\"},
]


def _ensure_trailing_backslash_folder(path: str) -> str:
    """Add trailing backslash for UNC / drive folder paths (not obvious files)."""
    if not path:
        return path
    if path.endswith("\\"):
        return path
    low = path.lower()
    for ext in (
        ".jar",
        ".zip",
        ".ear",
        ".war",
        ".txt",
        ".html",
        ".htm",
        ".pdf",
        ".xml",
        ".json",
        ".class",
        ".dll",
        ".exe",
        ".cmd",
        ".bat",
        ".sh",
    ):
        if low.endswith(ext):
            return path
    return path + "\\"


def normalize_build_path_for_windows(path: str) -> str:
    """
    Normalize paths for Windows Explorer, ``robocopy``, and the comparer.

    - ``//server/share/...`` → ``\\\\server\\share\\...`` (UNC)
    - ``file://server/share/...`` → UNC
    - ``file:///C:/...`` → ``C:\\...``
    - Folder UNC / drive paths get a trailing ``\\`` when not a known file extension.
    """
    p = (path or "").strip()
    if not p:
        return p

    # //ntnet/indiafilestore1/cm_share/...  (common paste from docs / URLs)
    if p.startswith("//") and not p.lower().startswith("file:"):
        body = p[2:].replace("\\", "/").strip("/")
        parts = [x for x in body.split("/") if x]
        if not parts:
            return p
        if len(parts) == 1:
            unc = "\\\\" + parts[0] + "\\"
        else:
            unc = "\\\\" + parts[0] + "\\" + "\\".join(parts[1:])
        return _ensure_trailing_backslash_folder(unc)

    if p.startswith("\\\\"):
        return _ensure_trailing_backslash_folder(p)
    low = p.lower()
    if not low.startswith("file:"):
        return _ensure_trailing_backslash_folder(p) if (len(p) >= 2 and p[1] == ":") else p

    u = urlparse(p)
    if u.scheme != "file":
        return p

    # file://host/share/...  (network / UNC)
    if u.netloc:
        parts = [u.netloc]
        if u.path:
            parts.extend(x for x in u.path.split("/") if x)
        if not parts:
            return p
        if len(parts) == 1:
            unc = "\\\\" + parts[0] + "\\"
        else:
            unc = "\\\\" + parts[0] + "\\" + "\\".join(parts[1:])
        return _ensure_trailing_backslash_folder(unc)

    # file:///C:/local/path
    raw = unquote(u.path or "")
    m = re.match(r"^/([A-Za-z]:)(/.*)?$", raw)
    if m:
        rest = (m.group(2) or "").replace("/", "\\")
        local = m.group(1) + rest
        return _ensure_trailing_backslash_folder(local)

    if raw.startswith("/") and len(raw) >= 3 and raw[2] == ":":
        return _ensure_trailing_backslash_folder(raw[1:].replace("/", "\\"))

    return p


def get_crm_build_base_paths() -> list[dict[str, str]]:
    raw = os.environ.get("CRM_BUILD_BASE_PATHS_JSON", "").strip()
    bases: list[dict[str, str]]
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                out: list[dict[str, str]] = []
                for x in data:
                    if isinstance(x, dict) and x.get("path"):
                        label = str(x.get("label") or "Path")
                        path = str(x["path"]).strip()
                        if path:
                            out.append({"label": label, "path": path})
                if out:
                    bases = out
                else:
                    bases = list(DEFAULT_CRM_BUILD_BASE_PATHS)
            else:
                bases = list(DEFAULT_CRM_BUILD_BASE_PATHS)
        except (json.JSONDecodeError, TypeError, ValueError):
            bases = list(DEFAULT_CRM_BUILD_BASE_PATHS)
    else:
        bases = list(DEFAULT_CRM_BUILD_BASE_PATHS)

    return [
        {"label": item["label"], "path": normalize_build_path_for_windows(item["path"])}
        for item in bases
    ]
