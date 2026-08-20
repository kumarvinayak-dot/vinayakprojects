"""
Report file naming: {prefix}_{user}_{YYYYMMDD}_{ordinal}_report.txt|.html

- user: client device / username (e.g. svinayak01)
- date: run date (YYYYMMDD), not timezone label
- ordinal: first_report, second_report, third_report, ...
"""
from __future__ import annotations

import getpass
import os
import re
import socket
from datetime import datetime
from pathlib import Path
from typing import Mapping, Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore

_ID_RE = re.compile(r"[^a-zA-Z0-9_-]+")
_PATH_RE = re.compile(r"[^a-zA-Z0-9_.-]+")

_ORDINAL_WORDS = (
    "first",
    "second",
    "third",
    "fourth",
    "fifth",
    "sixth",
    "seventh",
    "eighth",
    "ninth",
    "tenth",
    "eleventh",
    "twelfth",
    "thirteenth",
    "fourteenth",
    "fifteenth",
    "sixteenth",
    "seventeenth",
    "eighteenth",
    "nineteenth",
    "twentieth",
)

_ORDINAL_FILE_RE = re.compile(
    r"^(.+)_(\d{8})_((?:first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|"
    r"eleventh|twelfth|thirteenth|fourteenth|fifteenth|sixteenth|seventeenth|eighteenth|"
    r"nineteenth|twentieth|report_\d+)_report)\.(txt|html)$",
    re.IGNORECASE,
)


def _sanitize_token(value: str, *, max_len: int = 48, fallback: str = "unknown") -> str:
    s = str(value or "").strip()
    if not s:
        return fallback
    s = _ID_RE.sub("_", s).strip("_").lower()
    return (s[:max_len] if s else fallback) or fallback


def _path_leaf(path_value: str) -> str:
    s = str(path_value or "").replace("\\", "/").strip().rstrip("/")
    if not s:
        return ""
    return s.rsplit("/", 1)[-1]


def _shorten_leaf(leaf: str) -> str:
    leaf = str(leaf or "").strip()
    for ext in (".jar", ".zip", ".war", ".ear", ".tar", ".gz"):
        if leaf.lower().endswith(ext):
            leaf = leaf[: -len(ext)]
            break
    return _sanitize_token(leaf, max_len=36, fallback="build")


def comparison_path_slug(
    folder1: str,
    folder2: str,
    *,
    path_hint: Optional[str] = None,
    max_len: int = 72,
) -> str:
    """Compact old-vs-new label from comparison paths (or optional hint)."""
    if path_hint:
        slug = _sanitize_token(path_hint, max_len=max_len, fallback="comparison")
        return slug
    l1 = _shorten_leaf(_path_leaf(folder1))
    l2 = _shorten_leaf(_path_leaf(folder2))
    if l1 and l2 and l1 != l2:
        slug = f"{l1}_vs_{l2}"
    elif l1:
        slug = l1
    elif l2:
        slug = l2
    else:
        slug = "comparison"
    return _sanitize_token(slug, max_len=max_len, fallback="comparison")


def report_timezone_name() -> str:
    return (os.environ.get("REPORT_TIMEZONE") or "Asia/Kolkata").strip() or "Asia/Kolkata"


def report_timestamp_with_tz() -> tuple[str, str]:
    """Return (YYYYMMDD_HHMMSS, tz_label) for logs — not used in filenames."""
    tz_name = report_timezone_name()
    tz_label = tz_name.rsplit("/", 1)[-1].upper()
    now: datetime
    if ZoneInfo is not None:
        try:
            now = datetime.now(ZoneInfo(tz_name))
            tz_label = (now.strftime("%Z") or tz_label).replace(" ", "")
        except Exception:
            now = datetime.now().astimezone()
            tz_label = (now.strftime("%Z") or "LOCAL").replace(" ", "")
    else:
        now = datetime.now()
    return now.strftime("%Y%m%d_%H%M%S"), _sanitize_token(tz_label, max_len=12, fallback="LOCAL")


def report_run_date() -> str:
    """YYYYMMDD for report filenames."""
    tz_name = report_timezone_name()
    if ZoneInfo is not None:
        try:
            return datetime.now(ZoneInfo(tz_name)).strftime("%Y%m%d")
        except Exception:
            pass
    return datetime.now().strftime("%Y%m%d")


def ordinal_report_slug(sequence: int) -> str:
    """first_report, second_report, ... report_21 for higher counts."""
    n = max(1, int(sequence or 1))
    if 1 <= n <= len(_ORDINAL_WORDS):
        return f"{_ORDINAL_WORDS[n - 1]}_report"
    return f"report_{n}"


