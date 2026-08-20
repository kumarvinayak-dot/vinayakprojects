import os
import hashlib
from pathlib import Path
from datetime import datetime
import logging
import time
import socket
import shutil
import tempfile
import re
from collections import defaultdict
from difflib import SequenceMatcher
import zipfile
from io import BytesIO
import html
import struct
import subprocess
import gzip

from report_html_sections import inject_report_section_controls, report_collapse_extra_css

try:
    # Optional dependency; if available, enables RPM content listing
    import rpmfile  # type: ignore
except Exception:  # pragma: no cover - optional
    rpmfile = None

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def _to_long_path(path):
    r"""
    Convert a path to Windows long path format to support paths > 260 characters.
    
    For local paths: \\?\C:\path\to\file
    For UNC paths: \\?\UNC\server\share\path
    
    This is needed because Windows has a 260 character path limit by default,
    but using the \\?\ prefix bypasses this limit.
    """
    if os.name != 'nt':
        return path
    
    path_str = str(path)
    
    # Already a long path
    if path_str.startswith('\\\\?\\'):
        return Path(path_str)
    
    # UNC path (\\server\share\...)
    if path_str.startswith('\\\\'):
        # Convert \\server\share to \\?\UNC\server\share
        long_path = '\\\\?\\UNC\\' + path_str[2:]
        return Path(long_path)
    
    # Local path - make absolute and add prefix
    abs_path = os.path.abspath(path_str)
    if not abs_path.startswith('\\\\?\\'):
        long_path = '\\\\?\\' + abs_path
        return Path(long_path)
    
    return Path(path_str)

def _path_exists(path):
    r"""
    Check if a path exists, with support for long Windows paths and UNC paths.
    Uses the \\?\ prefix to bypass the 260 character path limit on Windows.
    """
    if os.name != 'nt':
        return Path(path).exists()
    
    try:
        long_path = _to_long_path(path)
        return long_path.exists()
    except Exception as e:
        logger.debug(f"Error checking path existence for {path}: {e}")
        # Fallback to regular check
        try:
            return Path(path).exists()
        except Exception:
            return False

def _path_is_file(path):
    """
    Check if a path is a file, with support for long Windows paths.
    """
    if os.name != 'nt':
        return Path(path).is_file()
    
    try:
        long_path = _to_long_path(path)
        return long_path.is_file()
    except Exception as e:
        logger.debug(f"Error checking if path is file for {path}: {e}")
        try:
            return Path(path).is_file()
        except Exception:
            return False

def _path_is_dir(path):
    """
    Check if a path is a directory, with support for long Windows paths.
    """
    if os.name != 'nt':
        return Path(path).is_dir()
    
    try:
        long_path = _to_long_path(path)
        return long_path.is_dir()
    except Exception as e:
        logger.debug(f"Error checking if path is directory for {path}: {e}")
        try:
            return Path(path).is_dir()
        except Exception:
            return False


def _compare_temp_disk_probe(path: Path) -> Path:
    """Volume path for shutil.disk_usage when the temp folder may not exist yet."""
    p = Path(path)
    parts = list(p.parts)
    s = str(p)
    if s.startswith("\\\\") and len(parts) >= 3 and parts[-2].lower() == "c$":
        host = parts[-3] if len(parts) >= 4 else parts[1]
        return Path(f"\\\\{host}\\c$")
    if p.exists():
        return p
    parent = p.parent
    while parent != parent.parent:
        if parent.exists():
            return parent
        parent = parent.parent
    return p


def get_compare_temp_dir_candidates(min_free_bytes: int = 0) -> list:
    """
    Directories with at least min_free_bytes free space.

    Priority (not overridden by larger disks elsewhere): client workstation
    (\\\\host\\c$\\crm_compare_temp), CRM_TEMP_DIR / server local drives, then remote
    fallback (\\\\inpnqbdnc04\\software\\crm_compare_temp) only when client/server are full.
    """
    if min_free_bytes < 0:
        min_free_bytes = 0

    tiered: list[tuple[int, Path]] = []
    tier = 0
    try:
        from client_temp_paths import (
            client_temp_dir_unc_candidates,
            get_compare_client_context,
        )

        machine_host, _identity, _app = get_compare_client_context()
        if machine_host:
            for p in client_temp_dir_unc_candidates(machine_host):
                tiered.append((tier, p))
    except ImportError:
        pass

    tier = 1
    crm_t = (os.environ.get("CRM_TEMP_DIR") or "").strip()
    if crm_t:
        tiered.append((tier, Path(crm_t)))
    for key in ("TMP", "TEMP", "TMPDIR"):
        v = (os.environ.get(key) or "").strip()
        if v:
            tiered.append((tier, Path(v)))
    try:
        tiered.append((tier, Path(tempfile.gettempdir())))
    except Exception:
        pass
    if os.name == "nt":
        import string

        for letter in string.ascii_uppercase:
            d = Path(f"{letter}:/")
            if d.is_dir():
                tiered.append((tier, d))

    tier = 2
    try:
        from client_temp_paths import remote_fallback_temp_root

        remote = remote_fallback_temp_root()
    except ImportError:
        remote = (os.environ.get("CRM_REMOTE_TEMP_ROOT") or r"\\inpnqbdnc04\software\crm_compare_temp").strip()
    if remote:
        tiered.append((tier, Path(remote)))

    seen: set[str] = set()
    by_tier: dict[int, list[tuple[int, Path]]] = {}
    for t, r in tiered:
        key = str(r).lower()
        if key in seen:
            continue
        seen.add(key)
        try:
            probe = _compare_temp_disk_probe(r)
            du = shutil.disk_usage(probe)
            if du.free >= min_free_bytes:
                by_tier.setdefault(t, []).append((du.free, r))
        except OSError:
            continue

    out: list[Path] = []
    for t in sorted(by_tier.keys()):
        scored = sorted(by_tier[t], reverse=True, key=lambda x: x[0])
        out.extend(p for _, p in scored)
    return out


def mkdtemp_compare(
    prefix: str = "compare_",
    min_free_bytes: int = 0,
    application: str = "",
) -> str:
    """Like tempfile.mkdtemp but prefers volumes with enough free space (incl. UNC fallback)."""
    try:
        from client_temp_paths import get_compare_application, normalize_application_slug

        app_slug = normalize_application_slug(application or get_compare_application())
    except ImportError:
        app_slug = ""
    effective_prefix = prefix
    if app_slug and not prefix.lower().startswith(f"{app_slug}_"):
        effective_prefix = f"{app_slug}_{prefix}"
    for d in get_compare_temp_dir_candidates(min_free_bytes):
        try:
            dest = d / app_slug if app_slug else d
            dest.mkdir(parents=True, exist_ok=True)
            return tempfile.mkdtemp(prefix=effective_prefix, dir=str(dest))
        except OSError:
            continue
    return tempfile.mkdtemp(prefix=effective_prefix)


def _set_logger_level_temporarily(suppress: bool):
    class _Guard:
        def __init__(self, suppress_flag: bool):
            self.suppress_flag = suppress_flag
            self.prev_level = None
        def __enter__(self):
            if self.suppress_flag:
                self.prev_level = logger.level
                logger.setLevel(logging.ERROR)
        def __exit__(self, exc_type, exc, tb):
            if self.suppress_flag and self.prev_level is not None:
                logger.setLevel(self.prev_level)
    return _Guard(suppress)

# ============================================================================
# CLASS FILE DECOMPILATION AND COMPARISON UTILITIES
# ============================================================================

def _find_java_decompiler():
    """
    Find available Java decompiler tools.
    Returns tuple: (decompiler_type, decompiler_path)
    decompiler_type can be: 'cfr', 'javap', 'procyon', or None
    """
    # Check for CFR decompiler (preferred) - check environment variable first
    cfr_env = os.environ.get('CFR_JAR', '')
    if cfr_env and os.path.exists(cfr_env):
        logger.info(f"Found CFR decompiler from CFR_JAR env: {cfr_env}")
        return ('cfr', cfr_env)
    
    # Check common CFR locations
    cfr_paths = [
        os.path.join(os.path.dirname(__file__), 'cfr.jar'),
        os.path.join(os.path.dirname(__file__), '..', 'cfr.jar'),
        os.path.join(os.path.dirname(__file__), '..', 'tools', 'cfr.jar'),
        os.path.join(os.getcwd(), 'cfr.jar'),
        os.path.join(os.getcwd(), 'tools', 'cfr.jar'),
        'cfr.jar',
        # Common installation paths on Windows
        os.path.join(os.environ.get('USERPROFILE', ''), 'cfr.jar'),
        os.path.join(os.environ.get('USERPROFILE', ''), 'tools', 'cfr.jar'),
    ]
    
    for cfr_path in cfr_paths:
        if cfr_path and os.path.exists(cfr_path):
            logger.info(f"Found CFR decompiler at: {cfr_path}")
            return ('cfr', os.path.abspath(cfr_path))
    
    # Check for javap (comes with JDK)
    try:
        result = subprocess.run(['javap', '-version'], capture_output=True, timeout=5)
        if result.returncode == 0 or b'javap' in result.stderr.lower() or b'usage' in result.stderr.lower():
            logger.info("Found javap decompiler (JDK)")
            return ('javap', 'javap')
    except Exception:
        pass
    
    # Check for java (to run javap via java home)
    java_home = os.environ.get('JAVA_HOME', '')
    if java_home:
        javap_path = os.path.join(java_home, 'bin', 'javap.exe' if os.name == 'nt' else 'javap')
        if os.path.exists(javap_path):
            logger.info(f"Found javap at JAVA_HOME: {javap_path}")
            return ('javap', javap_path)
    
    logger.warning("No Java decompiler found. Install JDK or set CFR_JAR environment variable.")
    return (None, None)

def _decompile_class_file(class_bytes, class_name, decompiler_type='javap', decompiler_path=None):
    """
    Decompile a .class file bytes to Java source or disassembly.
    
    Args:
        class_bytes: bytes content of the .class file
        class_name: name of the class (for display)
        decompiler_type: 'cfr', 'javap', or None
        decompiler_path: path to decompiler tool
    
    Returns:
        tuple: (success: bool, decompiled_source: str, error_msg: str)
    """
    if not class_bytes:
        return (False, "", "Empty class file")
    
    # Create temp file for the class
    temp_dir = None
    try:
        temp_dir = mkdtemp_compare(prefix='class_decompile_')
        
        # Determine class file name from class_name
        simple_name = class_name.split('.')[-1] if '.' in class_name else class_name
        if not simple_name.endswith('.class'):
            simple_name += '.class'
        
        class_file_path = os.path.join(temp_dir, simple_name)
        
        with open(class_file_path, 'wb') as f:
            f.write(class_bytes)
        
        if decompiler_type == 'cfr' and decompiler_path:
            # Use CFR decompiler for full Java source
            try:
                # Run CFR with options for cleaner output
                result = subprocess.run(
                    ['java', '-jar', decompiler_path, 
                     '--showversion', 'false',
                     '--silent', 'true',
                     class_file_path],
                    capture_output=True,
                    timeout=60,
                    cwd=temp_dir
                )
                
                # Try to decode output with multiple encodings
                output = ""
                for encoding in ['utf-8', 'latin-1', 'cp1252']:
                    try:
                        output = result.stdout.decode(encoding)
                        break
                    except UnicodeDecodeError:
                        continue
                
                if result.returncode == 0 and output:
                    # Clean up CFR output - remove comment headers if too verbose
                    lines = output.split('\n')
                    # Keep the decompiled code
                    return (True, output, "")
                elif output:
                    # Sometimes CFR returns non-zero but still produces output
                    return (True, output, "")
                else:
                    error_msg = result.stderr.decode('utf-8', errors='ignore') if result.stderr else "No output"
                    return (False, "", f"CFR error: {error_msg[:200]}")
            except subprocess.TimeoutExpired:
                return (False, "", "CFR timeout - class too complex")
            except FileNotFoundError:
                return (False, "", "Java not found - ensure JRE/JDK is installed and in PATH")
            except Exception as e:
                return (False, "", f"CFR failed: {str(e)}")
        
        elif decompiler_type == 'javap':
            # Use javap for disassembly (shows method signatures, fields, etc.)
            try:
                javap_cmd = decompiler_path if decompiler_path else 'javap'
                result = subprocess.run(
                    [javap_cmd, '-p', '-c', '-l', class_file_path],  # -p: show all, -c: code, -l: line numbers
                    capture_output=True,
                    timeout=30,
                    text=True
                )
                if result.returncode == 0:
                    return (True, result.stdout, "")
                else:
                    # Try simpler javap without bytecode
                    result = subprocess.run(
                        [javap_cmd, '-p', class_file_path],
                        capture_output=True,
                        timeout=30,
                        text=True
                    )
                    if result.returncode == 0:
                        return (True, result.stdout, "")
                    return (False, "", f"javap error: {result.stderr}")
            except FileNotFoundError:
                return (False, "", "javap not found - JDK not installed or not in PATH")
            except Exception as e:
                return (False, "", f"javap failed: {str(e)}")
        
        else:
            # No decompiler available - parse class file manually for basic info
            return _parse_class_file_basic(class_bytes, class_name)
    
    except Exception as e:
        return (False, "", f"Decompilation error: {str(e)}")
    
    finally:
        # Cleanup temp directory
        if temp_dir and os.path.exists(temp_dir):
            try:
                shutil.rmtree(temp_dir)
            except Exception:
                pass

def _parse_class_file_basic(class_bytes, class_name):
    """
    Parse class file manually to extract basic information when no decompiler is available.
    Extracts: class version, constants, method names, field names
    """
    try:
        if len(class_bytes) < 10:
            return (False, "", "Invalid class file")
        
        # Check magic number
        magic = struct.unpack('>I', class_bytes[0:4])[0]
        if magic != 0xCAFEBABE:
            return (False, "", "Not a valid Java class file")
        
        # Get version
        minor_version = struct.unpack('>H', class_bytes[4:6])[0]
        major_version = struct.unpack('>H', class_bytes[6:8])[0]
        
        # Map major version to Java version
        java_versions = {
            45: 'Java 1.1', 46: 'Java 1.2', 47: 'Java 1.3', 48: 'Java 1.4',
            49: 'Java 5', 50: 'Java 6', 51: 'Java 7', 52: 'Java 8',
            53: 'Java 9', 54: 'Java 10', 55: 'Java 11', 56: 'Java 12',
            57: 'Java 13', 58: 'Java 14', 59: 'Java 15', 60: 'Java 16',
            61: 'Java 17', 62: 'Java 18', 63: 'Java 19', 64: 'Java 20',
            65: 'Java 21'
        }
        java_ver = java_versions.get(major_version, f'Java {major_version - 44}')
        
        info = []
        info.append(f"// Class: {class_name}")
        info.append(f"// Compiled with: {java_ver} (version {major_version}.{minor_version})")
        info.append(f"// Size: {len(class_bytes)} bytes")
        info.append("")
        info.append("// [No decompiler available - showing basic class info]")
        info.append(f"// To see full Java source, install CFR decompiler:")
        info.append(f"// Download from: https://www.benf.org/other/cfr/")
        info.append(f"// Place cfr.jar in the project directory")
        
        return (True, '\n'.join(info), "")
    
    except Exception as e:
        return (False, "", f"Parse error: {str(e)}")

def _compare_decompiled_sources(source1, source2, class_name, *, diff_style: str = "dark"):
    """
    Compare two decompiled Java sources and generate a diff.

    diff_style:
      - "dark" — VS Code–like dark theme (default; used by generic folder compare UIs).
      - "p4" — light background, Perforce-style unified diff (CRM HTML reports).

    Returns:
        dict with keys: 'has_diff', 'diff_html', 'added_lines', 'removed_lines', 'changed_lines'
    """
    import difflib

    lines1 = source1.splitlines() if source1 else []
    lines2 = source2.splitlines() if source2 else []

    if diff_style == "p4":
        left_label = f"{class_name}#previous (old build)"
        right_label = f"{class_name}#new (new build)"
    else:
        left_label = f"{class_name} (Left)"
        right_label = f"{class_name} (Right)"

    # Generate unified diff
    diff = list(
        difflib.unified_diff(
            lines1,
            lines2,
            fromfile=left_label,
            tofile=right_label,
            lineterm="",
        )
    )

    has_diff = len(diff) > 0
    added_lines = sum(1 for line in diff if line.startswith("+") and not line.startswith("+++"))
    removed_lines = sum(1 for line in diff if line.startswith("-") and not line.startswith("---"))

    # Generate HTML diff
    diff_html: list[str] = []
    if has_diff:
        if diff_style == "p4":
            diff_html.append(
                "<div style='font-family:Consolas,monospace;font-size:11px;border:1px solid #94a3b8;"
                "background:#f1f5f9;border-radius:6px;overflow-x:auto;max-height:440px;overflow-y:auto;'>"
            )
            diff_html.append(
                "<div style='padding:8px 10px;background:#e2e8f0;border-bottom:1px solid #94a3b8;"
                "font-size:11px;color:#0f172a;'><strong>==== Java source (decompiled) — Perforce-style unified diff ====</strong></div>"
            )
            diff_html.append("<div style='padding:8px 10px;background:#ffffff;'>")
            for line in diff[:200]:
                escaped_line = html.escape(line)
                if line.startswith("+++") or line.startswith("---"):
                    diff_html.append(
                        f"<div style='color:#1d4ed8;font-weight:bold;border-bottom:1px solid #cbd5e1;"
                        f"padding:2px 0;'>{escaped_line}</div>"
                    )
                elif line.startswith("@@"):
                    diff_html.append(
                        f"<div style='color:#6d28d9;background:#ede9fe;padding:3px 6px;margin:4px 0;"
                        f"font-weight:600;'>{escaped_line}</div>"
                    )
                elif line.startswith("+"):
                    diff_html.append(
                        f"<div style='color:#166534;background:#dcfce7;'>{escaped_line}</div>"
                    )
                elif line.startswith("-"):
                    diff_html.append(
                        f"<div style='color:#991b1b;background:#fee2e2;'>{escaped_line}</div>"
                    )
                else:
                    diff_html.append(f"<div style='color:#334155;'>{escaped_line}</div>")
            diff_html.append("</div>")
            if len(diff) > 200:
                diff_html.append(
                    f"<div style='color:#64748b;font-style:italic;padding:6px 10px;font-size:11px;'>"
                    f"... and {len(diff) - 200} more lines</div>"
                )
            diff_html.append("</div>")
        else:
            diff_html.append(
                "<div style='font-family:Consolas,monospace;font-size:11px;background:#1e1e1e;color:#d4d4d4;"
                "padding:10px;border-radius:6px;overflow-x:auto;max-height:400px;overflow-y:auto;'>"
            )
            for line in diff[:200]:  # Limit to 200 lines
                escaped_line = html.escape(line)
                if line.startswith("+++") or line.startswith("---"):
                    diff_html.append(f"<div style='color:#569cd6;font-weight:bold;'>{escaped_line}</div>")
                elif line.startswith("@@"):
                    diff_html.append(
                        f"<div style='color:#c586c0;background:#2d2d2d;padding:2px 4px;margin:4px 0;'>{escaped_line}</div>"
                    )
                elif line.startswith("+"):
                    diff_html.append(f"<div style='color:#4ec9b0;background:#1e3a1e;'>{escaped_line}</div>")
                elif line.startswith("-"):
                    diff_html.append(f"<div style='color:#f14c4c;background:#3a1e1e;'>{escaped_line}</div>")
                else:
                    diff_html.append(f"<div>{escaped_line}</div>")

            if len(diff) > 200:
                diff_html.append(
                    f"<div style='color:#808080;font-style:italic;margin-top:8px;'>... and {len(diff) - 200} more lines</div>"
                )
            diff_html.append("</div>")
    
    return {
        'has_diff': has_diff,
        'diff_html': ''.join(diff_html),
        'added_lines': added_lines,
        'removed_lines': removed_lines,
        'changed_lines': added_lines + removed_lines,
        'diff_lines': diff
    }

def _extract_class_from_archive(archive_path, member_path):
    """
    Extract a class file from a JAR/ZIP archive.
    
    Returns:
        bytes content of the class file, or None if not found
    """
    try:
        with zipfile.ZipFile(archive_path, 'r') as zf:
            # Try exact path first
            try:
                return zf.read(member_path)
            except KeyError:
                pass
            
            # Try with normalized path
            normalized = member_path.replace('\\', '/')
            try:
                return zf.read(normalized)
            except KeyError:
                pass
            
            # Search for matching filename
            target_name = os.path.basename(member_path)
            for name in zf.namelist():
                if name.endswith(target_name) or os.path.basename(name) == target_name:
                    return zf.read(name)
        
        return None
    except Exception as e:
        logger.debug(f"Error extracting {member_path} from {archive_path}: {e}")
        return None

# Cache for decompiler availability check
_decompiler_cache = {'checked': False, 'type': None, 'path': None}

def _get_decompiler(force_refresh=False):
    """Get cached decompiler info."""
    if not _decompiler_cache['checked'] or force_refresh:
        dec_type, dec_path = _find_java_decompiler()
        _decompiler_cache['type'] = dec_type
        _decompiler_cache['path'] = dec_path
        _decompiler_cache['checked'] = True
        if dec_type:
            logger.info(f"Using decompiler: {dec_type} at {dec_path}")
    return _decompiler_cache['type'], _decompiler_cache['path']

def _reset_decompiler_cache():
    """Reset the decompiler cache to force re-detection."""
    _decompiler_cache['checked'] = False
    _decompiler_cache['type'] = None
    _decompiler_cache['path'] = None

class FileComparisonResult:
    def __init__(self, file_path, exists_in_folder1, exists_in_folder2):
        self.file_path = file_path
        self.exists_in_folder1 = exists_in_folder1
        self.exists_in_folder2 = exists_in_folder2
        self.size_folder1 = 0
        self.size_folder2 = 0
        self.modified_time_folder1 = ""
        self.modified_time_folder2 = ""
        self.content_identical = False
        self.hash_folder1 = ""
        self.hash_folder2 = ""
        # JAR manifest comparison metadata
        self.jar_impl_version_folder1 = ""
        self.jar_impl_version_folder2 = ""
        self.jar_version_diff = False
        self.jar_manifest_folder1 = ""
        self.jar_manifest_folder2 = ""
        self.jar_manifest_diff = False
        # Archive contents comparison
        self.is_archive = False
        self.archive_members_folder1 = set()
        self.archive_members_folder2 = set()
        self.archive_content_differs = False
        self.archive_members_only_in_folder1 = set()
        self.archive_members_only_in_folder2 = set()
        self.archive_members_different_content = []
        self.archive_members_meta_folder1 = {}  # member -> (size, crc)
        self.archive_members_meta_folder2 = {}  # member -> (size, crc)

