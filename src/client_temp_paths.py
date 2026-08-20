"""
Client-machine temp placement for comparison copies.

Prefers the workstation that opened the web UI (e.g. svinayak01) via UNC admin
share C$ before using the comparison server disk or \\\\inpnqbdnc04\\software fallback.
"""
from __future__ import annotations

import getpass
import logging
import os
import re
import shutil
import socket
import tempfile
import threading
from pathlib import Path
from typing import Mapping, Optional

logger = logging.getLogger(__name__)

_compare_client_ctx = threading.local()

# Normalized application folder names under dedicated compare temp roots.
_APPLICATION_SLUGS: dict[str, str] = {
    "ASCF": "ascf",
    "CRM": "crm",
    "CRM/AMSS": "crm",
    "UXF/OMNI": "uxf",
    "UXF": "uxf",
    "OMNI": "uxf",
    "DEX": "dex",
}

# Legacy flat temp name prefixes per application (general %TEMP% / C:\Temp scan).
_APPLICATION_LEGACY_PREFIXES: dict[str, tuple[str, ...]] = {
    "crm": (
        "crm_",
        "crm_playbooks_",
    ),
    "ascf": (
        "ascf_",
        "compare_",
        "folder_compare_",
        "net_",
        "rpm_extract_",
        "cpio_extract_",
        "class_decompile_",
    ),
    "uxf": (
        "uxf_",
        "class_decompile_",
    ),
    "dex": (
        "dex_",
        "dex_nexus_",
    ),
}

DEFAULT_MIN_COMPARE_SPACE_GB = 50
DEFAULT_REMOTE_TEMP_ROOT = r"\\inpnqbdnc04\software\crm_compare_temp"
DEFAULT_CLIENT_TEMP_TEMPLATE = r"\\{client}\c$\crm_compare_temp"

# Subdirectory / file name prefixes created by comparison copy & extract steps.
_COMPARE_TEMP_ENTRY_PREFIXES: tuple[str, ...] = (
    "net_",
    "folder_compare_",
    "compare_",
    "class_decompile_",
    "rpm_extract_",
    "cpio_extract_",
    "crm_class_diff_",
    "crm_clfy_outer_",
    "crm_clfy_agent_",
    "crm_nested_cmp_old_",
    "crm_nested_cmp_new_",
    "crm_clfy_jar_extract_",
    "crm_class_diff_jarxf_",
)
_COMPARE_TEMP_Z_DIR_RE = re.compile(r"^z[a-zA-Z0-9]{5,}$")


def normalize_application_slug(application: str = "") -> str:
    """Map UI application label to a stable temp subfolder slug (crm, ascf, uxf, dex)."""
    raw = str(application or "").strip()
    if not raw:
        return ""
    if raw in _APPLICATION_SLUGS:
        return _APPLICATION_SLUGS[raw]
    key = raw.upper().replace("\\", "/")
    if key in _APPLICATION_SLUGS:
        return _APPLICATION_SLUGS[key]
    if "CRM" in key or "AMSS" in key:
        return "crm"
    if "ASCF" in key:
        return "ascf"
    if "UXF" in key or "OMNI" in key:
        return "uxf"
    if "DEX" in key:
        return "dex"
    slug = re.sub(r"[^a-z0-9]+", "_", raw.lower()).strip("_")
    return slug[:32] if slug else ""


def set_compare_client_context(
    machine_host: str, identity: str = "", application: str = ""
) -> None:
    _compare_client_ctx.machine_host = str(machine_host or "").strip()
    _compare_client_ctx.identity = str(identity or "").strip()
    _compare_client_ctx.application = str(application or "").strip()


def clear_compare_client_context() -> None:
    _compare_client_ctx.machine_host = ""
    _compare_client_ctx.identity = ""
    _compare_client_ctx.application = ""


def get_compare_client_context() -> tuple[str, str, str]:
    return (
        str(getattr(_compare_client_ctx, "machine_host", "") or "").strip(),
        str(getattr(_compare_client_ctx, "identity", "") or "").strip(),
        str(getattr(_compare_client_ctx, "application", "") or "").strip(),
    )


def get_compare_application() -> str:
    return get_compare_client_context()[2]


def _short_hostname(name: str) -> str:
    return str(name or "").strip().split(".")[0]