def _ordinal_to_sequence(ordinal_slug: str) -> int:
    slug = str(ordinal_slug or "").lower()
    if slug.startswith("report_"):
        try:
            return max(1, int(slug.split("_", 1)[1]))
        except (IndexError, ValueError):
            return 1
    for i, word in enumerate(_ORDINAL_WORDS, start=1):
        if slug == f"{word}_report":
            return i
    return 1


def next_report_sequence(report_dir: Path, prefix: str, user: str, date_str: str) -> int:
    """Next report number for this user on this date (first=1, second=2, ...)."""
    base = Path(report_dir)
    if not base.exists():
        return 1
    user_tok = _sanitize_token(user, max_len=32)
    prefix_tok = str(prefix or "report").strip().rstrip("_")
    date_tok = str(date_str or report_run_date())
    expected_stem = f"{prefix_tok}_{user_tok}".lower()
    max_seq = 0
    names: list[str] = []
    try:
        for path in base.iterdir():
            if path.is_file():
                names.append(path.name)
    except OSError:
        for pat in ("*_report.txt", "*_report.html", f"{prefix_tok}_*.txt", f"{prefix_tok}_*.html"):
            try:
                names.extend(p.name for p in base.glob(pat) if p.is_file())
            except OSError:
                continue
    for name in names:
        m = _ORDINAL_FILE_RE.match(name)
        if not m:
            continue
        if m.group(1).lower() != expected_stem:
            continue
        if m.group(2) != date_tok:
            continue
        max_seq = max(max_seq, _ordinal_to_sequence(m.group(3)))
    return max_seq + 1


def allocate_report_filename(
    prefix: str,
    client_identity: str,
    report_dir: Path,
    *,
    suffix: str = ".txt",
) -> str:
    """
    {prefix}_{user}_{YYYYMMDD}_{ordinal}_report.txt
    e.g. crm_full_comparison_svinayak01_20260604_first_report.html
    """
    user = _sanitize_token(client_identity, max_len=32)
    date_str = report_run_date()
    seq = next_report_sequence(report_dir, prefix, user, date_str)
    ordinal = ordinal_report_slug(seq)
    base = str(prefix or "report").strip().rstrip("_")
    ext = suffix if suffix.startswith(".") else f".{suffix}"
    return f"{base}_{user}_{date_str}_{ordinal}{ext}"


def resolve_client_identity(
    client_host: str,
    headers: Optional[Mapping[str, str]] = None,
    qs_user: str = "",
) -> str:
    """
    Best-effort device/user slug for report names.
    Priority: query ?report_user= → X-Report-User header → reverse DNS / hostname → OS user.
    """
    override = str(qs_user or "").strip()
    if override:
        return _sanitize_token(override, max_len=32)

    hdrs = headers or {}
    for key in ("X-Report-User", "X-Client-Hostname", "X-Forwarded-User"):
        hv = str(hdrs.get(key) or hdrs.get(key.lower()) or "").strip()
        if hv:
            return _sanitize_token(hv.split(".")[0], max_len=32)

    host = str(client_host or "").strip().lower()
    if host in ("127.0.0.1", "::1", "localhost", ""):
        try:
            hn = socket.gethostname().split(".")[0]
            if hn:
                return _sanitize_token(hn, max_len=32)
        except OSError:
            pass
        try:
            return _sanitize_token(getpass.getuser(), max_len=32)
        except Exception:
            pass
        return "localhost"

    try:
        name, _, _ = socket.gethostbyaddr(host)
        short = str(name).split(".")[0]
        if short:
            return _sanitize_token(short, max_len=32)
    except (socket.herror, socket.gaierror, OSError):
        pass

    return _sanitize_token(host.replace(":", "_"), max_len=32, fallback="client")


def build_report_filename(
    prefix: str,
    folder1: str,
    folder2: str,
    client_identity: str,
    *,
    path_hint: Optional[str] = None,
    suffix: str = ".txt",
    report_dir: Optional[Path] = None,
) -> str:
    """
    Build report name with date + ordinal (first_report, second_report, ...).
    path_hint is ignored for filenames (kept for API compatibility).
    """
    target = report_dir if report_dir is not None else Path(".")
    return allocate_report_filename(prefix, client_identity, target, suffix=suffix)


def build_report_filename_from_meta(
    prefix: str,
    folder1: str,
    folder2: str,
    meta: Optional[dict],
    *,
    path_hint: Optional[str] = None,
    suffix: str = ".txt",
    report_dir: Optional[Path] = None,
) -> str:
    meta = meta or {}
    user = meta.get("client_identity") or meta.get("report_user") or "unknown"
    return build_report_filename(
        prefix,
        folder1,
        folder2,
        str(user),
        path_hint=path_hint,
        suffix=suffix,
        report_dir=report_dir,
    )