class FolderComparer:
    def __init__(self, folder1_path, folder2_path):
        self.folder1_path = Path(folder1_path)
        self.folder2_path = Path(folder2_path)
        self.results = []
        self.max_retries = 3
        self.retry_delay = 1.0  # seconds
        self.network_timeout = 30  # seconds
        self.temp_folder1 = None
        self.temp_folder2 = None
        self.should_cleanup_temp_folders = True
        self.ignore_timestamp_only_differences = False
        self.show_missing_only = True  # By default, only show missing files, not different files
        self.use_local_copy = False  # Disable local copy by default - work directly with network paths
        self.exclude_missing_from_folder2 = False  # Exclude files missing in folder 2 from report
        self.rename_folder2_only_files = False  # Rename files that exist only in folder 2
        self.folder2_rename_suffix = "_renamed"  # Suffix to add when renaming files in folder 2
        self.compare_archive_contents = True  # Deep compare archive contents (JAR/WAR/EAR/ZIP/RPM)
        self.compare_archive_contents_only_if_different = False  # Only do deep archive comparison if files differ (faster)
        self.suppress_warnings = False  # Suppress logger warnings during comparison
        self.show_progress = True  # Show progress during comparison
        # Progress tracking
        self.total_files = 0
        self.processed_files = 0
        self.current_file = ""
        self.progress_percentage = 0.0
        self.estimated_time_remaining = 0
        self.comparison_start_time = None
        self.comparison_error = None  # Store error if comparison fails
        self.comparison_timeout = 7200  # 2 hours default timeout
        self.file_timeout = 600  # 10 minutes per file timeout
    
    def _is_network_path(self, file_path):
        """Check if the path is a UNC network path."""
        path_str = str(file_path)
        return path_str.startswith('\\\\') or (len(path_str) > 1 and path_str[1] == ':' and not os.path.exists(path_str[:3]))
    
    def _check_network_connectivity(self, file_path):
        """Check if the network path is accessible."""
        if not self._is_network_path(file_path):
            return True
        
        try:
            # Extract server name from UNC path
            path_str = str(file_path)
            if path_str.startswith('\\\\'):
                parts = path_str.split('\\')
                if len(parts) >= 3:
                    server_name = parts[2]
                    # Try to connect to the server
                    socket.setdefaulttimeout(10)  # 10 second timeout for connectivity check
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    try:
                        result = sock.connect_ex((server_name, 445))  # SMB port
                        sock.close()
                        return result == 0
                    except:
                        sock.close()
                        return False
        except Exception as e:
            logger.warning(f"Network connectivity check failed for {file_path}: {e}")
            return False
        return True
    
    def _retry_operation(self, operation, file_path, operation_name):
        """Retry an operation with exponential backoff for network errors."""
        last_error = None
        
        for attempt in range(self.max_retries):
            try:
                return operation(file_path)
            except (OSError, IOError) as e:
                last_error = e
                error_code = getattr(e, 'winerror', getattr(e, 'errno', 0))
                
                # Check if it's a network-related error
                if error_code in [53, 59, 67, 1231, 1326]:  # Common network error codes
                    if attempt < self.max_retries - 1:
                        wait_time = self.retry_delay * (2 ** attempt)  # Exponential backoff
                        logger.info(f"Network error (attempt {attempt + 1}/{self.max_retries}) for {file_path}, retrying in {wait_time}s...")
                        time.sleep(wait_time)
                        continue
                    else:
                        logger.warning(f"{operation_name} failed after {self.max_retries} attempts for {file_path}: {e}")
                        break
                else:
                    # Non-network error, don't retry
                    logger.warning(f"{operation_name} failed for {file_path}: {e}")
                    break
            except Exception as e:
                last_error = e
                logger.warning(f"{operation_name} failed for {file_path}: {e}")
                break
        
        return None
    
    def _extract_zip_to_temp(self, zip_path):
        """Extract a zip file to a temporary directory."""
        if not _path_exists(zip_path):
            logger.error(f"Zip file does not exist: {zip_path}")
            return None
        
        if not _path_is_file(zip_path):
            logger.error(f"Path is not a file: {zip_path}")
            return None
        
        # Temp dir with space-aware placement (incl. UNC \\\\inpnqbdnc04\\software\\crm_compare_temp when local disk is full)
        temp_dir = mkdtemp_compare(prefix="z")
        
        temp_path = Path(temp_dir)
        
        # Enable long path support on Windows by using \\?\ prefix
        if os.name == 'nt':
            # Convert to absolute path with long path prefix
            temp_path_str = str(temp_path.resolve())
            if not temp_path_str.startswith('\\\\?\\'):
                temp_path_long = Path('\\\\?\\' + temp_path_str)
            else:
                temp_path_long = temp_path
        else:
            temp_path_long = temp_path
        
        logger.info(f"Extracting zip file to temporary directory: {zip_path.name} -> {temp_path}")
        
        try:
            with zipfile.ZipFile(zip_path, 'r') as zf:
                members = zf.namelist()
                total_members = len(members)
                extracted_count = 0
                failed_count = 0
                
                # Extract members one by one to handle errors gracefully
                for idx, member in enumerate(members):
                    try:
                        # Show progress for large archives
                        if idx % 1000 == 0 or idx == total_members - 1:
                            logger.info(f"Extracting: {idx+1}/{total_members} ({extracted_count} ok, {failed_count} failed)")
                        
                        # Extract to the long path version on Windows
                        if os.name == 'nt':
                            zf.extract(member, temp_path_long)
                        else:
                            zf.extract(member, temp_path)
                        extracted_count += 1
                    except Exception as member_err:
                        # Log but continue - some files might have path issues
                        failed_count += 1
                        if failed_count <= 10:  # Only log first 10 failures
                            logger.warning(f"Failed to extract '{member}': {member_err}")
                        elif failed_count == 11:
                            logger.warning(f"... (suppressing further extraction errors)")
                
                if failed_count > 0:
                    logger.warning(f"Extraction completed with {failed_count} failures out of {total_members} files")
                
                if extracted_count == 0:
                    logger.error(f"Failed to extract any files from {zip_path.name}")
                    # Clean up temp directory on total failure
                    try:
                        shutil.rmtree(temp_path)
                    except Exception:
                        pass
                    return None
                
                logger.info(f"Successfully extracted {extracted_count}/{total_members} items from {zip_path.name}")
                return temp_path  # Return the regular path (not the \\?\ version)
        except Exception as e:
            logger.error(f"Failed to open or extract zip file {zip_path}: {e}")
            # Clean up temp directory on failure
            try:
                shutil.rmtree(temp_path)
            except Exception:
                pass
            return None
    
    def _copy_network_folder_to_local(self, network_path):
        """Copy a network folder to a local temporary directory."""
        if not self._is_network_path(network_path):
            logger.info(f"Path is not a network path, using as-is: {network_path}")
            return network_path
        
        if not _path_exists(network_path):
            logger.error(f"Network folder does not exist: {network_path}")
            return None
        
        # Create a temporary directory (prefers volumes with space; UNC fallback when local full)
        temp_dir = mkdtemp_compare(prefix="folder_compare_")
        temp_path = Path(temp_dir)
        
        folder_name = network_path.name
        local_copy_path = temp_path / folder_name
        
        logger.info(f"Copying network folder to local: {network_path} -> {local_copy_path}")
        
        try:
            def copy_with_retry(src, dest):
                """Copy operation with retry logic for network errors."""
                for attempt in range(self.max_retries):
                    try:
                        if src.is_file():
                            dest.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(src, dest)
                        else:
                            shutil.copytree(src, dest, dirs_exist_ok=False)
                        return True
                    except (OSError, IOError) as e:
                        error_code = getattr(e, 'winerror', getattr(e, 'errno', 0))
                        if error_code in [53, 59, 67, 1231, 1326] and attempt < self.max_retries - 1:
                            wait_time = self.retry_delay * (2 ** attempt)
                            logger.warning(f"Copy error (attempt {attempt + 1}/{self.max_retries}): {e}")
                            logger.info(f"Retrying in {wait_time}s...")
                            time.sleep(wait_time)
                        else:
                            logger.error(f"Failed to copy after {self.max_retries} attempts: {e}")
                            return False
                    except Exception as e:
                        logger.error(f"Unexpected error during copy: {e}")
                        return False
                return False
            
            # Copy the entire folder structure
            if copy_with_retry(network_path, local_copy_path):
                logger.info(f"Successfully copied {network_path} to {local_copy_path}")
                return local_copy_path
            else:
                # Cleanup failed copy attempt
                if local_copy_path.exists():
                    shutil.rmtree(local_copy_path, ignore_errors=True)
                shutil.rmtree(temp_path, ignore_errors=True)
                return None
                
        except Exception as e:
            logger.error(f"Failed to copy network folder {network_path}: {e}")
            if temp_path.exists():
                shutil.rmtree(temp_path, ignore_errors=True)
            return None
    
    def cleanup_temp_folders(self):
        """Clean up temporary folders created during comparison."""
        if self.temp_folder1 and self.temp_folder1.exists():
            try:
                shutil.rmtree(self.temp_folder1.parent, ignore_errors=True)
                logger.info(f"Cleaned up temporary folder: {self.temp_folder1.parent}")
            except Exception as e:
                logger.warning(f"Failed to clean up temp folder 1: {e}")
        
        if self.temp_folder2 and self.temp_folder2.exists():
            try:
                shutil.rmtree(self.temp_folder2.parent, ignore_errors=True)
                logger.info(f"Cleaned up temporary folder: {self.temp_folder2.parent}")
            except Exception as e:
                logger.warning(f"Failed to clean up temp folder 2: {e}")
    
    def _rename_folder2_only_files(self, folder2_to_compare):
        """Rename files that exist only in folder 2."""
        if not self.rename_folder2_only_files:
            return
        
        logger.info("Renaming files that exist only in folder 2...")
        renamed_count = 0
        
        for result in self.results:
            if not result.exists_in_folder1 and result.exists_in_folder2:
                original_path = folder2_to_compare / result.file_path
                
                # Create new filename with suffix
                file_path_obj = Path(result.file_path)
                if file_path_obj.suffix:
                    # File has extension
                    new_name = file_path_obj.stem + self.folder2_rename_suffix + file_path_obj.suffix
                else:
                    # File has no extension
                    new_name = file_path_obj.name + self.folder2_rename_suffix
                
                new_path = original_path.parent / new_name
                
                try:
                    # Check if target file already exists
                    if new_path.exists():
                        logger.warning(f"Target file already exists, skipping rename: {new_path}")
                        continue
                    
                    # Perform the rename
                    original_path.rename(new_path)
                    logger.info(f"Renamed: {original_path} -> {new_path}")
                    renamed_count += 1
                    
                    # Update the result to reflect the new name
                    result.file_path = str(new_path.relative_to(folder2_to_compare)).replace('\\', '/')
                    
                except Exception as e:
                    logger.error(f"Failed to rename {original_path} to {new_path}: {e}")
        
        if renamed_count > 0:
            logger.info(f"Successfully renamed {renamed_count} files in folder 2")
        else:
            logger.info("No files were renamed in folder 2")
        
    def _calculate_file_hash(self, file_path):
        def _hash_operation(path):
            hash_md5 = hashlib.md5()
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(4096), b""):
                    hash_md5.update(chunk)
            return hash_md5.hexdigest()
        
        if self._is_network_path(file_path):
            result = self._retry_operation(_hash_operation, file_path, "Hash calculation")
            return result if result is not None else ""
        else:
            try:
                return _hash_operation(file_path)
            except Exception as e:
                logger.warning(f"Could not calculate hash for {file_path}: {e}")
                return ""
    
    def _get_file_info(self, file_path):
        def _stat_operation(path):
            stat = path.stat()
            size = stat.st_size
            mod_time = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            return size, mod_time
        
        if self._is_network_path(file_path):
            result = self._retry_operation(_stat_operation, file_path, "File info retrieval")
            return result if result is not None else (0, "")
        else:
            try:
                return _stat_operation(file_path)
            except Exception as e:
                logger.warning(f"Could not get file info for {file_path}: {e}")
                return 0, ""
    
    def _get_all_files(self, folder_path):
        files = set()
        try:
            for root, dirs, filenames in os.walk(folder_path):
                for filename in filenames:
                    full_path = Path(root) / filename
                    relative_path = full_path.relative_to(folder_path)
                    files.add(str(relative_path).replace('\\', '/'))
        except Exception as e:
            logger.error(f"Error scanning folder {folder_path}: {e}")
        return files
    
    def _read_jar_manifest_version(self, jar_path: Path) -> str:
        """Read Implementation-Version from META-INF/MANIFEST.MF for a JAR file (handles line folding)."""
        try:
            with zipfile.ZipFile(jar_path, 'r') as zf:
                try:
                    data = zf.read('META-INF/MANIFEST.MF')
                except KeyError:
                    return ""
            # Decode and handle RFC 822-style line folding (continuation lines start with a single space)
            text = data.decode('utf-8', errors='replace').replace('\r\n', '\n')
            lines = text.split('\n')
            unfolded = []
            for line in lines:
                if line.startswith(' ') and unfolded:
                    unfolded[-1] = unfolded[-1] + line[1:]
                else:
                    unfolded.append(line)
            for line in unfolded:
                if line.lower().startswith('implementation-version:'):
                    return line.split(':', 1)[1].strip()
            return ""
        except Exception:
            return ""
    
    def _compare_archive_contents_for_file(self, result: FileComparisonResult, archive_path1: Path, archive_path2: Path):
        """
        Compare the contents of two archive files (JAR/WAR/EAR/ZIP/RPM) with normalization.
        Applies version-specific filename normalization to treat versioned JARs as identical.
        """
        try:
            # Use ArchiveContentsComparer logic to list members
            file_ext = archive_path1.suffix.lower()
            
            # Create comparer for normalization support
            comparer = ArchiveContentsComparer(str(archive_path1), str(archive_path2))
            
            if file_ext == '.rpm':
                # Use ArchiveContentsComparer's RPM extraction logic
                members1, meta1, err1 = comparer._list_rpm_members(archive_path1)
                members2, meta2, err2 = comparer._list_rpm_members(archive_path2)
            else:
                # ZIP-based archives (JAR/WAR/EAR/ZIP)
                members1, meta1, err1 = comparer._list_zip_members(archive_path1)
                members2, meta2, err2 = comparer._list_zip_members(archive_path2)
            
            # Store raw members
            result.archive_members_folder1 = members1
            result.archive_members_folder2 = members2
            result.archive_members_meta_folder1 = meta1
            result.archive_members_meta_folder2 = meta2
            
            # Apply normalization to member sets for comparison
            normalized1 = comparer._normalize_member_set(members1)
            normalized2 = comparer._normalize_member_set(members2)
            
            # Find differences using normalized names
            only_in_1_normalized = normalized1 - normalized2
            only_in_2_normalized = normalized2 - normalized1
            
            # Store normalized differences for reporting
            result.archive_members_only_in_folder1 = only_in_1_normalized
            result.archive_members_only_in_folder2 = only_in_2_normalized
            
            # Find files with different content (same normalized name, different size/CRC)
            common_normalized = normalized1 & normalized2
            for norm_name in common_normalized:
                # Find original names that normalized to this name
                orig_names1 = [m for m in members1 if comparer._normalize_inner_archive_marker(m) == norm_name]
                orig_names2 = [m for m in members2 if comparer._normalize_inner_archive_marker(m) == norm_name]
                
                if orig_names1 and orig_names2:
                    m1 = meta1.get(orig_names1[0])
                    m2 = meta2.get(orig_names2[0])
                    if m1 and m2 and m1 != m2:
                        result.archive_members_different_content.append(norm_name)
            
            # Mark as different if there are any differences after normalization
            if only_in_1_normalized or only_in_2_normalized or result.archive_members_different_content:
                result.archive_content_differs = True
                # Override content_identical if archive contents differ
                if result.content_identical:
                    result.content_identical = False
            else:
                # Archives are identical after normalization
                logger.info(f"Archive {result.file_path}: No differences after normalization")
                    
        except Exception as e:
            logger.debug(f"Archive content comparison failed for {result.file_path}: {e}")
    
    def _read_jar_full_manifest(self, jar_path: Path) -> str:
        """Read the complete META-INF/MANIFEST.MF content from a JAR file (handles line folding)."""
        try:
            with zipfile.ZipFile(jar_path, 'r') as zf:
                try:
                    data = zf.read('META-INF/MANIFEST.MF')
                except KeyError:
                    return ""
            # Decode and handle RFC 822-style line folding (continuation lines start with a single space)
            text = data.decode('utf-8', errors='replace').replace('\r\n', '\n')
            lines = text.split('\n')
            unfolded = []
            for line in lines:
                if line.startswith(' ') and unfolded:
                    unfolded[-1] = unfolded[-1] + line[1:]
                else:
                    unfolded.append(line)
            # Return the unfolded manifest as a single string
            return '\n'.join(unfolded)
        except Exception:
            return ""
    
    def _update_progress(self, current_file=""):
        """Update progress tracking information."""
        if self.total_files > 0:
            self.progress_percentage = (self.processed_files / self.total_files) * 100
            
            # Estimate time remaining
            if self.comparison_start_time and self.processed_files > 0:
                elapsed_time = time.time() - self.comparison_start_time
                avg_time_per_file = elapsed_time / self.processed_files
                remaining_files = self.total_files - self.processed_files
                self.estimated_time_remaining = int(avg_time_per_file * remaining_files)
        
        self.current_file = current_file
    
    def compare_folders(self):
        import time
        self.comparison_start_time = time.time()
        self.comparison_error = None

        try:
            from client_temp_paths import (
                cleanup_compare_temp_before_run,
                get_compare_client_context,
            )

            host, _ident, app = get_compare_client_context()
            pre = cleanup_compare_temp_before_run(host, application=app)
            if pre.get("removed"):
                logger.info(
                    "Client temp cleanup before run: removed %s item(s), ~%s freed",
                    pre["removed"],
                    pre.get("freed_human", "0 B"),
                )
        except Exception as e:
            logger.warning("Client temp pre-run cleanup skipped: %s", e)
        
        with _set_logger_level_temporarily(self.suppress_warnings):
            logger.info(f"Comparing folders: {self.folder1_path} vs {self.folder2_path}")
        
            # Store original paths for reporting
            original_folder1_path = self.folder1_path
            original_folder2_path = self.folder2_path
        
            # Handle network paths by copying to local if enabled
            folder1_to_compare = self.folder1_path
            folder2_to_compare = self.folder2_path
        
            if self.use_local_copy and self._is_network_path(self.folder1_path):
                logger.info("Folder 1 is on network path, copying to local temporary directory...")
                local_folder1 = self._copy_network_folder_to_local(self.folder1_path)
                if local_folder1:
                    self.temp_folder1 = local_folder1
                    folder1_to_compare = local_folder1
                    logger.info(f"Using local copy for folder 1: {local_folder1}")
                else:
                    logger.error("Failed to copy folder 1 to local, aborting comparison")
                    return []
        
            if self.use_local_copy and self._is_network_path(self.folder2_path):
                logger.info("Folder 2 is on network path, copying to local temporary directory...")
                local_folder2 = self._copy_network_folder_to_local(self.folder2_path)
                if local_folder2:
                    self.temp_folder2 = local_folder2
                    folder2_to_compare = local_folder2
                    logger.info(f"Using local copy for folder 2: {local_folder2}")
                else:
                    logger.error("Failed to copy folder 2 to local, aborting comparison")
                    if self.should_cleanup_temp_folders:
                        self.cleanup_temp_folders()
                    return []
        
            # Check if paths are zip files and extract them if needed
            # Check if folder1 is a zip file
            if _path_exists(folder1_to_compare) and _path_is_file(folder1_to_compare) and folder1_to_compare.suffix.lower() in ['.zip', '.jar', '.war', '.ear']:
                logger.info(f"Folder 1 is a zip archive, extracting: {folder1_to_compare}")
                temp_extract_folder1 = self._extract_zip_to_temp(folder1_to_compare)
                if temp_extract_folder1:
                    self.temp_folder1 = temp_extract_folder1
                    folder1_to_compare = temp_extract_folder1
                    logger.info(f"Using extracted folder 1: {folder1_to_compare}")
                else:
                    logger.error("Failed to extract folder 1 zip file, aborting comparison")
                    if self.should_cleanup_temp_folders:
                        self.cleanup_temp_folders()
                    return []
            
            # Check if folder2 is a zip file
            if _path_exists(folder2_to_compare) and _path_is_file(folder2_to_compare) and folder2_to_compare.suffix.lower() in ['.zip', '.jar', '.war', '.ear']:
                logger.info(f"Folder 2 is a zip archive, extracting: {folder2_to_compare}")
                temp_extract_folder2 = self._extract_zip_to_temp(folder2_to_compare)
                if temp_extract_folder2:
                    self.temp_folder2 = temp_extract_folder2
                    folder2_to_compare = temp_extract_folder2
                    logger.info(f"Using extracted folder 2: {folder2_to_compare}")
                else:
                    logger.error("Failed to extract folder 2 zip file, aborting comparison")
                    if self.should_cleanup_temp_folders:
                        self.cleanup_temp_folders()
                    return []
        
            # Check if folders exist (either original or local copies)
            if not _path_exists(folder1_to_compare):
                error_msg = f"Folder 1 does not exist or is not accessible: {folder1_to_compare}"
                logger.error(error_msg)
                self.comparison_error = error_msg
                if self.should_cleanup_temp_folders:
                    self.cleanup_temp_folders()
                return []
        
            if not _path_exists(folder2_to_compare):
                error_msg = f"Folder 2 does not exist or is not accessible: {folder2_to_compare}"
                logger.error(error_msg)
                self.comparison_error = error_msg
                if self.should_cleanup_temp_folders:
                    self.cleanup_temp_folders()
                return []
        
            files1 = self._get_all_files(folder1_to_compare)
            files2 = self._get_all_files(folder2_to_compare)
            all_files = files1.union(files2)
        
            self.total_files = len(all_files)
            logger.info(f"Found {len(files1)} files in folder 1, {len(files2)} files in folder 2")
            logger.info(f"Total files to compare: {self.total_files}")
        
            self.results = []
            processed_count = 0
            archive_count = 0
            file_start_time = time.time()
        
            try:
                for file_path in sorted(all_files):
                    # Check for overall timeout
                    if time.time() - self.comparison_start_time > self.comparison_timeout:
                        error_msg = f"Comparison timeout after {self.comparison_timeout} seconds"
                        logger.error(error_msg)
                        self.comparison_error = error_msg
                        break
                    
                    # Check for per-file timeout
                    if time.time() - file_start_time > self.file_timeout:
                        error_msg = f"File processing timeout: {self.current_file} (>{self.file_timeout}s)"
                        logger.error(error_msg)
                        self.comparison_error = error_msg
                        # Continue to next file instead of breaking
                        file_start_time = time.time()
                    
                    processed_count += 1
                    self.processed_files = processed_count
                    self.current_file = str(file_path)
                    self._update_progress(str(file_path))
                    
                    # Show progress every 100 files or for archives
                    if self.show_progress and (processed_count % 100 == 0 or processed_count == self.total_files):
                        logger.info(f"Progress: {processed_count}/{self.total_files} files processed ({self.progress_percentage:.1f}%) - ETA: {self.estimated_time_remaining}s")
                    # Reset file timer for this file
                    file_start_time = time.time()
                    
                    result = FileComparisonResult(file_path, file_path in files1, file_path in files2)
                    
                    # Skip files missing in folder 2 if exclude_missing_from_folder2 is enabled
                    if self.exclude_missing_from_folder2 and result.exists_in_folder1 and not result.exists_in_folder2:
                        continue
                    
                    if result.exists_in_folder1:
                        full_path1 = folder1_to_compare / file_path
                        result.size_folder1, result.modified_time_folder1 = self._get_file_info(full_path1)
                        result.hash_folder1 = self._calculate_file_hash(full_path1)
                    
                    if result.exists_in_folder2:
                        full_path2 = folder2_to_compare / file_path
                        result.size_folder2, result.modified_time_folder2 = self._get_file_info(full_path2)
                        result.hash_folder2 = self._calculate_file_hash(full_path2)
                    
                    if result.exists_in_folder1 and result.exists_in_folder2:
                        if result.hash_folder1 != "" and result.hash_folder2 != "":
                            # Both hashes calculated successfully
                            result.content_identical = (result.hash_folder1 == result.hash_folder2)
                        elif result.hash_folder1 == "" and result.hash_folder2 == "" and self.ignore_timestamp_only_differences:
                            # Both hash calculations failed, but user wants to ignore timestamp differences
                            # Assume files are identical (since we can't verify content)
                            result.content_identical = True
                        else:
                            # At least one hash calculation failed and we're not ignoring timestamp differences
                            result.content_identical = False
                    
                    # If this is a JAR present in both, capture manifest Implementation-Version and full manifest
                    if result.exists_in_folder1 and result.exists_in_folder2 and result.file_path.lower().endswith('.jar'):
                        full_path1 = folder1_to_compare / file_path
                        full_path2 = folder2_to_compare / file_path
                        v1 = self._read_jar_manifest_version(full_path1)
                        v2 = self._read_jar_manifest_version(full_path2)
                        result.jar_impl_version_folder1 = v1
                        result.jar_impl_version_folder2 = v2
                        if v1 != v2:
                            result.jar_version_diff = True
                        
                        # Also read and compare full manifest content
                        m1 = self._read_jar_full_manifest(full_path1)
                        m2 = self._read_jar_full_manifest(full_path2)
                        result.jar_manifest_folder1 = m1
                        result.jar_manifest_folder2 = m2
                        if m1 and m2 and m1 != m2:
                            result.jar_manifest_diff = True
                            # If manifest differs but content_identical is True (same file hash), 
                            # the hash matched but manifest differs - mark as different
                            if result.content_identical:
                                result.content_identical = False
                    
                    # Deep archive contents comparison for all archive types
                    if self.compare_archive_contents and result.exists_in_folder1 and result.exists_in_folder2:
                        file_lower = result.file_path.lower()
                        if file_lower.endswith(('.jar', '.war', '.ear', '.zip', '.rpm')):
                            result.is_archive = True
                            
                            # Option to skip deep comparison if files are identical (for performance)
                            should_deep_compare = True
                            if self.compare_archive_contents_only_if_different:
                                # Only do deep comparison if file content differs
                                should_deep_compare = not result.content_identical
                            
                            if should_deep_compare:
                                archive_count += 1
                                full_path1 = folder1_to_compare / file_path
                                full_path2 = folder2_to_compare / file_path
                                if self.show_progress:
                                    logger.info(f"Deep archive comparison [{archive_count}]: {result.file_path}")
                                self._compare_archive_contents_for_file(result, full_path1, full_path2)
                            elif self.show_progress and result.is_archive:
                                logger.info(f"Skipping deep comparison (files identical): {result.file_path}")
                    
                    self.results.append(result)
                
                # Rename files that exist only in folder 2 if enabled
                self._rename_folder2_only_files(folder2_to_compare)
                
                if self.comparison_error:
                    logger.warning(f"Comparison completed with errors: {self.comparison_error}")
                else:
                    logger.info("Comparison completed successfully")
                    
            except KeyboardInterrupt:
                error_msg = "Comparison interrupted by user"
                logger.warning(error_msg)
                self.comparison_error = error_msg
                raise
            except Exception as e:
                error_msg = f"Comparison failed with exception: {str(e)}"
                logger.error(error_msg)
                self.comparison_error = error_msg
                # Don't raise - return partial results
            finally:
                # Clean up temporary folders if enabled
                if self.should_cleanup_temp_folders:
                    self.cleanup_temp_folders()
        
            return self.results
    
    def generate_report(self):
        if not self.results:
            error_msg = "No comparison results available."
            if hasattr(self, 'comparison_error') and self.comparison_error:
                error_msg += f"\n\nError: {self.comparison_error}"
                error_msg += f"\n\nPlease check that:\n  1. Both paths exist and are accessible\n  2. You have permission to read the folders\n  3. Network paths are correctly formatted (e.g., \\\\server\\share\\path)"
            return error_msg
        
        report = []
        report.append("=" * 80)
        report.append("FOLDER COMPARISON REPORT")
        report.append("=" * 80)
        report.append(f"Folder 1: {self.folder1_path}")
        report.append(f"Folder 2: {self.folder2_path}")
        report.append(f"Comparison Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        
        # Show comparison status and any errors
        if self.comparison_error:
            report.append("")
            report.append("*** COMPARISON STATUS: INCOMPLETE/ERROR ***")
            report.append(f"Error: {self.comparison_error}")
            report.append(f"Files processed before error: {self.processed_files}/{self.total_files}")
            report.append("Note: This is a PARTIAL report with results up to the point of error")
        else:
            report.append("")
            report.append("*** COMPARISON STATUS: COMPLETE ***")
        
        report.append("")
        
        # Extract folder names for clearer section headings
        folder1_name = self.folder1_path.name if self.folder1_path.name else "Folder 1"
        folder2_name = self.folder2_path.name if self.folder2_path.name else "Folder 2"
        
        total_files = len(self.results)
        only_in_folder1 = sum(1 for r in self.results if r.exists_in_folder1 and not r.exists_in_folder2)
        only_in_folder2 = sum(1 for r in self.results if not r.exists_in_folder1 and r.exists_in_folder2)
        in_both = sum(1 for r in self.results if r.exists_in_folder1 and r.exists_in_folder2)
        identical = sum(1 for r in self.results if r.content_identical)
        # Count all files with content differences (not just JAR manifest differences)
        different_files = [r for r in self.results if r.exists_in_folder1 and r.exists_in_folder2 and not r.content_identical]
        different = len(different_files)
        
        # Separate archive files from regular files
        archive_files_with_diffs = [r for r in different_files if r.is_archive]
        regular_files_with_diffs = [r for r in different_files if not r.is_archive]
        
        report.append("SUMMARY")
        report.append("-" * 40)
        report.append(f"Total files analyzed: {total_files}")
        if self.compare_archive_contents:
            report.append("Archive normalization: ENABLED (version patterns like __V10_0_1_22 are normalized)")
        if self.exclude_missing_from_folder2:
            report.append(f"Note: Files missing in {folder2_name} have been excluded from this report")
        else:
            report.append(f"Files missing in {folder2_name}: {only_in_folder1}")
        report.append(f"Files missing in {folder1_name}: {only_in_folder2}")
        if self.rename_folder2_only_files:
            report.append(f"  (Files only in {folder2_name} have been renamed with suffix '{self.folder2_rename_suffix}')")
        report.append(f"Files in both folders: {in_both}")
        if self.ignore_timestamp_only_differences:
            report.append(f"  - Identical or timestamp-only differences: {identical}")
        else:
            report.append(f"  - Identical: {identical}")
            if self.show_missing_only:
                report.append(f"  - Different: {different} (not shown in report)")
            else:
                report.append(f"  - Different: {different}")
                if self.compare_archive_contents and (archive_files_with_diffs or regular_files_with_diffs):
                    report.append(f"    - Archive files (.jar/.war/.ear/.zip/.rpm): {len(archive_files_with_diffs)}")
                    report.append(f"    - Regular files: {len(regular_files_with_diffs)}")
        report.append("")
        
        if only_in_folder1 > 0 and not self.exclude_missing_from_folder2:
            report.append(f"Files Missing in {folder2_name}")
            report.append("-" * 40)
            for result in self.results:
                if result.exists_in_folder1 and not result.exists_in_folder2:
                    report.append(f"  {result.file_path}")
            report.append("")
        
        if only_in_folder2 > 0:
            report.append(f"Files Missing in {folder1_name}")
            report.append("-" * 40)
            for result in self.results:
                if not result.exists_in_folder1 and result.exists_in_folder2:
                    report.append(f"  {result.file_path}")
            report.append("")
        
        # Count files where hash calculation failed
        hash_failed_both = sum(1 for r in self.results 
                              if r.exists_in_folder1 and r.exists_in_folder2 
                              and r.hash_folder1 == "" and r.hash_folder2 == "")
        
        # Archive files with differences section
        if archive_files_with_diffs and not self.show_missing_only:
            report.append("ARCHIVE FILES WITH DIFFERENCES (.jar/.war/.ear/.zip/.rpm)")
            report.append("=" * 80)
            report.append("Note: Archive member names are normalized (version patterns like __V10_0_1_22")
            report.append("      and .pack.gz files are treated as identical to their versioned counterparts)")
            report.append("")
            for result in archive_files_with_diffs:
                report.append(f"\n{result.file_path}")
                report.append(f"  {folder1_name}: Size={result.size_folder1} bytes, Modified={result.modified_time_folder1}")
                report.append(f"  {folder2_name}: Size={result.size_folder2} bytes, Modified={result.modified_time_folder2}")
                
                # Show archive content differences
                if result.archive_content_differs:
                    total_members_f1 = len(result.archive_members_folder1)
                    total_members_f2 = len(result.archive_members_folder2)
                    report.append(f"\n  Archive Contents Summary:")
                    report.append(f"    Total members in {folder1_name}: {total_members_f1}")
                    report.append(f"    Total members in {folder2_name}: {total_members_f2}")
                    
                    if result.archive_members_only_in_folder1:
                        report.append(f"\n  Members only in {folder1_name}: {len(result.archive_members_only_in_folder1)}")
                        for member in sorted(result.archive_members_only_in_folder1)[:15]:
                            report.append(f"    - {member}")
                        if len(result.archive_members_only_in_folder1) > 15:
                            report.append(f"    ... and {len(result.archive_members_only_in_folder1) - 15} more")
                    
                    if result.archive_members_only_in_folder2:
                        report.append(f"\n  Members only in {folder2_name}: {len(result.archive_members_only_in_folder2)}")
                        for member in sorted(result.archive_members_only_in_folder2)[:15]:
                            report.append(f"    - {member}")
                        if len(result.archive_members_only_in_folder2) > 15:
                            report.append(f"    ... and {len(result.archive_members_only_in_folder2) - 15} more")
                    
                    if result.archive_members_different_content:
                        report.append(f"\n  Members with different content (size/CRC): {len(result.archive_members_different_content)}")
                        for member in sorted(result.archive_members_different_content)[:50]:
                            meta1 = result.archive_members_meta_folder1.get(member, (0, 0))
                            meta2 = result.archive_members_meta_folder2.get(member, (0, 0))
                            size1, crc1 = meta1
                            size2, crc2 = meta2
                            report.append(f"    - {member}")
                            if size1 != size2:
                                report.append(f"      Size: {folder1_name}={size1} bytes, {folder2_name}={size2} bytes")
                            if crc1 != crc2:
                                report.append(f"      CRC:  {folder1_name}={crc1:08x}, {folder2_name}={crc2:08x}")
                        if len(result.archive_members_different_content) > 50:
                            report.append(f"    ... and {len(result.archive_members_different_content) - 50} more")
                
                # Show JAR manifest version if available
                if result.jar_version_diff:
                    report.append(f"\n  JAR MANIFEST Implementation-Version:")
                    report.append(f"    {folder1_name}: {result.jar_impl_version_folder1 or 'N/A'}")
                    report.append(f"    {folder2_name}: {result.jar_impl_version_folder2 or 'N/A'}")
                
                # Show full manifest diff if available
                if result.jar_manifest_diff and result.jar_manifest_folder1 and result.jar_manifest_folder2:
                    report.append(f"\n  JAR MANIFEST.MF Full Diff:")
                    lines1 = result.jar_manifest_folder1.split('\n')
                    lines2 = result.jar_manifest_folder2.split('\n')
                    import difflib
                    diff = difflib.unified_diff(lines1, lines2, lineterm='', 
                                               fromfile=f'{folder1_name}',
                                               tofile=f'{folder2_name}')
                    diff_text = '\n'.join(list(diff))
                    if diff_text:
                        for diff_line in diff_text.split('\n'):
                            report.append(f"    {diff_line}")
                
                report.append("-" * 80)
            report.append("")
        
        # Regular files with differences section
        if regular_files_with_diffs and not self.show_missing_only:
            report.append("DIFFERENT FILES (Non-Archive)")
            report.append("=" * 80)
            for result in regular_files_with_diffs:
                report.append(f"  {result.file_path}")
                report.append(f"    {folder1_name}: Size={result.size_folder1} bytes, Modified={result.modified_time_folder1}")
                report.append(f"    {folder2_name}: Size={result.size_folder2} bytes, Modified={result.modified_time_folder2}")
                
                # Show hash information
                if result.hash_folder1 and result.hash_folder2:
                    report.append(f"    Content hash differs: {folder1_name}={result.hash_folder1[:16]}... {folder2_name}={result.hash_folder2[:16]}...")
            report.append("")
        
        if hash_failed_both > 0 and self.ignore_timestamp_only_differences:
            report.append("FILES WITH TIMESTAMP DIFFERENCES ONLY (IGNORED)")
            report.append("-" * 40)
            report.append(f"Found {hash_failed_both} files where content comparison failed but")
            report.append("timestamp differences are being ignored (assumed identical).")
            report.append("These files are not listed in the 'Different Files' section above.")
            report.append("")
        
        return "\n".join(report)
    
    def save_report(self, output_file):
        report = self.generate_report()
        try:
            with open(output_file, 'w', encoding='utf-8') as f:
                f.write(report)
            logger.info(f"Report saved to: {output_file}")
            # Also save HTML version with anchors and colors
            try:
                html_report = self.generate_html_report()
                with open(Path(output_file).with_suffix('.html'), 'w', encoding='utf-8') as hf:
                    hf.write(html_report)
                logger.info(f"HTML report saved to: {Path(output_file).with_suffix('.html')}")
            except Exception as _e:
                logger.warning(f"Could not save HTML report: {_e}")
        except Exception as e:
            logger.error(f"Error saving report to {output_file}: {e}")
    
    def generate_html_report(self) -> str:
        if not self.results:
            error_detail = ""
            if hasattr(self, 'comparison_error') and self.comparison_error:
                error_detail = f"""
                <div style="background:#fee2e2;border:1px solid #fca5a5;border-radius:8px;padding:16px;margin:20px 0;">
                    <h3 style="color:#dc2626;margin:0 0 10px 0;">❌ Error Details</h3>
                    <p style="font-family:monospace;color:#991b1b;margin:0;">{html.escape(self.comparison_error)}</p>
                </div>
                <div style="background:#fef3c7;border:1px solid #fcd34d;border-radius:8px;padding:16px;margin:20px 0;">
                    <h3 style="color:#92400e;margin:0 0 10px 0;">💡 Troubleshooting Tips</h3>
                    <ul style="color:#92400e;margin:0;padding-left:20px;">
                        <li>Verify both folder paths exist and are accessible</li>
                        <li>Check you have read permissions on the folders</li>
                        <li>For network paths, ensure the format is correct: <code>\\\\server\\share\\path</code></li>
                        <li>Check if the paths require VPN or special network access</li>
                    </ul>
                </div>
                """
            return f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Comparison Error</title>
    <style>
        body {{ font-family: system-ui, sans-serif; margin: 0; padding: 20px; background: #f3f4f6; }}
        .container {{ max-width: 800px; margin: 0 auto; background: white; border-radius: 12px; box-shadow: 0 4px 20px rgba(0,0,0,0.1); padding: 30px; }}
        h1 {{ color: #dc2626; margin: 0 0 20px 0; }}
        .paths {{ background: #f7f7f9; padding: 16px; border-radius: 8px; margin: 20px 0; }}
        .path-label {{ font-weight: 600; color: #4b5563; font-size: 14px; }}
        .path-value {{ font-family: monospace; color: #1f2937; margin-top: 4px; word-break: break-all; }}
        a.back {{ display: inline-block; margin-top: 20px; padding: 10px 20px; background: #3b82f6; color: white; text-decoration: none; border-radius: 8px; }}
        a.back:hover {{ background: #2563eb; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>⚠️ No Comparison Results Available</h1>
        <p>The comparison could not be completed. Please check the paths and try again.</p>
        
        <div class="paths">
            <div class="path-label">📁 Old Build Path:</div>
            <div class="path-value">{html.escape(str(self.folder1_path))}</div>
            <div class="path-label" style="margin-top:12px;">📁 New Build Path:</div>
            <div class="path-value">{html.escape(str(self.folder2_path))}</div>
        </div>
        
        {error_detail}
        
        <a href="/" class="back">← Back to Main Page</a>
    </div>
</body>
</html>"""
        
        # Reset decompiler cache to pick up any newly configured decompilers
        _reset_decompiler_cache()
        dec_type, dec_path = _get_decompiler()
        if dec_type:
            logger.info(f"Class file decompilation enabled using: {dec_type}")
        
        # Extract folder names - using Old Build / New Build terminology (like Perforce)
        folder1_name = "Old Build"  # Folder 1 = Old/Previous build
        folder2_name = "New Build"  # Folder 2 = New/Current build
        folder1_display = self.folder1_path.name if self.folder1_path.name else "Old Build"
        folder2_display = self.folder2_path.name if self.folder2_path.name else "New Build"
        
        # Build groupings similar to text report
        only_in_folder1 = [r for r in self.results if r.exists_in_folder1 and not r.exists_in_folder2]
        only_in_folder2 = [r for r in self.results if r.exists_in_folder2 and not r.exists_in_folder1]
        in_both = [r for r in self.results if r.exists_in_folder1 and r.exists_in_folder2]
        identical_files = [r for r in in_both if r.content_identical]
        timestamp_compare_failed = []
        true_content_diffs = []
        for r in in_both:
            if not r.content_identical:
                compare_failed = (r.hash_folder1 == "" or r.hash_folder2 == "")
                timestamps_differ = (r.modified_time_folder1 != r.modified_time_folder2)
                if compare_failed and timestamps_differ:
                    timestamp_compare_failed.append(r)
                else:
                    true_content_diffs.append(r)
        
        # Separate archive files from regular files
        archive_diffs = [r for r in true_content_diffs if r.is_archive]
        regular_diffs = [r for r in true_content_diffs if not r.is_archive]
        
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
        .summary { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 20px; margin-bottom: 30px; }
        .stat-card { background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%); color: white; padding: 20px; border-radius: 12px; box-shadow: 0 4px 15px rgba(0,0,0,0.1); }
        .stat-card.success { background: linear-gradient(135deg, #4facfe 0%, #00f2fe 100%); }
        .stat-card.warning { background: linear-gradient(135deg, #fa709a 0%, #fee140 100%); }
        .stat-card.info { background: linear-gradient(135deg, #30cfd0 0%, #330867 100%); }
        .stat-card.danger { background: linear-gradient(135deg, #f5576c 0%, #f093fb 100%); }
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
        /* Table styles */
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
        .comparison-table tr.compare-failed { background: #f3e8ff !important; border-left: 4px solid #a855f7; }
        .status-badge { display: inline-block; padding: 5px 12px; border-radius: 16px; font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px; white-space: nowrap; }
        .status-badge.identical { background: #dbeafe; color: #1e40af; }
        .status-badge.missing-left { background: #fee2e2; color: #991b1b; }
        .status-badge.missing-right { background: #dcfce7; color: #166534; }
        .status-badge.different { background: #fef3c7; color: #92400e; }
        .status-badge.compare-failed { background: #f3e8ff; color: #6b21a8; }
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
        @media (max-width: 1024px) { 
            .summary { grid-template-columns: repeat(2, 1fr); } 
            .comparison-table { font-size: 11px; }
        }
        @media (max-width: 768px) { 
            .summary { grid-template-columns: 1fr; } 
            .nav-links { flex-direction: column; }
        }
        """ + report_collapse_extra_css() + """
        </style>
        """
        
        # Build comprehensive comparison data for table
        all_files = {}
        for r in self.results:
            status = ''
            status_text = ''
            row_class = ''
            
            if r.exists_in_folder1 and r.exists_in_folder2:
                if r.content_identical:
                    status = 'identical'
                    status_text = '✓ Unchanged'
                    row_class = 'identical'
                else:
                    compare_failed = (r.hash_folder1 == "" or r.hash_folder2 == "")
                    timestamps_differ = (r.modified_time_folder1 != r.modified_time_folder2)
                    if compare_failed and timestamps_differ:
                        status = 'compare-failed'
                        status_text = '⚠ Compare Failed'
                        row_class = 'compare-failed'
                    else:
                        status = 'different'
                        status_text = '📝 Modified'
                        row_class = 'different'
            elif r.exists_in_folder1 and not r.exists_in_folder2:
                status = 'missing-right'
                status_text = '🗑️ Removed'
                row_class = 'missing-left'
            else:
                status = 'missing-left'
                status_text = '✨ New'
                row_class = 'missing-right'
            
            all_files[r.file_path] = {
                'result': r,
                'status': status,
                'status_text': status_text,
                'row_class': row_class
            }
        
        # Sort files: differences first, then by name
        sorted_files = sorted(all_files.items(), key=lambda x: (
            0 if x[1]['status'] == 'different' else
            1 if x[1]['status'] == 'compare-failed' else
            2 if x[1]['status'] in ('missing-right', 'missing-left') else 3,
            x[0].lower()
        ))
        
        html_content = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Build Comparison Report - Old vs New</title>
    {style}
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>📂 Build Comparison Report</h1>
            <div class="subtitle">Comparing Old Build vs New Build (Perforce-style diff) • {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</div>
            <div class="paths">
                <div class="path-label">📁 OLD BUILD PATH ({html.escape(folder1_display)})</div>
                <div class="path-value">{html.escape(str(self.folder1_path))}</div>
                <div class="path-label">📁 NEW BUILD PATH ({html.escape(folder2_display)})</div>
                <div class="path-value">{html.escape(str(self.folder2_path))}</div>
            </div>
        </div>
        
        <div class="content">
            <!-- Summary Statistics -->
            <div class="summary">
                <div class="stat-card info">
                    <div class="label">Total Files</div>
                    <div class="value">{len(self.results)}</div>
                </div>
                <div class="stat-card success">
                    <div class="label">Unchanged</div>
                    <div class="value">{len(identical_files)}</div>
                </div>
                <div class="stat-card warning">
                    <div class="label">Modified</div>
                    <div class="value">{len(true_content_diffs)}</div>
                </div>
                <div class="stat-card danger">
                    <div class="label">Removed in New</div>
                    <div class="value">{len(only_in_folder1)}</div>
                </div>
                <div class="stat-card" style="background: linear-gradient(135deg, #48bb78 0%, #38a169 100%);">
                    <div class="label">Added in New</div>
                    <div class="value">{len(only_in_folder2)}</div>
                </div>
            </div>
            
            <!-- Navigation Menu -->
            <div class="nav-menu">
                <h3>📑 Quick Navigation</h3>
                <div class="nav-links">
                    <a href="#comparison-table" class="nav-link">📊 Full Comparison Table</a>"""
        
        if true_content_diffs:
            html_content += f"""
                    <a href="#different-files" class="nav-link different">≠ Modified Files ({len(true_content_diffs)})</a>"""
        if only_in_folder1:
            html_content += f"""
                    <a href="#missing-right" class="nav-link missing-left">🗑️ Removed in New Build ({len(only_in_folder1)})</a>"""
        if only_in_folder2:
            html_content += f"""
                    <a href="#missing-left" class="nav-link missing-right">✨ Added in New Build ({len(only_in_folder2)})</a>"""
        if identical_files:
            html_content += f"""
                    <a href="#identical-files" class="nav-link identical">✓ Unchanged Files ({len(identical_files)})</a>"""
        
        html_content += """
                </div>
            </div>
            
            <!-- Legend -->
            <div class="legend">
                <div class="legend-title">💡 Color Legend (Perforce-style Build Comparison)</div>
                <div class="legend-items">
                    <div class="legend-item"><span class="legend-color" style="background: #dbeafe; border: 2px solid #3b82f6;"></span> Unchanged</div>
                    <div class="legend-item"><span class="legend-color" style="background: #fef3c7; border: 2px solid #f59e0b;"></span> Modified</div>
                    <div class="legend-item"><span class="legend-color" style="background: #fee2e2; border: 2px solid #ef4444;"></span> Removed in New Build</div>
                    <div class="legend-item"><span class="legend-color" style="background: #dcfce7; border: 2px solid #22c55e;"></span> Added in New Build</div>
                    <div class="legend-item"><span class="legend-color" style="background: #f3e8ff; border: 2px solid #a855f7;"></span> Compare Failed</div>
                </div>
            </div>
            
            <!-- Full Comparison Table -->
            <div class="section" id="comparison-table">
                <div class="section-title">📊 Full Build Comparison Table</div>
                <div class="table-container">
                    <table class="comparison-table">
                        <thead>
                            <tr>
                                <th>File Path</th>
                                <th class="center">Status</th>
                                <th>""" + html.escape(folder1_name) + """<br/><small style="font-weight:normal;opacity:0.8;">Size / Modified</small></th>
                                <th>""" + html.escape(folder2_name) + """<br/><small style="font-weight:normal;opacity:0.8;">Size / Modified</small></th>
                                <th class="center">Change</th>
                            </tr>
                        </thead>
                        <tbody>"""
        
        # Helper function to format file size
        def format_size(size_bytes):
            if size_bytes < 1024:
                return f"{size_bytes} B"
            elif size_bytes < 1024 * 1024:
                return f"{size_bytes / 1024:.1f} KB"
            elif size_bytes < 1024 * 1024 * 1024:
                return f"{size_bytes / (1024 * 1024):.2f} MB"
            else:
                return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"
        
        # Helper function to calculate size difference
        def get_size_diff_info(size1, size2):
            if size1 == 0 and size2 == 0:
                return "", "", False
            if size1 == 0 or size2 == 0:
                return "N/A", "", False
            
            diff = size2 - size1
            if size1 > 0:
                pct = ((size2 - size1) / size1) * 100
            else:
                pct = 100 if size2 > 0 else 0
            
            # Match CRM high-size alert: |% change| >= 40%
            _high_size_min_abs_pct = 40.0
            is_high_diff = abs(pct) >= _high_size_min_abs_pct
            
            if diff > 0:
                diff_text = f"+{format_size(diff)}"
                diff_pct = f"+{pct:.1f}%"
            elif diff < 0:
                diff_text = f"-{format_size(abs(diff))}"
                diff_pct = f"{pct:.1f}%"
            else:
                diff_text = "0"
                diff_pct = "0%"
            
            return diff_text, diff_pct, is_high_diff
        
        for file_path, info in sorted_files:
            r = info['result']
            
            # Calculate size difference
            size_diff_text, size_diff_pct, is_high_diff = get_size_diff_info(r.size_folder1, r.size_folder2)
            
            html_content += f"""
                            <tr class="{info['row_class']}">
                                <td class="filename">{html.escape(file_path)}</td>
                                <td class="status">
                                    <span class="status-badge {info['status']}">{info['status_text']}</span>
                                </td>
                                <td class="info">"""
            
            if r.exists_in_folder1:
                html_content += f"""<div style="font-weight:600;">{format_size(r.size_folder1)}</div>"""
                html_content += f"""<div style="font-size:10px;color:#718096;">{html.escape(r.modified_time_folder1)}</div>"""
            else:
                html_content += """<span class="empty-cell">—</span>"""
            
            html_content += """</td>
                                <td class="info">"""
            
            if r.exists_in_folder2:
                html_content += f"""<div style="font-weight:600;">{format_size(r.size_folder2)}</div>"""
                html_content += f"""<div style="font-size:10px;color:#718096;">{html.escape(r.modified_time_folder2)}</div>"""
            else:
                html_content += """<span class="empty-cell">—</span>"""
            
            html_content += """</td>
                                <td class="info" style="text-align:center;">"""
            
            # Show size difference with highlighting for high differences
            if r.exists_in_folder1 and r.exists_in_folder2:
                if is_high_diff:
                    diff_color = "#dc2626" if r.size_folder2 > r.size_folder1 else "#059669"
                    html_content += f"""<div style="background:#fef2f2;padding:4px 8px;border-radius:4px;border:2px solid {diff_color};">"""
                    html_content += f"""<div style="font-weight:700;color:{diff_color};">{size_diff_text}</div>"""
                    html_content += f"""<div style="font-size:10px;color:{diff_color};">{size_diff_pct}</div>"""
                    html_content += f"""<div style="font-size:9px;color:#dc2626;font-weight:600;margin-top:2px;">⚠️ HIGH DIFF</div>"""
                    html_content += """</div>"""
                elif size_diff_text != "0":
                    html_content += f"""<div style="font-size:11px;">{size_diff_text}</div>"""
                    html_content += f"""<div style="font-size:10px;color:#718096;">{size_diff_pct}</div>"""
                else:
                    html_content += """<span style="color:#10b981;">✓ Same</span>"""
            else:
                html_content += """<span class="empty-cell">—</span>"""
            
            html_content += """</td>
                            </tr>"""
        
        html_content += """
                        </tbody>
                    </table>
                </div>
            </div>"""
        
        # Different Files Section with details
        if true_content_diffs:
            # Find files with high size differences for summary (|change| >= 40%)
            _HIGH_SIZE_ALERT_MIN_ABS_PCT = 40.0
            high_diff_files = []
            for r in true_content_diffs:
                if r.size_folder1 > 0 and r.size_folder2 > 0:
                    diff = abs(r.size_folder2 - r.size_folder1)
                    abs_pct = abs((r.size_folder2 - r.size_folder1) / r.size_folder1 * 100)
                    if abs_pct >= _HIGH_SIZE_ALERT_MIN_ABS_PCT:
                        high_diff_files.append((r, diff, abs_pct))
            
            # Sort by percentage difference descending
            high_diff_files.sort(key=lambda x: x[2], reverse=True)
            
            html_content += f"""
            <div class="section" id="different-files">
                <div class="section-title">≠ Different Files ({len(true_content_diffs)})</div>"""
            
            # High Size Difference Summary Box
            if high_diff_files:
                html_content += f"""
                <div style="background:linear-gradient(135deg,#fef2f2,#fee2e2);padding:20px;border-radius:12px;margin-bottom:20px;border:2px solid #f87171;">
                    <div style="display:flex;align-items:center;gap:10px;margin-bottom:15px;">
                        <span style="font-size:24px;">⚠️</span>
                        <h3 style="margin:0;color:#dc2626;">Major size differences ({len(high_diff_files)} files, |change| ≥ {_HIGH_SIZE_ALERT_MIN_ABS_PCT:.0f}%)</h3>
                    </div>
                    <p style="margin:0 0 12px 0;font-size:12px;color:#64748b;">
                        Files where size changed by at least {_HIGH_SIZE_ALERT_MIN_ABS_PCT:.0f}% in either direction.
                    </p>
                    <div style="background:white;border-radius:8px;overflow:hidden;">
                        <table style="width:100%;border-collapse:collapse;font-size:13px;">
                            <thead>
                                <tr style="background:#fecaca;">
                                    <th style="padding:10px;text-align:left;color:#991b1b;">JAR/File Name</th>
                                    <th style="padding:10px;text-align:right;color:#991b1b;">Left Size</th>
                                    <th style="padding:10px;text-align:right;color:#991b1b;">Right Size</th>
                                    <th style="padding:10px;text-align:right;color:#991b1b;">Difference</th>
                                    <th style="padding:10px;text-align:right;color:#991b1b;">% Change</th>
                                </tr>
                            </thead>
                            <tbody>"""
                
                for r, diff, pct in high_diff_files[:15]:  # Show top 15
                    file_name = r.file_path.split('/')[-1].split('\\\\')[-1]
                    diff_sign = "+" if r.size_folder2 > r.size_folder1 else "-"
                    diff_color = "#dc2626" if r.size_folder2 > r.size_folder1 else "#059669"
                    html_content += f"""
                                <tr style="border-bottom:1px solid #fecaca;">
                                    <td style="padding:10px;font-family:Consolas,monospace;font-weight:600;">{html.escape(file_name)}</td>
                                    <td style="padding:10px;text-align:right;">{format_size(r.size_folder1)}</td>
                                    <td style="padding:10px;text-align:right;">{format_size(r.size_folder2)}</td>
                                    <td style="padding:10px;text-align:right;color:{diff_color};font-weight:700;">{diff_sign}{format_size(diff)}</td>
                                    <td style="padding:10px;text-align:right;color:{diff_color};font-weight:700;">{diff_sign}{pct:.1f}%</td>
                                </tr>"""
                
                if len(high_diff_files) > 15:
                    html_content += f"""
                                <tr>
                                    <td colspan="5" style="padding:10px;text-align:center;color:#991b1b;font-style:italic;">
                                        ... and {len(high_diff_files) - 15} more files with high size differences
                                    </td>
                                </tr>"""
                
                html_content += """
                            </tbody>
                        </table>
                    </div>
                </div>"""
            else:
                html_content += f"""
                <div style="background:linear-gradient(135deg,#ecfdf5,#d1fae5);padding:20px;border-radius:12px;margin-bottom:20px;border:2px solid #6ee7b7;">
                    <div style="display:flex;align-items:center;gap:10px;margin-bottom:8px;">
                        <span style="font-size:24px;">✓</span>
                        <h3 style="margin:0;color:#047857;">Major size differences (|change| ≥ {_HIGH_SIZE_ALERT_MIN_ABS_PCT:.0f}%)</h3>
                    </div>
                    <p style="margin:0;font-size:13px;color:#065f46;line-height:1.55;">
                        Not found any files with a size change of {_HIGH_SIZE_ALERT_MIN_ABS_PCT:.0f}% or more
                        in either direction (increase or decrease).
                    </p>
                </div>"""
            
            html_content += f"""
                <div class="table-container">
                    <table class="comparison-table">
                        <thead>
                            <tr>
                                <th>File Path</th>
                                <th>Type</th>
                                <th>{html.escape(folder1_name)} (Left)<br/><small style="font-weight:normal;opacity:0.8;">Size / Modified</small></th>
                                <th>{html.escape(folder2_name)} (Right)<br/><small style="font-weight:normal;opacity:0.8;">Size / Modified</small></th>
                                <th style="min-width:300px;">Content Differences</th>
                            </tr>
                        </thead>
                        <tbody>"""
            
            for idx, r in enumerate(true_content_diffs):
                file_type = "Archive" if r.is_archive else "File"
                type_badge_color = "#9f7aea" if r.is_archive else "#4299e1"
                
                # Calculate size difference for this file
                size_diff_text, size_diff_pct, is_high_diff = get_size_diff_info(r.size_folder1, r.size_folder2)
                
                # Left column info (Folder 1) - Size and Modified date
                left_info = f"<div style='font-weight:600;font-size:14px;'>{format_size(r.size_folder1)}</div>"
                left_info += f"<div style='font-size:10px;color:#718096;margin-top:2px;'>{html.escape(r.modified_time_folder1)}</div>"
                
                # Right column info (Folder 2) - Size and Modified date
                right_info = f"<div style='font-weight:600;font-size:14px;'>{format_size(r.size_folder2)}</div>"
                right_info += f"<div style='font-size:10px;color:#718096;margin-top:2px;'>{html.escape(r.modified_time_folder2)}</div>"
                
                # Add size difference indicator if high diff
                if is_high_diff:
                    diff_color = "#dc2626" if r.size_folder2 > r.size_folder1 else "#059669"
                    right_info += f"<div style='margin-top:6px;padding:4px 8px;background:#fef2f2;border:2px solid {diff_color};border-radius:4px;'>"
                    right_info += f"<span style='font-weight:700;color:{diff_color};'>{size_diff_text} ({size_diff_pct})</span>"
                    right_info += f"<span style='font-size:9px;color:#dc2626;display:block;'>⚠️ HIGH DIFF</span>"
                    right_info += "</div>"
                
                # Details column with expandable content
                details = ""
                
                # Check if archive has any member differences (content, only in left/right)
                has_member_diffs = (r.is_archive and (
                    r.archive_content_differs or 
                    r.archive_members_different_content or 
                    r.archive_members_only_in_folder1 or 
                    r.archive_members_only_in_folder2
                ))
                
                if has_member_diffs:
                    # Archive with content differences
                    details += f"<div style='margin-bottom:8px;'><strong style='color:#d97706;'>📦 Archive Contents Changed</strong></div>"
                    details += f"<div style='display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:8px;'>"
                    details += f"<div style='background:#fef3c7;padding:6px 8px;border-radius:4px;font-size:11px;'><strong>Old Build:</strong> {len(r.archive_members_folder1)} members</div>"
                    details += f"<div style='background:#dcfce7;padding:6px 8px;border-radius:4px;font-size:11px;'><strong>New Build:</strong> {len(r.archive_members_folder2)} members</div>"
                    details += "</div>"
                    
                    # Helper function to filter out compiler/temporary files
                    def filter_temp_files(members):
                        exclude_patterns = [
                            'plexus.compiler.javac',
                            'JavacCompiler',
                            'arguments',
                            '.tmp',
                            '.temp',
                            '_temp_',
                            'compilerId',
                        ]
                        filtered = []
                        for m in members:
                            should_exclude = False
                            for pattern in exclude_patterns:
                                if pattern.lower() in m.lower():
                                    should_exclude = True
                                    break
                            if not should_exclude:
                                filtered.append(m)
                        return filtered
                    
                    # Members only in Old Build (removed in new build)
                    if r.archive_members_only_in_folder1:
                        filtered_left = filter_temp_files(r.archive_members_only_in_folder1)
                        if filtered_left:
                            details += f"<details style='margin:8px 0;' id='only_left_{idx}'>"
                            details += f"<summary style='cursor:pointer;color:#dc2626;font-weight:600;font-size:12px;'>🗑️ Removed in New Build ({len(filtered_left)} files)</summary>"
                            details += "<ul style='margin:8px 0 0 16px;padding:0;font-size:11px;max-height:150px;overflow-y:auto;'>"
                            sorted_left = sorted(filtered_left)
                            for member in sorted_left[:20]:
                                details += f"<li style='margin:2px 0;font-family:Consolas,monospace;'>{html.escape(member)}</li>"
                            if len(sorted_left) > 20:
                                details += f"<li style='margin:8px 0;list-style:none;'><a href='#' onclick=\"document.getElementById('all_left_{idx}').style.display='block';this.style.display='none';return false;\" style='color:#dc2626;font-weight:600;'>📋 Click to see {len(sorted_left) - 20} more files...</a></li>"
                                details += f"<div id='all_left_{idx}' style='display:none;'>"
                                for member in sorted_left[20:]:
                                    details += f"<li style='margin:2px 0;font-family:Consolas,monospace;'>{html.escape(member)}</li>"
                                details += "</div>"
                            details += "</ul></details>"
                    
                    # Members only in New Build (added)
                    if r.archive_members_only_in_folder2:
                        filtered_right = filter_temp_files(r.archive_members_only_in_folder2)
                        if filtered_right:
                            details += f"<details style='margin:8px 0;' id='only_right_{idx}'>"
                            details += f"<summary style='cursor:pointer;color:#059669;font-weight:600;font-size:12px;'>✨ Added in New Build ({len(filtered_right)} files)</summary>"
                            details += "<ul style='margin:8px 0 0 16px;padding:0;font-size:11px;max-height:150px;overflow-y:auto;'>"
                            sorted_right = sorted(filtered_right)
                            for member in sorted_right[:20]:
                                details += f"<li style='margin:2px 0;font-family:Consolas,monospace;'>{html.escape(member)}</li>"
                            if len(sorted_right) > 20:
                                details += f"<li style='margin:8px 0;list-style:none;'><a href='#' onclick=\"document.getElementById('all_right_{idx}').style.display='block';this.style.display='none';return false;\" style='color:#059669;font-weight:600;'>📋 Click to see {len(sorted_right) - 20} more files...</a></li>"
                                details += f"<div id='all_right_{idx}' style='display:none;'>"
                                for member in sorted_right[20:]:
                                    details += f"<li style='margin:2px 0;font-family:Consolas,monospace;'>{html.escape(member)}</li>"
                                details += "</div>"
                            details += "</ul></details>"
                    
                    # Members with different content - show file names with content difference details
                    if r.archive_members_different_content:
                        filtered_diff = filter_temp_files(r.archive_members_different_content)
                        if filtered_diff:
                            details += f"<details style='margin:8px 0;' id='diff_content_{idx}'>"
                            details += f"<summary style='cursor:pointer;color:#d97706;font-weight:600;font-size:12px;'>📝 Files with Different Content ({len(filtered_diff)})</summary>"
                            details += "<div style='margin:8px 0 0 0;padding:8px;background:#fffbeb;border-radius:6px;max-height:400px;overflow-y:auto;'>"
                            
                            # Helper function to render member diff item
                            def render_member_diff(member, meta1, meta2, show_manifest_version=False):
                                size1, crc1 = meta1
                                size2, crc2 = meta2
                                
                                # Calculate size difference
                                size_diff_val = size2 - size1
                                if size_diff_val != 0:
                                    diff_sign = "+" if size_diff_val > 0 else ""
                                    diff_color = "#dc2626" if size_diff_val > 0 else "#059669"
                                    size_info = f"<span style='color:{diff_color};font-weight:600;'>{diff_sign}{size_diff_val:,} bytes</span>"
                                else:
                                    size_info = "<span style='color:#6b7280;'>same size</span>"
                                
                                result_html = ""
                                member_lower = member.lower()
                                
                                # Check if it's a MANIFEST.MF file
                                if member_lower.endswith('manifest.mf') or 'meta-inf/manifest' in member_lower:
                                    result_html += f"<div style='padding:10px;margin:6px 0;background:linear-gradient(135deg,#ede9fe,#ddd6fe);border-radius:6px;border-left:4px solid #7c3aed;'>"
                                    result_html += f"<div style='display:flex;align-items:center;gap:8px;'>"
                                    result_html += f"<span style='font-size:16px;'>📋</span>"
                                    result_html += f"<span style='font-family:Consolas,monospace;font-size:12px;color:#5b21b6;font-weight:600;'>{html.escape(member)}</span>"
                                    result_html += f"</div>"
                                    result_html += f"<div style='margin-top:8px;padding:8px;background:white;border-radius:4px;'>"
                                    result_html += f"<div style='font-size:11px;color:#7c3aed;font-weight:600;margin-bottom:6px;'>MANIFEST.MF Version Difference:</div>"
                                    # Show Implementation-Version from parent JAR if available
                                    if r.jar_impl_version_folder1 or r.jar_impl_version_folder2:
                                        result_html += f"<div style='display:grid;grid-template-columns:1fr 1fr;gap:8px;'>"
                                        result_html += f"<div style='padding:6px;background:#fef3c7;border-radius:4px;'>"
                                        result_html += f"<div style='font-size:9px;color:#92400e;'>LEFT (Implementation-Version)</div>"
                                        result_html += f"<div style='font-size:12px;font-weight:700;color:#78350f;'>{html.escape(r.jar_impl_version_folder1 or 'N/A')}</div>"
                                        result_html += f"</div>"
                                        result_html += f"<div style='padding:6px;background:#dcfce7;border-radius:4px;'>"
                                        result_html += f"<div style='font-size:9px;color:#166534;'>RIGHT (Implementation-Version)</div>"
                                        result_html += f"<div style='font-size:12px;font-weight:700;color:#14532d;'>{html.escape(r.jar_impl_version_folder2 or 'N/A')}</div>"
                                        result_html += f"</div>"
                                        result_html += f"</div>"
                                    else:
                                        result_html += f"<div style='font-size:11px;color:#64748b;'>Manifest content modified (size: {size_info})</div>"
                                    result_html += f"</div>"
                                    result_html += f"</div>"
                                
                                # Check if it's a .class file (Java bytecode)
                                elif member_lower.endswith('.class') or '.class' in member_lower:
                                    # Extract class name from path - handle both / and \ separators
                                    class_path = member.replace('\\', '/').replace('//', '/')
                                    class_name = class_path.replace('/', '.')
                                    if class_name.endswith('.class'):
                                        class_name = class_name[:-6]  # Remove .class extension
                                    # Get just the simple class name (last part)
                                    simple_name = class_name.split('.')[-1] if '.' in class_name else class_name
                                    # Get package name
                                    package_name = '.'.join(class_name.split('.')[:-1]) if '.' in class_name else ''
                                    
                                    # Generate unique ID for this class diff
                                    class_diff_id = f"class_diff_{idx}_{hash(member) % 10000}"
                                    
                                    result_html += f"<div style='padding:10px;margin:6px 0;background:linear-gradient(135deg,#dbeafe,#bfdbfe);border-radius:6px;border-left:4px solid #2563eb;'>"
                                    result_html += f"<div style='display:flex;align-items:center;gap:8px;flex-wrap:wrap;'>"
                                    result_html += f"<span style='font-size:16px;'>☕</span>"
                                    result_html += f"<span style='font-family:Consolas,monospace;font-size:12px;color:#1e40af;font-weight:700;'>{html.escape(simple_name)}.class</span>"
                                    result_html += f"<span style='font-size:9px;padding:2px 6px;background:#2563eb;color:white;border-radius:3px;'>JAVA CLASS</span>"
                                    result_html += f"</div>"
                                    if package_name:
                                        result_html += f"<div style='font-size:10px;color:#3b82f6;margin-top:4px;margin-left:24px;'>Package: {html.escape(package_name)}</div>"
                                    result_html += f"<div style='font-size:10px;color:#64748b;margin-top:2px;margin-left:24px;word-break:break-all;'>Path: {html.escape(class_path)}</div>"
                                    result_html += f"<div style='font-size:10px;margin-top:4px;margin-left:24px;'>"
                                    result_html += f"<span style='color:#dc2626;font-weight:600;'>⚡ Bytecode Modified</span>"
                                    result_html += f" | Size change: {size_info}"
                                    result_html += f"</div>"
                                    
                                    # Try to decompile and show Java source diff
                                    try:
                                        dec_type, dec_path = _get_decompiler()
                                        if dec_type:
                                            # Get archive paths
                                            archive_path1 = self.folder1_path / r.file_path if hasattr(self, 'folder1_path') else None
                                            archive_path2 = self.folder2_path / r.file_path if hasattr(self, 'folder2_path') else None
                                            
                                            class_bytes1 = None
                                            class_bytes2 = None
                                            
                                            if archive_path1 and _path_exists(archive_path1):
                                                class_bytes1 = _extract_class_from_archive(str(archive_path1), member)
                                            if archive_path2 and _path_exists(archive_path2):
                                                class_bytes2 = _extract_class_from_archive(str(archive_path2), member)
                                            
                                            if class_bytes1 and class_bytes2:
                                                # Decompile both versions
                                                success1, source1, err1 = _decompile_class_file(class_bytes1, simple_name, dec_type, dec_path)
                                                success2, source2, err2 = _decompile_class_file(class_bytes2, simple_name, dec_type, dec_path)
                                                
                                                if success1 and success2:
                                                    # Compare the decompiled sources
                                                    diff_result = _compare_decompiled_sources(source1, source2, simple_name)
                                                    
                                                    if diff_result['has_diff']:
                                                        result_html += f"<details style='margin-top:8px;' id='{class_diff_id}'>"
                                                        result_html += f"<summary style='cursor:pointer;color:#1e40af;font-weight:600;font-size:11px;background:#e0e7ff;padding:6px 10px;border-radius:4px;'>"
                                                        result_html += f"📝 View Java Source Differences "
                                                        result_html += f"<span style='color:#dc2626;'>(+{diff_result['added_lines']} / -{diff_result['removed_lines']} lines)</span>"
                                                        result_html += f"</summary>"
                                                        result_html += f"<div style='margin-top:8px;'>"
                                                        result_html += diff_result['diff_html']
                                                        result_html += f"</div>"
                                                        result_html += f"</details>"
                                                elif err1 or err2:
                                                    result_html += f"<div style='font-size:9px;color:#6b7280;margin-top:4px;margin-left:24px;'>Decompile note: {html.escape(err1 or err2)[:50]}</div>"
                                        else:
                                            # No decompiler available - show hint
                                            result_html += f"<div style='font-size:9px;color:#9ca3af;margin-top:4px;margin-left:24px;font-style:italic;'>"
                                            result_html += f"💡 Install JDK (javap) or CFR decompiler to see Java source diff"
                                            result_html += f"</div>"
                                    except Exception as decompile_err:
                                        # Silently ignore decompilation errors
                                        pass
                                    
                                    result_html += f"</div>"
                                
                                # Check if it's a properties/config file
                                elif member_lower.endswith(('.properties', '.xml', '.json', '.yml', '.yaml', '.conf', '.cfg')):
                                    ext = member.split('.')[-1].upper()
                                    result_html += f"<div style='padding:8px;margin:4px 0;background:linear-gradient(135deg,#fef3c7,#fde68a);border-radius:4px;border-left:4px solid #d97706;'>"
                                    result_html += f"<div style='display:flex;align-items:center;gap:8px;'>"
                                    result_html += f"<span style='font-size:14px;'>⚙️</span>"
                                    result_html += f"<span style='font-family:Consolas,monospace;font-size:11px;color:#92400e;font-weight:600;'>{html.escape(member)}</span>"
                                    result_html += f"<span style='font-size:9px;padding:2px 6px;background:#d97706;color:white;border-radius:3px;'>{ext}</span>"
                                    result_html += f"</div>"
                                    result_html += f"<div style='font-size:10px;color:#64748b;margin-top:4px;margin-left:22px;'>Configuration modified | Size: {size_info}</div>"
                                    result_html += f"</div>"
                                
                                # Default display for other files
                                else:
                                    result_html += f"<div style='padding:6px 8px;margin:4px 0;background:white;border-radius:4px;border-left:3px solid #d97706;'>"
                                    result_html += f"<div style='font-family:Consolas,monospace;font-size:11px;color:#92400e;word-break:break-all;'>{html.escape(member)}</div>"
                                    result_html += f"<div style='font-size:10px;color:#64748b;margin-top:2px;'>Size: {size_info} | Content: <span style='color:#dc2626;'>Modified</span></div>"
                                    result_html += f"</div>"
                                
                                return result_html
                            
                            sorted_diff = sorted(filtered_diff)
                            visible_count = 20
                            
                            for i, member in enumerate(sorted_diff[:visible_count]):
                                meta1 = r.archive_members_meta_folder1.get(member, (0, 0))
                                meta2 = r.archive_members_meta_folder2.get(member, (0, 0))
                                details += render_member_diff(member, meta1, meta2, True)
                            
                            if len(sorted_diff) > visible_count:
                                remaining = len(sorted_diff) - visible_count
                                details += f"<div style='margin-top:10px;text-align:center;'>"
                                details += f"<a href='#' onclick=\"document.getElementById('all_diff_{idx}').style.display='block';this.parentElement.style.display='none';return false;\" style='display:inline-block;padding:8px 16px;background:#d97706;color:white;border-radius:6px;text-decoration:none;font-weight:600;font-size:12px;'>📋 Click to see all {remaining} more files with differences</a>"
                                details += f"</div>"
                                details += f"<div id='all_diff_{idx}' style='display:none;'>"
                                for member in sorted_diff[visible_count:]:
                                    meta1 = r.archive_members_meta_folder1.get(member, (0, 0))
                                    meta2 = r.archive_members_meta_folder2.get(member, (0, 0))
                                    details += render_member_diff(member, meta1, meta2, True)
                                details += f"</div>"
                            
                            details += "</div></details>"
                
                elif r.jar_manifest_diff or (r.is_archive and r.archive_members_different_content):
                    # JAR/Archive with manifest and/or member differences
                    
                    # Show manifest differences if present
                    if r.jar_manifest_diff:
                        details += f"<div style='margin-bottom:8px;'><strong style='color:#7c3aed;'>📋 JAR Manifest Differs</strong></div>"
                        if r.jar_version_diff:
                            details += f"<div style='background:#ede9fe;padding:8px;border-radius:4px;font-size:11px;margin-bottom:10px;'>"
                            details += f"<div><strong>Implementation-Version:</strong></div>"
                            details += f"<div style='margin-top:4px;display:grid;grid-template-columns:1fr 1fr;gap:8px;'>"
                            details += f"<div style='background:white;padding:4px 8px;border-radius:3px;'><strong>Left:</strong> {html.escape(r.jar_impl_version_folder1 or 'N/A')}</div>"
                            details += f"<div style='background:white;padding:4px 8px;border-radius:3px;'><strong>Right:</strong> {html.escape(r.jar_impl_version_folder2 or 'N/A')}</div>"
                            details += "</div></div>"
                        else:
                            details += f"<div style='font-size:11px;color:#6b7280;margin-bottom:10px;'>Manifest content differs (non-version attributes)</div>"
                    
                    # Show member differences if present
                    if r.is_archive and r.archive_members_different_content:
                        # Helper function to filter out compiler/temporary files  
                        def filter_temp_files_manifest(members):
                            exclude_patterns = ['plexus.compiler.javac', 'JavacCompiler', 'arguments', '.tmp', '.temp', '_temp_', 'compilerId']
                            filtered = []
                            for m in members:
                                should_exclude = any(pattern.lower() in m.lower() for pattern in exclude_patterns)
                                if not should_exclude:
                                    filtered.append(m)
                            return filtered
                        
                        filtered_diff = filter_temp_files_manifest(r.archive_members_different_content)
                        if filtered_diff:
                            details += f"<details style='margin:8px 0;' open>"
                            details += f"<summary style='cursor:pointer;color:#d97706;font-weight:600;font-size:12px;'>📝 Files with Different Content ({len(filtered_diff)})</summary>"
                            details += "<div style='margin:8px 0 0 0;padding:8px;background:#fffbeb;border-radius:6px;max-height:300px;overflow-y:auto;'>"
                            
                            sorted_diff = sorted(filtered_diff)
                            for member in sorted_diff[:15]:
                                meta1 = r.archive_members_meta_folder1.get(member, (0, 0)) if r.archive_members_meta_folder1 else (0, 0)
                                meta2 = r.archive_members_meta_folder2.get(member, (0, 0)) if r.archive_members_meta_folder2 else (0, 0)
                                size1, crc1 = meta1
                                size2, crc2 = meta2
                                
                                size_diff_val = size2 - size1
                                if size_diff_val != 0:
                                    diff_sign = "+" if size_diff_val > 0 else ""
                                    diff_color = "#dc2626" if size_diff_val > 0 else "#059669"
                                    size_info = f"<span style='color:{diff_color};font-weight:600;'>{diff_sign}{size_diff_val:,} bytes</span>"
                                else:
                                    size_info = "<span style='color:#6b7280;'>same size</span>"
                                
                                member_lower = member.lower()
                                class_path = member.replace('\\', '/').replace('//', '/')
                                
                                # Check if it's a .class file
                                if member_lower.endswith('.class') or '.class' in member_lower:
                                    class_name = class_path.replace('/', '.')
                                    if class_name.endswith('.class'):
                                        class_name = class_name[:-6]
                                    simple_name = class_name.split('.')[-1] if '.' in class_name else class_name
                                    package_name = '.'.join(class_name.split('.')[:-1]) if '.' in class_name else ''
                                    class_diff_id2 = f"class_diff2_{idx}_{hash(member) % 10000}"
                                    
                                    details += f"<div style='padding:8px;margin:4px 0;background:linear-gradient(135deg,#dbeafe,#bfdbfe);border-radius:6px;border-left:4px solid #2563eb;'>"
                                    details += f"<div style='display:flex;align-items:center;gap:6px;flex-wrap:wrap;'>"
                                    details += f"<span style='font-size:14px;'>☕</span>"
                                    details += f"<span style='font-family:Consolas,monospace;font-size:11px;color:#1e40af;font-weight:700;'>{html.escape(simple_name)}.class</span>"
                                    details += f"<span style='font-size:8px;padding:2px 4px;background:#2563eb;color:white;border-radius:2px;'>CLASS</span>"
                                    details += f"</div>"
                                    if package_name:
                                        details += f"<div style='font-size:9px;color:#3b82f6;margin-top:2px;margin-left:20px;'>pkg: {html.escape(package_name)}</div>"
                                    details += f"<div style='font-size:9px;color:#dc2626;margin-top:2px;margin-left:20px;'>⚡ Bytecode Modified | {size_info}</div>"
                                    
                                    # Try to decompile and show Java source diff
                                    try:
                                        dec_type, dec_path = _get_decompiler()
                                        if dec_type:
                                            archive_path1 = self.folder1_path / r.file_path if hasattr(self, 'folder1_path') else None
                                            archive_path2 = self.folder2_path / r.file_path if hasattr(self, 'folder2_path') else None
                                            
                                            if archive_path1 and archive_path2 and _path_exists(archive_path1) and _path_exists(archive_path2):
                                                class_bytes1 = _extract_class_from_archive(str(archive_path1), member)
                                                class_bytes2 = _extract_class_from_archive(str(archive_path2), member)
                                                
                                                if class_bytes1 and class_bytes2:
                                                    success1, source1, _ = _decompile_class_file(class_bytes1, simple_name, dec_type, dec_path)
                                                    success2, source2, _ = _decompile_class_file(class_bytes2, simple_name, dec_type, dec_path)
                                                    
                                                    if success1 and success2:
                                                        diff_result = _compare_decompiled_sources(source1, source2, simple_name)
                                                        if diff_result['has_diff']:
                                                            details += f"<details style='margin-top:6px;' id='{class_diff_id2}'>"
                                                            details += f"<summary style='cursor:pointer;color:#1e40af;font-weight:600;font-size:10px;background:#e0e7ff;padding:4px 8px;border-radius:3px;margin-left:20px;'>"
                                                            details += f"📝 View Java Diff (+{diff_result['added_lines']}/-{diff_result['removed_lines']})</summary>"
                                                            details += f"<div style='margin:6px 0 0 20px;'>{diff_result['diff_html']}</div>"
                                                            details += f"</details>"
                                        else:
                                            details += f"<div style='font-size:8px;color:#9ca3af;margin-top:2px;margin-left:20px;'>💡 JDK/CFR for Java diff</div>"
                                    except Exception:
                                        pass
                                    
                                    details += f"</div>"
                                
                                # Check if it's MANIFEST.MF
                                elif 'manifest.mf' in member_lower:
                                    details += f"<div style='padding:8px;margin:4px 0;background:linear-gradient(135deg,#ede9fe,#ddd6fe);border-radius:6px;border-left:4px solid #7c3aed;'>"
                                    details += f"<div style='display:flex;align-items:center;gap:6px;'>"
                                    details += f"<span style='font-size:14px;'>📋</span>"
                                    details += f"<span style='font-family:Consolas,monospace;font-size:11px;color:#5b21b6;font-weight:700;'>MANIFEST.MF</span>"
                                    details += f"</div>"
                                    if r.jar_impl_version_folder1 or r.jar_impl_version_folder2:
                                        details += f"<div style='font-size:9px;color:#7c3aed;margin-top:2px;margin-left:20px;'>Version: {html.escape(r.jar_impl_version_folder1 or 'N/A')} → {html.escape(r.jar_impl_version_folder2 or 'N/A')}</div>"
                                    details += f"</div>"
                                
                                # Other files
                                else:
                                    details += f"<div style='padding:6px 8px;margin:4px 0;background:white;border-radius:4px;border-left:3px solid #d97706;'>"
                                    details += f"<div style='font-family:Consolas,monospace;font-size:10px;color:#92400e;word-break:break-all;'>{html.escape(member)}</div>"
                                    details += f"<div style='font-size:9px;color:#64748b;margin-top:2px;'>{size_info}</div>"
                                    details += f"</div>"
                            
                            if len(sorted_diff) > 15:
                                details += f"<div style='margin-top:8px;text-align:center;'>"
                                details += f"<a href='#' onclick=\"var el=this.nextElementSibling;el.style.display=el.style.display==='none'?'block':'none';this.textContent=el.style.display==='none'?'📋 Click to see {len(sorted_diff)-15} more files':'Hide extra files';return false;\" style='color:#d97706;font-weight:600;font-size:11px;'>📋 Click to see {len(sorted_diff)-15} more files</a>"
                                details += f"<div style='display:none;'>"
                                for member in sorted_diff[15:]:
                                    details += f"<div style='padding:4px 8px;margin:2px 0;background:white;border-radius:3px;border-left:2px solid #d97706;'>"
                                    details += f"<span style='font-family:Consolas,monospace;font-size:10px;color:#92400e;'>{html.escape(member)}</span>"
                                    details += f"</div>"
                                details += f"</div></div>"
                            
                            details += "</div></details>"
                
                else:
                    # Regular file with content difference - show the difference prominently
                    details += f"<div style='margin-bottom:8px;'><strong style='color:#2563eb;'>📄 File Content Differs</strong></div>"
                    
                    # Size difference box
                    details += f"<div style='background:#dbeafe;padding:12px;border-radius:6px;font-size:11px;'>"
                    
                    if r.size_folder1 != r.size_folder2:
                        size_diff = r.size_folder2 - r.size_folder1
                        diff_sign = "+" if size_diff > 0 else ""
                        diff_color = "#dc2626" if size_diff > 0 else "#059669"
                        pct_diff = ((r.size_folder2 - r.size_folder1) / r.size_folder1 * 100) if r.size_folder1 > 0 else 100
                        
                        details += f"<div style='text-align:center;padding:10px;background:{'#fee2e2' if is_high_diff else 'white'};border-radius:6px;border:2px solid {diff_color};'>"
                        details += f"<div style='font-size:10px;color:#64748b;margin-bottom:4px;'>SIZE DIFFERENCE</div>"
                        details += f"<div style='font-size:20px;font-weight:700;color:{diff_color};'>{diff_sign}{format_size(abs(size_diff))}</div>"
                        details += f"<div style='font-size:12px;color:{diff_color};margin-top:2px;'>{diff_sign}{pct_diff:.1f}%</div>"
                        details += f"<div style='font-size:10px;color:#94a3b8;margin-top:4px;'>({diff_sign}{abs(size_diff):,} bytes)</div>"
                        if is_high_diff:
                            details += f"<div style='margin-top:6px;padding:4px;background:#dc2626;color:white;border-radius:4px;font-size:10px;font-weight:600;'>⚠️ HIGH DIFFERENCE</div>"
                        details += f"</div>"
                    else:
                        details += f"<div style='text-align:center;padding:10px;background:#dcfce7;border-radius:6px;border:2px solid #10b981;'>"
                        details += f"<div style='font-size:10px;color:#64748b;margin-bottom:4px;'>SIZE DIFFERENCE</div>"
                        details += f"<div style='font-size:16px;font-weight:700;color:#166534;'>No size change</div>"
                        details += f"<div style='font-size:11px;color:#4ade80;margin-top:4px;'>Content modified (same size)</div>"
                        details += f"</div>"
                    
                    # Content status
                    details += f"<div style='margin-top:10px;text-align:center;'>"
                    details += f"<span style='display:inline-block;padding:6px 12px;background:#fee2e2;border-radius:4px;color:#dc2626;font-weight:600;font-size:11px;'>✗ Content Modified</span>"
                    details += f"</div>"
                    
                    details += f"</div>"
                
                html_content += f"""
                            <tr class="different">
                                <td class="filename">{html.escape(r.file_path)}</td>
                                <td><span style="display:inline-block;padding:3px 8px;border-radius:4px;font-size:10px;font-weight:600;background:{type_badge_color};color:white;">{file_type}</span></td>
                                <td class="info" style="font-size:11px;">{left_info}</td>
                                <td class="info" style="font-size:11px;">{right_info}</td>
                                <td class="info" style="font-size:11px;">{details}</td>
                            </tr>"""
            
            html_content += """
                        </tbody>
                    </table>
                </div>
            </div>"""
        
        # Removed in New Build (was in Old Build only) Section
        if only_in_folder1:
            total_size_left = sum(r.size_folder1 for r in only_in_folder1)
            html_content += f"""
            <div class="section" id="missing-right">
                <div class="section-title">🗑️ Removed in New Build ({len(only_in_folder1)} files)</div>
                <div style="background:#fef2f2;padding:12px;border-radius:8px;margin-bottom:15px;border-left:4px solid #f87171;">
                    <strong style="color:#991b1b;">Total Size of Removed Files:</strong> 
                    <span style="font-size:18px;font-weight:700;color:#dc2626;">{format_size(total_size_left)}</span>
                    <span style="color:#6b7280;font-size:12px;">({total_size_left:,} bytes)</span>
                </div>
                <div class="table-container">
                    <table class="comparison-table">
                        <thead>
                            <tr>
                                <th>File Path</th>
                                <th>Size (Old Build)</th>
                                <th>Last Modified</th>
                                <th>Status</th>
                            </tr>
                        </thead>
                        <tbody>"""
            
            for r in only_in_folder1:
                html_content += f"""
                            <tr class="missing-left">
                                <td class="filename">{html.escape(r.file_path)}</td>
                                <td class="info" style="font-weight:600;">{format_size(r.size_folder1)}</td>
                                <td class="info" style="font-size:11px;color:#718096;">{html.escape(r.modified_time_folder1)}</td>
                                <td class="status"><span class="status-badge missing-right">🗑️ Removed</span></td>
                            </tr>"""
            
            html_content += """
                        </tbody>
                    </table>
                </div>
            </div>"""
        
        # Added in New Build Section
        if only_in_folder2:
            total_size_right = sum(r.size_folder2 for r in only_in_folder2)
            html_content += f"""
            <div class="section" id="missing-left">
                <div class="section-title">✨ Added in New Build ({len(only_in_folder2)} files)</div>
                <div style="background:#f0fdf4;padding:12px;border-radius:8px;margin-bottom:15px;border-left:4px solid #4ade80;">
                    <strong style="color:#166534;">Total Size of New Files:</strong> 
                    <span style="font-size:18px;font-weight:700;color:#16a34a;">{format_size(total_size_right)}</span>
                    <span style="color:#6b7280;font-size:12px;">({total_size_right:,} bytes)</span>
                </div>
                <div class="table-container">
                    <table class="comparison-table">
                        <thead>
                            <tr>
                                <th>File Path</th>
                                <th>Size (New Build)</th>
                                <th>Last Modified</th>
                                <th>Status</th>
                            </tr>
                        </thead>
                        <tbody>"""
            
            for r in only_in_folder2:
                html_content += f"""
                            <tr class="missing-right">
                                <td class="filename">{html.escape(r.file_path)}</td>
                                <td class="info" style="font-weight:600;">{format_size(r.size_folder2)}</td>
                                <td class="info" style="font-size:11px;color:#718096;">{html.escape(r.modified_time_folder2)}</td>
                                <td class="status"><span class="status-badge missing-left">✨ New</span></td>
                            </tr>"""
            
            html_content += """
                        </tbody>
                    </table>
                </div>
            </div>"""
        
        # Identical Files Section
        if identical_files:
            total_size_identical = sum(r.size_folder1 for r in identical_files)
            html_content += f"""
            <div class="section" id="identical-files">
                <div class="section-title">✓ Identical Files ({len(identical_files)} files)</div>
                <div style="background:#f0f9ff;padding:12px;border-radius:8px;margin-bottom:15px;border-left:4px solid #3b82f6;">
                    <strong style="color:#1e40af;">Total Size of Identical Files:</strong> 
                    <span style="font-size:18px;font-weight:700;color:#2563eb;">{format_size(total_size_identical)}</span>
                    <span style="color:#6b7280;font-size:12px;">({total_size_identical:,} bytes)</span>
                </div>
                <div class="table-container">
                    <table class="comparison-table">
                        <thead>
                            <tr>
                                <th>File Path</th>
                                <th>Size</th>
                                <th>Modified Date</th>
                                <th>Status</th>
                            </tr>
                        </thead>
                        <tbody>"""
            
            # Show first 100 identical files
            for r in identical_files[:100]:
                html_content += f"""
                            <tr class="identical">
                                <td class="filename">{html.escape(r.file_path)}</td>
                                <td class="info" style="font-weight:600;">{format_size(r.size_folder1)}</td>
                                <td class="info" style="font-size:11px;color:#718096;">{html.escape(r.modified_time_folder1)}</td>
                                <td class="status"><span class="status-badge identical">✓ Identical</span></td>
                            </tr>"""
            
            if len(identical_files) > 100:
                html_content += f"""
                            <tr>
                                <td colspan="4" style="text-align:center;padding:20px;color:#718096;font-style:italic;">
                                    ... and {len(identical_files) - 100} more identical files
                                </td>
                            </tr>"""
            
            html_content += """
                        </tbody>
                    </table>
                </div>
            </div>"""
        
        html_content += """
            <a href="/" class="back-link">← Back to Home</a>
        </div>
    </div>
</body>
</html>"""
        
        return inject_report_section_controls(html_content)

class SimpleFileNameResult:
    def __init__(self, file_path):
        self.file_path = file_path
        self.exists_in_folder1 = False
        self.exists_in_folder2 = False

class SimpleFileNameComparer:
    def __init__(self, folder1_path, folder2_path):
        self.folder1_path = Path(folder1_path)
        self.folder2_path = Path(folder2_path)
        self.results = []
    
    def _get_all_files(self, folder_path):
        """Get all files in a folder as relative paths."""
        files = set()
        try:
            for root, dirs, filenames in os.walk(folder_path):
                for filename in filenames:
                    full_path = Path(root) / filename
                    relative_path = full_path.relative_to(folder_path)
                    files.add(str(relative_path).replace('\\', '/'))
        except Exception as e:
            logger.error(f"Error scanning folder {folder_path}: {e}")
        return files
    
    def compare_file_names(self):
        """Compare only file names between two folders."""
        logger.info(f"Comparing file names only: {self.folder1_path} vs {self.folder2_path}")
        
        # Check if folders exist
        if not _path_exists(self.folder1_path):
            logger.error(f"Folder 1 does not exist: {self.folder1_path}")
            return []
        
        if not _path_exists(self.folder2_path):
            logger.error(f"Folder 2 does not exist: {self.folder2_path}")
            return []
        
        # Get all files from both folders
        files1 = self._get_all_files(self.folder1_path)
        files2 = self._get_all_files(self.folder2_path)
        all_files = files1.union(files2)
        
        logger.info(f"Found {len(files1)} files in folder 1, {len(files2)} files in folder 2")
        
        self.results = []
        
        # Create results for each file
        for file_path in sorted(all_files):
            result = SimpleFileNameResult(file_path)
            result.exists_in_folder1 = file_path in files1
            result.exists_in_folder2 = file_path in files2
            self.results.append(result)
        
        logger.info("File name comparison completed successfully")
        return self.results
    
    def generate_simple_report(self):
        """Generate a simple report showing only file name differences."""
        if not self.results:
            return "No comparison results available."
        
        report = []
        report.append("=" * 80)
        report.append("SIMPLE FILE NAME COMPARISON REPORT")
        report.append("=" * 80)
        report.append(f"Folder 1: {self.folder1_path}")
        report.append(f"Folder 2: {self.folder2_path}")
        report.append(f"Comparison Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        report.append("")
        
        # Calculate statistics
        total_files = len(self.results)
        only_in_folder1 = [r for r in self.results if r.exists_in_folder1 and not r.exists_in_folder2]
        only_in_folder2 = [r for r in self.results if not r.exists_in_folder1 and r.exists_in_folder2]
        in_both = [r for r in self.results if r.exists_in_folder1 and r.exists_in_folder2]
        
        report.append("SUMMARY")
        report.append("-" * 40)
        report.append(f"Total unique files: {total_files}")
        report.append(f"Files only in folder 1: {len(only_in_folder1)}")
        report.append(f"Files only in folder 2: {len(only_in_folder2)}")
        report.append(f"Files in both folders: {len(in_both)}")
        report.append("")
        
        # List files only in folder 1
        if only_in_folder1:
            report.append("FILES ONLY IN FOLDER 1")
            report.append("-" * 40)
            for result in only_in_folder1:
                report.append(f"  {result.file_path}")
            report.append("")
        
        # List files only in folder 2
        if only_in_folder2:
            report.append("FILES ONLY IN FOLDER 2")
            report.append("-" * 40)
            for result in only_in_folder2:
                report.append(f"  {result.file_path}")
            report.append("")
        
        # Show files in both folders (optional - can be commented out if not needed)
        if in_both:
            report.append("FILES IN BOTH FOLDERS (SAME NAME)")
            report.append("-" * 40)
            for result in in_both:
                report.append(f"  {result.file_path}")
            report.append("")
        
        return "\n".join(report)
    
    def save_simple_report(self, output_file):
        """Save simple file name comparison report to file."""
        report = self.generate_simple_report()
        try:
            with open(output_file, 'w', encoding='utf-8') as f:
                f.write(report)
            logger.info(f"Simple file name comparison report saved to: {output_file}")
        except Exception as e:
            logger.error(f"Error saving report to {output_file}: {e}")

class NamingConventionResult:
    def __init__(self, file_name, file_type):
        self.file_name = file_name
        self.file_type = file_type  # 'jar', 'other'
        self.exists_in_folder1 = False
        self.exists_in_folder2 = False
        self.relative_paths1 = []  # List of paths (for duplicate filenames)
        self.relative_paths2 = []  # List of paths (for duplicate filenames)
        # Backwards compatibility
        self.relative_path1 = ""
        self.relative_path2 = ""
        self.is_exact_match = False
        self.potential_matches = []  # List of similar names from other folder
        self.similarity_scores = []  # Similarity scores for potential matches

class NamingConventionComparer:
    def __init__(self, folder1_path, folder2_path):
        self.folder1_path = Path(folder1_path)
        self.folder2_path = Path(folder2_path)
        self.results = []
        self.jar_files_folder1 = {}  # filename -> [list of relative_paths]
        self.jar_files_folder2 = {}  # filename -> [list of relative_paths]
        self.other_files_folder1 = {}  # filename -> [list of relative_paths]
        self.other_files_folder2 = {}  # filename -> [list of relative_paths]
        self.similarity_threshold = 0.6  # Threshold for considering files as potential matches
        # Temp folder tracking for cleanup
        self.temp_folder1 = None
        self.temp_folder2 = None
        self.should_cleanup_temp_folders = True
        # Network path handling
        self.max_retries = 3
        self.retry_delay = 1.0  # seconds
        self.network_timeout = 30  # seconds
        self.use_local_copy = False  # Disable local copy by default - work directly with network paths
        # Progress tracking
        self.total_files = 0
        self.processed_files = 0
        self.current_file = ""
        self.progress_percentage = 0.0
        self.estimated_time_remaining = 0
        self.comparison_start_time = None
        self.comparison_error = None
    
    def _is_network_path(self, file_path):
        """Check if the path is a UNC network path."""
        path_str = str(file_path)
        return path_str.startswith('\\\\') or (len(path_str) > 1 and path_str[1] == ':' and not os.path.exists(path_str[:3]))
    
    def _check_network_connectivity(self, file_path):
        """Check if the network path is accessible."""
        if not self._is_network_path(file_path):
            return True
        
        try:
            # Extract server name from UNC path
            path_str = str(file_path)
            if path_str.startswith('\\\\'):
                parts = path_str.split('\\')
                if len(parts) >= 3:
                    server_name = parts[2]
                    # Try to connect to the server
                    socket.setdefaulttimeout(10)  # 10 second timeout for connectivity check
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    try:
                        result = sock.connect_ex((server_name, 445))  # SMB port
                        sock.close()
                        return result == 0
                    except:
                        sock.close()
                        return False
        except Exception as e:
            logger.warning(f"Network connectivity check failed for {file_path}: {e}")
            return False
        return True
    
    def _retry_operation(self, operation, file_path, operation_name):
        """Retry an operation with exponential backoff for network errors."""
        last_error = None
        
        for attempt in range(self.max_retries):
            try:
                return operation(file_path)
            except (OSError, IOError) as e:
                last_error = e
                error_code = getattr(e, 'winerror', getattr(e, 'errno', 0))
                
                # Check if it's a network-related error
                if error_code in [53, 59, 67, 1231, 1326]:  # Common network error codes
                    if attempt < self.max_retries - 1:
                        wait_time = self.retry_delay * (2 ** attempt)  # Exponential backoff
                        logger.info(f"Network error (attempt {attempt + 1}/{self.max_retries}) for {file_path}, retrying in {wait_time}s...")
                        time.sleep(wait_time)
                        continue
                    else:
                        logger.warning(f"{operation_name} failed after {self.max_retries} attempts for {file_path}: {e}")
                        break
                else:
                    # Non-network error, don't retry
                    logger.warning(f"{operation_name} failed for {file_path}: {e}")
                    break
            except Exception as e:
                last_error = e
                logger.warning(f"{operation_name} failed for {file_path}: {e}")
                break
        
        return None
    
    def _copy_network_folder_to_local(self, source_folder):
        """Copy a network folder to a local temporary directory for faster access."""
        temp_path = None
        try:
            temp_dir = mkdtemp_compare(prefix="net_")
            temp_path = Path(temp_dir)
            logger.info(f"Copying network folder to local: {source_folder} -> {temp_path}")
            
            # Use robocopy on Windows for better network copy performance
            if os.name == 'nt':
                # robocopy /E /Z /R:3 /W:5 /MT:8 source dest
                # /E = copy subdirectories including empty ones
                # /Z = restartable mode
                # /R:3 = retry 3 times
                # /W:5 = wait 5 seconds between retries
                # /MT:8 = multi-threaded (8 threads)
                cmd = ['robocopy', str(source_folder), str(temp_path), '/E', '/Z', '/R:3', '/W:5', '/MT:8', '/NP']
                result = subprocess.run(cmd, capture_output=True, text=True)
                # robocopy exit codes: 0-7 are success (various levels of success)
                if result.returncode >= 8:
                    logger.error(f"robocopy failed with exit code {result.returncode}")
                    logger.error(f"stderr: {result.stderr}")
                    # Try shutil.copytree as fallback
                    logger.info("Trying fallback copy method (shutil.copytree)...")
                    shutil.rmtree(temp_path)  # Clean up failed robocopy attempt
                    shutil.copytree(source_folder, temp_path)
            else:
                # On non-Windows systems, use shutil.copytree
                shutil.copytree(source_folder, temp_path)
            
            logger.info(f"Successfully copied network folder to local: {temp_path}")
            return temp_path
        except Exception as e:
            logger.error(f"Failed to copy network folder to local: {e}")
            # Clean up temp directory on failure
            try:
                if temp_path and temp_path.exists():
                    shutil.rmtree(temp_path)
            except Exception:
                pass
            return None
    
    def _update_progress(self, current_file=""):
        """Update progress tracking information."""
        if self.total_files > 0:
            self.progress_percentage = (self.processed_files / self.total_files) * 100
            
            # Estimate time remaining
            if self.comparison_start_time and self.processed_files > 0:
                import time
                elapsed_time = time.time() - self.comparison_start_time
                avg_time_per_file = elapsed_time / self.processed_files
                remaining_files = self.total_files - self.processed_files
                self.estimated_time_remaining = int(avg_time_per_file * remaining_files)
        
        self.current_file = current_file
    
    def _extract_zip_to_temp(self, zip_path):
        """Extract a zip file to a temporary directory."""
        if not _path_exists(zip_path):
            logger.error(f"Zip file does not exist: {zip_path}")
            return None
        
        if not _path_is_file(zip_path):
            logger.error(f"Path is not a file: {zip_path}")
            return None
        
        temp_dir = mkdtemp_compare(prefix="z")
        temp_path = Path(temp_dir)
        
        # Enable long path support on Windows by using \\?\ prefix
        if os.name == 'nt':
            # Convert to absolute path with long path prefix
            temp_path_str = str(temp_path.resolve())
            if not temp_path_str.startswith('\\\\?\\'):
                temp_path_long = Path('\\\\?\\' + temp_path_str)
            else:
                temp_path_long = temp_path
        else:
            temp_path_long = temp_path
        
        logger.info(f"Extracting zip file to temporary directory: {zip_path.name} -> {temp_path}")
        
        try:
            with zipfile.ZipFile(zip_path, 'r') as zf:
                members = zf.namelist()
                total_members = len(members)
                extracted_count = 0
                failed_count = 0
                
                # Extract members one by one to handle errors gracefully
                for idx, member in enumerate(members):
                    try:
                        # Show progress for large archives
                        if idx % 1000 == 0 or idx == total_members - 1:
                            logger.info(f"Extracting: {idx+1}/{total_members} ({extracted_count} ok, {failed_count} failed)")
                        
                        # Extract to the long path version on Windows
                        if os.name == 'nt':
                            zf.extract(member, temp_path_long)
                        else:
                            zf.extract(member, temp_path)
                        extracted_count += 1
                    except Exception as member_err:
                        # Log but continue - some files might have path issues
                        failed_count += 1
                        if failed_count <= 10:  # Only log first 10 failures
                            logger.warning(f"Failed to extract '{member}': {member_err}")
                        elif failed_count == 11:
                            logger.warning(f"... (suppressing further extraction errors)")
                
                if failed_count > 0:
                    logger.warning(f"Extraction completed with {failed_count} failures out of {total_members} files")
                
                if extracted_count == 0:
                    logger.error(f"Failed to extract any files from {zip_path.name}")
                    # Clean up temp directory on total failure
                    try:
                        shutil.rmtree(temp_path)
                    except Exception:
                        pass
                    return None
                
                logger.info(f"Successfully extracted {extracted_count}/{total_members} items from {zip_path.name}")
                return temp_path  # Return the regular path (not the \\?\ version)
        except Exception as e:
            logger.error(f"Failed to open or extract zip file {zip_path}: {e}")
            # Clean up temp directory on failure
            try:
                shutil.rmtree(temp_path)
            except Exception:
                pass
            return None
    
    def cleanup_temp_folders(self):
        """Clean up temporary folders created for zip extraction."""
        if self.should_cleanup_temp_folders:
            if self.temp_folder1:
                try:
                    logger.info(f"Cleaning up temporary folder 1: {self.temp_folder1}")
                    shutil.rmtree(self.temp_folder1)
                    self.temp_folder1 = None
                except Exception as e:
                    logger.warning(f"Failed to clean up temp folder 1: {e}")
            
            if self.temp_folder2:
                try:
                    logger.info(f"Cleaning up temporary folder 2: {self.temp_folder2}")
                    shutil.rmtree(self.temp_folder2)
                    self.temp_folder2 = None
                except Exception as e:
                    logger.warning(f"Failed to clean up temp folder 2: {e}")
    
    def _extract_naming_patterns(self, filename):
        """Extract meaningful parts from filename for better matching."""
        # Remove extension
        name_without_ext = os.path.splitext(filename)[0]
        
        # Common patterns in jar/file names
        patterns = {
            'version': re.findall(r'\d+\.\d+(?:\.\d+)*(?:-\w+)*', name_without_ext),
            'build_number': re.findall(r'build-?\d+|b\d+', name_without_ext, re.IGNORECASE),
            'snapshot': re.findall(r'snapshot', name_without_ext, re.IGNORECASE),
            'timestamp': re.findall(r'\d{8}|\d{14}|\d{4}-\d{2}-\d{2}', name_without_ext),
            'base_name': re.sub(r'[-_]\d+\.\d+.*$|[-_]build.*$|[-_]\d{8}.*$|[-_]\d{14}.*$', '', name_without_ext, flags=re.IGNORECASE)
        }
        
        return patterns
    
    def _calculate_similarity(self, name1, name2):
        """Calculate similarity between two filenames using multiple methods."""
        # Direct string similarity
        direct_similarity = SequenceMatcher(None, name1.lower(), name2.lower()).ratio()
        
        # Pattern-based similarity
        patterns1 = self._extract_naming_patterns(name1)
        patterns2 = self._extract_naming_patterns(name2)
        
        # Base name similarity (most important)
        base_similarity = SequenceMatcher(None, patterns1['base_name'].lower(), patterns2['base_name'].lower()).ratio()
        
        # Combined score (weighted)
        combined_similarity = (direct_similarity * 0.4) + (base_similarity * 0.6)
        
        return combined_similarity
    
    def _get_file_categorization(self, folder_path):
        """Categorize files by type (jar vs other) from a folder.
        
        Returns dictionaries mapping filename -> list of relative paths.
        This handles cases where the same filename appears in multiple subdirectories.
        """
        jar_files = {}  # filename -> [relative_path1, relative_path2, ...]
        other_files = {}  # filename -> [relative_path1, relative_path2, ...]
        
        try:
            for root, dirs, filenames in os.walk(folder_path):
                for filename in filenames:
                    full_path = Path(root) / filename
                    relative_path = str(full_path.relative_to(folder_path)).replace('\\', '/')
                    
                    if filename.lower().endswith('.jar'):
                        if filename not in jar_files:
                            jar_files[filename] = []
                        jar_files[filename].append(relative_path)
                    else:
                        if filename not in other_files:
                            other_files[filename] = []
                        other_files[filename].append(relative_path)
        except Exception as e:
            logger.error(f"Error scanning folder {folder_path}: {e}")
        
        return jar_files, other_files
    
    def _find_potential_matches(self, target_name, candidate_files):
        """Find potential matches for a file name using similarity scoring."""
        matches = []
        
        for candidate_name in candidate_files.keys():
            similarity = self._calculate_similarity(target_name, candidate_name)
            if similarity >= self.similarity_threshold:
                matches.append((candidate_name, similarity))
        
        # Sort by similarity score (highest first)
        matches.sort(key=lambda x: x[1], reverse=True)
        return matches
    
    def compare_naming_conventions(self):
        """Compare naming conventions between two folders."""
        import time
        self.comparison_start_time = time.time()
        self.comparison_error = None
        
        logger.info(f"Comparing naming conventions: {self.folder1_path} vs {self.folder2_path}")
        
        # Store original paths for reporting
        original_folder1_path = self.folder1_path
        original_folder2_path = self.folder2_path
        
        # Handle network paths by copying to local if enabled
        folder1_to_compare = self.folder1_path
        folder2_to_compare = self.folder2_path
        
        if self.use_local_copy and self._is_network_path(self.folder1_path):
            logger.info("Folder 1 is on network path, copying to local temporary directory...")
            local_folder1 = self._copy_network_folder_to_local(self.folder1_path)
            if local_folder1:
                self.temp_folder1 = local_folder1
                folder1_to_compare = local_folder1
                logger.info(f"Using local copy for folder 1: {local_folder1}")
            else:
                logger.error("Failed to copy folder 1 to local, aborting comparison")
                self.results = []
                return []
        
        if self.use_local_copy and self._is_network_path(self.folder2_path):
            logger.info("Folder 2 is on network path, copying to local temporary directory...")
            local_folder2 = self._copy_network_folder_to_local(self.folder2_path)
            if local_folder2:
                self.temp_folder2 = local_folder2
                folder2_to_compare = local_folder2
                logger.info(f"Using local copy for folder 2: {local_folder2}")
            else:
                logger.error("Failed to copy folder 2 to local, aborting comparison")
                if self.should_cleanup_temp_folders:
                    self.cleanup_temp_folders()
                self.results = []
                return []
        
        # Check if paths are zip files and extract them if needed
        temp_extract_folder1 = None
        temp_extract_folder2 = None
        
        # Check if folder1 is a zip file
        if _path_exists(folder1_to_compare) and _path_is_file(folder1_to_compare) and folder1_to_compare.suffix.lower() in ['.zip', '.jar', '.war', '.ear']:
            logger.info(f"Folder 1 is a zip archive, extracting: {folder1_to_compare}")
            temp_extract_folder1 = self._extract_zip_to_temp(folder1_to_compare)
            if temp_extract_folder1:
                # If we already copied to local, clean up that temp folder
                if self.temp_folder1 and folder1_to_compare == self.temp_folder1:
                    try:
                        shutil.rmtree(self.temp_folder1)
                    except Exception:
                        pass
                self.temp_folder1 = temp_extract_folder1
                folder1_to_compare = temp_extract_folder1
                logger.info(f"Using extracted folder 1: {folder1_to_compare}")
            else:
                logger.error("Failed to extract folder 1 zip file, aborting comparison")
                if self.should_cleanup_temp_folders:
                    self.cleanup_temp_folders()
                self.results = []
                return []
        
        # Check if folder2 is a zip file
        if _path_exists(folder2_to_compare) and _path_is_file(folder2_to_compare) and folder2_to_compare.suffix.lower() in ['.zip', '.jar', '.war', '.ear']:
            logger.info(f"Folder 2 is a zip archive, extracting: {folder2_to_compare}")
            temp_extract_folder2 = self._extract_zip_to_temp(folder2_to_compare)
            if temp_extract_folder2:
                # If we already copied to local, clean up that temp folder
                if self.temp_folder2 and folder2_to_compare == self.temp_folder2:
                    try:
                        shutil.rmtree(self.temp_folder2)
                    except Exception:
                        pass
                self.temp_folder2 = temp_extract_folder2
                folder2_to_compare = temp_extract_folder2
                logger.info(f"Using extracted folder 2: {folder2_to_compare}")
            else:
                logger.error("Failed to extract folder 2 zip file, aborting comparison")
                if self.should_cleanup_temp_folders:
                    self.cleanup_temp_folders()
                self.results = []
                return []
        
        # Check if folders exist
        if not _path_exists(folder1_to_compare):
            logger.error(f"Folder 1 does not exist: {folder1_to_compare}")
            if self.should_cleanup_temp_folders:
                self.cleanup_temp_folders()
            self.results = []
            return []
        
        if not _path_exists(folder2_to_compare):
            logger.error(f"Folder 2 does not exist: {folder2_to_compare}")
            if self.should_cleanup_temp_folders:
                self.cleanup_temp_folders()
            self.results = []
            return []
        
        # Get file categorizations
        logger.info("Scanning folder 1 for files...")
        self.jar_files_folder1, self.other_files_folder1 = self._get_file_categorization(folder1_to_compare)
        logger.info("Scanning folder 2 for files...")
        self.jar_files_folder2, self.other_files_folder2 = self._get_file_categorization(folder2_to_compare)
        
        logger.info(f"Found {len(self.jar_files_folder1)} unique jar filenames and {len(self.other_files_folder1)} unique other filenames in folder 1")
        logger.info(f"Found {len(self.jar_files_folder2)} unique jar filenames and {len(self.other_files_folder2)} unique other filenames in folder 2")
        
        # Check if folders are empty
        if len(self.jar_files_folder1) == 0 and len(self.other_files_folder1) == 0:
            logger.warning(f"Folder 1 appears to be empty or inaccessible: {folder1_to_compare}")
        if len(self.jar_files_folder2) == 0 and len(self.other_files_folder2) == 0:
            logger.warning(f"Folder 2 appears to be empty or inaccessible: {folder2_to_compare}")
        
        # Debug: Check for duplicates
        total_jar_instances_f1 = sum(len(paths) for paths in self.jar_files_folder1.values())
        total_jar_instances_f2 = sum(len(paths) for paths in self.jar_files_folder2.values())
        if total_jar_instances_f1 > len(self.jar_files_folder1):
            logger.info(f"Note: Folder 1 has {total_jar_instances_f1} JAR file instances but only {len(self.jar_files_folder1)} unique names (duplicates detected)")
        if total_jar_instances_f2 > len(self.jar_files_folder2):
            logger.info(f"Note: Folder 2 has {total_jar_instances_f2} JAR file instances but only {len(self.jar_files_folder2)} unique names (duplicates detected)")
        
        self.results = []
        processed_files = set()
        
        # Calculate total files for progress tracking
        all_jar_files = set(self.jar_files_folder1.keys()) | set(self.jar_files_folder2.keys())
        all_other_files = set(self.other_files_folder1.keys()) | set(self.other_files_folder2.keys())
        self.total_files = len(all_jar_files) + len(all_other_files)
        self.processed_files = 0
        logger.info(f"Total unique filenames to analyze: {self.total_files}")
        logger.info(f"  - Unique JAR filenames: {len(all_jar_files)}")
        logger.info(f"  - Unique other filenames: {len(all_other_files)}")
        
        # Process jar files from both folders
        for jar_name in all_jar_files:
            if jar_name in processed_files:
                continue
            
            self.current_file = jar_name
            self._update_progress(jar_name)
            
            result = NamingConventionResult(jar_name, 'jar')
            
            if jar_name in self.jar_files_folder1:
                result.exists_in_folder1 = True
                paths = self.jar_files_folder1[jar_name]
                # Ensure paths is a list
                if not isinstance(paths, list):
                    paths = [paths] if paths else []
                result.relative_paths1 = paths
                result.relative_path1 = paths[0] if paths else ""
            
            if jar_name in self.jar_files_folder2:
                result.exists_in_folder2 = True
                paths = self.jar_files_folder2[jar_name]
                # Ensure paths is a list
                if not isinstance(paths, list):
                    paths = [paths] if paths else []
                result.relative_paths2 = paths
                result.relative_path2 = paths[0] if paths else ""
            
            result.is_exact_match = result.exists_in_folder1 and result.exists_in_folder2
            
            # Find potential matches if not exact match
            if not result.is_exact_match:
                if result.exists_in_folder1 and not result.exists_in_folder2:
                    # File exists in folder1, find similar files in folder2
                    matches = self._find_potential_matches(jar_name, self.jar_files_folder2)
                    result.potential_matches = [match[0] for match in matches[:3]]  # Top 3 matches
                    result.similarity_scores = [match[1] for match in matches[:3]]
                    
                elif result.exists_in_folder2 and not result.exists_in_folder1:
                    # File exists in folder2, find similar files in folder1
                    matches = self._find_potential_matches(jar_name, self.jar_files_folder1)
                    result.potential_matches = [match[0] for match in matches[:3]]  # Top 3 matches
                    result.similarity_scores = [match[1] for match in matches[:3]]
            
            self.results.append(result)
            processed_files.add(jar_name)
            self.processed_files += 1
            self._update_progress(jar_name)
        
        # Process other files from both folders
        for file_name in all_other_files:
            if file_name in processed_files:
                continue
            
            self.current_file = file_name
            self._update_progress(file_name)
            
            result = NamingConventionResult(file_name, 'other')
            
            if file_name in self.other_files_folder1:
                result.exists_in_folder1 = True
                paths = self.other_files_folder1[file_name]
                # Ensure paths is a list
                if not isinstance(paths, list):
                    paths = [paths] if paths else []
                result.relative_paths1 = paths
                result.relative_path1 = paths[0] if paths else ""
            
            if file_name in self.other_files_folder2:
                result.exists_in_folder2 = True
                paths = self.other_files_folder2[file_name]
                # Ensure paths is a list
                if not isinstance(paths, list):
                    paths = [paths] if paths else []
                result.relative_paths2 = paths
                result.relative_path2 = paths[0] if paths else ""
            
            result.is_exact_match = result.exists_in_folder1 and result.exists_in_folder2
            
            # Find potential matches if not exact match
            if not result.is_exact_match:
                if result.exists_in_folder1 and not result.exists_in_folder2:
                    # File exists in folder1, find similar files in folder2
                    matches = self._find_potential_matches(file_name, self.other_files_folder2)
                    result.potential_matches = [match[0] for match in matches[:3]]  # Top 3 matches
                    result.similarity_scores = [match[1] for match in matches[:3]]
                    
                elif result.exists_in_folder2 and not result.exists_in_folder1:
                    # File exists in folder2, find similar files in folder1
                    matches = self._find_potential_matches(file_name, self.other_files_folder1)
                    result.potential_matches = [match[0] for match in matches[:3]]  # Top 3 matches
                    result.similarity_scores = [match[1] for match in matches[:3]]
            
            self.results.append(result)
            processed_files.add(file_name)
            self.processed_files += 1
            self._update_progress(file_name)
        
        # Log summary statistics
        exact_matches = [r for r in self.results if r.is_exact_match]
        missing_from_folder2 = [r for r in self.results if r.exists_in_folder1 and not r.exists_in_folder2]
        missing_from_folder1 = [r for r in self.results if r.exists_in_folder2 and not r.exists_in_folder1]
        
        logger.info("Naming convention comparison completed successfully")
        logger.info(f"Results Summary:")
        logger.info(f"  - Total results: {len(self.results)}")
        logger.info(f"  - Exact name matches: {len(exact_matches)}")
        logger.info(f"  - Missing from folder 2: {len(missing_from_folder2)}")
        logger.info(f"  - Missing from folder 1: {len(missing_from_folder1)}")
        
        # Clean up temp folders if created
        if self.should_cleanup_temp_folders:
            self.cleanup_temp_folders()
        return self.results
    
    def generate_naming_convention_report(self):
        """Generate a comprehensive naming convention comparison report."""
        if not self.results:
            return "No comparison results available."
        
        try:
            report = []
            report.append("=" * 80)
            report.append("NAMING CONVENTION COMPARISON REPORT")
            report.append("=" * 80)
            report.append(f"Folder 1: {self.folder1_path}")
            report.append(f"Folder 2: {self.folder2_path}")
            report.append(f"Comparison Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            report.append("")
            
            # Calculate statistics
            total_files = len(self.results)
            jar_results = [r for r in self.results if r.file_type == 'jar']
            other_results = [r for r in self.results if r.file_type == 'other']
            
            exact_matches = [r for r in self.results if r.is_exact_match]
            missing_from_folder2 = [r for r in self.results if r.exists_in_folder1 and not r.exists_in_folder2]
            missing_from_folder1 = [r for r in self.results if r.exists_in_folder2 and not r.exists_in_folder1]
            potential_mismatches = [r for r in self.results if not r.is_exact_match and r.potential_matches]
            
            # Summary statistics
            report.append("SUMMARY STATISTICS")
            report.append("-" * 50)
            report.append(f"Total files analyzed: {total_files}")
            report.append(f"  - JAR files: {len(jar_results)}")
            report.append(f"  - Other files: {len(other_results)}")
            report.append("")
            
            # Check for duplicate filenames
            duplicates_in_f1 = [r for r in self.results if len(r.relative_paths1) > 1]
            duplicates_in_f2 = [r for r in self.results if len(r.relative_paths2) > 1]
            if duplicates_in_f1 or duplicates_in_f2:
                report.append("⚠️  DUPLICATE FILENAMES DETECTED:")
                if duplicates_in_f1:
                    report.append(f"  - Folder 1 has {len(duplicates_in_f1)} filename(s) appearing in multiple locations")
                if duplicates_in_f2:
                    report.append(f"  - Folder 2 has {len(duplicates_in_f2)} filename(s) appearing in multiple locations")
                report.append("  (See details in the sections below)")
                report.append("")
            
            report.append(f"Exact name matches: {len(exact_matches)}")
            report.append(f"Missing from folder 2: {len(missing_from_folder2)}")
            report.append(f"Missing from folder 1: {len(missing_from_folder1)}")
            report.append(f"Potential naming mismatches: {len(potential_mismatches)}")
            report.append("")
        
            # Exact matches section
            if exact_matches:
                report.append("EXACT NAME MATCHES (NO MISMATCH)")
            report.append("EXACT NAME MATCHES (NO MISMATCH)")
            report.append("=" * 50)
            
            jar_matches = [r for r in exact_matches if r.file_type == 'jar']
            other_matches = [r for r in exact_matches if r.file_type == 'other']
            
            if jar_matches:
                report.append(f"JAR Files with Exact Name Matches ({len(jar_matches)}):")
                report.append("-" * 40)
                for result in sorted(jar_matches, key=lambda x: x.file_name.lower()):
                    report.append(f"  [OK] {result.file_name}")
                    # Show all paths if there are duplicates
                    if len(result.relative_paths1) > 1 or len(result.relative_paths2) > 1:
                        report.append(f"    Found {len(result.relative_paths1)} instance(s) in folder 1, {len(result.relative_paths2)} in folder 2:")
                        for path1 in result.relative_paths1:
                            report.append(f"      Folder 1: {path1}")
                        for path2 in result.relative_paths2:
                            report.append(f"      Folder 2: {path2}")
                    elif result.relative_path1 != result.relative_path2:
                        report.append(f"    Path 1: {result.relative_path1}")
                        report.append(f"    Path 2: {result.relative_path2}")
                report.append("")
            
            if other_matches:
                report.append(f"Other Files with Exact Name Matches ({len(other_matches)}):")
                report.append("-" * 40)
                for result in sorted(other_matches, key=lambda x: x.file_name.lower()):
                    report.append(f"  [OK] {result.file_name}")
                    # Show all paths if there are duplicates
                    if len(result.relative_paths1) > 1 or len(result.relative_paths2) > 1:
                        report.append(f"    Found {len(result.relative_paths1)} instance(s) in folder 1, {len(result.relative_paths2)} in folder 2:")
                        for path1 in result.relative_paths1:
                            report.append(f"      Folder 1: {path1}")
                        for path2 in result.relative_paths2:
                            report.append(f"      Folder 2: {path2}")
                    elif result.relative_path1 != result.relative_path2:
                        report.append(f"    Path 1: {result.relative_path1}")
                        report.append(f"    Path 2: {result.relative_path2}")
                report.append("")
        
            # Missing files section
            if missing_from_folder2:
                report.append("MISSING FROM FOLDER 2")
            report.append("MISSING FROM FOLDER 2")
            report.append("=" * 50)
            
            jar_missing = [r for r in missing_from_folder2 if r.file_type == 'jar']
            other_missing = [r for r in missing_from_folder2 if r.file_type == 'other']
            
            if jar_missing:
                report.append(f"Missing JAR Files ({len(jar_missing)}):")
                report.append("-" * 30)
                for result in sorted(jar_missing, key=lambda x: x.file_name.lower()):
                    report.append(f"  [X] {result.file_name}")
                    if len(result.relative_paths1) > 1:
                        report.append(f"    Found {len(result.relative_paths1)} instance(s) in folder 1:")
                        for path1 in result.relative_paths1:
                            report.append(f"      {path1}")
                    else:
                        report.append(f"    Path in folder 1: {result.relative_path1}")
                    if result.potential_matches:
                        report.append("    Potential similar files in folder 2:")
                        for i, (match, score) in enumerate(zip(result.potential_matches, result.similarity_scores)):
                            report.append(f"      {i+1}. {match} (similarity: {score:.2f})")
                report.append("")
            
            if other_missing:
                report.append(f"Missing Other Files ({len(other_missing)}):")
                report.append("-" * 30)
                for result in sorted(other_missing, key=lambda x: x.file_name.lower()):
                    report.append(f"  [X] {result.file_name}")
                    if len(result.relative_paths1) > 1:
                        report.append(f"    Found {len(result.relative_paths1)} instance(s) in folder 1:")
                        for path1 in result.relative_paths1:
                            report.append(f"      {path1}")
                    else:
                        report.append(f"    Path in folder 1: {result.relative_path1}")
                    if result.potential_matches:
                        report.append("    Potential similar files in folder 2:")
                        for i, (match, score) in enumerate(zip(result.potential_matches, result.similarity_scores)):
                            report.append(f"      {i+1}. {match} (similarity: {score:.2f})")
                report.append("")
        
            if missing_from_folder1:
                report.append("MISSING FROM FOLDER 1")
            report.append("MISSING FROM FOLDER 1")
            report.append("=" * 50)
            
            jar_missing = [r for r in missing_from_folder1 if r.file_type == 'jar']
            other_missing = [r for r in missing_from_folder1 if r.file_type == 'other']
            
            if jar_missing:
                report.append(f"Missing JAR Files ({len(jar_missing)}):")
                report.append("-" * 30)
                for result in sorted(jar_missing, key=lambda x: x.file_name.lower()):
                    report.append(f"  [X] {result.file_name}")
                    if len(result.relative_paths2) > 1:
                        report.append(f"    Found {len(result.relative_paths2)} instance(s) in folder 2:")
                        for path2 in result.relative_paths2:
                            report.append(f"      {path2}")
                    else:
                        report.append(f"    Path in folder 2: {result.relative_path2}")
                    if result.potential_matches:
                        report.append("    Potential similar files in folder 1:")
                        for i, (match, score) in enumerate(zip(result.potential_matches, result.similarity_scores)):
                            report.append(f"      {i+1}. {match} (similarity: {score:.2f})")
                report.append("")
            
            if other_missing:
                report.append(f"Missing Other Files ({len(other_missing)}):")
                report.append("-" * 30)
                for result in sorted(other_missing, key=lambda x: x.file_name.lower()):
                    report.append(f"  [X] {result.file_name}")
                    if len(result.relative_paths2) > 1:
                        report.append(f"    Found {len(result.relative_paths2)} instance(s) in folder 2:")
                        for path2 in result.relative_paths2:
                            report.append(f"      {path2}")
                    else:
                        report.append(f"    Path in folder 2: {result.relative_path2}")
                    if result.potential_matches:
                        report.append("    Potential similar files in folder 1:")
                        for i, (match, score) in enumerate(zip(result.potential_matches, result.similarity_scores)):
                            report.append(f"      {i+1}. {match} (similarity: {score:.2f})")
                report.append("")
        
            # Analysis insights
            report.append("ANALYSIS INSIGHTS")
            report.append("=" * 50)
        
            if len(exact_matches) == total_files:
                report.append("[OK] All files have exact name matches - no naming convention issues detected!")
            elif len(exact_matches) / total_files > 0.8:
                report.append(f"[OK] High compatibility: {len(exact_matches)/total_files:.1%} of files have exact matches")
            else:
                report.append(f"[WARNING] Naming inconsistencies detected: Only {len(exact_matches)/total_files:.1%} exact matches")
            
            if potential_mismatches:
                report.append(f"[WARNING] {len(potential_mismatches)} files may have naming convention mismatches")
                high_similarity_matches = [r for r in potential_mismatches if r.similarity_scores and max(r.similarity_scores) > 0.8]
                if high_similarity_matches:
                    report.append(f"  - {len(high_similarity_matches)} files have high similarity matches (>80%)")
        
            report.append("")
            return "\n".join(report)
        except Exception as e:
            logger.error(f"Error generating naming convention report: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            # Return a basic error report
            error_report = []
            error_report.append("=" * 80)
            error_report.append("NAMING CONVENTION COMPARISON REPORT - ERROR")
            error_report.append("=" * 80)
            error_report.append(f"An error occurred while generating the report: {e}")
            error_report.append("")
            error_report.append(f"Total results collected: {len(self.results)}")
            error_report.append("Please check the logs for details.")
            error_report.append("=" * 80)
            return "\n".join(error_report)
    
    def save_naming_convention_report(self, output_file):
        """Save naming convention report to file and HTML."""
        report = self.generate_naming_convention_report()
        try:
            with open(output_file, 'w', encoding='utf-8') as f:
                f.write(report)
            logger.info(f"Naming convention report saved to: {output_file}")
        except Exception as e:
            logger.error(f"Error saving text report to {output_file}: {e}")
        
        try:
            html = self.generate_naming_convention_html_report()
            html_file = Path(output_file).with_suffix('.html')
            with open(html_file, 'w', encoding='utf-8') as hf:
                hf.write(html)
            logger.info(f"Naming convention HTML report saved to: {html_file}")
        except Exception as e:
            logger.error(f"Error saving HTML report: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
    
    def generate_naming_convention_html_report(self) -> str:
        style = """
        <style>
        body{font-family:system-ui,Arial,sans-serif;padding:16px}
        h1{margin:0 0 4px 0}
        small{color:#6b7280}
        h2{color:#0d6efd}
        ul{margin:6px 0 16px 20px}
        .error{color:#ef4444;background:#fee;padding:12px;border:1px solid #fca5a5;border-radius:8px;margin:16px 0;}
        </style>
        """
        header = f"<h1>Naming Convention Comparison</h1><small>Folder 1: {html.escape(str(self.folder1_path))}<br/>Folder 2: {html.escape(str(self.folder2_path))}<br/>Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</small><p><a href='/'>← Back to main</a></p>"
        
        # Check if results are empty
        if not self.results:
            error_section = """
            <div class="error">
                <h2>No Results Available</h2>
                <p>The comparison did not produce any results. This could be due to:</p>
                <ul>
                    <li>One or both folders do not exist or are not accessible</li>
                    <li>Network path access issues</li>
                    <li>Permission denied errors</li>
                    <li>Comparison was aborted due to an error</li>
                </ul>
                <p>Please check the server logs for more details.</p>
            </div>
            """
            return inject_report_section_controls(
                f"<html><head><meta charset='utf-8'/><title>Naming Convention Report</title>{style}</head><body>{header}{error_section}</body></html>"
            )
        
        exact_matches = [r for r in self.results if r.is_exact_match]
        missing_from_folder2 = [r for r in self.results if r.exists_in_folder1 and not r.exists_in_folder2]
        missing_from_folder1 = [r for r in self.results if r.exists_in_folder2 and not r.exists_in_folder1]
        sections = []
        if exact_matches:
            items = "".join(f"<li>{html.escape(r.file_name)}</li>" for r in sorted(exact_matches, key=lambda x: x.file_name.lower()))
            sections.append(f'<div class="section" id="exact-matches"><h2>Exact name matches</h2><ul>{items}</ul></div>')
        if missing_from_folder2:
            items = "".join(f"<li>{html.escape(r.file_name)}</li>" for r in sorted(missing_from_folder2, key=lambda x: x.file_name.lower()))
            sections.append(f'<div class="section" id="missing-folder2"><h2>Missing from Folder 2</h2><ul>{items}</ul></div>')
        if missing_from_folder1:
            items = "".join(f"<li>{html.escape(r.file_name)}</li>" for r in sorted(missing_from_folder1, key=lambda x: x.file_name.lower()))
            sections.append(f'<div class="section" id="missing-folder1"><h2>Missing from Folder 1</h2><ul>{items}</ul></div>')
        return inject_report_section_controls(
            f"<html><head><meta charset='utf-8'/><title>Naming Convention Report</title>{style}</head><body>{header}{''.join(sections)}</body></html>"
        )

def compare_ascf_patches(base_path=r"\\svinayakmv2\share", max_retries=3, retry_delay=1.0, use_local_copy=True, cleanup_temp_folders=True, ignore_timestamp_only=False, show_missing_only=True, exclude_missing_folder2=False, rename_folder2_only=False, rename_suffix="_renamed"):
    folder1_path = r"C:\\Users\\SVINAYAK\\ASCF_HF_10240pb01hf04"
    folder2_path = r"C:\\Users\\SVINAYAK\\ASCF_HF_10240pb01hf05"

    # Derive names and base_path for consistent display
    folder1_name = os.path.basename(folder1_path)
    folder2_name = os.path.basename(folder2_path)
    base_path = os.path.dirname(folder1_path)
    
    print(f"Comparing ASCF patch folders:")
    print(f"  {folder1_name}")
    print(f"  {folder2_name}")
    print(f"Location: {base_path}")
    print(f"Network retry settings: {max_retries} retries, {retry_delay}s initial delay")
    print(f"Access mode: {'Local copy ENABLED' if use_local_copy else 'Direct network access ENABLED'}")
    if not cleanup_temp_folders:
        print("Temporary folders will be kept after comparison")
    if ignore_timestamp_only:
        print("Timestamp-only differences: IGNORED (files with identical content but different timestamps will be treated as identical)")
    if show_missing_only:
        print("Report mode: Only showing files missing in Folder 2 (different files are not displayed)")
    else:
        print("Report mode: Showing all differences (missing files and content differences)")
    print()
    
    comparer = FolderComparer(folder1_path, folder2_path)
    comparer.max_retries = max_retries
    comparer.retry_delay = retry_delay
    comparer.use_local_copy = use_local_copy
    comparer.should_cleanup_temp_folders = cleanup_temp_folders
    comparer.ignore_timestamp_only_differences = ignore_timestamp_only
    comparer.show_missing_only = show_missing_only
    comparer.exclude_missing_from_folder2 = exclude_missing_folder2
    comparer.rename_folder2_only_files = rename_folder2_only
    comparer.folder2_rename_suffix = rename_suffix
    results = comparer.compare_folders()
    
    if results:
        print(comparer.generate_report())
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_filename = f"ascf_patch_comparison_{timestamp}.txt"
        comparer.save_report(report_filename)
        print(f"Report saved to: {report_filename}")
    else:
        print("Comparison failed. Check the folder paths and try again.")


class ArchiveEntryComparison:
    def __init__(self, archive_relative_path: str, archive_type: str):
        self.archive_relative_path = archive_relative_path
        self.archive_type = archive_type  # 'ear' | 'war' | 'zip' | 'rpm'
        self.exists_in_folder1 = False
        self.exists_in_folder2 = False
        self.members_folder1 = set()
        self.members_folder2 = set()
        self.members_folder1_normalized = set()  # Normalized member names
        self.members_folder2_normalized = set()  # Normalized member names
        self.member_differs = False
        self.members_meta_folder1: dict[str, tuple[int, int]] = {}
        self.members_meta_folder2: dict[str, tuple[int, int]] = {}
        self.content_differs = False
        self.content_diff_member_names: list[str] = []
        self.error_folder1 = ""
        self.error_folder2 = ""


class ArchiveContentsComparer:
    def __init__(self, folder1_path: str, folder2_path: str):
        self.folder1_path = Path(folder1_path)
        self.folder2_path = Path(folder2_path)
        self.results: list[ArchiveEntryComparison] = []
        # Deep mode: also inspect nested .jar/.war inside archives
        self.deep_nested_archives = True
        self.suppress_warnings = False
        # Temp folder tracking for cleanup
        self.temp_folder1 = None
        self.temp_folder2 = None
        self.should_cleanup_temp_folders = True
        # Network path handling
        self.max_retries = 3
        self.retry_delay = 1.0  # seconds
        self.network_timeout = 30  # seconds
        self.use_local_copy = False  # Disable local copy by default - work directly with network paths
        # Progress tracking
        self.total_files = 0
        self.processed_files = 0
        self.current_file = ""
        self.progress_percentage = 0.0
        self.estimated_time_remaining = 0
        self.comparison_start_time = None
        self.comparison_error = None
    
    def _is_network_path(self, file_path):
        """Check if the path is a UNC network path."""
        path_str = str(file_path)
        return path_str.startswith('\\\\') or (len(path_str) > 1 and path_str[1] == ':' and not os.path.exists(path_str[:3]))
    
    def _check_network_connectivity(self, file_path):
        """Check if the network path is accessible."""
        if not self._is_network_path(file_path):
            return True
        
        try:
            # Extract server name from UNC path
            path_str = str(file_path)
            if path_str.startswith('\\\\'):
                parts = path_str.split('\\')
                if len(parts) >= 3:
                    server_name = parts[2]
                    # Try to connect to the server
                    socket.setdefaulttimeout(10)  # 10 second timeout for connectivity check
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    try:
                        result = sock.connect_ex((server_name, 445))  # SMB port
                        sock.close()
                        return result == 0
                    except:
                        sock.close()
                        return False
        except Exception as e:
            logger.warning(f"Network connectivity check failed for {file_path}: {e}")
            return False
        return True
    
    def _retry_operation(self, operation, file_path, operation_name):
        """Retry an operation with exponential backoff for network errors."""
        last_error = None
        
        for attempt in range(self.max_retries):
            try:
                return operation(file_path)
            except (OSError, IOError) as e:
                last_error = e
                error_code = getattr(e, 'winerror', getattr(e, 'errno', 0))
                
                # Check if it's a network-related error
                if error_code in [53, 59, 67, 1231, 1326]:  # Common network error codes
                    if attempt < self.max_retries - 1:
                        wait_time = self.retry_delay * (2 ** attempt)  # Exponential backoff
                        logger.info(f"Network error (attempt {attempt + 1}/{self.max_retries}) for {file_path}, retrying in {wait_time}s...")
                        time.sleep(wait_time)
                        continue
                    else:
                        logger.warning(f"{operation_name} failed after {self.max_retries} attempts for {file_path}: {e}")
                        break
                else:
                    # Non-network error, don't retry
                    logger.warning(f"{operation_name} failed for {file_path}: {e}")
                    break
            except Exception as e:
                last_error = e
                logger.warning(f"{operation_name} failed for {file_path}: {e}")
                break
        
        return None
    
    def _copy_network_folder_to_local(self, source_folder):
        """Copy a network folder to a local temporary directory for faster access."""
        temp_path = None
        try:
            temp_dir = mkdtemp_compare(prefix="net_")
            temp_path = Path(temp_dir)
            logger.info(f"Copying network folder to local: {source_folder} -> {temp_path}")
            
            # Use robocopy on Windows for better network copy performance
            if os.name == 'nt':
                # robocopy /E /Z /R:3 /W:5 /MT:8 source dest
                # /E = copy subdirectories including empty ones
                # /Z = restartable mode
                # /R:3 = retry 3 times
                # /W:5 = wait 5 seconds between retries
                # /MT:8 = multi-threaded (8 threads)
                cmd = ['robocopy', str(source_folder), str(temp_path), '/E', '/Z', '/R:3', '/W:5', '/MT:8', '/NP']
                result = subprocess.run(cmd, capture_output=True, text=True)
                # robocopy exit codes: 0-7 are success (various levels of success)
                if result.returncode >= 8:
                    logger.error(f"robocopy failed with exit code {result.returncode}")
                    logger.error(f"stderr: {result.stderr}")
                    # Try shutil.copytree as fallback
                    logger.info("Trying fallback copy method (shutil.copytree)...")
                    shutil.rmtree(temp_path)  # Clean up failed robocopy attempt
                    shutil.copytree(source_folder, temp_path)
            else:
                # On non-Windows systems, use shutil.copytree
                shutil.copytree(source_folder, temp_path)
            
            logger.info(f"Successfully copied network folder to local: {temp_path}")
            return temp_path
        except Exception as e:
            logger.error(f"Failed to copy network folder to local: {e}")
            # Clean up temp directory on failure
            try:
                if temp_path and temp_path.exists():
                    shutil.rmtree(temp_path)
            except Exception:
                pass
            return None
    
    def cleanup_temp_folders(self):
        """Clean up temporary folders created during comparison."""
        if self.should_cleanup_temp_folders:
            if self.temp_folder1:
                try:
                    logger.info(f"Cleaning up temporary folder 1: {self.temp_folder1}")
                    shutil.rmtree(self.temp_folder1)
                    self.temp_folder1 = None
                except Exception as e:
                    logger.warning(f"Failed to clean up temp folder 1: {e}")
            
            if self.temp_folder2:
                try:
                    logger.info(f"Cleaning up temporary folder 2: {self.temp_folder2}")
                    shutil.rmtree(self.temp_folder2)
                    self.temp_folder2 = None
                except Exception as e:
                    logger.warning(f"Failed to clean up temp folder 2: {e}")
    
    def _update_progress(self, current_file=""):
        """Update progress tracking information."""
        if self.total_files > 0:
            self.progress_percentage = (self.processed_files / self.total_files) * 100
            
            # Estimate time remaining
            if self.comparison_start_time and self.processed_files > 0:
                elapsed_time = time.time() - self.comparison_start_time
                avg_time_per_file = elapsed_time / self.processed_files
                remaining_files = self.total_files - self.processed_files
                self.estimated_time_remaining = int(avg_time_per_file * remaining_files)
        
        self.current_file = current_file
    
    def _normalize_archive_filename(self, filename: str) -> str:
        """
        Normalize archive file names by stripping common version/build/timestamp suffixes.
        Examples:
          lightsaber-10.2.0.3-67024.war -> lightsaber.war
          abc_1.2.3-SNAPSHOT.jar -> abc.jar
          comp-build123.zip -> comp.zip
          winpack__V10_0_1_22.jar -> winpack.jar
          SDK_CRM_10.0.1.22_linux64_20251120_2043.jar -> SDK_CRM_linux64.jar
          module_unsigned.jar -> module.jar
          AmdocsCRM_10.0.1.22.ear -> AmdocsCRM.ear
        """
        base, ext = os.path.splitext(filename)
        
        # Strip "unsigned" suffix for unsigned JAR files: _unsigned, -unsigned
        base = re.sub(r'[-_]unsigned$', '', base, flags=re.IGNORECASE)
        
        # Strip CRM-specific version patterns: __V10_0_1_22, __V10_0_1_21, etc.
        base = re.sub(r'__+V\d+(?:[_\.]\d+)*', '', base, flags=re.IGNORECASE)
        
        # Strip version patterns with dots and underscores: _10.0.1.22_, _10.0.1.21_
        base = re.sub(r'_\d+\.\d+(?:\.\d+)*(?:\.\d+)*_', '_', base, flags=re.IGNORECASE)
        
        # Strip version-like suffixes: -1.2.3, _1.2.3, optionally with qualifiers (-SNAPSHOT, -rc1, etc.)
        base = re.sub(r'[-_]\d+\.\d+(?:\.\d+)*(?:-[A-Za-z0-9]+)*$', '', base, flags=re.IGNORECASE)
        
        # Strip trailing build/timestamp identifiers: -67024, _build123, -20240101, -20240101123456, _20251120_2043
        base = re.sub(r'[-_](?:b?\d{3,}|build-?\d+|\d{8}(?:_\d{4})?|\d{14})$', '', base, flags=re.IGNORECASE)
        
        # Clean up any double underscores or trailing underscores left after normalization
        base = re.sub(r'_+', '_', base)
        base = base.strip('_-')
        
        return f"{base}{ext.lower()}"
    
    def _normalize_inner_archive_marker(self, member_path: str) -> str:
        """
        Given a path within an outer archive pointing to an inner archive file,
        normalize only the last segment (file name) and preserve directories.
        Handles .jar, .war, .ear, .zip and also .pack.gz compressed versions.
        """
        parts = member_path.replace('\\', '/').split('/')
        if not parts:
            return member_path
        name = parts[-1]
        lower = name.lower()
        
        # Handle .pack.gz compressed JARs (Pack200 format)
        if lower.endswith('.pack.gz'):
            # Extract the base name (e.g., "file.jar" from "file.jar.pack.gz")
            base_name = name[:-8]  # Remove ".pack.gz"
            if base_name.lower().endswith(('.jar', '.war', '.ear')):
                normalized_base = self._normalize_archive_filename(base_name)
                parts[-1] = normalized_base + '.pack.gz'
        elif lower.endswith(('.jar', '.war', '.zip', '.ear')):
            parts[-1] = self._normalize_archive_filename(name)
        
        return '/'.join(parts)
    
    def _normalize_member_set(self, members: set[str]) -> set[str]:
        """
        Normalize all member names in a set by applying normalization to archive filenames.
        This ensures version-specific JAR names are treated as identical.
        """
        normalized = set()
        for member in members:
            normalized_member = self._normalize_inner_archive_marker(member)
            normalized.add(normalized_member)
        return normalized

    def _process_nested_archive(self, outer_zip, member_name, normalized_name, names_set, meta_dict, depth=0, max_depth=5):
        """
        Recursively process nested archives (JAR/WAR/EAR inside ZIP/EAR/WAR/JAR).
        depth limits recursion to prevent infinite loops.
        """
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
                        # Use normalized outer archive marker so identical inner structures match across versioned names
                        norm_outer = self._normalize_inner_archive_marker(normalized_name)
                        full_path = f"{norm_outer}!/{inner_name}"
                        names_set.add(full_path)
                        if not inner_info.is_dir():
                            meta_dict[full_path] = (inner_info.file_size, inner_info.CRC)
                        
                        # Recursively process nested archives inside this one (EAR → WAR → JAR, etc.)
                        if inner_name.lower().endswith(('.jar', '.war', '.ear')):
                            try:
                                self._process_nested_archive(inner, inner_info.filename, full_path, names_set, meta_dict, depth + 1, max_depth)
                            except Exception:
                                # If we can't read deeper, just continue
                                pass
        except Exception as ie:
            # Record a best-effort error but continue
            norm_outer = self._normalize_inner_archive_marker(normalized_name)
            names_set.add(f"{norm_outer}!/[error-reading-inner:{ie.__class__.__name__}]")
    
    def _list_zip_members(self, archive_path: Path):
        try:
            # First try: standard zipfile reading
            try:
                with zipfile.ZipFile(archive_path, 'r') as zf:
                    names = set()
                    meta: dict[str, tuple[int, int]] = {}
                    infolist = zf.infolist()
                    total_members = len(infolist)
                    
                    # Update progress to show we're scanning
                    self.current_file = f"Scanning {archive_path.name} ({total_members} entries)..."
                    self._update_progress(self.current_file)
                    
                    for idx, info in enumerate(infolist):
                        try:
                            name = info.filename.replace('\\', '/').strip('/')
                            if not name:
                                continue
                            # Normalize directories by removing trailing '/'
                            if name.endswith('/'):
                                name = name[:-1]
                            names.add(name)
                            if not info.is_dir():
                                meta[name] = (info.file_size, info.CRC)
                            
                            # Update progress every 100 members
                            if idx % 100 == 0 or idx == total_members - 1:
                                self.current_file = f"Scanning {archive_path.name}: {idx+1}/{total_members} entries"
                                self._update_progress(self.current_file)
                            
                            # Optionally dive into nested archives (jar/war/ear) inside this archive
                            if self.deep_nested_archives and name.lower().endswith(('.jar', '.war', '.ear')):
                                self.current_file = f"Processing nested archive: {name}"
                                self._update_progress(self.current_file)
                                self._process_nested_archive(zf, info.filename, name, names, meta)
                        except Exception as member_err:
                            # Skip individual members that cause issues
                            logger.warning(f"Skipping member due to error: {member_err}")
                            continue
                    return names, meta, ""
            except Exception as zip_err:
                # If standard reading fails, try lenient mode using subprocess with jar command
                if "Corrupt" in str(zip_err) or "Bad" in str(zip_err):
                    logger.warning(f"Standard zip reading failed ({zip_err}), trying Java jar command...")
                    return self._list_zip_members_with_jar_command(archive_path)
                raise
        except Exception as e:
            return set(), {}, str(e)
    
    def _list_zip_members_with_jar_command(self, archive_path: Path):
        """Fallback: Use Java's jar command to list archive contents."""
        import subprocess
        try:
            # Try to list with jar command (if Java is available)
            result = subprocess.run(
                ['jar', 'tf', str(archive_path)],
                capture_output=True,
                text=True,
                timeout=60
            )
            
            if result.returncode == 0:
                names = set()
                meta = {}
                for line in result.stdout.splitlines():
                    name = line.strip().replace('\\', '/').strip('/')
                    if name and not name.endswith('/'):
                        names.add(name)
                        # Can't get size/CRC from jar tf, but at least we have the list
                        meta[name] = (0, 0)  # Placeholder
                
                logger.info(f"Successfully listed {len(names)} members using jar command")
                return names, meta, ""
            else:
                error_msg = f"jar command failed: {result.stderr}"
                logger.error(error_msg)
                return set(), {}, error_msg
                
        except FileNotFoundError:
            error_msg = "Java 'jar' command not found. Install Java JDK to read these JAR files."
            logger.error(error_msg)
            return set(), {}, error_msg
        except Exception as e:
            error_msg = f"jar command error: {e}"
            logger.error(error_msg)
            return set(), {}, error_msg

    def _extract_rpm_payload_fallback(self, rpm_path: Path):
        """
        Fallback method to extract RPM contents without rpmfile module.
        Extracts to a temporary directory and lists files.
        """
        try:
            temp_dir = Path(mkdtemp_compare(prefix="rpm_extract_"))
            
            # Try using rpm2cpio + cpio if available (Linux/Unix systems)
            try:
                # rpm2cpio outputs cpio archive to stdout
                rpm2cpio = subprocess.run(['rpm2cpio', str(rpm_path)], 
                                         capture_output=True, check=True, timeout=30)
                # cpio extracts from stdin
                subprocess.run(['cpio', '-idm'], 
                              input=rpm2cpio.stdout, 
                              cwd=temp_dir, 
                              capture_output=True, 
                              check=True, 
                              timeout=30)
                
                # List all extracted files
                names = set()
                meta = {}
                for root, dirs, files in os.walk(temp_dir):
                    for f in files:
                        full_path = Path(root) / f
                        rel_path = str(full_path.relative_to(temp_dir)).replace('\\', '/')
                        names.add(rel_path)
                        # Get file size for metadata
                        try:
                            size = full_path.stat().st_size
                            # Calculate CRC32 for consistency with ZIP format
                            import zlib
                            with open(full_path, 'rb') as fp:
                                crc = zlib.crc32(fp.read()) & 0xffffffff
                            meta[rel_path] = (size, crc)
                        except Exception:
                            pass
                
                # Process nested archives in extracted content
                if self.deep_nested_archives:
                    for name in list(names):
                        if name.lower().endswith(('.jar', '.war', '.ear')):
                            archive_file = temp_dir / name
                            if archive_file.exists():
                                try:
                                    with zipfile.ZipFile(archive_file, 'r') as zf:
                                        norm_outer = self._normalize_inner_archive_marker(name)
                                        for inner_info in zf.infolist():
                                            inner_name = inner_info.filename.replace('\\', '/').strip('/')
                                            if inner_name and inner_name.endswith('/'):
                                                inner_name = inner_name[:-1]
                                            if inner_name:
                                                full_path = f"{norm_outer}!/{inner_name}"
                                                names.add(full_path)
                                                if not inner_info.is_dir():
                                                    meta[full_path] = (inner_info.file_size, inner_info.CRC)
                                                
                                                # Recursively process nested archives
                                                if inner_name.lower().endswith(('.jar', '.war', '.ear')):
                                                    try:
                                                        self._process_nested_archive(zf, inner_info.filename, full_path, names, meta, depth=1)
                                                    except Exception:
                                                        pass
                                except Exception:
                                    pass
                
                # Cleanup temp directory
                try:
                    shutil.rmtree(temp_dir)
                except Exception:
                    pass
                
                return names, meta, ""
                
            except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
                # rpm2cpio/cpio not available, try 7-Zip (Windows)
                seven_zip_paths = [
                    r"C:\Program Files\7-Zip\7z.exe",
                    r"C:\Program Files (x86)\7-Zip\7z.exe",
                    "7z",  # Try from PATH
                ]
                
                for sz_path in seven_zip_paths:
                    try:
                        # 7z x extracts with full paths
                        result = subprocess.run([sz_path, 'x', str(rpm_path), f'-o{temp_dir}', '-y'], 
                                              capture_output=True, check=True, timeout=60)
                        
                        # List all extracted files
                        names = set()
                        meta = {}
                        for root, dirs, files in os.walk(temp_dir):
                            for f in files:
                                full_path = Path(root) / f
                                rel_path = str(full_path.relative_to(temp_dir)).replace('\\', '/')
                                names.add(rel_path)
                                try:
                                    size = full_path.stat().st_size
                                    import zlib
                                    with open(full_path, 'rb') as fp:
                                        crc = zlib.crc32(fp.read()) & 0xffffffff
                                    meta[rel_path] = (size, crc)
                                except Exception:
                                    pass
                        
                        # Process nested archives and CPIO files
                        if self.deep_nested_archives:
                            for name in list(names):
                                archive_file = temp_dir / name
                                if not archive_file.exists():
                                    continue
                                
                                # Handle CPIO files (extracted from RPM by 7-Zip)
                                if name.lower().endswith('.cpio'):
                                    try:
                                        # Extract CPIO contents using 7-Zip
                                        cpio_temp_dir = Path(mkdtemp_compare(prefix="cpio_extract_"))
                                        for sz_path in seven_zip_paths:
                                            try:
                                                subprocess.run([sz_path, 'x', str(archive_file), f'-o{cpio_temp_dir}', '-y'], 
                                                             capture_output=True, check=True, timeout=60)
                                                
                                                # List all extracted files from CPIO
                                                for root, dirs, files in os.walk(cpio_temp_dir):
                                                    for f in files:
                                                        full_path = Path(root) / f
                                                        rel_path = str(full_path.relative_to(cpio_temp_dir)).replace('\\', '/')
                                                        names.add(rel_path)
                                                        try:
                                                            size = full_path.stat().st_size
                                                            import zlib
                                                            with open(full_path, 'rb') as fp:
                                                                crc = zlib.crc32(fp.read()) & 0xffffffff
                                                            meta[rel_path] = (size, crc)
                                                        except Exception:
                                                            pass
                                                        
                                                        # Process nested JARs/WARs/EARs within CPIO
                                                        if rel_path.lower().endswith(('.jar', '.war', '.ear')):
                                                            nested_archive = cpio_temp_dir / rel_path
                                                            if nested_archive.exists():
                                                                try:
                                                                    with zipfile.ZipFile(nested_archive, 'r') as zf:
                                                                        norm_outer = self._normalize_inner_archive_marker(rel_path)
                                                                        for inner_info in zf.infolist():
                                                                            inner_name = inner_info.filename.replace('\\', '/').strip('/')
                                                                            if inner_name and inner_name.endswith('/'):
                                                                                inner_name = inner_name[:-1]
                                                                            if inner_name:
                                                                                full_nested_path = f"{norm_outer}!/{inner_name}"
                                                                                names.add(full_nested_path)
                                                                                if not inner_info.is_dir():
                                                                                    meta[full_nested_path] = (inner_info.file_size, inner_info.CRC)
                                                                                
                                                                                if inner_name.lower().endswith(('.jar', '.war', '.ear')):
                                                                                    try:
                                                                                        self._process_nested_archive(zf, inner_info.filename, full_nested_path, names, meta, depth=1)
                                                                                    except Exception:
                                                                                        pass
                                                                except Exception:
                                                                    pass
                                                
                                                # Remove CPIO file from names since we've extracted its contents
                                                names.discard(name)
                                                if name in meta:
                                                    del meta[name]
                                                
                                                # Cleanup CPIO temp directory
                                                try:
                                                    shutil.rmtree(cpio_temp_dir)
                                                except Exception:
                                                    pass
                                                
                                                break  # Successfully extracted, exit loop
                                            except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
                                                continue
                                    except Exception:
                                        pass
                                
                                # Handle regular JARs/WARs/EARs
                                elif name.lower().endswith(('.jar', '.war', '.ear')):
                                    try:
                                        with zipfile.ZipFile(archive_file, 'r') as zf:
                                            norm_outer = self._normalize_inner_archive_marker(name)
                                            for inner_info in zf.infolist():
                                                inner_name = inner_info.filename.replace('\\', '/').strip('/')
                                                if inner_name and inner_name.endswith('/'):
                                                    inner_name = inner_name[:-1]
                                                if inner_name:
                                                    full_path = f"{norm_outer}!/{inner_name}"
                                                    names.add(full_path)
                                                    if not inner_info.is_dir():
                                                        meta[full_path] = (inner_info.file_size, inner_info.CRC)
                                                    
                                                    if inner_name.lower().endswith(('.jar', '.war', '.ear')):
                                                        try:
                                                            self._process_nested_archive(zf, inner_info.filename, full_path, names, meta, depth=1)
                                                        except Exception:
                                                            pass
                                    except Exception:
                                        pass
                        
                        # Cleanup
                        try:
                            shutil.rmtree(temp_dir)
                        except Exception:
                            pass
                        
                        return names, meta, ""
                        
                    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
                        continue
                
                # All extraction methods failed
                try:
                    shutil.rmtree(temp_dir)
                except Exception:
                    pass
                return set(), {}, "RPM extraction failed: Please install 7-Zip or rpmfile module (pip install rpmfile)"
                
        except Exception as e:
            return set(), {}, f"RPM extraction error: {e}"
    
    def _list_rpm_members(self, archive_path: Path):
        if rpmfile is None:
            # Try fallback extraction method
            logger.info(f"rpmfile module not available, trying fallback extraction for {archive_path.name}")
            return self._extract_rpm_payload_fallback(archive_path)
        try:
            names = set()
            meta: dict[str, tuple[int, int]] = {}
            with rpmfile.open(archive_path) as rpm:  # type: ignore[attr-defined]
                for member in rpm.getmembers():
                    # member.name is like '/usr/bin/...'; normalize to relative
                    name = str(member.name).lstrip('/')
                    if name:
                        names.add(name)
                        # Dive into nested archives (jar/war/ear) inside RPM payload
                        if self.deep_nested_archives and name.lower().endswith(('.jar', '.war', '.ear')):
                            try:
                                fobj = rpm.extractfile(member)
                                if fobj is not None:
                                    data = fobj.read()
                                    with zipfile.ZipFile(BytesIO(data), 'r') as inner:
                                        norm_outer = self._normalize_inner_archive_marker(name)
                                        for inner_info in inner.infolist():
                                            inner_name = inner_info.filename.replace('\\', '/').strip('/')
                                            if inner_name and inner_name.endswith('/'):
                                                inner_name = inner_name[:-1]
                                            if inner_name:
                                                full_path = f"{norm_outer}!/{inner_name}"
                                                names.add(full_path)
                                                if not inner_info.is_dir():
                                                    meta[full_path] = (inner_info.file_size, inner_info.CRC)
                                                
                                                # Recursively process nested archives (EAR → WAR → JAR, etc.)
                                                if inner_name.lower().endswith(('.jar', '.war', '.ear')):
                                                    try:
                                                        self._process_nested_archive(inner, inner_info.filename, full_path, names, meta, depth=1)
                                                    except Exception:
                                                        pass
                            except Exception as ie:
                                norm_outer = self._normalize_inner_archive_marker(name)
                                names.add(f"{norm_outer}!/[error-reading-inner:{ie.__class__.__name__}]")
            return names, meta, ""
        except Exception as e:
            return set(), {}, str(e)

    def _gather_archives(self, base: Path):
        candidates = {}
        for root, dirs, files in os.walk(base):
            for filename in files:
                lower = filename.lower()
                if lower.endswith(('.ear', '.war', '.zip', '.rpm', '.jar')):
                    full_path = Path(root) / filename
                    rel_path = str(full_path.relative_to(base)).replace('\\', '/')
                    candidates[rel_path] = lower.rsplit('.', 1)[-1]  # archive type
        return candidates

    def compare_archives(self):
        import time
        self.comparison_start_time = time.time()
        self.comparison_error = None
        
        with _set_logger_level_temporarily(self.suppress_warnings):
            logger.info(f"Comparing archive contents: {self.folder1_path} vs {self.folder2_path}")
            
            # Store original paths for reporting
            original_folder1_path = self.folder1_path
            original_folder2_path = self.folder2_path
            
            # Handle network paths by copying to local if enabled
            folder1_to_compare = self.folder1_path
            folder2_to_compare = self.folder2_path
            
            if self.use_local_copy and self._is_network_path(self.folder1_path):
                logger.info("Folder 1 is on network path, copying to local temporary directory...")
                local_folder1 = self._copy_network_folder_to_local(self.folder1_path)
                if local_folder1:
                    self.temp_folder1 = local_folder1
                    folder1_to_compare = local_folder1
                    logger.info(f"Using local copy for folder 1: {local_folder1}")
                else:
                    logger.error("Failed to copy folder 1 to local, aborting comparison")
                    self.results = []
                    return self.results
            
            if self.use_local_copy and self._is_network_path(self.folder2_path):
                logger.info("Folder 2 is on network path, copying to local temporary directory...")
                local_folder2 = self._copy_network_folder_to_local(self.folder2_path)
                if local_folder2:
                    self.temp_folder2 = local_folder2
                    folder2_to_compare = local_folder2
                    logger.info(f"Using local copy for folder 2: {local_folder2}")
                else:
                    logger.error("Failed to copy folder 2 to local, aborting comparison")
                    if self.should_cleanup_temp_folders:
                        self.cleanup_temp_folders()
                    self.results = []
                    return self.results

            if not _path_exists(folder1_to_compare):
                logger.error(f"Folder 1 does not exist: {folder1_to_compare}")
                if self.should_cleanup_temp_folders:
                    self.cleanup_temp_folders()
                self.results = []
                return self.results
            if not _path_exists(folder2_to_compare):
                logger.error(f"Folder 2 does not exist: {folder2_to_compare}")
                if self.should_cleanup_temp_folders:
                    self.cleanup_temp_folders()
                self.results = []
                return self.results

            # Special-case: if both inputs are archive files, compare them directly as a pair
            supported_exts = ('.ear', '.war', '.zip', '.rpm', '.jar')
            if _path_is_file(folder1_to_compare) and _path_is_file(folder2_to_compare):
                ext1 = folder1_to_compare.suffix.lower()
                ext2 = folder2_to_compare.suffix.lower()
                if ext1 in supported_exts and ext2 in supported_exts:
                    self.results = []
                    # Set total to 3 phases: scan archive1, scan archive2, compare
                    self.total_files = 3
                    self.processed_files = 0
                    
                    # Use ext1 primarily; prefer zip-style handler for ear/war/zip/jar
                    # Create a synthetic relative path label for reporting
                    label = f"{folder1_to_compare.name} <-> {folder2_to_compare.name}"
                    archive_type = (ext1[1:] if ext1 == ext2 else f"{ext1[1:]}|{ext2[1:]}")
                    entry = ArchiveEntryComparison(label, archive_type)
                    entry.exists_in_folder1 = True
                    entry.exists_in_folder2 = True
                    
                    # Phase 1: List members for archive 1
                    self.current_file = f"Phase 1/3: Scanning {folder1_to_compare.name}..."
                    self._update_progress(self.current_file)
                    logger.info(f"Scanning archive 1: {folder1_to_compare.name}")
                    
                    if ext1 in ('.ear', '.war', '.zip', '.jar'):
                        members, meta, err = self._list_zip_members(folder1_to_compare)
                        entry.members_folder1 = members
                        entry.members_meta_folder1 = meta
                        entry.error_folder1 = err
                    elif ext1 == '.rpm':
                        members, meta, err = self._list_rpm_members(folder1_to_compare)
                        entry.members_folder1 = members
                        entry.members_meta_folder1 = meta
                        entry.error_folder1 = err
                    
                    self.processed_files = 1
                    self.current_file = f"Phase 1/3: Scanned {folder1_to_compare.name} ({len(entry.members_folder1)} files)"
                    self._update_progress(self.current_file)
                    logger.info(f"Found {len(entry.members_folder1)} members in archive 1")
                    
                    # Phase 2: List members for archive 2
                    self.current_file = f"Phase 2/3: Scanning {folder2_to_compare.name}..."
                    self._update_progress(self.current_file)
                    logger.info(f"Scanning archive 2: {folder2_to_compare.name}")
                    
                    if ext2 in ('.ear', '.war', '.zip', '.jar'):
                        members, meta, err = self._list_zip_members(folder2_to_compare)
                        entry.members_folder2 = members
                        entry.members_meta_folder2 = meta
                        entry.error_folder2 = err
                    elif ext2 == '.rpm':
                        members, meta, err = self._list_rpm_members(folder2_to_compare)
                        entry.members_folder2 = members
                        entry.members_meta_folder2 = meta
                        entry.error_folder2 = err
                    
                    self.processed_files = 2
                    self.current_file = f"Phase 2/3: Scanned {folder2_to_compare.name} ({len(entry.members_folder2)} files)"
                    self._update_progress(self.current_file)
                    logger.info(f"Found {len(entry.members_folder2)} members in archive 2")
                    # Phase 3: Compute diffs WITHOUT normalization
                    self.current_file = f"Phase 3/3: Comparing archives..."
                    self._update_progress(self.current_file)
                    logger.info("Comparing archive contents directly (no normalization)")
                    
                    if entry.members_folder1 or entry.members_folder2:
                        # Direct comparison without normalization
                        set1 = entry.members_folder1
                        set2 = entry.members_folder2
                        entry.members_folder1_normalized = set1  # Keep for compatibility
                        entry.members_folder2_normalized = set2  # Keep for compatibility
                        entry.member_differs = (set1 != set2)
                        
                        # For content comparison, use actual common members
                        common_members = set1 & set2
                        total_common = len(common_members)
                        for idx, member_name in enumerate(sorted(common_members)):
                            # Update progress during comparison
                            if idx % 100 == 0 or idx == total_common - 1:
                                self.current_file = f"Phase 3/3: Comparing {idx+1}/{total_common} common files"
                                self._update_progress(self.current_file)
                            
                            # Compare metadata for files with same name
                            m1 = entry.members_meta_folder1.get(member_name)
                            m2 = entry.members_meta_folder2.get(member_name)
                            if m1 and m2 and m1 != m2:
                                entry.content_differs = True
                                entry.content_diff_member_names.append(member_name)
                    
                    self.results.append(entry)
                    self.processed_files = 3
                    self.current_file = f"Comparison complete!"
                    self._update_progress(self.current_file)
                    logger.info("Direct archive-to-archive comparison completed")
                    # Clean up temp folders if created
                    if self.should_cleanup_temp_folders:
                        self.cleanup_temp_folders()
                    return self.results

            archives1 = self._gather_archives(folder1_to_compare)
            archives2 = self._gather_archives(folder2_to_compare)
            all_rel_paths = set(archives1.keys()) | set(archives2.keys())

            self.results = []
            self.total_files = len(all_rel_paths)
            self.processed_files = 0
            
            logger.info(f"Found {len(all_rel_paths)} archives to compare")
            
            for idx, rel in enumerate(sorted(all_rel_paths)):
                self.processed_files = idx
                self.current_file = rel
                self._update_progress(rel)
                
                if idx % 10 == 0 or idx == self.total_files - 1:
                    logger.info(f"Progress: {self.processed_files}/{self.total_files} archives ({self.progress_percentage:.1f}%) - ETA: {self.estimated_time_remaining}s")
                archive_type = (archives1.get(rel) or archives2.get(rel) or '').lower()
                entry = ArchiveEntryComparison(rel, archive_type)
                path1 = folder1_to_compare / rel
                path2 = folder2_to_compare / rel
                entry.exists_in_folder1 = _path_exists(path1)
                entry.exists_in_folder2 = _path_exists(path2)

                if entry.exists_in_folder1:
                    if archive_type in ('ear', 'war', 'zip', 'jar'):
                        members, meta, err = self._list_zip_members(path1)
                        entry.members_folder1 = members
                        entry.members_meta_folder1 = meta
                        entry.error_folder1 = err
                    elif archive_type == 'rpm':
                        members, meta, err = self._list_rpm_members(path1)
                        entry.members_folder1 = members
                        entry.members_meta_folder1 = meta
                        entry.error_folder1 = err

                if entry.exists_in_folder2:
                    if archive_type in ('ear', 'war', 'zip', 'jar'):
                        members, meta, err = self._list_zip_members(path2)
                        entry.members_folder2 = members
                        entry.members_meta_folder2 = meta
                        entry.error_folder2 = err
                    elif archive_type == 'rpm':
                        members, meta, err = self._list_rpm_members(path2)
                        entry.members_folder2 = members
                        entry.members_meta_folder2 = meta
                        entry.error_folder2 = err

                if entry.exists_in_folder1 and entry.exists_in_folder2:
                    if entry.members_folder1 or entry.members_folder2:
                        # Direct comparison without normalization
                        set1 = entry.members_folder1
                        set2 = entry.members_folder2
                        entry.members_folder1_normalized = set1  # Keep for compatibility
                        entry.members_folder2_normalized = set2  # Keep for compatibility
                        entry.member_differs = (set1 != set2)
                        
                        # Check content where both sides have meta for same member name
                        common_members = set1 & set2
                        for member_name in sorted(common_members):
                            # Compare metadata for files with same name
                            m1 = entry.members_meta_folder1.get(member_name)
                            m2 = entry.members_meta_folder2.get(member_name)
                            if m1 and m2 and m1 != m2:
                                entry.content_differs = True
                                entry.content_diff_member_names.append(member_name)
                    else:
                        # If we couldn't list members in either/both, mark as unknown (not differing)
                        entry.member_differs = False

                self.results.append(entry)
                self.processed_files = idx + 1
                self._update_progress(rel)

            logger.info("Archive content comparison completed")
            # Clean up temp folders if created
            if self.should_cleanup_temp_folders:
                self.cleanup_temp_folders()
            return self.results

    def generate_archive_report(self):
        if not self.results:
            # Provide helpful error message
            msg = []
            msg.append("=" * 80)
            msg.append("ARCHIVE CONTENTS COMPARISON REPORT")
            msg.append("=" * 80)
            msg.append(f"Folder 1: {self.folder1_path}")
            msg.append(f"Folder 2: {self.folder2_path}")
            msg.append("")
            msg.append("ERROR: No comparison results available.")
            msg.append("")
            msg.append("Possible reasons:")
            msg.append("  1. One or both paths do not exist or are not accessible")
            msg.append("  2. Paths are not valid archive files (.jar/.war/.ear/.zip/.rpm)")
            msg.append("  3. Permission denied when accessing the files")
            msg.append("  4. Network path is not available")
            msg.append("")
            msg.append("Please verify:")
            msg.append(f"  - Path 1 exists: {_path_exists(self.folder1_path)}")
            msg.append(f"  - Path 2 exists: {_path_exists(self.folder2_path)}")
            if _path_exists(self.folder1_path):
                msg.append(f"  - Path 1 is file: {_path_is_file(self.folder1_path)}")
            if _path_exists(self.folder2_path):
                msg.append(f"  - Path 2 is file: {_path_is_file(self.folder2_path)}")
            msg.append("=" * 80)
            return "\n".join(msg)

        report = []
        report.append("=" * 80)
        report.append("ARCHIVE CONTENTS COMPARISON REPORT (.ear .war .zip .rpm .jar)")
        report.append("=" * 80)
        report.append(f"Folder 1: {self.folder1_path}")
        report.append(f"Folder 2: {self.folder2_path}")
        report.append(f"Comparison Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        report.append("")

        only_in_folder1 = [r for r in self.results if r.exists_in_folder1 and not r.exists_in_folder2]
        only_in_folder2 = [r for r in self.results if r.exists_in_folder2 and not r.exists_in_folder1]
        in_both = [r for r in self.results if r.exists_in_folder1 and r.exists_in_folder2]
        member_diffs = [r for r in in_both if r.member_differs]
        content_diffs = [r for r in in_both if r.content_differs]
        differing = [r for r in in_both if r.member_differs or r.content_differs]

        report.append("SUMMARY")
        report.append("-" * 40)
        report.append(f"Archives only in folder 1: {len(only_in_folder1)}")
        report.append(f"Archives only in folder 2: {len(only_in_folder2)}")
        report.append(f"Archives in both: {len(in_both)}")
        report.append(f"Archives with member list differences: {len(member_diffs)}")
        report.append(f"Archives with file content differences: {len(content_diffs)}")
        if rpmfile is None:
            report.append("Note: RPM content listing unavailable without 'rpmfile' module. RPMs compared by presence only.")
        report.append("")

        if not only_in_folder1 and not only_in_folder2 and not differing:
            report.append("No differences found")
            report.append("")

        if only_in_folder1:
            report.append("ONLY IN FOLDER 1")
            report.append("-" * 40)
            for r in only_in_folder1:
                report.append(f"  [{r.archive_type}] {r.archive_relative_path}")
            report.append("")

        if only_in_folder2:
            report.append("ONLY IN FOLDER 2")
            report.append("-" * 40)
            for r in only_in_folder2:
                report.append(f"  [{r.archive_type}] {r.archive_relative_path}")
            report.append("")

        if differing:
            report.append("DIFFERENCES")
            report.append("-" * 40)
            for r in differing:
                report.append(f"  [{r.archive_type}] {r.archive_relative_path}")
                # Show sample member list diffs (using normalized names if available)
                if r.member_differs:
                    # Use normalized sets if available, otherwise fall back to raw sets
                    set1 = r.members_folder1_normalized if r.members_folder1_normalized else r.members_folder1
                    set2 = r.members_folder2_normalized if r.members_folder2_normalized else r.members_folder2
                    only1 = sorted(list(set1 - set2))[:10]
                    only2 = sorted(list(set2 - set1))[:10]
                    if only1:
                        report.append("    Only in archive from folder 1 (after normalization):")
                        for name in only1:
                            report.append(f"      - {name}")
                    if only2:
                        report.append("    Only in archive from folder 2 (after normalization):")
                        for name in only2:
                            report.append(f"      - {name}")
                if r.content_differs:
                    # List up to N inner members with content diffs
                    max_list = 25
                    if not r.member_differs:
                        report.append("    Member lists match but file contents differ (size/CRC).")
                    differing_members = r.content_diff_member_names[:max_list]
                    if differing_members:
                        report.append("    Files with differing content:")
                        for name in differing_members:
                            m1 = r.members_meta_folder1.get(name)
                            m2 = r.members_meta_folder2.get(name)
                            # Display size and CRC where available
                            if m1 and m2:
                                report.append(f"      - {name}  [folder1: size={m1[0]}, crc={m1[1]} | folder2: size={m2[0]}, crc={m2[1]}]")
                            else:
                                report.append(f"      - {name}")
                        if len(r.content_diff_member_names) > max_list:
                            report.append(f"      ... and {len(r.content_diff_member_names) - max_list} more")
                if r.error_folder1:
                    report.append(f"    Note (folder1): {r.error_folder1}")
                if r.error_folder2:
                    report.append(f"    Note (folder2): {r.error_folder2}")
            report.append("")

        return "\n".join(report)

    def save_archive_report(self, output_file: str):
        print(f"[DEBUG] save_archive_report called with: {output_file}")
        report = self.generate_archive_report()
        try:
            with open(output_file, 'w', encoding='utf-8') as f:
                f.write(report)
            logger.info(f"Archive report saved to: {output_file}")
            print(f"[DEBUG] Archive text report saved to: {output_file}")
        except Exception as e:
            logger.error(f"Error saving text report to {output_file}: {e}")
            print(f"[ERROR] Error saving text report to {output_file}: {e}")
        
        try:
            html_report = self.generate_archive_html_report()
            html_file = Path(output_file).with_suffix('.html')
            with open(html_file, 'w', encoding='utf-8') as hf:
                hf.write(html_report)
            logger.info(f"Archive HTML report saved to: {html_file}")
            print(f"[DEBUG] Archive HTML report saved to: {html_file}")
        except Exception as e:
            logger.error(f"Error saving HTML report: {e}")
            print(f"[ERROR] Error saving HTML report: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            print(f"[ERROR] Traceback: {traceback.format_exc()}")
    
    def generate_archive_html_report(self) -> str:
        style = """
        <style>
        * { box-sizing: border-box; }
        body { font-family: 'Segoe UI', system-ui, -apple-system, sans-serif; margin: 0; padding: 20px; background: linear-gradient(135deg, #134e5e 0%, #71b280 100%); min-height: 100vh; }
        .container { max-width: 1400px; margin: 0 auto; background: white; border-radius: 16px; box-shadow: 0 20px 60px rgba(0,0,0,0.3); overflow: hidden; }
        .header { background: linear-gradient(135deg, #134e5e 0%, #71b280 100%); color: white; padding: 30px; }
        .header h1 { margin: 0 0 10px 0; font-size: 28px; font-weight: 600; }
        .header .subtitle { opacity: 0.9; font-size: 14px; }
        .header .paths { margin-top: 15px; padding-top: 15px; border-top: 1px solid rgba(255,255,255,0.3); }
        .header .path-label { font-weight: 600; font-size: 12px; text-transform: uppercase; opacity: 0.8; margin-top: 8px; }
        .header .path-value { font-family: 'Consolas', monospace; font-size: 13px; margin-top: 4px; word-break: break-all; }
        .content { padding: 30px; }
        .summary { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 20px; margin-bottom: 30px; }
        .stat-card { background: linear-gradient(135deg, #11998e 0%, #38ef7d 100%); color: white; padding: 20px; border-radius: 12px; box-shadow: 0 4px 15px rgba(0,0,0,0.1); }
        .stat-card.warning { background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%); }
        .stat-card.danger { background: linear-gradient(135deg, #eb3349 0%, #f45c43 100%); }
        .stat-card.info { background: linear-gradient(135deg, #4facfe 0%, #00f2fe 100%); }
        .stat-card .label { font-size: 12px; font-weight: 500; opacity: 0.9; text-transform: uppercase; letter-spacing: 0.5px; }
        .stat-card .value { font-size: 32px; font-weight: 700; margin-top: 8px; }
        .nav-menu { background: #f7fafc; padding: 20px; border-radius: 8px; margin-bottom: 30px; }
        .nav-menu h3 { margin: 0 0 15px 0; font-size: 18px; color: #2d3748; }
        .nav-links { display: flex; flex-wrap: wrap; gap: 12px; }
        .nav-link { display: inline-block; padding: 10px 16px; background: white; border-left: 4px solid #134e5e; border-radius: 6px; text-decoration: none; color: #2d3748; font-weight: 600; transition: all 0.2s; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
        .nav-link:hover { transform: translateX(4px); border-left-width: 6px; }
        .nav-link.missing-left { border-left-color: #f56565; }
        .nav-link.missing-right { border-left-color: #48bb78; }
        .nav-link.different { border-left-color: #ed8936; }
        .nav-link.identical { border-left-color: #4299e1; }
        .section { margin-bottom: 40px; }
        .section-title { font-size: 22px; font-weight: 600; color: #1a202c; margin-bottom: 20px; padding-bottom: 10px; border-bottom: 3px solid #134e5e; }
        .table-container { background: #f7fafc; border-radius: 8px; padding: 20px; margin-bottom: 20px; overflow-x: auto; }
        .comparison-table { width: 100%; border-collapse: separate; border-spacing: 0; background: white; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.1); min-width: 800px; }
        .comparison-table thead { background: linear-gradient(135deg, #134e5e 0%, #71b280 100%); color: white; }
        .comparison-table th { padding: 14px 12px; text-align: left; font-weight: 600; font-size: 13px; text-transform: uppercase; letter-spacing: 0.5px; }
        .comparison-table th.center { text-align: center; }
        .comparison-table tbody tr { transition: all 0.2s; }
        .comparison-table tbody tr:hover { filter: brightness(0.95); }
        .comparison-table td { padding: 12px; border-bottom: 1px solid #e2e8f0; font-size: 13px; vertical-align: top; }
        .comparison-table td.filename { font-family: 'Consolas', monospace; font-weight: 600; color: #2d3748; max-width: 350px; word-break: break-word; }
        .comparison-table td.info { font-family: 'Consolas', monospace; font-size: 11px; color: #718096; }
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
        .back-link { display: inline-block; margin-top: 20px; padding: 12px 24px; background: linear-gradient(135deg, #134e5e 0%, #71b280 100%); color: white; text-decoration: none; border-radius: 8px; font-weight: 600; transition: transform 0.2s; }
        .back-link:hover { transform: translateY(-2px); }
        .legend { background: #e6fffa; padding: 15px; border-radius: 8px; margin-bottom: 20px; border-left: 4px solid #38b2ac; }
        .legend-title { font-weight: 600; color: #234e52; margin-bottom: 8px; }
        .legend-items { display: flex; flex-wrap: wrap; gap: 15px; font-size: 13px; color: #285e61; }
        .legend-item { display: flex; align-items: center; gap: 6px; }
        .legend-color { width: 20px; height: 20px; border-radius: 4px; }
        .error { color: #ef4444; background: #fee; padding: 20px; border: 1px solid #fca5a5; border-radius: 8px; margin: 16px 0; }
        .details-box { margin-top: 10px; padding: 12px; background: #f9fafb; border-radius: 6px; border-left: 3px solid #d97706; }
        .details-box summary { cursor: pointer; font-weight: 600; color: #d97706; padding: 4px 0; }
        .details-box ul { margin: 8px 0 0 20px; padding: 0; font-size: 12px; }
        .details-box li { margin: 4px 0; color: #4b5563; font-family: 'Consolas', monospace; }
        .type-badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 10px; font-weight: 600; text-transform: uppercase; background: #e2e8f0; color: #475569; margin-right: 8px; }
        @media (max-width: 768px) { .summary { grid-template-columns: 1fr; } .nav-links { flex-direction: column; } }
        </style>
        """
        script = """
        <script>
        function toggleMore(id, el){
          var box = document.getElementById(id);
          if(!box){ return false; }
          var hidden = (box.style.display === '' || box.style.display === 'none');
          box.style.display = hidden ? 'block' : 'none';
          if(el){
            var moreText = el.getAttribute('data-more-text') || 'Show more';
            el.textContent = hidden ? 'Show less' : moreText;
          }
          return false;
        }
        </script>
        """
        
        # Check if results are empty and provide helpful error message
        if not self.results:
            error_html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Archive Comparison Error</title>
    {style}
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>📦 Archive Contents Comparison</h1>
            <div class="subtitle">Error - No Results</div>
            <div class="paths">
                <div class="path-label">📁 Folder 1</div>
                <div class="path-value">{html.escape(str(self.folder1_path))}</div>
                <div class="path-label">📁 Folder 2</div>
                <div class="path-value">{html.escape(str(self.folder2_path))}</div>
            </div>
        </div>
        <div class="content">
            <div class="error">
                <h2>❌ No Comparison Results Available</h2>
                <p>The comparison could not generate results. Possible reasons:</p>
                <ul>
                    <li>One or both paths do not exist or are not accessible</li>
                    <li>Paths are not valid archive files (.jar/.war/.ear/.zip/.rpm)</li>
                    <li>Permission denied when accessing the files</li>
                    <li>Network path is not available</li>
                </ul>
                <p><strong>Path Verification:</strong></p>
                <ul>
                    <li>Path 1 exists: {_path_exists(self.folder1_path)}</li>
                    <li>Path 2 exists: {_path_exists(self.folder2_path)}</li>
                </ul>
            </div>
            <a href="/" class="back-link">← Back to Home</a>
        </div>
    </div>
</body>
</html>"""
            return error_html
        
        only_in_folder1 = [r for r in self.results if r.exists_in_folder1 and not r.exists_in_folder2]
        only_in_folder2 = [r for r in self.results if r.exists_in_folder2 and not r.exists_in_folder1]
        in_both = [r for r in self.results if r.exists_in_folder1 and r.exists_in_folder2]
        identical = [r for r in in_both if not r.member_differs and not r.content_differs]
        member_diffs = [r for r in in_both if r.member_differs]
        content_diffs = [r for r in in_both if r.content_differs]
        differing = [r for r in in_both if r.member_differs or r.content_differs]
        
        html_content = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Archive Contents Comparison Report</title>
    {style}
    {script}
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>📦 Archive Contents Comparison Report</h1>
            <div class="subtitle">Deep comparison of archive contents • {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</div>
            <div class="paths">
                <div class="path-label">📁 Folder 1 (Left)</div>
                <div class="path-value">{html.escape(str(self.folder1_path))}</div>
                <div class="path-label">📁 Folder 2 (Right)</div>
                <div class="path-value">{html.escape(str(self.folder2_path))}</div>
            </div>
        </div>
        
        <div class="content">
            <!-- Summary Statistics -->
            <div class="summary">
                <div class="stat-card info">
                    <div class="label">Total Archives</div>
                    <div class="value">{len(self.results)}</div>
                </div>
                <div class="stat-card">
                    <div class="label">Identical</div>
                    <div class="value">{len(identical)}</div>
                </div>
                <div class="stat-card warning">
                    <div class="label">Different</div>
                    <div class="value">{len(differing)}</div>
                </div>
                <div class="stat-card danger">
                    <div class="label">Only in Left</div>
                    <div class="value">{len(only_in_folder1)}</div>
                </div>
                <div class="stat-card" style="background: linear-gradient(135deg, #48bb78 0%, #38a169 100%);">
                    <div class="label">Only in Right</div>
                    <div class="value">{len(only_in_folder2)}</div>
                </div>
            </div>
            
            <!-- Navigation Menu -->
            <div class="nav-menu">
                <h3>📑 Quick Navigation</h3>
                <div class="nav-links">
                    <a href="#comparison-table" class="nav-link">📊 Full Comparison Table</a>"""
        
        if differing:
            html_content += f"""
                    <a href="#different-archives" class="nav-link different">≠ Different Archives ({len(differing)})</a>"""
        if only_in_folder1:
            html_content += f"""
                    <a href="#missing-right" class="nav-link missing-left">← Only in Left ({len(only_in_folder1)})</a>"""
        if only_in_folder2:
            html_content += f"""
                    <a href="#missing-left" class="nav-link missing-right">→ Only in Right ({len(only_in_folder2)})</a>"""
        if identical:
            html_content += f"""
                    <a href="#identical-archives" class="nav-link identical">✓ Identical Archives ({len(identical)})</a>"""
        
        html_content += """
                </div>
            </div>
            
            <!-- Legend -->
            <div class="legend">
                <div class="legend-title">💡 Color Legend</div>
                <div class="legend-items">
                    <div class="legend-item"><span class="legend-color" style="background: #dbeafe; border: 2px solid #3b82f6;"></span> Identical</div>
                    <div class="legend-item"><span class="legend-color" style="background: #fef3c7; border: 2px solid #f59e0b;"></span> Different</div>
                    <div class="legend-item"><span class="legend-color" style="background: #fee2e2; border: 2px solid #ef4444;"></span> Only in Left</div>
                    <div class="legend-item"><span class="legend-color" style="background: #dcfce7; border: 2px solid #22c55e;"></span> Only in Right</div>
                </div>
            </div>
            
            <!-- Full Comparison Table -->
            <div class="section" id="comparison-table">
                <div class="section-title">📊 Full Archive Comparison Table</div>
                <div class="table-container">
                    <table class="comparison-table">
                        <thead>
                            <tr>
                                <th>Archive Path</th>
                                <th>Type</th>
                                <th class="center">Status</th>
                                <th>Folder 1 (Left)</th>
                                <th>Folder 2 (Right)</th>
                            </tr>
                        </thead>
                        <tbody>"""
        
        # Build all files list with status
        all_archives = []
        for r in self.results:
            if r.exists_in_folder1 and r.exists_in_folder2:
                if r.member_differs or r.content_differs:
                    status = 'different'
                    status_text = '≠ Different'
                    row_class = 'different'
                else:
                    status = 'identical'
                    status_text = '✓ Identical'
                    row_class = 'identical'
            elif r.exists_in_folder1:
                status = 'missing-right'
                status_text = '← Only in Left'
                row_class = 'missing-left'
            else:
                status = 'missing-left'
                status_text = '→ Only in Right'
                row_class = 'missing-right'
            
            all_archives.append({
                'result': r,
                'status': status,
                'status_text': status_text,
                'row_class': row_class
            })
        
        # Sort: differences first, then missing, then identical
        all_archives.sort(key=lambda x: (
            0 if x['status'] == 'different' else
            1 if x['status'] in ('missing-right', 'missing-left') else 2,
            x['result'].archive_relative_path.lower()
        ))
        
        for info in all_archives:
            r = info['result']
            html_content += f"""
                            <tr class="{info['row_class']}">
                                <td class="filename">{html.escape(r.archive_relative_path)}</td>
                                <td><span class="type-badge">{html.escape(r.archive_type.upper())}</span></td>
                                <td class="status">
                                    <span class="status-badge {info['status']}">{info['status_text']}</span>
                                </td>
                                <td class="info">"""
            
            if r.exists_in_folder1:
                member_count = len(r.members_folder1) if r.members_folder1 else 0
                html_content += f"""{member_count} members"""
            else:
                html_content += """<span class="empty-cell">—</span>"""
            
            html_content += """</td>
                                <td class="info">"""
            
            if r.exists_in_folder2:
                member_count = len(r.members_folder2) if r.members_folder2 else 0
                html_content += f"""{member_count} members"""
            else:
                html_content += """<span class="empty-cell">—</span>"""
            
            html_content += """</td>
                            </tr>"""
        
        html_content += """
                        </tbody>
                    </table>
                </div>
            </div>"""
        
        # Different Archives Section with details
        if differing:
            html_content += f"""
            <div class="section" id="different-archives">
                <div class="section-title">≠ Different Archives ({len(differing)})</div>
                <div class="table-container">
                    <table class="comparison-table">
                        <thead>
                            <tr>
                                <th>Archive Path</th>
                                <th>Type</th>
                                <th>Difference Type</th>
                                <th>Details</th>
                            </tr>
                        </thead>
                        <tbody>"""
            
            for idx, r in enumerate(differing):
                diff_types = []
                if r.member_differs:
                    diff_types.append("Member List")
                if r.content_differs:
                    diff_types.append("Content")
                
                # Build details
                details_html = ""
                if r.member_differs:
                    set1 = r.members_folder1_normalized if r.members_folder1_normalized else r.members_folder1
                    set2 = r.members_folder2_normalized if r.members_folder2_normalized else r.members_folder2
                    only1 = sorted(list(set1 - set2))[:5]
                    only2 = sorted(list(set2 - set1))[:5]
                    if only1:
                        details_html += f"<div style='font-size:11px;'><strong>Only in left ({len(set1 - set2)}):</strong> {', '.join(html.escape(n) for n in only1)}{'...' if len(set1 - set2) > 5 else ''}</div>"
                    if only2:
                        details_html += f"<div style='font-size:11px;'><strong>Only in right ({len(set2 - set1)}):</strong> {', '.join(html.escape(n) for n in only2)}{'...' if len(set2 - set1) > 5 else ''}</div>"
                
                if r.content_differs and r.content_diff_member_names:
                    count = len(r.content_diff_member_names)
                    sample = r.content_diff_member_names[:3]
                    details_html += f"<div style='font-size:11px;'><strong>Content differs ({count}):</strong> {', '.join(html.escape(n) for n in sample)}{'...' if count > 3 else ''}</div>"
                
                html_content += f"""
                            <tr class="different">
                                <td class="filename">{html.escape(r.archive_relative_path)}</td>
                                <td><span class="type-badge">{html.escape(r.archive_type.upper())}</span></td>
                                <td>{', '.join(diff_types)}</td>
                                <td class="info">{details_html if details_html else 'Content hash differs'}</td>
                            </tr>"""
            
            html_content += """
                        </tbody>
                    </table>
                </div>
            </div>"""
        
        # Only in Left Section
        if only_in_folder1:
            html_content += f"""
            <div class="section" id="missing-right">
                <div class="section-title">← Only in Left ({len(only_in_folder1)} archives)</div>
                <div class="table-container">
                    <table class="comparison-table">
                        <thead>
                            <tr>
                                <th>Archive Path</th>
                                <th>Type</th>
                                <th>Members</th>
                                <th>Status</th>
                            </tr>
                        </thead>
                        <tbody>"""
            
            for r in only_in_folder1:
                member_count = len(r.members_folder1) if r.members_folder1 else 0
                html_content += f"""
                            <tr class="missing-left">
                                <td class="filename">{html.escape(r.archive_relative_path)}</td>
                                <td><span class="type-badge">{html.escape(r.archive_type.upper())}</span></td>
                                <td class="info">{member_count} members</td>
                                <td class="status"><span class="status-badge missing-right">Missing in Right</span></td>
                            </tr>"""
            
            html_content += """
                        </tbody>
                    </table>
                </div>
            </div>"""
        
        # Only in Right Section
        if only_in_folder2:
            html_content += f"""
            <div class="section" id="missing-left">
                <div class="section-title">→ Only in Right ({len(only_in_folder2)} archives)</div>
                <div class="table-container">
                    <table class="comparison-table">
                        <thead>
                            <tr>
                                <th>Archive Path</th>
                                <th>Type</th>
                                <th>Members</th>
                                <th>Status</th>
                            </tr>
                        </thead>
                        <tbody>"""
            
            for r in only_in_folder2:
                member_count = len(r.members_folder2) if r.members_folder2 else 0
                html_content += f"""
                            <tr class="missing-right">
                                <td class="filename">{html.escape(r.archive_relative_path)}</td>
                                <td><span class="type-badge">{html.escape(r.archive_type.upper())}</span></td>
                                <td class="info">{member_count} members</td>
                                <td class="status"><span class="status-badge missing-left">Missing in Left</span></td>
                            </tr>"""
            
            html_content += """
                        </tbody>
                    </table>
                </div>
            </div>"""
        
        # Identical Archives Section
        if identical:
            html_content += f"""
            <div class="section" id="identical-archives">
                <div class="section-title">✓ Identical Archives ({len(identical)})</div>
                <div class="table-container">
                    <table class="comparison-table">
                        <thead>
                            <tr>
                                <th>Archive Path</th>
                                <th>Type</th>
                                <th>Members</th>
                                <th>Status</th>
                            </tr>
                        </thead>
                        <tbody>"""
            
            # Show first 50 identical
            for r in identical[:50]:
                member_count = len(r.members_folder1) if r.members_folder1 else 0
                html_content += f"""
                            <tr class="identical">
                                <td class="filename">{html.escape(r.archive_relative_path)}</td>
                                <td><span class="type-badge">{html.escape(r.archive_type.upper())}</span></td>
                                <td class="info">{member_count} members</td>
                                <td class="status"><span class="status-badge identical">✓ Identical</span></td>
                            </tr>"""
            
            if len(identical) > 50:
                html_content += f"""
                            <tr>
                                <td colspan="4" style="text-align:center;padding:20px;color:#718096;font-style:italic;">
                                    ... and {len(identical) - 50} more identical archives
                                </td>
                            </tr>"""
            
            html_content += """
                        </tbody>
                    </table>
                </div>
            </div>"""
        
        # No differences message
        if not only_in_folder1 and not only_in_folder2 and not differing:
            html_content += """
            <div class="section">
                <div style="text-align:center;padding:40px;background:#f0fdf4;border-radius:12px;">
                    <h2 style="color:#166534;margin:0;">✅ No Differences Found</h2>
                    <p style="color:#4ade80;margin-top:10px;">All archives are identical between the two folders.</p>
                </div>
            </div>"""
        
        html_content += """
            <a href="/" class="back-link">← Back to Home</a>
        </div>
    </div>
</body>
</html>"""
        
        return inject_report_section_controls(html_content)

class JarManifestResult:
    def __init__(self, jar_name: str):
        self.jar_name = jar_name
        self.path_folder1 = ""
        self.path_folder2 = ""
        self.version_folder1 = ""
        self.version_folder2 = ""
        self.error_folder1 = ""
        self.error_folder2 = ""


class JarManifestComparer:
    """Compare Implementation-Version from META-INF/MANIFEST.MF for a given JAR name in two folders."""

    def __init__(self, folder1_path: str, folder2_path: str, jar_name: str):
        self.folder1_path = Path(folder1_path)
        self.folder2_path = Path(folder2_path)
        self.jar_name = jar_name.strip()
        self.result = JarManifestResult(self.jar_name)
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
            
            # Estimate time remaining
            if self.comparison_start_time and self.processed_files > 0:
                import time
                elapsed_time = time.time() - self.comparison_start_time
                avg_time_per_file = elapsed_time / self.processed_files
                remaining_files = self.total_files - self.processed_files
                self.estimated_time_remaining = int(avg_time_per_file * remaining_files)
        
        self.current_file = current_file

    def _find_jar(self, base: Path):
        target = self.jar_name.lower()
        exact = None
        partial = None
        for root, _, files in os.walk(base):
            for f in files:
                if not f.lower().endswith('.jar'):
                    continue
                if f.lower() == target:
                    return str((Path(root) / f))
                if target in f.lower():
                    if partial is None:
                        partial = str((Path(root) / f))
        return exact or partial or ""

    def _read_manifest_version(self, jar_path: str):
        if not jar_path:
            return "", "Jar not found"
        try:
            with zipfile.ZipFile(jar_path, 'r') as zf:
                # Common manifest path
                manifest_path = 'META-INF/MANIFEST.MF'
                try:
                    data = zf.read(manifest_path)
                except Exception as e:
                    return "", f"Manifest read error: {e}"
            # Parse manifest properties (simple key: value lines)
            text = data.decode('utf-8', errors='replace')
            impl_version = ""
            for line in text.splitlines():
                if line.lower().startswith('implementation-version:'):
                    impl_version = line.split(':', 1)[1].strip()
                    break
            if impl_version:
                return impl_version, ""
            return "", "Implementation-Version not found"
        except Exception as e:
            return "", f"Jar open error: {e}"

    def compare_versions(self):
        import time
        self.comparison_start_time = time.time()
        self.comparison_error = None
        self.total_files = 1  # Only comparing one JAR
        self.processed_files = 0
        self.current_file = self.jar_name
        self._update_progress(self.jar_name)
        
        if not _path_exists(self.folder1_path) or not _path_exists(self.folder2_path):
            logger.error("One or both folders do not exist for JarManifestComparer")
            return self.result

        jar1 = self._find_jar(self.folder1_path)
        jar2 = self._find_jar(self.folder2_path)
        self.result.path_folder1 = jar1
        self.result.path_folder2 = jar2
        v1, e1 = self._read_manifest_version(jar1)
        v2, e2 = self._read_manifest_version(jar2)
        self.result.version_folder1 = v1
        self.result.version_folder2 = v2
        self.result.error_folder1 = e1
        self.result.error_folder2 = e2
        
        self.processed_files = 1
        self._update_progress(self.jar_name)
        
        return self.result

    def generate_report(self):
        r = self.result
        report = []
        report.append("=" * 80)
        report.append("FOSS/FOUNDATION JAR IMPLEMENTATION-VERSION COMPARISON")
        report.append("=" * 80)
        report.append(f"Folder 1: {self.folder1_path}")
        report.append(f"Folder 2: {self.folder2_path}")
        report.append(f"Target JAR name: {r.jar_name}")
        report.append("")
        report.append("RESULTS")
        report.append("-" * 40)
        report.append(f"Folder 1 jar: {r.path_folder1 or 'NOT FOUND'}")
        if r.error_folder1:
            report.append(f"  Note: {r.error_folder1}")
        report.append(f"Implementation-Version (Folder 1): {r.version_folder1 or 'N/A'}")
        report.append("")
        report.append(f"Folder 2 jar: {r.path_folder2 or 'NOT FOUND'}")
        if r.error_folder2:
            report.append(f"  Note: {r.error_folder2}")
        report.append(f"Implementation-Version (Folder 2): {r.version_folder2 or 'N/A'}")
        report.append("")
        same = (r.version_folder1 != "" and r.version_folder1 == r.version_folder2)
        if r.path_folder1 and r.path_folder2 and r.version_folder1 and r.version_folder2:
            if same:
                report.append("SUMMARY: No difference found (versions match).")
            else:
                report.append("SUMMARY: Versions differ.")
        else:
            report.append("SUMMARY: Could not compare versions (missing jar or version).")
        report.append("")
        return "\n".join(report)
    
    def save_report(self, output_file: str):
        report = self.generate_report()
        try:
            with open(output_file, 'w', encoding='utf-8') as f:
                f.write(report)
            logger.info(f"FOSS/Foundation report saved to: {output_file}")
            try:
                html = self.generate_html_report()
                with open(Path(output_file).with_suffix('.html'), 'w', encoding='utf-8') as hf:
                    hf.write(html)
                logger.info(f"FOSS/Foundation HTML report saved to: {Path(output_file).with_suffix('.html')}")
            except Exception as _e:
                logger.warning(f"Could not save FOSS/Foundation HTML report: {_e}")
        except Exception as e:
            logger.error(f"Error saving FOSS/Foundation report to {output_file}: {e}")
    
    def generate_html_report(self) -> str:
        r = self.result
        style = """
        <style>
        body{font-family:system-ui,Arial,sans-serif;padding:16px}
        h1{margin:0 0 4px 0}
        small{color:#6b7280}
        h2{color:#0d6efd}
        </style>
        """
        header = f"<h1>FOSS/Foundation JAR Version Comparison</h1><small>Folder 1: {html.escape(str(self.folder1_path))}<br/>Folder 2: {html.escape(str(self.folder2_path))}<br/>Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</small><p><a href='/'>← Back to main</a></p>"
        body = []
        body.append(f'<div class="section" id="target-jar"><h2>Target JAR: {html.escape(r.jar_name)}</h2>')
        body.append(f"<p><strong>Folder 1 jar:</strong> {html.escape(r.path_folder1 or 'NOT FOUND')}<br/>Implementation-Version: {html.escape(r.version_folder1 or 'N/A')}</p>")
        body.append(f"<p><strong>Folder 2 jar:</strong> {html.escape(r.path_folder2 or 'NOT FOUND')}<br/>Implementation-Version: {html.escape(r.version_folder2 or 'N/A')}</p></div>")
        same = (r.version_folder1 != "" and r.version_folder1 == r.version_folder2)
        if r.path_folder1 and r.path_folder2 and r.version_folder1 and r.version_folder2:
            summary = "No difference found (versions match)." if same else "Versions differ."
        else:
            summary = "Could not compare versions (missing jar or version)."
        body.append(f'<div class="section" id="summary"><h2>Summary</h2><p>{html.escape(summary)}</p></div>')
        return inject_report_section_controls(
            f"<html><head><meta charset='utf-8'/><title>FOSS/Foundation Report</title>{style}</head><body>{header}{''.join(body)}</body></html>"
        )