def resolve_client_machine_hostname(
    client_ip: str,
    headers: Optional[Mapping[str, str]] = None,
    qs_user: str = "",
) -> str:
    """
    Windows NetBIOS / DNS short hostname for UNC paths (e.g. svinayak01).
    """
    override = str(qs_user or "").strip()
    if override:
        return _short_hostname(override)

    hdrs = headers or {}
    for key in ("X-Client-Hostname", "X-Report-User", "X-Forwarded-User"):
        hv = str(hdrs.get(key) or hdrs.get(key.lower()) or "").strip()
        if hv:
            return _short_hostname(hv)

    ip = str(client_ip or "").strip().lower()
    if ip in ("127.0.0.1", "::1", "localhost", ""):
        try:
            return _short_hostname(socket.gethostname())
        except OSError:
            pass
        try:
            return _short_hostname(getpass.getuser())
        except Exception:
            return "localhost"

    try:
        name, _, _ = socket.gethostbyaddr(ip)
        return _short_hostname(name)
    except (socket.herror, socket.gaierror, OSError):
        return _short_hostname(ip.replace(":", "_"))


def client_temp_dir_unc_candidates(machine_host: str) -> list[Path]:
    """UNC folders on the client workstation (admin C$ share) for comparison temp."""
    host = _short_hostname(machine_host)
    if not host or host in ("localhost", "client", "127.0.0.1"):
        return []

    tpl = (os.environ.get("CRM_CLIENT_TEMP_ROOT") or DEFAULT_CLIENT_TEMP_TEMPLATE).strip()
    paths: list[Path] = []
    if "{client}" in tpl:
        paths.append(Path(tpl.replace("{client}", host)))
    elif tpl:
        paths.append(Path(tpl))

    paths.extend(
        [
            Path(rf"\\{host}\c$\crm_compare_temp"),
            Path(rf"\\{host}\c$\Temp\crm_compare_temp"),
            Path(rf"\\{host}\c$\Users\Public\crm_compare_temp"),
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


def remote_fallback_temp_root() -> str:
    return (os.environ.get("CRM_REMOTE_TEMP_ROOT") or DEFAULT_REMOTE_TEMP_ROOT).strip()


def min_compare_space_bytes() -> int:
    try:
        gb = float(os.environ.get("CRM_MIN_COMPARE_SPACE_GB", str(DEFAULT_MIN_COMPARE_SPACE_GB)))
    except (TypeError, ValueError):
        gb = float(DEFAULT_MIN_COMPARE_SPACE_GB)
    return max(1, int(gb * 1024 * 1024 * 1024))


def _is_network_path_value(path_value: str) -> bool:
    p = str(path_value or "").strip()
    return p.startswith("\\\\") or p.startswith("//")


def _path_size_bytes(path_value: str, *, quick: bool = False) -> int:
    from folder_compare import _path_exists, _path_is_file, _path_is_dir

    p = str(path_value or "").strip()
    if not p or not _path_exists(p):
        return 0
    # NAS/UNC walks can take many minutes on large CRM builds — never block the UI on them.
    if quick or _is_network_path_value(p):
        return 0
    try:
        if _path_is_file(p):
            return Path(p).stat().st_size
        if _path_is_dir(p):
            total = 0
            for root, _dirs, files in os.walk(p):
                for fn in files:
                    try:
                        total += (Path(root) / fn).stat().st_size
                    except OSError:
                        continue
                if total > 50 * 1024 * 1024 * 1024:
                    break
            return total
    except OSError:
        return 0
    return 0


def estimate_comparison_space_bytes(
    folder1: str = "",
    folder2: str = "",
    *,
    quick: bool = False,
) -> int:
    """Bytes needed on client C: (both builds + working margin, or configured minimum)."""
    floor = min_compare_space_bytes()
    s1 = _path_size_bytes(folder1, quick=quick)
    s2 = _path_size_bytes(folder2, quick=quick)
    if s1 or s2:
        need = int((s1 + s2) * 1.35) + (2 * 1024 * 1024 * 1024)
        return max(floor, need)
    return floor


def disk_usage_safe(path_value: str) -> Optional[dict]:
    import shutil

    p = str(path_value or "").strip()
    if not p:
        return None
    try:
        du = shutil.disk_usage(p)
        return {
            "path": p,
            "total": du.total,
            "used": du.used,
            "free": du.free,
            "accessible": True,
        }
    except OSError as e:
        logger.debug("disk_usage failed for %s: %s", p, e)
        return {"path": p, "accessible": False, "error": str(e)}


def client_c_drive_disk_info(machine_host: str) -> dict:
    host = _short_hostname(machine_host)
    candidates = [rf"\\{host}\c$"]
    for unc in candidates:
        info = disk_usage_safe(unc)
        if info and info.get("accessible"):
            info["label"] = f"{host} C:"
            return info
    return {
        "path": rf"\\{host}\c$",
        "label": f"{host} C:",
        "accessible": False,
        "free": 0,
        "total": 0,
    }


def format_bytes_human(n: int) -> str:
    n = int(n or 0)
    if n < 1024:
        return f"{n} B"
    for unit, div in (("KB", 1024), ("MB", 1024**2), ("GB", 1024**3), ("TB", 1024**4)):
        if n < div * 1024 or unit == "TB":
            return f"{n / div:.1f} {unit}"
    return f"{n} B"


def comparison_space_status(
    machine_host: str,
    folder1: str = "",
    folder2: str = "",
    *,
    quick: bool = False,
) -> dict:
    required = estimate_comparison_space_bytes(folder1, folder2, quick=quick)
    c_info = client_c_drive_disk_info(machine_host)
    free = int(c_info.get("free") or 0)
    accessible = bool(c_info.get("accessible"))
    sufficient = accessible and free >= required
    return {
        "client_machine": _short_hostname(machine_host),
        "client_c_drive": c_info,
        "minimum_required_bytes": required,
        "minimum_required_gb": round(required / (1024**3), 1),
        "minimum_required_human": format_bytes_human(required),
        "client_free_bytes": free,
        "client_free_human": format_bytes_human(free) if accessible else "n/a",
        "client_c_accessible": accessible,
        "client_has_enough_space": sufficient,
        "temp_strategy": (
            "client_workstation"
            if sufficient
            else ("remote_fallback" if not accessible else "remote_fallback_low_space")
        ),
        "remote_fallback_root": remote_fallback_temp_root(),
    }


def is_compare_temp_entry_name(name: str) -> bool:
    """True if a temp folder/file name was likely created by this comparison tool."""
    n = str(name or "").strip()
    if not n:
        return False
    lower = n.lower()
    if any(lower.startswith(p) for p in _COMPARE_TEMP_ENTRY_PREFIXES):
        return True
    if lower.startswith("ascf_") or lower.startswith("uxf_") or lower.startswith("dex_"):
        return True
    # mkdtemp_compare(prefix='z') for zip extracts
    return bool(_COMPARE_TEMP_Z_DIR_RE.match(n))


def is_compare_temp_entry_for_application(name: str, app_slug: str) -> bool:
    """True if a temp entry belongs to the given application (crm, ascf, uxf, dex)."""
    n = str(name or "").strip()
    if not n:
        return False
    slug = normalize_application_slug(app_slug)
    if not slug:
        return is_compare_temp_entry_name(n)
    lower = n.lower()
    if lower in _APPLICATION_SLUGS.values():
        return lower == slug
    if lower.startswith(f"{slug}_"):
        return True
    legacy = _APPLICATION_LEGACY_PREFIXES.get(slug, ())
    if any(lower.startswith(p) for p in legacy):
        return True
    if slug == "ascf" and bool(_COMPARE_TEMP_Z_DIR_RE.match(n)):
        return True
    return False


def compare_temp_application_subdirs() -> frozenset[str]:
    return frozenset(_APPLICATION_SLUGS.values())


def compare_temp_dedicated_roots(machine_host: str = "") -> list[Path]:
    """Client UNC folders reserved for comparison copies (contents wiped before each run)."""
    host = _short_hostname(machine_host or get_compare_client_context()[0])
    if not host or host in ("localhost", "client", "127.0.0.1"):
        return []
    return client_temp_dir_unc_candidates(host)


def compare_temp_general_dirs(machine_host: str = "") -> list[Path]:
    """C:\\Temp / %TEMP% locations where comparison artifacts may also land."""
    host = _short_hostname(machine_host or get_compare_client_context()[0])
    dirs: list[Path] = []
    if host and host not in ("localhost", "client", "127.0.0.1"):
        dirs.append(Path(rf"\\{host}\c$\Temp"))
    crm_t = (os.environ.get("CRM_TEMP_DIR") or "").strip()
    if crm_t:
        dirs.append(Path(crm_t))
    for key in ("TMP", "TEMP", "TMPDIR"):
        v = (os.environ.get(key) or "").strip()
        if v:
            dirs.append(Path(v))
    try:
        dirs.append(Path(tempfile.gettempdir()))
    except Exception:
        pass
    if os.name == "nt":
        dirs.extend([Path(r"C:\Temp"), Path(r"C:\crm_compare_temp")])
    seen: set[str] = set()
    out: list[Path] = []
    for p in dirs:
        key = str(p).lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def _safe_listdir(path: Path) -> list[str]:
    try:
        return os.listdir(str(path))
    except OSError as e:
        logger.debug("listdir failed for %s: %s", path, e)
        return []


def _try_remove_path(path: Path) -> tuple[bool, int]:
    from folder_compare import _path_exists, _path_is_dir

    p = str(path)
    if not _path_exists(p):
        return False, 0
    size = _path_size_bytes(p)
    try:
        if _path_is_dir(p):
            shutil.rmtree(p, ignore_errors=False)
        else:
            os.unlink(p)
        return True, size
    except OSError as e:
        logger.warning("Could not remove compare temp %s: %s", path, e)
        return False, 0


def cleanup_compare_temp_before_run(
    machine_host: str = "", application: str = ""
) -> dict:
    """
    Free disk on the client workstation before a new comparison run.

    When *application* is set (ASCF, CRM, UXF/OMNI, DEX), only that application's
    temp folders are removed so other apps' copies are left intact.

    - Dedicated roots: wipes \\\\host\\c$\\crm_compare_temp\\<app>\\ and legacy flat
      entries for that app at the root of each dedicated folder.
    - General temp: removes tool-created folders/files for that app only.
    """
    host = _short_hostname(machine_host or get_compare_client_context()[0])
    app_label = str(application or get_compare_application() or "").strip()
    app_slug = normalize_application_slug(app_label)
    removed = 0
    freed = 0
    errors: list[str] = []
    cleaned_roots: list[str] = []
    app_subdirs = compare_temp_application_subdirs()

    for root in compare_temp_dedicated_roots(host):
        root_s = str(root)
        names = _safe_listdir(root)
        if not names and not (app_slug and _safe_listdir(root / app_slug)):
            continue
        if app_slug:
            app_root = root / app_slug
            if _safe_listdir(app_root):
                cleaned_roots.append(str(app_root))
                for name in _safe_listdir(app_root):
                    ok, nbytes = _try_remove_path(app_root / name)
                    if ok:
                        removed += 1
                        freed += nbytes
                    else:
                        errors.append(f"failed: {app_root / name}")
            for name in names:
                if name.lower() in app_subdirs:
                    continue
                if not is_compare_temp_entry_for_application(name, app_slug):
                    continue
                cleaned_roots.append(root_s)
                ok, nbytes = _try_remove_path(root / name)
                if ok:
                    removed += 1
                    freed += nbytes
                else:
                    errors.append(f"failed: {root / name}")
        else:
            cleaned_roots.append(root_s)
            for name in names:
                ok, nbytes = _try_remove_path(root / name)
                if ok:
                    removed += 1
                    freed += nbytes
                else:
                    errors.append(f"failed: {root / name}")

    for temp_dir in compare_temp_general_dirs(host):
        names = _safe_listdir(temp_dir)
        if not names:
            continue
        hit = False
        for name in names:
            if app_slug:
                if not is_compare_temp_entry_for_application(name, app_slug):
                    continue
            elif not is_compare_temp_entry_name(name):
                continue
            hit = True
            ok, nbytes = _try_remove_path(temp_dir / name)
            if ok:
                removed += 1
                freed += nbytes
            else:
                errors.append(f"failed: {temp_dir / name}")
        if hit:
            cleaned_roots.append(str(temp_dir))

    # Server/local system temp (comparison server disk) — same rules as general temp.
    try:
        local_sys = Path(tempfile.gettempdir())
        for name in _safe_listdir(local_sys):
            if app_slug:
                if not is_compare_temp_entry_for_application(name, app_slug):
                    continue
            elif not is_compare_temp_entry_name(name):
                continue
            ok, nbytes = _try_remove_path(local_sys / name)
            if ok:
                removed += 1
                freed += nbytes
                cleaned_roots.append(str(local_sys))
            else:
                errors.append(f"failed: {local_sys / name}")
    except Exception as e:
        errors.append(f"local temp scan: {e}")

    summary = {
        "client_machine": host,
        "application": app_label or app_slug or "",
        "application_slug": app_slug,
        "removed": removed,
        "freed_bytes": freed,
        "freed_human": format_bytes_human(freed),
        "cleaned_roots": cleaned_roots,
        "errors": errors[:20],
    }
    if removed:
        logger.info(
            "Compare temp cleanup on %s (%s): removed %s item(s), ~%s freed",
            host or "local",
            app_slug or "all",
            removed,
            summary["freed_human"],
        )
    return summary
