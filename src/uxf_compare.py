#!/usr/bin/env python3
"""
UXF Application Comparison Module

This module provides comparison functionality specific to UXF application builds.
The comparison logic for UXF is similar to ASCF but maintained separately
to avoid any discrepancy with ASCF comparison behavior.
"""

import os
import re
import html
import zipfile
import tempfile
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from io import BytesIO

from report_html_sections import inject_report_section_controls, report_collapse_extra_css

# Import decompilation utilities from folder_compare
try:
    from folder_compare import _find_java_decompiler, _decompile_class_file, _compare_decompiled_sources, _extract_class_from_archive
except ImportError:
    # Define fallback functions if import fails
    def _find_java_decompiler():
        """Find available Java decompiler tools."""
        cfr_env = os.environ.get('CFR_JAR', '')
        if cfr_env and os.path.exists(cfr_env):
            return ('cfr', cfr_env)
        
        cfr_paths = [
            os.path.join(os.path.dirname(__file__), 'cfr.jar'),
            os.path.join(os.path.dirname(__file__), '..', 'cfr.jar'),
            os.path.join(os.getcwd(), 'cfr.jar'),
            'C:\\cfr-0.152.jar',
        ]
        
        for cfr_path in cfr_paths:
            if cfr_path and os.path.exists(cfr_path):
                return ('cfr', os.path.abspath(cfr_path))
        
        try:
            result = subprocess.run(['javap', '-version'], capture_output=True, timeout=5)
            if result.returncode == 0:
                return ('javap', 'javap')
        except Exception:
            pass
        
        return (None, None)
    
    def _decompile_class_file(class_bytes, class_name, decompiler_type='javap', decompiler_path=None):
        """Decompile a .class file bytes to Java source."""
        if not class_bytes:
            return (False, "", "Empty class file")
        
        temp_dir = None
        try:
            temp_dir = tempfile.mkdtemp(prefix='class_decompile_')
            simple_name = class_name.split('.')[-1] if '.' in class_name else class_name
            if not simple_name.endswith('.class'):
                simple_name += '.class'
            
            class_file_path = os.path.join(temp_dir, simple_name)
            with open(class_file_path, 'wb') as f:
                f.write(class_bytes)
            
            if decompiler_type == 'cfr' and decompiler_path:
                try:
                    result = subprocess.run(
                        ['java', '-jar', decompiler_path, '--showversion', 'false', '--silent', 'true', class_file_path],
                        capture_output=True, timeout=60, cwd=temp_dir
                    )
                    output = ""
                    for encoding in ['utf-8', 'latin-1', 'cp1252']:
                        try:
                            output = result.stdout.decode(encoding)
                            break
                        except UnicodeDecodeError:
                            continue
                    if output:
                        return (True, output, "")
                    return (False, "", "No output from CFR")
                except Exception as e:
                    return (False, "", f"CFR failed: {str(e)}")
            
            elif decompiler_type == 'javap':
                try:
                    result = subprocess.run(['javap', '-p', '-c', class_file_path], capture_output=True, timeout=30, text=True)
                    if result.returncode == 0:
                        return (True, result.stdout, "")
                    return (False, "", f"javap error: {result.stderr}")
                except Exception as e:
                    return (False, "", f"javap failed: {str(e)}")
            
            return (False, "", "No decompiler available")
        except Exception as e:
            return (False, "", f"Decompilation error: {str(e)}")
        finally:
            if temp_dir and os.path.exists(temp_dir):
                try:
                    shutil.rmtree(temp_dir)
                except Exception:
                    pass
    
    def _compare_decompiled_sources(source1, source2, class_name):
        """Compare two decompiled Java sources and generate a diff."""
        import difflib
        
        lines1 = source1.splitlines() if source1 else []
        lines2 = source2.splitlines() if source2 else []
        
        diff = list(difflib.unified_diff(lines1, lines2, fromfile=f'{class_name} (Old)', tofile=f'{class_name} (New)', lineterm=''))
        
        has_diff = len(diff) > 0
        added_lines = sum(1 for line in diff if line.startswith('+') and not line.startswith('+++'))
        removed_lines = sum(1 for line in diff if line.startswith('-') and not line.startswith('---'))
        
        diff_html = []
        if has_diff:
            diff_html.append("<div style='font-family:Consolas,monospace;font-size:11px;background:#1e1e1e;color:#d4d4d4;padding:10px;border-radius:6px;overflow-x:auto;max-height:400px;overflow-y:auto;'>")
            for line in diff[:200]:
                escaped_line = html.escape(line)
                if line.startswith('+++') or line.startswith('---'):
                    diff_html.append(f"<div style='color:#569cd6;font-weight:bold;'>{escaped_line}</div>")
                elif line.startswith('@@'):
                    diff_html.append(f"<div style='color:#c586c0;'>{escaped_line}</div>")
                elif line.startswith('+'):
                    diff_html.append(f"<div style='color:#4ec9b0;background:#1e3a2f;'>{escaped_line}</div>")
                elif line.startswith('-'):
                    diff_html.append(f"<div style='color:#f14c4c;background:#3a1e1e;'>{escaped_line}</div>")
                else:
                    diff_html.append(f"<div>{escaped_line}</div>")
            diff_html.append("</div>")
        
        return {
            'has_diff': has_diff,
            'diff_html': ''.join(diff_html),
            'added_lines': added_lines,
            'removed_lines': removed_lines
        }
    
    def _extract_class_from_archive(archive_path, member_path):
        """Extract a class file from a JAR/ZIP archive."""
        try:
            with zipfile.ZipFile(archive_path, 'r') as zf:
                try:
                    return zf.read(member_path)
                except KeyError:
                    pass
                normalized = member_path.replace('\\', '/')
                try:
                    return zf.read(normalized)
                except KeyError:
                    pass
                target_name = os.path.basename(member_path)
                for name in zf.namelist():
                    if name.endswith(target_name) or os.path.basename(name) == target_name:
                        return zf.read(name)
            return None
        except Exception:
            return None


class UXFComparer:
    """
    UXF-specific folder/build comparer.
    
    This class handles comparison logic that is specific to UXF application builds.
    It scans ZIP/JAR/WAR/EAR archives and compares their contents including class files.
    """
    
    def __init__(self, folder1: str, folder2: str):
        """Initialize UXF comparer with two folder paths."""
        self.folder1 = folder1
        self.folder2 = folder2
        self.results = []
        self.comparison_error = None
        self.suppress_warnings = True
        self.show_missing_only = False
        self.ignore_timestamp_only_differences = True
        
        # Comparison results
        self.archives1 = {}
        self.archives2 = {}
        self.only_in_1 = []
        self.only_in_2 = []
        self.in_both = []
        self.version_changes = []
        self.size_changes = []
        self.truly_new_in_1 = []
        self.truly_new_in_2 = []
        self.only_in_1_keys = []
        self.only_in_2_keys = []
        self.naming_convention_pairs = []
        self.lib_jar_naming_diffs = []
        
        # Class file diff results for modified JARs
        self.class_diffs = {}  # {jar_name: [{'class': name, 'diff': diff_result}]}
        
        # Member-level diffs for modified archives (what files changed inside)
        self.member_diffs = {}  # {archive_name: {'added': [], 'removed': [], 'modified': []}}
        
        # Decompiler info
        self.decompiler_type = None
        self.decompiler_path = None
        
        # Session logging and progress callback
        self.log_callback = None
        self.progress_callback = None
        
    def set_log_callback(self, callback):
        """Set a callback function for logging progress"""
        self.log_callback = callback
    
    def set_progress_callback(self, callback):
        """Set a callback function for progress updates: callback(current_file, progress, total)"""
        self.progress_callback = callback
        
    def _log(self, message, level="INFO"):
        """Log a message"""
        print(f"[UXF] [{level}] {message}")
        if self.log_callback:
            self.log_callback(message, level)
    
    def _update_progress(self, current_file, progress=None, total=None):
        """Update progress status"""
        if self.progress_callback:
            self.progress_callback(current_file, progress, total)
    
    def _get_base_name(self, filename):
        """Extract base name without version/build numbers for matching"""
        base = filename
        base = re.sub(r'-\d+\.\d+\.\d+\.[\w\d_.-]+\.(jar|war|ear|zip|rpm)$', r'.\1', base, flags=re.IGNORECASE)
        base = re.sub(r'__V\d+_\d+_\d+_\d+\.(jar|war|ear|zip)$', r'.\1', base, flags=re.IGNORECASE)
        base = re.sub(r'_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}', '', base)
        base = re.sub(r'_\d{8,}', '', base)
        base = re.sub(r'-\d+(\.\d+)*\.(jar|war|ear|zip|rpm)$', r'.\2', base, flags=re.IGNORECASE)
        return base.lower()

    @staticmethod
    def _uxf_strip_build_tokens(path: str) -> str:
        """Neutralize OMNI/UXF daily build tokens (BN5256 vs BN5249, zip folder names)."""
        s = str(path or "").replace("\\", "/")
        s = re.sub(r"(?i)\bBN\d+\b", "{BN}", s)
        s = re.sub(r"(?i)v(\d+(?:\.\d+)*)_BN\d+\.zip", r"v\1_{BN}.zip", s)
        s = re.sub(r"(?i)v(\d+(?:\.\d+)*)_BN\d+", r"v\1_{BN}", s)
        return s.lower()

    @staticmethod
    def _uxf_is_lib_compare_root(path: str) -> bool:
        """True when the compare root is (or ends at) cm-server WEB-INF/lib."""
        low = str(path or "").replace("\\", "/").lower().rstrip("/")
        if low.endswith("/web-inf/lib"):
            return True
        if low.endswith("/lib") and "/web-inf/" in low:
            return True
        return False

    def _uxf_lib_compare_mode(self) -> bool:
        """Both builds compare the same OMNI lib folder (often entered as WEB-INF/lib path)."""
        return self._uxf_is_lib_compare_root(self.folder1) or self._uxf_is_lib_compare_root(self.folder2)

    def _uxf_in_omni_compare_scope(self) -> bool:
        """True when comparing OMNI/CM_REP cm-server WEB-INF/lib JARs."""
        if self._uxf_lib_compare_mode():
            return True
        for raw in (self.folder1, self.folder2):
            low = str(raw or "").replace("\\", "/").lower()
            if "cm_rep" in low or ("web-inf" in low and "lib" in low):
                return True
        for archives in (getattr(self, "archives1", {}), getattr(self, "archives2", {})):
            for info in archives.values():
                fp = str(info.get("full_path") or "").replace("\\", "/").lower()
                if "cm-server.war" in fp or "rp-webapp" in fp or "web-inf/lib" in fp:
                    return True
        return False

    def _uxf_is_bare_lib_jar_path(self, full_path: str, filename: str) -> bool:
        """JAR indexed with leaf-only path when compare root is WEB-INF/lib."""
        fp = str(full_path or "").replace("\\", "/").strip("/")
        if not filename or "::" in fp:
            return False
        return fp.lower() == filename.lower()

    def _uxf_is_omni_lib_jar_path(self, full_path: str, filename: str) -> bool:
        """True when a JAR lives under cm-server WEB-INF/lib (OMNI compare scope)."""
        if self._uxf_is_bare_lib_jar_path(full_path, filename) and self._uxf_in_omni_compare_scope():
            return True
        fp = str(full_path or "").replace("\\", "/").lower()
        if "/web-inf/lib/" in fp:
            return True
        if fp.endswith("/web-inf/lib"):
            return True
        if filename and fp.endswith("/" + filename.lower()):
            return "/web-inf/" in fp or "/lib/" in fp
        return False

    def _uxf_archive_key(self, full_path: str, filename: str) -> str:
        """Logical archive key: neutral build path + version-stripped leaf name."""
        logical_name = self._get_base_name(filename)
        if filename.lower().endswith(".jar") and (
            self._uxf_lib_compare_mode() or self._uxf_is_omni_lib_jar_path(full_path, filename)
        ):
            return f"web-inf/lib::{logical_name}"
        if "::" in full_path:
            norm_path = "::".join(self._uxf_strip_build_tokens(p) for p in full_path.split("::"))
        else:
            norm_path = self._uxf_strip_build_tokens(full_path)
        norm_path = norm_path.replace("\\", "/")
        if filename and norm_path.lower().endswith(filename.lower()):
            norm_path = norm_path[: -len(filename)]
        else:
            leaf = norm_path.rsplit("/", 1)[-1] if "/" in norm_path else norm_path
            if leaf.lower().endswith((".jar", ".war", ".ear", ".zip")) and self._get_base_name(leaf) == logical_name:
                norm_path = norm_path.rsplit("/", 1)[0]
        norm_path = norm_path.rstrip("/:")
        return f"{norm_path}::{logical_name}"

    def _uxf_drop_false_orphans_by_cross_build_filename(
        self, in_both: set, only1: set, only2: set
    ) -> None:
        """Drop orphan keys when the same leaf JAR filename exists on the other build."""
        by_name2 = {v["name"].lower(): k for k, v in self.archives2.items()}
        by_name1 = {v["name"].lower(): k for k, v in self.archives1.items()}
        for k1 in list(only1):
            name = str((self.archives1.get(k1) or {}).get("name") or "").lower()
            if not name:
                continue
            k2 = by_name2.get(name)
            if k2 is None:
                continue
            only1.discard(k1)
            in_both.add(k1)
            if k1 != k2:
                self._uxf_key_pairs[k1] = k2
        for k2 in list(only2):
            name = str((self.archives2.get(k2) or {}).get("name") or "").lower()
            if not name:
                continue
            k1 = by_name1.get(name)
            if k1 is None:
                continue
            only2.discard(k2)
            in_both.add(k1)
            if k1 != k2:
                self._uxf_key_pairs[k1] = k2

    def _uxf_filter_version_changes(self) -> None:
        """Keep only true naming-convention changes (old name != new name)."""
        deduped: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for vc in self.version_changes:
            old_name = str(vc.get("old_name") or "")
            new_name = str(vc.get("new_name") or "")
            if not old_name or not new_name:
                continue
            if old_name.lower() == new_name.lower():
                continue
            pair = (old_name.lower(), new_name.lower())
            if pair in seen:
                continue
            seen.add(pair)
            deduped.append(vc)
        self.version_changes = deduped

    def _uxf_is_meaningful_size_change(self, sc: dict) -> bool:
        """Keep modified rows only when size changed or inner archive diffs exist."""
        jar_name = str(sc.get("name") or "")
        old_size = int(sc.get("old_size") or 0)
        new_size = int(sc.get("new_size") or 0)
        if old_size != new_size:
            return True
        md = self.member_diffs.get(jar_name) or {}
        if md.get("added") or md.get("removed") or md.get("modified"):
            return True
        if self.class_diffs.get(jar_name):
            return True
        return False

    def _uxf_prune_empty_size_changes(self) -> None:
        """Remove modified-archive noise (0-byte delta, no member/class diffs)."""
        self.size_changes = [
            sc for sc in self.size_changes if self._uxf_is_meaningful_size_change(sc)
        ]

    def _uxf_index_jars_by_logical(self, archives: dict) -> dict[str, list[dict]]:
        """Group WEB-INF/lib JAR entries by version-neutral logical name."""
        out: dict[str, list[dict]] = {}
        for info in archives.values():
            name = str(info.get("name") or "")
            if not name.lower().endswith(".jar"):
                continue
            base = self._get_base_name(name)
            out.setdefault(base, []).append(info)
        return out

    def _uxf_build_naming_convention_pairs(self) -> list[dict]:
        """
        Pair the same logical lib JAR across builds when leaf file names differ
        (e.g. acpe-portfolio-project ...772.jar vs ...758.jar).
        """
        idx1 = self._uxf_index_jars_by_logical(self.archives1)
        idx2 = self._uxf_index_jars_by_logical(self.archives2)
        pairs: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for base in sorted(set(idx1.keys()) & set(idx2.keys())):
            by_name1 = {str(i.get("name") or "").lower(): i for i in idx1[base] if i.get("name")}
            by_name2 = {str(i.get("name") or "").lower(): i for i in idx2[base] if i.get("name")}
            if not by_name1 or not by_name2:
                continue
            if set(by_name1) == set(by_name2):
                continue
            for o_low, i1 in by_name1.items():
                for n_low, i2 in by_name2.items():
                    if o_low == n_low:
                        continue
                    key = (o_low, n_low)
                    if key in seen:
                        continue
                    seen.add(key)
                    pairs.append({
                        "logical_name": base,
                        "old_name": str(i1.get("name") or ""),
                        "new_name": str(i2.get("name") or ""),
                        "old_info": i1,
                        "new_info": i2,
                        "old_path": str(i1.get("full_path") or ""),
                        "new_path": str(i2.get("full_path") or ""),
                    })
                    break
                else:
                    continue
                break
        return pairs

    def _uxf_register_archive(
        self,
        dest: dict,
        *,
        full_path: str,
        filename: str,
        size: int,
        crc: int,
        parent: str,
        source_path: Path = None,
    ) -> None:
        if self._uxf_lib_compare_mode() and str(filename or "").lower().endswith(".jar"):
            leaf = os.path.basename(str(filename or ""))
            full_path = f"WEB-INF/lib/{leaf}"
        key = self._uxf_archive_key(full_path, filename)
        dest[key] = {
            "name": filename,
            "full_path": full_path,
            "size": size,
            "crc": crc,
            "parent": parent,
            "source_path": str(source_path) if source_path else None,
        }

    def _uxf_reconcile_orphans_by_filename(self, in_both: set, only1: set, only2: set) -> None:
        """Pair remaining orphans that share the same leaf filename (expanded vs archive layouts)."""
        by_name1 = {self.archives1[k]["name"].lower(): k for k in only1}
        by_name2 = {self.archives2[k]["name"].lower(): k for k in only2}
        for name in list(by_name1.keys() & by_name2.keys()):
            k1, k2 = by_name1[name], by_name2[name]
            only1.discard(k1)
            only2.discard(k2)
            in_both.add(k1)
            if k1 != k2:
                self._uxf_key_pairs[k1] = k2

    def _uxf_display_label(self, archive_key: str, side: int) -> str:
        info = (self.archives1 if side == 1 else self.archives2).get(archive_key) or {}
        return str(info.get("full_path") or info.get("name") or archive_key)

    def _scan_filesystem_tree(self, root: Path) -> dict:
        """Index archives from an extracted UXF/OMNI tree (wars, zips, and leaf jars)."""
        all_archives: dict = {}
        supported_extensions = {".zip", ".jar", ".war", ".ear"}

        for f in root.rglob("*"):
            try:
                rel = f.relative_to(root).as_posix()
            except ValueError:
                continue

            if f.is_dir():
                if f.suffix.lower() in (".war", ".ear", ".zip"):
                    for jar in f.rglob("*.jar"):
                        if not jar.is_file():
                            continue
                        try:
                            jrel = jar.relative_to(root).as_posix()
                        except ValueError:
                            continue
                        st = jar.stat()
                        self._uxf_register_archive(
                            all_archives,
                            full_path=jrel,
                            filename=jar.name,
                            size=st.st_size,
                            crc=0,
                            parent=f.name,
                            source_path=jar,
                        )
                continue

            if not f.is_file():
                continue
            ext = f.suffix.lower()
            if ext not in supported_extensions:
                continue

            if ext == ".jar":
                st = f.stat()
                self._uxf_register_archive(
                    all_archives,
                    full_path=rel,
                    filename=f.name,
                    size=st.st_size,
                    crc=0,
                    parent=f.parent.name,
                    source_path=f,
                )
            else:
                nested = self._scan_zip_archive(f, prefix=f"{rel}::")
                all_archives.update(nested)

        return all_archives

    def _scan_zip_archive(self, zip_path, prefix=""):
        """Scan a ZIP/JAR/WAR/EAR file and extract all archives and members."""
        all_archives = {}
        
        def get_parent_folder(path):
            parts = path.replace('\\', '/').split('/')
            for i, part in enumerate(parts):
                if part and part != '.' and i < len(parts) - 1:
                    if part.lower() in ['uxf', 'lib', 'web-inf', 'meta-inf', 'classes', 'unsigned_jars', 'signed_jars']:
                        return part
                    if parts[i+1] and os.path.splitext(parts[-1])[1].lower() in ['.jar', '.war', '.ear']:
                        return part
            return os.path.basename(str(zip_path))
        
        def scan_nested(data, parent_name, full_prefix):
            nested = {}
            try:
                with zipfile.ZipFile(BytesIO(data), 'r') as zf:
                    for info in zf.infolist():
                        if info.is_dir():
                            continue
                        filename = os.path.basename(info.filename)
                        ext = os.path.splitext(filename)[1].lower()
                        nested_full_path = f"{full_prefix}{info.filename}"
                        
                        if ext in ['.jar', '.war', '.ear', '.zip']:
                            folder = get_parent_folder(info.filename)
                            self._uxf_register_archive(
                                nested,
                                full_path=nested_full_path,
                                filename=filename,
                                size=info.file_size,
                                crc=info.CRC,
                                parent=f"{parent_name}/{folder}" if folder != parent_name else parent_name,
                            )
                        
                        if ext in ['.war', '.ear', '.zip']:
                            try:
                                deeper_data = zf.read(info.filename)
                                deeper_prefix = f"{nested_full_path}::"
                                deeper = scan_nested(deeper_data, filename, deeper_prefix)
                                nested.update(deeper)
                            except Exception:
                                pass
            except Exception:
                pass
            return nested
        
        try:
            self._log(f"Scanning archive: {zip_path}")
            self._update_progress(f"Scanning: {os.path.basename(str(zip_path))}")
            
            with zipfile.ZipFile(str(zip_path), 'r') as zf:
                members = zf.namelist()
                total_members = len(members)
                
                for idx, member in enumerate(members):
                    if idx % 100 == 0:
                        self._update_progress(f"Scanning: {os.path.basename(str(zip_path))} ({idx}/{total_members})", idx, total_members)
                    
                    try:
                        info = zf.getinfo(member)
                    except KeyError:
                        continue
                        
                    if info.is_dir():
                        continue
                    filename = os.path.basename(info.filename)
                    ext = os.path.splitext(filename)[1].lower()
                    full_path = f"{prefix}{info.filename}"
                    
                    if ext in ['.jar', '.war', '.ear', '.zip']:
                        folder = get_parent_folder(info.filename)
                        self._uxf_register_archive(
                            all_archives,
                            full_path=full_path,
                            filename=filename,
                            size=info.file_size,
                            crc=info.CRC,
                            parent=folder,
                        )
                    
                    if ext in ['.war', '.ear', '.zip']:
                        try:
                            nested_data = zf.read(info.filename)
                            nested_prefix = f"{full_path}::"
                            nested = scan_nested(nested_data, filename, nested_prefix)
                            all_archives.update(nested)
                        except Exception:
                            pass
            
            self._log(f"Found {len(all_archives)} archives in {os.path.basename(str(zip_path))}")
        except Exception as e:
            self._log(f"Error reading archive {zip_path}: {e}", "ERROR")
            self.comparison_error = str(e)
        
        return all_archives

    def _extract_jar_bytes(self, info: dict, main_archive_path):
        """Read JAR bytes from a filesystem path or nested archive member."""
        source_path = info.get("source_path")
        if source_path and os.path.isfile(source_path):
            with open(source_path, "rb") as f:
                return f.read()
        full_path = info.get("full_path") or ""
        if not full_path or not main_archive_path:
            return None
        root = Path(main_archive_path)
        if root.is_file():
            return self._extract_jar_from_archive(root, full_path)
        if not root.is_dir():
            return None
        parts = full_path.split("::")
        if len(parts) == 1:
            candidate = root / parts[0]
            if candidate.is_file():
                with open(candidate, "rb") as f:
                    return f.read()
            return None
        archive_file = root / parts[0]
        if not archive_file.is_file():
            return None
        inner_path = "::".join(parts[1:])
        return self._extract_jar_from_archive(archive_file, inner_path)

    def _extract_jar_from_archive(self, main_archive_path, jar_full_path):
        """Extract a JAR file from the main archive (handling nested paths)."""
        try:
            parts = jar_full_path.split('::')
            
            with zipfile.ZipFile(str(main_archive_path), 'r') as zf:
                if len(parts) == 1:
                    return zf.read(parts[0])
                else:
                    current_data = zf.read(parts[0])
                    for part in parts[1:]:
                        with zipfile.ZipFile(BytesIO(current_data), 'r') as nested_zf:
                            current_data = nested_zf.read(part)
                    return current_data
        except Exception as e:
            self._log(f"Error extracting JAR {jar_full_path}: {e}", "WARNING")
            return None
    
    def _compare_jar_members(self, jar_name, jar_data1, jar_data2):
        """Compare all members inside two versions of a JAR."""
        member_diff = {'added': [], 'removed': [], 'modified': []}
        
        try:
            with zipfile.ZipFile(BytesIO(jar_data1), 'r') as zf1, \
                 zipfile.ZipFile(BytesIO(jar_data2), 'r') as zf2:
                
                members1 = {n: zf1.getinfo(n) for n in zf1.namelist() if not n.endswith('/')}
                members2 = {n: zf2.getinfo(n) for n in zf2.namelist() if not n.endswith('/')}
                
                set1 = set(members1.keys())
                set2 = set(members2.keys())
                
                # Find added/removed
                member_diff['removed'] = sorted(set1 - set2)
                member_diff['added'] = sorted(set2 - set1)
                
                # Find modified (same name, different size/crc)
                for name in set1 & set2:
                    info1 = members1[name]
                    info2 = members2[name]
                    if info1.file_size != info2.file_size or info1.CRC != info2.CRC:
                        member_diff['modified'].append({
                            'name': name,
                            'old_size': info1.file_size,
                            'new_size': info2.file_size,
                            'old_crc': info1.CRC,
                            'new_crc': info2.CRC
                        })
        except Exception as e:
            self._log(f"Error comparing JAR members in {jar_name}: {e}", "WARNING")
        
        return member_diff
    
    def _compare_jar_class_files(self, jar_name, jar_data1, jar_data2):
        """Compare class files inside two versions of a JAR and return diffs."""
        class_diffs = []
        
        if not self.decompiler_type:
            return class_diffs
        
        try:
            with zipfile.ZipFile(BytesIO(jar_data1), 'r') as zf1, \
                 zipfile.ZipFile(BytesIO(jar_data2), 'r') as zf2:
                
                classes1 = {n: zf1.getinfo(n) for n in zf1.namelist() if n.endswith('.class')}
                classes2 = {n: zf2.getinfo(n) for n in zf2.namelist() if n.endswith('.class')}
                
                common_classes = set(classes1.keys()) & set(classes2.keys())
                modified_classes = []
                
                for class_path in common_classes:
                    info1 = classes1[class_path]
                    info2 = classes2[class_path]
                    if info1.file_size != info2.file_size or info1.CRC != info2.CRC:
                        modified_classes.append(class_path)
                
                # Decompile modified classes (limit to first 10 for performance)
                for i, class_path in enumerate(modified_classes[:10]):
                    self._update_progress(f"Decompiling: {os.path.basename(class_path)}")
                    
                    class_bytes1 = zf1.read(class_path)
                    class_bytes2 = zf2.read(class_path)
                    
                    class_name = os.path.basename(class_path)
                    success1, source1, err1 = _decompile_class_file(
                        class_bytes1, class_name, self.decompiler_type, self.decompiler_path
                    )
                    success2, source2, err2 = _decompile_class_file(
                        class_bytes2, class_name, self.decompiler_type, self.decompiler_path
                    )
                    
                    if success1 and success2:
                        diff_result = _compare_decompiled_sources(source1, source2, class_name)
                        if diff_result['has_diff']:
                            class_diffs.append({
                                'class': class_path,
                                'class_name': class_name,
                                'old_size': classes1[class_path].file_size,
                                'new_size': classes2[class_path].file_size,
                                'diff': diff_result,
                                'source1': source1,
                                'source2': source2
                            })
                
                # Note if there are more modified classes
                if len(modified_classes) > 10:
                    self._log(f"  Note: {len(modified_classes) - 10} more modified class files not decompiled", "INFO")
                    
        except Exception as e:
            self._log(f"Error comparing class files in {jar_name}: {e}", "WARNING")
        
        return class_diffs
        
    def compare_folders(self):
        """Perform UXF-specific folder comparison."""
        self.results = []
        self._log(f"Starting UXF comparison")
        self._log(f"Old Build: {self.folder1}")
        self._log(f"New Build: {self.folder2}")
        self._update_progress("Initializing comparison...", 0, 100)
        
        # Initialize decompiler
        self.decompiler_type, self.decompiler_path = _find_java_decompiler()
        if self.decompiler_type:
            self._log(f"Found decompiler: {self.decompiler_type} at {self.decompiler_path}")
        else:
            self._log("No Java decompiler found - class file diffs will not be available", "WARNING")
        
        p1 = Path(self.folder1)
        p2 = Path(self.folder2)
        
        supported_extensions = ['.zip', '.jar', '.war', '.ear']
        
        self.main_archive1 = None
        self.main_archive2 = None
        
        # Scan first archive
        self._update_progress("Scanning old build...", 5, 100)
        if p1.exists() and p1.is_file() and p1.suffix.lower() in supported_extensions:
            self._log(f"Old Build is an archive: {p1.suffix}")
            self.archives1 = self._scan_zip_archive(p1)
            self.main_archive1 = p1
        elif p1.exists() and p1.is_dir():
            self._log(f"Scanning folder for archives: {p1}")
            self.archives1 = self._scan_filesystem_tree(p1)
            self.main_archive1 = p1
        else:
            self._log(f"Old Build path not found: {self.folder1}", "ERROR")
            self.comparison_error = f"Old Build path not found: {self.folder1}"
            return self.results
        
        # Scan second archive
        self._update_progress("Scanning new build...", 25, 100)
        if p2.exists() and p2.is_file() and p2.suffix.lower() in supported_extensions:
            self._log(f"New Build is an archive: {p2.suffix}")
            self.archives2 = self._scan_zip_archive(p2)
            self.main_archive2 = p2
        elif p2.exists() and p2.is_dir():
            self._log(f"Scanning folder for archives: {p2}")
            self.archives2 = self._scan_filesystem_tree(p2)
            self.main_archive2 = p2
        else:
            self._log(f"New Build path not found: {self.folder2}", "ERROR")
            self.comparison_error = f"New Build path not found: {self.folder2}"
            return self.results
        
        # Compare archives
        self._update_progress("Comparing archives...", 45, 100)
        self._log(f"Comparing {len(self.archives1)} vs {len(self.archives2)} archives")
        
        set1 = set(self.archives1.keys())
        set2 = set(self.archives2.keys())

        in_both_set = set1 & set2
        exact_only_in_1 = set1 - set2
        exact_only_in_2 = set2 - set1
        self._uxf_key_pairs = {}

        self._uxf_reconcile_orphans_by_filename(in_both_set, exact_only_in_1, exact_only_in_2)
        self._uxf_drop_false_orphans_by_cross_build_filename(
            in_both_set, exact_only_in_1, exact_only_in_2
        )

        self.version_changes = []
        self.size_changes = []

        # Pair remaining orphans by version-stripped leaf name (1:1 per logical jar).
        base_names1: dict[str, list[str]] = {}
        base_names2: dict[str, list[str]] = {}
        for k in exact_only_in_1:
            base_names1.setdefault(self._get_base_name(self.archives1[k]["name"]), []).append(k)
        for k in exact_only_in_2:
            base_names2.setdefault(self._get_base_name(self.archives2[k]["name"]), []).append(k)
        common_bases = set(base_names1.keys()) & set(base_names2.keys())

        for base in sorted(common_bases):
            k1 = base_names1[base][0]
            k2 = base_names2[base][0]
            info1 = self.archives1[k1]
            info2 = self.archives2[k2]
            old_name = str(info1["name"] or "")
            new_name = str(info2["name"] or "")
            if old_name.lower() == new_name.lower():
                exact_only_in_1.discard(k1)
                exact_only_in_2.discard(k2)
                in_both_set.add(k1)
                if k1 != k2:
                    self._uxf_key_pairs[k1] = k2
                continue
            self.version_changes.append({
                "old_name": old_name,
                "new_name": new_name,
                "old_info": info1,
                "new_info": info2,
                "old_path": info1["full_path"],
                "new_path": info2["full_path"],
            })
            exact_only_in_1.discard(k1)
            exact_only_in_2.discard(k2)
            in_both_set.add(k1)
            if k1 != k2:
                self._uxf_key_pairs[k1] = k2

        self.in_both = sorted(in_both_set)
        self.only_in_1 = sorted(self._uxf_display_label(k, 1) for k in exact_only_in_1)
        self.only_in_2 = sorted(self._uxf_display_label(k, 2) for k in exact_only_in_2)
        self.only_in_1_keys = sorted(exact_only_in_1)
        self.only_in_2_keys = sorted(exact_only_in_2)
        self.truly_new_in_1 = list(self.only_in_1)
        self.truly_new_in_2 = list(self.only_in_2)

        # Check for version renames and size/content changes in matching archives
        self._update_progress("Finding modified archives...", 55, 100)
        for key in self.in_both:
            info1 = self.archives1[key]
            key2 = self._uxf_key_pairs.get(key, key)
            info2 = self.archives2.get(key2) or self.archives2.get(key)
            if not info2:
                continue
            if info1["name"].lower() != info2["name"].lower():
                self.version_changes.append({
                    "old_name": info1["name"],
                    "new_name": info2["name"],
                    "old_info": info1,
                    "new_info": info2,
                    "old_path": info1["full_path"],
                    "new_path": info2["full_path"],
                })
            if info1["size"] != info2["size"] or (
                info1["crc"] and info2["crc"] and info1["crc"] != info2["crc"]
            ):
                self.size_changes.append({
                    "key": key,
                    "name": info1["name"],
                    "old_size": info1["size"],
                    "new_size": info2["size"],
                    "old_crc": info1["crc"],
                    "new_crc": info2["crc"],
                    "old_path": info1["full_path"],
                    "new_path": info2["full_path"],
                    "old_info": info1,
                    "new_info": info2,
                })

        self._uxf_filter_version_changes()

        self.naming_convention_pairs = self._uxf_build_naming_convention_pairs()
        vc_pairs = {
            (str(v.get("old_name") or "").lower(), str(v.get("new_name") or "").lower())
            for v in self.version_changes
        }
        for pair in self.naming_convention_pairs:
            key = (pair["old_name"].lower(), pair["new_name"].lower())
            if key not in vc_pairs:
                self.version_changes.append(pair)
                vc_pairs.add(key)

        self._uxf_sync_lib_jar_naming_diffs()
        
        # Analyze modified JARs - compare members and class files
        if self.main_archive1 and self.main_archive2:
            total_modified = len(self.size_changes)
            for idx, sc in enumerate(self.size_changes):
                jar_name = sc["name"]
                progress_pct = 60 + int((idx / max(total_modified, 1)) * 30)
                self._update_progress(f"Analyzing: {jar_name}", progress_pct, 100)

                if jar_name.lower().endswith(".jar"):
                    self._log(f"  Analyzing: {jar_name}")

                    jar_data1 = self._extract_jar_bytes(sc["old_info"], self.main_archive1)
                    jar_data2 = self._extract_jar_bytes(sc["new_info"], self.main_archive2)
                    
                    if jar_data1 and jar_data2:
                        # Compare all members
                        member_diff = self._compare_jar_members(jar_name, jar_data1, jar_data2)
                        if member_diff['added'] or member_diff['removed'] or member_diff['modified']:
                            self.member_diffs[jar_name] = member_diff
                        
                        # Compare class files with decompilation
                        if self.decompiler_type:
                            diffs = self._compare_jar_class_files(jar_name, jar_data1, jar_data2)
                            if diffs:
                                self.class_diffs[jar_name] = diffs
                                self._log(f"    Found {len(diffs)} class file differences")

        self._uxf_prune_empty_size_changes()
        
        # Build results
        self._update_progress("Building results...", 95, 100)
        for name in self.only_in_1:
            self.results.append(f"REMOVED: {name}")
        for name in self.only_in_2:
            self.results.append(f"ADDED: {name}")
        for vc in self.version_changes:
            self.results.append(f"VERSION CHANGE: {vc['old_name']} -> {vc['new_name']}")
        for sc in self.size_changes:
            self.results.append(f"MODIFIED: {sc['name']} (size: {sc['old_size']} -> {sc['new_size']})")
        
        self._update_progress("Comparison complete!", 100, 100)
        self._log(f"Comparison complete: {len(self.results)} differences found")
        self._log(f"  - Only in Old: {len(self.only_in_1)}")
        self._log(f"  - Only in New: {len(self.only_in_2)}")
        self._log(f"  - Version Changes: {len(self.version_changes)}")
        self._log(f"  - JAR naming diffs (lib): {len(self.lib_jar_naming_diffs)}")
        self._log(f"  - Size/Content Changes: {len(self.size_changes)}")
        self._log(f"  - Exact Matches: {len(self.in_both)}")
        self._log(f"  - JARs with member diffs: {len(self.member_diffs)}")
        self._log(f"  - JARs with class diffs: {len(self.class_diffs)}")
        
        return self.results

    def _uxf_report_compare_base_path(self) -> str:
        """
        OMNI/UXF JAR compare location shown once in the report.
        Neutralizes BN tokens (BN5256 vs BN5249) in the displayed path.
        """
        omni_default = (
            r"\\RAAISILON\cmprod_dev\pbg\cmdev\CM_REP\v10.1\Daily\{BN}\All"
            r"\v10.1_{BN}.zip\v10.1\server\rp-webapp\target\cm-server.war\WEB-INF\lib"
        )
        marker_win = "web-inf\\lib"
        marker_posix = "web-inf/lib"
        for raw in (self.folder1, self.folder2):
            p = str(raw or "").strip()
            if not p:
                continue
            norm = p.replace("/", "\\")
            low = norm.lower()
            if marker_win in low:
                end = low.index(marker_win) + len(marker_win)
                base = norm[:end]
                base = re.sub(r"(?i)\bBN\d+\b", "{BN}", base)
                base = re.sub(r"(?i)v(\d+(?:\.\d+)*)_BN\d+\.zip", r"v\1_{BN}.zip", base)
                return base

        omni_layout = False
        for info in list(self.archives1.values()) + list(self.archives2.values()):
            fp = str(info.get("full_path") or "").replace("\\", "/").lower()
            if "cm-server.war" in fp and marker_posix in fp:
                omni_layout = True
                break
            if marker_posix in fp and "rp-webapp" in fp:
                omni_layout = True
                break

        if omni_layout or "cm_rep" in str(self.folder1 or "").lower() or "cm_rep" in str(self.folder2 or "").lower():
            return omni_default

        for info in list(self.archives1.values()) + list(self.archives2.values()):
            fp = str(info.get("full_path") or "").replace("\\", "/")
            low = fp.lower()
            if marker_posix in low:
                end = low.index(marker_posix) + len(marker_posix)
                rel = fp[:end]
                rel = self._uxf_strip_build_tokens(rel).replace("/", "\\")
                rel = re.sub(r"\{bn\}", "{BN}", rel, flags=re.IGNORECASE)
                rel = re.sub(r"web-inf", "WEB-INF", rel, flags=re.IGNORECASE)
                return rel

        return omni_default

    def _uxf_collect_all_lib_naming_diffs(self) -> list[dict]:
        """
        JARs present on both builds (same logical lib artifact) with different leaf file names.
        Authoritative for OMNI WEB-INF/lib compare — does not rely on orphan lists.
        """
        if self._uxf_use_lib_naming_table():
            return [
                {
                    "logical_name": r["logical_name"],
                    "old_name": r["old_leaf"],
                    "new_name": r["new_leaf"],
                    "old_info": {},
                    "new_info": {},
                    "old_path": r.get("old_name"),
                    "new_path": r.get("new_name"),
                }
                for r in self._uxf_build_complete_lib_naming_rows()
                if r.get("naming_diff")
                and r.get("old_leaf")
                and r.get("new_leaf")
                and str(r["old_leaf"]).lower() != str(r["new_leaf"]).lower()
            ]

        rows: list[dict] = []
        seen_pairs: set[tuple[str, str]] = set()

        def _append(old_name: str, new_name: str, *, old_info: dict = None, new_info: dict = None) -> None:
            old_name = self._uxf_leaf_jar_name(old_name)
            new_name = self._uxf_leaf_jar_name(new_name)
            if not old_name or not new_name:
                return
            if old_name.lower() == new_name.lower():
                return
            pair = (old_name.lower(), new_name.lower())
            if pair in seen_pairs:
                return
            seen_pairs.add(pair)
            rows.append({
                "logical_name": self._get_base_name(old_name or new_name),
                "old_name": old_name,
                "new_name": new_name,
                "old_info": old_info or {},
                "new_info": new_info or {},
                "old_path": str((old_info or {}).get("full_path") or old_name),
                "new_path": str((new_info or {}).get("full_path") or new_name),
            })

        for key in getattr(self, "in_both", []) or []:
            info1 = self.archives1.get(key) or {}
            name1 = str(info1.get("name") or "")
            if not name1.lower().endswith(".jar"):
                continue
            key2 = self._uxf_key_pairs.get(key, key)
            info2 = self.archives2.get(key2) or self.archives2.get(key) or {}
            name2 = str(info2.get("name") or "")
            if name1 and name2:
                _append(name1, name2, old_info=info1, new_info=info2)

        idx1 = self._uxf_index_jars_by_logical(self.archives1)
        idx2 = self._uxf_index_jars_by_logical(self.archives2)
        for base in sorted(set(idx1.keys()) & set(idx2.keys())):
            by_name1 = {str(i.get("name") or "").lower(): i for i in idx1[base] if i.get("name")}
            by_name2 = {str(i.get("name") or "").lower(): i for i in idx2[base] if i.get("name")}
            if not by_name1 or not by_name2 or set(by_name1) == set(by_name2):
                continue
            for o_low, i1 in by_name1.items():
                for n_low, i2 in by_name2.items():
                    if o_low == n_low:
                        continue
                    _append(
                        str(i1.get("name") or ""),
                        str(i2.get("name") or ""),
                        old_info=i1,
                        new_info=i2,
                    )
                    break
                else:
                    continue
                break

        rows.sort(key=lambda r: (str(r.get("logical_name") or ""), str(r.get("old_name") or "")))
        return rows

    def _uxf_sync_lib_jar_naming_diffs(self) -> None:
        """Populate lib_jar_naming_diffs and ensure version_changes includes every naming pair."""
        rows = self._uxf_build_complete_lib_naming_rows() if self._uxf_use_lib_naming_table() else []
        self.lib_jar_naming_diffs = [
            {
                "logical_name": r["logical_name"],
                "old_name": r["old_leaf"],
                "new_name": r["new_leaf"],
                "old_info": {},
                "new_info": {},
                "old_path": r.get("old_name"),
                "new_path": r.get("new_name"),
            }
            for r in rows
            if r.get("naming_diff")
            and r.get("old_leaf")
            and r.get("new_leaf")
            and str(r["old_leaf"]).lower() != str(r["new_leaf"]).lower()
        ]
        if not self.lib_jar_naming_diffs:
            self.lib_jar_naming_diffs = self._uxf_collect_all_lib_naming_diffs()
        existing = {
            (str(v.get("old_name") or "").lower(), str(v.get("new_name") or "").lower())
            for v in self.version_changes
        }
        for diff in self.lib_jar_naming_diffs:
            old_leaf = self._uxf_leaf_jar_name(diff.get("old_name") or "")
            new_leaf = self._uxf_leaf_jar_name(diff.get("new_name") or "")
            pair = (old_leaf.lower(), new_leaf.lower())
            if not old_leaf or not new_leaf or pair in existing:
                continue
            self.version_changes.append({
                "old_name": old_leaf,
                "new_name": new_leaf,
                "old_info": diff.get("old_info") or {},
                "new_info": diff.get("new_info") or {},
                "old_path": diff.get("old_path") or old_leaf,
                "new_path": diff.get("new_path") or new_leaf,
            })
            existing.add(pair)
        self._uxf_filter_version_changes()

    @staticmethod
    def _uxf_leaf_jar_name(name: str) -> str:
        return os.path.basename(str(name or "").replace("\\", "/"))

    def _uxf_jar_report_path(self, info: dict, side: int) -> str:
        """Full path for report cells (UNC when available)."""
        if not info:
            return ""
        sp = info.get("source_path")
        if sp:
            return str(sp)
        leaf = self._uxf_leaf_jar_name(info.get("name") or "")
        fp = str(info.get("full_path") or "").replace("\\", "/")
        root = str(self.folder1 if side == 1 else self.folder2 or "")
        if root and leaf:
            if fp and fp.lower().endswith(leaf.lower()):
                return str(Path(root) / Path(fp))
            return str(Path(root) / leaf)
        return leaf or fp

    def _uxf_use_lib_naming_table(self) -> bool:
        if self._uxf_lib_compare_mode():
            return True
        for raw in (self.folder1, self.folder2):
            if "cm_rep" in str(raw or "").lower():
                return True
        for info in list(self.archives1.values()) + list(self.archives2.values()):
            fp = str(info.get("full_path") or "").replace("\\", "/").lower()
            if "web-inf/lib" in fp or "/lib/" in fp:
                return True
        return False

    def _uxf_pick_jar_info(self, jars: list[dict], other_jars: list[dict]) -> dict:
        if not jars:
            return {}
        if len(jars) == 1:
            return jars[0]
        if not other_jars:
            return jars[0]
        other_leaves = {self._uxf_leaf_jar_name(j.get("name")).lower() for j in other_jars if j.get("name")}
        for jar in jars:
            leaf = self._uxf_leaf_jar_name(jar.get("name")).lower()
            if leaf in other_leaves:
                return jar
        return jars[0]

    def _uxf_build_complete_lib_naming_rows(self) -> list[dict]:
        """
        One row per logical WEB-INF/lib JAR from both builds.
        Uses leaf file names only to decide naming diffs (avoids false highlights).
        """
        idx1 = self._uxf_index_jars_by_logical(self.archives1)
        idx2 = self._uxf_index_jars_by_logical(self.archives2)
        rows: list[dict] = []

        for base in sorted(set(idx1.keys()) | set(idx2.keys())):
            list1 = idx1.get(base, [])
            list2 = idx2.get(base, [])
            info1 = self._uxf_pick_jar_info(list1, list2)
            info2 = self._uxf_pick_jar_info(list2, list1)
            leaf1 = self._uxf_leaf_jar_name(info1.get("name") if info1 else "")
            leaf2 = self._uxf_leaf_jar_name(info2.get("name") if info2 else "")
            path1 = self._uxf_jar_report_path(info1, 1) if info1 else ""
            path2 = self._uxf_jar_report_path(info2, 2) if info2 else ""

            if leaf1 and leaf2:
                if leaf1.lower() == leaf2.lower():
                    status = "Present in both"
                    naming_diff = False
                    orphan = False
                    orphan_side = ""
                else:
                    status = "Different naming convention"
                    naming_diff = True
                    orphan = False
                    orphan_side = ""
            elif leaf1:
                status = "Only in old build"
                naming_diff = False
                orphan = True
                orphan_side = "old"
            else:
                status = "Only in new build"
                naming_diff = False
                orphan = True
                orphan_side = "new"

            rows.append({
                "logical_name": base,
                "old_name": path1,
                "new_name": path2,
                "old_leaf": leaf1,
                "new_leaf": leaf2,
                "naming_diff": naming_diff,
                "orphan": orphan,
                "orphan_side": orphan_side,
                "status": status,
            })

        rows.sort(
            key=lambda r: (
                0 if r.get("status") == "Different naming convention" else 1,
                0 if r.get("status") == "Only in old build" else 1,
                0 if r.get("status") == "Only in new build" else 1,
                str(r.get("logical_name") or ""),
            )
        )
        return rows

    def _uxf_build_naming_alignment_rows(self) -> list[dict]:
        """Side-by-side rows for lib JAR naming across old/new builds."""
        if self._uxf_use_lib_naming_table():
            return self._uxf_build_complete_lib_naming_rows()

        rows: list[dict] = []
        seen_logical: set[str] = set()
        seen_pairs: set[tuple[str, str]] = set()

        def _add_row(
            *,
            logical: str,
            old_name: str,
            new_name: str,
            orphan_side: str = "",
            force_naming_diff: bool = False,
        ) -> None:
            logical_key = logical or self._get_base_name(old_name or new_name)
            old_name = str(old_name or "")
            new_name = str(new_name or "")
            if old_name and new_name and old_name.lower() == new_name.lower():
                return
            pair_key = (old_name.lower(), new_name.lower())
            if pair_key in seen_pairs:
                return
            seen_pairs.add(pair_key)
            seen_logical.add(logical_key)
            orphan = bool(orphan_side) or not old_name or not new_name
            naming_diff = force_naming_diff or bool(
                old_name and new_name and old_name.lower() != new_name.lower()
            )
            rows.append({
                "logical_name": logical_key,
                "old_name": old_name,
                "new_name": new_name,
                "naming_diff": naming_diff,
                "orphan": orphan,
                "orphan_side": orphan_side,
            })

        for diff in getattr(self, "lib_jar_naming_diffs", []) or self._uxf_collect_all_lib_naming_diffs():
            _add_row(
                logical=str(diff.get("logical_name") or ""),
                old_name=str(diff.get("old_name") or ""),
                new_name=str(diff.get("new_name") or ""),
                force_naming_diff=True,
            )

        for pair in getattr(self, "naming_convention_pairs", []) or []:
            _add_row(
                logical=str(pair.get("logical_name") or ""),
                old_name=str(pair.get("old_name") or ""),
                new_name=str(pair.get("new_name") or ""),
                force_naming_diff=True,
            )

        for vc in self.version_changes:
            old_name = str(vc.get("old_name") or "")
            new_name = str(vc.get("new_name") or "")
            if old_name.lower() == new_name.lower():
                continue
            _add_row(
                logical=self._get_base_name(old_name or new_name),
                old_name=old_name,
                new_name=new_name,
                force_naming_diff=True,
            )

        idx1 = self._uxf_index_jars_by_logical(self.archives1)
        idx2 = self._uxf_index_jars_by_logical(self.archives2)
        only_bases_1 = set(idx1) - set(idx2)
        only_bases_2 = set(idx2) - set(idx1)
        for base in sorted(only_bases_1):
            if base in seen_logical:
                continue
            info = idx1[base][0]
            _add_row(
                logical=base,
                old_name=str(info.get("name") or ""),
                new_name="",
                orphan_side="old",
            )
        for base in sorted(only_bases_2):
            if base in seen_logical:
                continue
            info = idx2[base][0]
            _add_row(
                logical=base,
                old_name="",
                new_name=str(info.get("name") or ""),
                orphan_side="new",
            )

        only1_keys = list(getattr(self, "only_in_1_keys", []) or [])
        only2_keys = list(getattr(self, "only_in_2_keys", []) or [])
        by_base2 = {
            self._get_base_name(self.archives2[k]["name"]): k
            for k in only2_keys
        }
        matched2: set[str] = set()

        for k1 in only1_keys:
            info1 = self.archives1.get(k1) or {}
            old_name = str(info1.get("name") or "")
            base = self._get_base_name(old_name)
            if base in seen_logical:
                continue
            k2 = by_base2.get(base)
            if k2:
                matched2.add(k2)
                new_name = str((self.archives2.get(k2) or {}).get("name") or "")
                if old_name.lower() != new_name.lower():
                    _add_row(logical=base, old_name=old_name, new_name=new_name, force_naming_diff=True)
            else:
                _add_row(logical=base, old_name=old_name, new_name="", orphan_side="old")

        for k2 in only2_keys:
            if k2 in matched2:
                continue
            info2 = self.archives2.get(k2) or {}
            new_name = str(info2.get("name") or "")
            base = self._get_base_name(new_name)
            if base in seen_logical:
                continue
            _add_row(logical=base, old_name="", new_name=new_name, orphan_side="new")

        rows = self._uxf_merge_orphan_naming_rows(rows)

        rows.sort(
            key=lambda r: (
                0 if r.get("orphan") else 1,
                str(r.get("logical_name") or ""),
                str(r.get("old_name") or ""),
                str(r.get("new_name") or ""),
            )
        )
        return rows

    def _uxf_merge_orphan_naming_rows(self, rows: list[dict]) -> list[dict]:
        """
        Merge separate only-old / only-new orphan rows that share the same logical JAR
        into one highlighted side-by-side naming row (e.g. acpe ...758 vs ...772).
        """
        paired: list[dict] = []
        orphan_old: dict[str, dict] = {}
        orphan_new: dict[str, dict] = {}
        for row in rows:
            side = str(row.get("orphan_side") or "")
            logical = str(row.get("logical_name") or "")
            if row.get("orphan") and side == "old" and logical:
                orphan_old[logical] = row
            elif row.get("orphan") and side == "new" and logical:
                orphan_new[logical] = row
            else:
                paired.append(row)

        merged_logical: set[str] = set()
        for logical in sorted(set(orphan_old.keys()) & set(orphan_new.keys())):
            old_row = orphan_old[logical]
            new_row = orphan_new[logical]
            old_name = str(old_row.get("old_name") or "")
            new_name = str(new_row.get("new_name") or "")
            if not old_name or not new_name:
                continue
            if old_name.lower() == new_name.lower():
                continue
            paired.append({
                "logical_name": logical,
                "old_name": old_name,
                "new_name": new_name,
                "naming_diff": True,
                "orphan": False,
                "orphan_side": "",
            })
            merged_logical.add(logical)

        for logical, row in orphan_old.items():
            if logical not in merged_logical:
                paired.append(row)
        for logical, row in orphan_new.items():
            if logical not in merged_logical:
                paired.append(row)
        return paired

    @staticmethod
    def _uxf_html_name_cell(name: str, *, highlight: bool, tone: str = "", wrap: bool = False) -> str:
        label = html.escape(name) if name else "<span style='color:#94a3b8;'>—</span>"
        wrap_style = (
            "word-break:break-all;overflow-wrap:anywhere;white-space:normal;"
            if wrap
            else ""
        )
        if not name:
            return (
                f"<td class='uxf-naming-path-cell' style='font-family:Consolas,monospace;"
                f"font-size:12px;color:#94a3b8;{wrap_style}'>{label}</td>"
            )
        style = f"font-family:Consolas,monospace;font-size:12px;{wrap_style}"
        if highlight:
            style += "background:#fef3c7;color:#92400e;font-weight:700;"
        elif tone == "old":
            style += "background:#fef2f2;color:#991b1b;"
        elif tone == "new":
            style += "background:#f0fdf4;color:#166534;"
        cell_class = " class='uxf-naming-path-cell'" if wrap else ""
        return f"<td{cell_class} style='{style}'>{label}</td>"

    def _uxf_html_naming_side_by_side_table(self, rows: list[dict], *, section_id: str) -> str:
        if not rows:
            return ""
        base_path = self._uxf_report_compare_base_path()

        def _is_true_naming_diff(r: dict) -> bool:
            leaf1 = self._uxf_leaf_jar_name(r.get("old_leaf") or r.get("old_name") or "")
            leaf2 = self._uxf_leaf_jar_name(r.get("new_leaf") or r.get("new_name") or "")
            return bool(leaf1 and leaf2 and leaf1.lower() != leaf2.lower())

        naming_diff_rows = [r for r in rows if _is_true_naming_diff(r)]
        alignment_rows = [r for r in rows if r not in naming_diff_rows]
        present_both = sum(1 for r in alignment_rows if r.get("status") == "Present in both")
        only_old = sum(1 for r in alignment_rows if r.get("status") == "Only in old build")
        only_new = sum(1 for r in alignment_rows if r.get("status") == "Only in new build")

        def _row_html(r: dict, *, highlight_naming: bool) -> str:
            old_name = str(r.get("old_name") or "")
            new_name = str(r.get("new_name") or "")
            status = str(r.get("status") or "")
            if not status:
                orphan_side = str(r.get("orphan_side") or "")
                if orphan_side == "old":
                    status = "Only in old build"
                elif orphan_side == "new":
                    status = "Only in new build"
                elif highlight_naming:
                    status = "Different naming convention"
                elif old_name and new_name:
                    status = "Present in both"
                else:
                    status = "—"
            row_style = ""
            if highlight_naming:
                row_style = "background:#fffbeb;"
            elif status == "Present in both":
                row_style = "background:#f8fafc;"
            return (
                f"<tr style='{row_style}'>"
                f"<td style='font-family:Consolas,monospace;font-size:12px;color:#475569;'>"
                f"{html.escape(str(r.get('logical_name') or ''))}</td>"
                f"{self._uxf_html_name_cell(old_name, highlight=highlight_naming, tone='old' if status == 'Only in old build' else '', wrap=True)}"
                f"{self._uxf_html_name_cell(new_name, highlight=highlight_naming, tone='new' if status == 'Only in new build' else '', wrap=True)}"
                f"<td style='font-size:12px;'>{html.escape(status)}</td>"
                "</tr>"
            )

        naming_body = "".join(_row_html(r, highlight_naming=True) for r in naming_diff_rows)
        alignment_body = "".join(_row_html(r, highlight_naming=False) for r in alignment_rows)

        naming_list_html = ""
        if naming_diff_rows:
            items = "".join(
                f"<li><code>{html.escape(self._uxf_leaf_jar_name(r.get('old_leaf') or r.get('old_name') or ''))}</code>"
                f" → <code>{html.escape(self._uxf_leaf_jar_name(r.get('new_leaf') or r.get('new_name') or ''))}</code></li>"
                for r in naming_diff_rows
            )
            naming_list_html = f"""
                <div style="margin:0 0 16px 0;padding:12px 14px;background:#fffbeb;border:1px solid #fcd34d;border-radius:8px;">
                    <div style="font-weight:700;color:#92400e;margin-bottom:8px;">
                        JARs with different naming convention ({len(naming_diff_rows)})
                    </div>
                    <ul style="margin:0;padding-left:20px;font-family:Consolas,monospace;font-size:12px;line-height:1.7;">{items}</ul>
                </div>"""

        naming_table_html = ""
        if naming_diff_rows:
            naming_table_html = f"""
                <h3 style="font-size:15px;color:#92400e;margin:18px 0 10px 0;">Different naming convention (highlighted)</h3>
                <div class="uxf-naming-table-wrap" style="border:1px solid #fcd34d;border-radius:8px;margin-bottom:16px;">
                    <table class="comparison-table uxf-naming-alignment-table" style="margin:0;table-layout:fixed;width:100%;">
                        <thead><tr>
                            <th style="width:18%;">Logical JAR name</th>
                            <th style="width:34%;">Old build naming</th>
                            <th style="width:34%;">New build naming</th>
                            <th style="width:14%;">Status</th>
                        </tr></thead>
                        <tbody>{naming_body}</tbody>
                    </table>
                </div>"""

        alignment_table_html = ""
        if alignment_rows:
            alignment_table_html = f"""
                <h3 style="font-size:15px;color:#475569;margin:18px 0 10px 0;">
                    Lib JAR alignment — old vs new naming ({len(alignment_rows)})
                </h3>
                <p style="margin:0 0 10px 0;font-size:12px;color:#64748b;">
                    Present in both: {present_both} | Only in old: {only_old} | Only in new: {only_new}
                </p>
                <div class="uxf-naming-table-wrap" style="border:1px solid #e2e8f0;border-radius:8px;">
                    <table class="comparison-table uxf-naming-alignment-table" style="margin:0;table-layout:fixed;width:100%;">
                        <thead><tr>
                            <th style="width:18%;">Logical JAR name</th>
                            <th style="width:34%;">Old build naming</th>
                            <th style="width:34%;">New build naming</th>
                            <th style="width:14%;">Status</th>
                        </tr></thead>
                        <tbody>{alignment_body}</tbody>
                    </table>
                </div>"""

        return f"""
            <div class="section" id="{section_id}">
                <h2>📋 JAR naming — Old vs New</h2>
                <p style="margin:0 0 12px 0;font-size:13px;color:#475569;line-height:1.55;">
                    Compare location (shown once):
                    <code style="display:block;margin-top:6px;padding:10px 12px;background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;word-break:break-all;">{html.escape(base_path)}</code>
                </p>
                <p style="margin:0 0 12px 0;font-size:12px;color:#64748b;">
                    <strong style="color:#92400e;">Different naming convention</strong> is highlighted only when the
                    <strong>leaf .jar file name</strong> differs between builds (e.g. <code>...758.jar</code> vs <code>...772.jar</code>).
                    JARs with the same file name on both builds show status <strong>Present in both</strong>.
                    Different naming: {len(naming_diff_rows)} | Aligned rows: {len(alignment_rows)}
                </p>
                {naming_list_html}
                {naming_table_html}
                {alignment_table_html}
            </div>"""

    def _uxf_text_naming_side_by_side_block(self, rows: list[dict]) -> list[str]:
        if not rows:
            return []
        lines = [
            "-" * 80,
            "JAR NAMING — OLD VS NEW",
            f"Compare location: {self._uxf_report_compare_base_path()}",
            "-" * 80,
        ]

        def _is_true_naming_diff(r: dict) -> bool:
            leaf1 = self._uxf_leaf_jar_name(r.get("old_leaf") or r.get("old_name") or "")
            leaf2 = self._uxf_leaf_jar_name(r.get("new_leaf") or r.get("new_name") or "")
            return bool(leaf1 and leaf2 and leaf1.lower() != leaf2.lower())

        naming_diff_rows = [r for r in rows if _is_true_naming_diff(r)]
        alignment_rows = [r for r in rows if r not in naming_diff_rows]

        if naming_diff_rows:
            lines.append(f"JARs with DIFFERENT naming convention ({len(naming_diff_rows)}):")
            for r in naming_diff_rows:
                old_leaf = self._uxf_leaf_jar_name(r.get("old_leaf") or r.get("old_name") or "")
                new_leaf = self._uxf_leaf_jar_name(r.get("new_leaf") or r.get("new_name") or "")
                lines.append(f"  {old_leaf} -> {new_leaf}")
            lines.append("-" * 80)
            lines.append(
                f"{'Logical JAR':<36} {'Old build naming':<40} {'New build naming':<40} Status"
            )
            lines.append("-" * 80)
            for r in naming_diff_rows:
                lines.append(
                    f"{str(r.get('logical_name') or ''):<36} "
                    f"{str(r.get('old_name') or ''):<40} "
                    f"{str(r.get('new_name') or ''):<40} "
                    f"{str(r.get('status') or 'Different naming convention')}"
                )
        if alignment_rows:
            lines.append(f"Lib JAR alignment ({len(alignment_rows)}):")
            lines.append("-" * 80)
            lines.append(
                f"{'Logical JAR':<36} {'Old build naming':<40} {'New build naming':<40} Status"
            )
            lines.append("-" * 80)
            for r in alignment_rows:
                lines.append(
                    f"{str(r.get('logical_name') or ''):<36} "
                    f"{str(r.get('old_name') or '—'):<40} "
                    f"{str(r.get('new_name') or '—'):<40} "
                    f"{str(r.get('status') or '')}"
                )
        lines.append("")
        return lines
    
    def generate_report(self) -> str:
        """Generate a text report for UXF comparison."""
        lines = []
        lines.append("=" * 80)
        lines.append("UXF BUILD COMPARISON REPORT")
        lines.append("=" * 80)
        lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"Old Build Path: {self.folder1}")
        lines.append(f"New Build Path: {self.folder2}")
        lines.append(f"Decompiler: {self.decompiler_type or 'None'}")
        lines.append("")
        
        if self.comparison_error:
            lines.append("-" * 80)
            lines.append(f"ERROR: {self.comparison_error}")
            lines.append("-" * 80)
        else:
            lines.append("-" * 80)
            lines.append("SUMMARY")
            lines.append("-" * 80)
            lines.append(f"Total archives in Old Build: {len(self.archives1)}")
            lines.append(f"Total archives in New Build: {len(self.archives2)}")
            lines.append(f"Exact matches: {len(self.in_both)}")
            lines.append(f"Only in Old Build: {len(self.only_in_1)}")
            lines.append(f"Only in New Build: {len(self.only_in_2)}")
            lines.append(f"Version Changes: {len(self.version_changes)}")
            lines.append(f"JAR Naming Diffs (lib): {len(getattr(self, 'lib_jar_naming_diffs', []) or [])}")
            lines.append(f"Size/Content Changes: {len(self.size_changes)}")
            lines.append(f"JARs with class diffs: {len(self.class_diffs)}")
            lines.append("")

            naming_rows = self._uxf_build_naming_alignment_rows()
            if naming_rows:
                lines.extend(self._uxf_text_naming_side_by_side_block(naming_rows))
            
            if self.size_changes:
                lines.append("-" * 80)
                lines.append(f"MODIFIED ARCHIVES ({len(self.size_changes)})")
                lines.append("-" * 80)
                for sc in self.size_changes:
                    size_diff = sc['new_size'] - sc['old_size']
                    lines.append(f"  [M] {sc['name']} ({sc['old_size']} -> {sc['new_size']}, {size_diff:+d} bytes)")
                    
                    # Show member diffs
                    if sc['name'] in self.member_diffs:
                        md = self.member_diffs[sc['name']]
                        if md['added']:
                            lines.append(f"      Added files: {len(md['added'])}")
                        if md['removed']:
                            lines.append(f"      Removed files: {len(md['removed'])}")
                        if md['modified']:
                            lines.append(f"      Modified files: {len(md['modified'])}")
                    
                    if sc['name'] in self.class_diffs:
                        for cd in self.class_diffs[sc['name']]:
                            lines.append(f"      - {cd['class_name']}: +{cd['diff']['added_lines']}/-{cd['diff']['removed_lines']} lines")
                lines.append("")
        
        lines.append("=" * 80)
        lines.append("END OF UXF COMPARISON REPORT")
        lines.append("=" * 80)
        
        return "\n".join(lines)
    
    def generate_html_report(self) -> str:
        """Generate an HTML report for UXF comparison with class file diffs."""
        if not getattr(self, "lib_jar_naming_diffs", None):
            self._uxf_sync_lib_jar_naming_diffs()

        naming_diff_count = len(self.lib_jar_naming_diffs)
        total_differences = (
            len(self.only_in_1)
            + len(self.only_in_2)
            + len(self.version_changes)
            + len(self.size_changes)
        )
        if naming_diff_count and total_differences < naming_diff_count:
            total_differences = (
                len(self.only_in_1)
                + len(self.only_in_2)
                + max(len(self.version_changes), naming_diff_count)
                + len(self.size_changes)
            )
        total_class_diffs = sum(len(diffs) for diffs in self.class_diffs.values())
        total_member_changes = sum(
            len(md['added']) + len(md['removed']) + len(md['modified']) 
            for md in self.member_diffs.values()
        )
        
        decompiler_info = f"🔧 <strong style='color:#48bb78;'>Decompiler: {self.decompiler_type.upper() if self.decompiler_type else 'None'}</strong>"
        
        size_change_keys = {sc.get("key") for sc in self.size_changes}
        unchanged_count = 0
        for key in self.in_both:
            if key in size_change_keys:
                continue
            info1 = self.archives1.get(key) or {}
            key2 = self._uxf_key_pairs.get(key, key)
            info2 = self.archives2.get(key2) or self.archives2.get(key) or {}
            if str(info1.get("name") or "").lower() != str(info2.get("name") or "").lower():
                continue
            unchanged_count += 1

        naming_rows = self._uxf_build_naming_alignment_rows()
        naming_orphan_count = len(self.only_in_1) + len(self.only_in_2)
        
        # Build Quick Navigation (ASCF-style with better visibility)
        nav_links = []
        # Always show summary link first
        nav_links.append('<a href="#summary" class="nav-link" style="border-left-color:#6366f1;">📊 Summary</a>')
        
        if naming_rows or naming_diff_count:
            nav_naming_count = naming_diff_count or sum(
                1 for r in naming_rows if r.get("naming_diff") and not r.get("orphan")
            )
            nav_links.append(
                f'<a href="#jar-naming-side-by-side" class="nav-link version" style="border-left-color:#f59e0b;">'
                f'📋 JAR Naming (side by side) <span class="count">{nav_naming_count}</span></a>'
            )
        elif self.version_changes:
            nav_links.append(f'<a href="#version-changes" class="nav-link version" style="border-left-color:#f59e0b;">🔄 Version Changes <span class="count">{len(self.version_changes)}</span></a>')
        if naming_orphan_count and not naming_rows:
            nav_links.append(f'<a href="#only-in-old" class="nav-link removed" style="border-left-color:#ef4444;">🗑️ Removed in New Build <span class="count">{len(self.only_in_1)}</span></a>')
            nav_links.append(f'<a href="#only-in-new" class="nav-link added" style="border-left-color:#22c55e;">✨ Added in New Build <span class="count">{len(self.only_in_2)}</span></a>')
        if self.size_changes:
            nav_links.append(f'<a href="#modified" class="nav-link modified" style="border-left-color:#3b82f6;">📝 Modified Archives <span class="count">{len(self.size_changes)}</span></a>')
        if total_class_diffs > 0:
            nav_links.append(f'<a href="#modified" class="nav-link class-diff" style="border-left-color:#8b5cf6;">☕ Class File Diffs <span class="count">{total_class_diffs}</span></a>')
        if unchanged_count > 0:
            nav_links.append(f'<a href="#unchanged" class="nav-link unchanged" style="border-left-color:#10b981;">✓ Unchanged <span class="count">{unchanged_count}</span></a>')
        
        quick_nav_html = f"""
            <div class="nav-menu" id="quick-nav">
                <h3>📑 Quick Navigation</h3>
                <div class="nav-links">
                    {''.join(nav_links)}
                </div>
            </div>"""
        
        # JAR naming side-by-side (orphans + naming convention diffs)
        if not naming_rows and naming_diff_count:
            naming_rows = [
                {
                    "logical_name": d.get("logical_name"),
                    "old_name": d.get("old_name"),
                    "new_name": d.get("new_name"),
                    "naming_diff": True,
                    "orphan": False,
                    "orphan_side": "",
                }
                for d in self.lib_jar_naming_diffs
            ]
        naming_side_by_side_html = self._uxf_html_naming_side_by_side_table(
            naming_rows, section_id="jar-naming-side-by-side"
        )

        # Legacy list sections only when naming table is empty
        version_changes_html = ""
        only_in_1_html = ""
        only_in_2_html = ""
        if not naming_rows and not naming_diff_count:
            if self.version_changes:
                rows = ""
                for vc in self.version_changes:
                    rows += f"""<tr>
                        <td style="color:#ef4444;">{html.escape(vc['old_name'])}</td>
                        <td style="text-align:center;">→</td>
                        <td style="color:#22c55e;">{html.escape(vc['new_name'])}</td>
                    </tr>"""
                version_changes_html = f"""
                <div class="section" id="version-changes">
                    <h2>🔄 Version Changes ({len(self.version_changes)})</h2>
                    <table class="comparison-table">
                        <thead><tr><th>Old Version</th><th></th><th>New Version</th></tr></thead>
                        <tbody>{rows}</tbody>
                    </table>
                </div>"""
            if self.only_in_1:
                items = "".join([f'<li class="removed">❌ {html.escape(n)}</li>' for n in self.only_in_1])
                only_in_1_html = f"""
                <div class="section" id="only-in-old">
                    <h2>❌ Only in Old Build ({len(self.only_in_1)})</h2>
                    <ul class="file-list">{items}</ul>
                </div>"""
            if self.only_in_2:
                items = "".join([f'<li class="added">✅ {html.escape(n)}</li>' for n in self.only_in_2])
                only_in_2_html = f"""
                <div class="section" id="only-in-new">
                    <h2>✅ Only in New Build ({len(self.only_in_2)})</h2>
                    <ul class="file-list">{items}</ul>
                </div>"""
        
        # Modified archives with member and class diffs
        size_changes_html = ""
        if self.size_changes:
            rows = ""
            diff_counter = 0
            
            for sc in self.size_changes:
                size_diff = sc['new_size'] - sc['old_size']
                size_class = "size-increase" if size_diff > 0 else "size-decrease"
                size_str = f"+{size_diff:,}" if size_diff > 0 else f"{size_diff:,}"
                
                diff_counter += 1
                diff_id = f"diff-{diff_counter}"
                
                jar_member_diffs = self.member_diffs.get(sc['name'], {'added': [], 'removed': [], 'modified': []})
                jar_class_diffs = self.class_diffs.get(sc['name'], [])
                
                has_details = jar_member_diffs['added'] or jar_member_diffs['removed'] or jar_member_diffs['modified'] or jar_class_diffs
                
                view_diff_btn = ""
                diff_content = ""
                
                if has_details:
                    view_diff_btn = f"""<button onclick="var d=document.getElementById('{diff_id}');d.style.display=d.style.display==='none'?'block':'none';this.textContent=d.style.display==='none'?'View Diff':'Hide'" style="background:#667eea;color:white;border:none;padding:6px 12px;border-radius:6px;cursor:pointer;font-size:11px;">View Diff</button>"""
                    
                    # Build member changes section
                    member_html = ""
                    if jar_member_diffs['added']:
                        items = "".join([f"<div style='color:#22c55e;padding:2px 8px;'>+ {html.escape(m)}</div>" for m in jar_member_diffs['added'][:20]])
                        if len(jar_member_diffs['added']) > 20:
                            items += f"<div style='color:#94a3b8;padding:2px 8px;'>... and {len(jar_member_diffs['added']) - 20} more</div>"
                        member_html += f"""<div style="margin-bottom:10px;">
                            <div style="font-weight:600;color:#22c55e;margin-bottom:5px;">✅ Added Files ({len(jar_member_diffs['added'])})</div>
                            <div style="background:#0f1f0f;border-radius:4px;padding:8px;max-height:150px;overflow:auto;font-size:11px;font-family:Consolas,monospace;">{items}</div>
                        </div>"""
                    
                    if jar_member_diffs['removed']:
                        items = "".join([f"<div style='color:#ef4444;padding:2px 8px;'>- {html.escape(m)}</div>" for m in jar_member_diffs['removed'][:20]])
                        if len(jar_member_diffs['removed']) > 20:
                            items += f"<div style='color:#94a3b8;padding:2px 8px;'>... and {len(jar_member_diffs['removed']) - 20} more</div>"
                        member_html += f"""<div style="margin-bottom:10px;">
                            <div style="font-weight:600;color:#ef4444;margin-bottom:5px;">❌ Removed Files ({len(jar_member_diffs['removed'])})</div>
                            <div style="background:#1f0f0f;border-radius:4px;padding:8px;max-height:150px;overflow:auto;font-size:11px;font-family:Consolas,monospace;">{items}</div>
                        </div>"""
                    
                    if jar_member_diffs['modified']:
                        items = ""
                        for m in jar_member_diffs['modified'][:20]:
                            sz_diff = m['new_size'] - m['old_size']
                            sz_str = f"+{sz_diff}" if sz_diff > 0 else str(sz_diff)
                            items += f"<div style='color:#f59e0b;padding:2px 8px;'>~ {html.escape(m['name'])} ({m['old_size']} → {m['new_size']}, {sz_str})</div>"
                        if len(jar_member_diffs['modified']) > 20:
                            items += f"<div style='color:#94a3b8;padding:2px 8px;'>... and {len(jar_member_diffs['modified']) - 20} more</div>"
                        member_html += f"""<div style="margin-bottom:10px;">
                            <div style="font-weight:600;color:#f59e0b;margin-bottom:5px;">📝 Modified Files ({len(jar_member_diffs['modified'])})</div>
                            <div style="background:#1f1f0f;border-radius:4px;padding:8px;max-height:150px;overflow:auto;font-size:11px;font-family:Consolas,monospace;">{items}</div>
                        </div>"""
                    
                    # Build class diff section
                    class_diff_html = ""
                    if jar_class_diffs:
                        class_items = ""
                        for cd in jar_class_diffs:
                            class_diff_id = f"{diff_id}-{cd['class_name'].replace('.', '-')}"
                            class_items += f"""
                            <div style="margin-bottom:15px;border:1px solid #374151;border-radius:8px;overflow:hidden;">
                                <div style="background:#1e3a5f;color:white;padding:10px;font-weight:600;display:flex;justify-content:space-between;align-items:center;">
                                    <span>☕ {html.escape(cd['class_name'])}</span>
                                    <span><span style="color:#4ade80;">+{cd['diff']['added_lines']}</span> / <span style="color:#f87171;">-{cd['diff']['removed_lines']}</span></span>
                                </div>
                                <div style="display:grid;grid-template-columns:1fr 1fr;gap:0;">
                                    <div style="border-right:1px solid #374151;">
                                        <div style="background:#991b1b;color:white;padding:8px;font-size:12px;font-weight:600;">❌ Old Version</div>
                                        <div style="padding:10px;font-size:10px;font-family:Consolas,monospace;max-height:300px;overflow:auto;background:#1e1e1e;color:#d4d4d4;">
                                            {''.join(f"<div style='padding:1px 4px;'>{html.escape(line)}</div>" for line in cd['source1'].split(chr(10))[:80])}
                                        </div>
                                    </div>
                                    <div>
                                        <div style="background:#166534;color:white;padding:8px;font-size:12px;font-weight:600;">✅ New Version</div>
                                        <div style="padding:10px;font-size:10px;font-family:Consolas,monospace;max-height:300px;overflow:auto;background:#1e1e1e;color:#d4d4d4;">
                                            {''.join(f"<div style='padding:1px 4px;'>{html.escape(line)}</div>" for line in cd['source2'].split(chr(10))[:80])}
                                        </div>
                                    </div>
                                </div>
                            </div>"""
                        
                        class_diff_html = f"""<div style="margin-top:15px;">
                            <div style="font-weight:600;color:#8b5cf6;margin-bottom:10px;">☕ Java Class Differences ({len(jar_class_diffs)})</div>
                            {class_items}
                        </div>"""
                    
                    diff_content = f"""<tr><td colspan="5" style="padding:0;border:none;">
                        <div id="{diff_id}" style="display:none;margin:10px;background:#0f172a;border-radius:8px;padding:15px;">
                            {member_html}
                            {class_diff_html}
                        </div>
                    </td></tr>"""
                
                # Summary of what changed
                change_summary = []
                if jar_member_diffs['added']:
                    change_summary.append(f"+{len(jar_member_diffs['added'])} files")
                if jar_member_diffs['removed']:
                    change_summary.append(f"-{len(jar_member_diffs['removed'])} files")
                if jar_member_diffs['modified']:
                    change_summary.append(f"~{len(jar_member_diffs['modified'])} files")
                if jar_class_diffs:
                    change_summary.append(f"{len(jar_class_diffs)} class diffs")
                
                summary_str = ", ".join(change_summary) if change_summary else "Size changed"
                
                rows += f"""<tr class="modified">
                    <td>{html.escape(sc['name'])}{(
                        f"<br/><span style='font-size:11px;color:#92400e;'>New: {html.escape(str(sc.get('new_info', {}).get('name') or sc['name']))}</span>"
                        if str(sc.get('new_info', {}).get('name') or sc['name']).lower() != str(sc['name']).lower()
                        else ""
                    )}</td>
                    <td style="text-align:right;">{sc['old_size']:,}</td>
                    <td style="text-align:right;">{sc['new_size']:,}</td>
                    <td style="text-align:right;" class="{size_class}">{size_str}</td>
                    <td style="text-align:center;font-size:11px;">{summary_str}<br/>{view_diff_btn}</td>
                </tr>{diff_content}"""
            
            size_changes_html = f"""
            <div class="section" id="modified">
                <h2>📝 Modified Archives ({len(self.size_changes)})</h2>
                <p style="color:#6b7280;font-size:13px;margin-bottom:15px;">
                    Total member changes: {total_member_changes} | Class file diffs: {total_class_diffs}
                </p>
                <table class="comparison-table">
                    <thead><tr><th>Archive Name</th><th>Old Size</th><th>New Size</th><th>Diff</th><th>Changes</th></tr></thead>
                    <tbody>{rows}</tbody>
                </table>
            </div>"""
        
        error_html = ""
        if self.comparison_error:
            error_html = f"""
            <div class="error-box">
                <h3>⚠️ Comparison Error</h3>
                <p>{html.escape(self.comparison_error)}</p>
            </div>"""
        
        no_diff_html = ""
        if total_differences == 0 and naming_diff_count == 0 and not self.comparison_error:
            no_diff_html = """
            <div class="success-box">
                <h3>✅ No Differences Found</h3>
                <p>The two builds contain identical archives.</p>
            </div>"""
        
        # Unchanged archives section (same leaf name and size on both builds)
        unchanged_html = ""
        if unchanged_count > 0:
            unchanged_items = []
            for key in self.in_both:
                if key in size_change_keys:
                    continue
                info1 = self.archives1.get(key) or {}
                key2 = self._uxf_key_pairs.get(key, key)
                info2 = self.archives2.get(key2) or self.archives2.get(key) or {}
                if str(info1.get("name") or "").lower() != str(info2.get("name") or "").lower():
                    continue
                unchanged_items.append(self._uxf_display_label(key, 1))
            
            if unchanged_items:
                items_html = "".join([f'<li class="unchanged">✓ {html.escape(n)}</li>' for n in sorted(unchanged_items)[:50]])
                more_text = f"<li style='color:#6b7280;font-style:italic;'>... and {len(unchanged_items) - 50} more unchanged archives</li>" if len(unchanged_items) > 50 else ""
                unchanged_html = f"""
            <div class="section" id="unchanged">
                <h2>✓ Unchanged Archives ({unchanged_count})</h2>
                <p style="color:#6b7280;font-size:13px;margin-bottom:15px;">These archives are identical in both builds (same size and CRC).</p>
                <ul class="file-list">{items_html}{more_text}</ul>
            </div>"""
        
        html_content = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>UXF Build Comparison Report</title>
    <style>
        * {{ box-sizing: border-box; }}
        body {{ font-family: 'Segoe UI', system-ui, sans-serif; margin: 0; padding: 20px; background: linear-gradient(135deg, #065f46 0%, #047857 100%); min-height: 100vh; }}
        .container {{ max-width: 1400px; margin: 0 auto; background: white; border-radius: 16px; box-shadow: 0 20px 60px rgba(0,0,0,0.3); overflow: hidden; }}
        .header {{ background: linear-gradient(135deg, #065f46 0%, #047857 100%); color: white; padding: 30px; }}
        .header h1 {{ margin: 0 0 10px 0; font-size: 28px; }}
        .header .subtitle {{ opacity: 0.9; font-size: 14px; }}
        .header .paths {{ margin-top: 15px; padding-top: 15px; border-top: 1px solid rgba(255,255,255,0.3); }}
        .header .path-label {{ font-weight: 600; font-size: 12px; text-transform: uppercase; opacity: 0.8; margin-top: 8px; }}
        .header .path-value {{ font-family: Consolas, monospace; font-size: 13px; margin-top: 4px; word-break: break-all; }}
        .summary {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 12px; padding: 20px 30px; background: #f0fdf4; border-bottom: 1px solid #bbf7d0; }}
        .summary-card {{ background: white; padding: 12px; border-radius: 8px; text-align: center; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
        .summary-card .number {{ font-size: 24px; font-weight: 700; color: #065f46; }}
        .summary-card .label {{ font-size: 11px; color: #6b7280; margin-top: 4px; }}
        .decompiler-info {{ padding: 10px 30px; background: #ecfdf5; border-bottom: 1px solid #bbf7d0; font-size: 13px; }}
        .nav-menu {{ background: linear-gradient(135deg, #f0fdf4 0%, #ecfdf5 100%); border: 2px solid #86efac; border-radius: 12px; padding: 20px; margin-bottom: 25px; box-shadow: 0 4px 12px rgba(0,0,0,0.08); }}
        .nav-menu h3 {{ margin: 0 0 15px 0; color: #065f46; font-size: 18px; font-weight: 700; }}
        .nav-links {{ display: flex; flex-wrap: wrap; gap: 12px; }}
        .nav-link {{ display: inline-flex; align-items: center; gap: 8px; padding: 12px 16px; background: white; border: 1px solid #e2e8f0; border-left: 5px solid #3b82f6; border-radius: 8px; text-decoration: none; color: #1f2937; font-size: 13px; font-weight: 600; transition: all 0.2s; box-shadow: 0 2px 4px rgba(0,0,0,0.05); }}
        .nav-link:hover {{ background: #f8fafc; transform: translateX(4px); border-left-width: 8px; box-shadow: 0 4px 8px rgba(0,0,0,0.1); }}
        .nav-link .count {{ background: #e5e7eb; color: #374151; padding: 2px 8px; border-radius: 12px; font-size: 11px; font-weight: 700; }}
        .nav-link.removed .count {{ background: #fee2e2; color: #991b1b; }}
        .nav-link.added .count {{ background: #dcfce7; color: #166534; }}
        .nav-link.modified .count {{ background: #dbeafe; color: #1e40af; }}
        .nav-link.version .count {{ background: #fef3c7; color: #92400e; }}
        .nav-link.class-diff .count {{ background: #ede9fe; color: #5b21b6; }}
        .nav-link.unchanged .count {{ background: #d1fae5; color: #065f46; }}
        .content {{ padding: 30px; }}
        .section {{ margin-bottom: 30px; }}
        .section h2 {{ color: #065f46; font-size: 18px; margin: 0 0 15px 0; padding-bottom: 10px; border-bottom: 2px solid #d1fae5; }}
        .comparison-table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
        .comparison-table th {{ background: #065f46; color: white; padding: 12px; text-align: left; }}
        .comparison-table td {{ padding: 10px 12px; border-bottom: 1px solid #e5e7eb; vertical-align: top; }}
        .uxf-naming-alignment-table .uxf-naming-path-cell {{
            word-break: break-all;
            overflow-wrap: anywhere;
            white-space: normal;
        }}
        .uxf-naming-alignment-table th:nth-child(2),
        .uxf-naming-alignment-table th:nth-child(3) {{
            word-break: break-word;
            white-space: normal;
        }}
        .comparison-table tr:hover {{ background: #f0fdf4; }}
        .file-list {{ list-style: none; padding: 0; margin: 0; max-height: 400px; overflow-y: auto; }}
        .file-list li {{ padding: 8px 12px; border-bottom: 1px solid #e5e7eb; font-family: Consolas, monospace; font-size: 12px; }}
        .file-list li.removed {{ background: #fef2f2; color: #991b1b; }}
        .file-list li.added {{ background: #f0fdf4; color: #166534; }}
        .file-list li.unchanged {{ background: #f8fafc; color: #475569; }}
        .size-increase {{ color: #dc2626; font-weight: 600; }}
        .size-decrease {{ color: #16a34a; font-weight: 600; }}
        .error-box {{ background: #fef2f2; border: 2px solid #fca5a5; border-radius: 8px; padding: 20px; margin-bottom: 20px; }}
        .error-box h3 {{ color: #dc2626; margin: 0 0 10px 0; }}
        .success-box {{ background: #f0fdf4; border: 2px solid #86efac; border-radius: 8px; padding: 20px; margin-bottom: 20px; text-align: center; }}
        .success-box h3 {{ color: #166534; margin: 0 0 10px 0; }}
        .back-link {{ display: inline-block; margin-top: 20px; padding: 10px 20px; background: #065f46; color: white; text-decoration: none; border-radius: 8px; font-weight: 600; }}
        .back-link:hover {{ background: #047857; }}
        {report_collapse_extra_css()}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🎨 UXF Build Comparison Report</h1>
            <div class="subtitle">Generated on {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</div>
            <div class="paths">
                <div class="path-label">📁 Old Build Path</div>
                <div class="path-value">{html.escape(self.folder1)}</div>
                <div class="path-label">📁 New Build Path</div>
                <div class="path-value">{html.escape(self.folder2)}</div>
            </div>
        </div>
        
        <div class="decompiler-info">{decompiler_info} | ☕ Class diffs: {total_class_diffs} | 📄 Member changes: {total_member_changes}</div>
        
        <div class="summary" id="summary">
            <div class="summary-card"><div class="number">{len(self.archives1)}</div><div class="label">Archives in Old</div></div>
            <div class="summary-card"><div class="number">{len(self.archives2)}</div><div class="label">Archives in New</div></div>
            <div class="summary-card"><div class="number">{len(self.version_changes)}</div><div class="label">Version Changes</div></div>
            <div class="summary-card"><div class="number">{naming_diff_count}</div><div class="label">JAR Naming Diffs</div></div>
            <div class="summary-card"><div class="number">{len(self.only_in_1)}</div><div class="label">Only in Old</div></div>
            <div class="summary-card"><div class="number">{len(self.only_in_2)}</div><div class="label">Only in New</div></div>
            <div class="summary-card"><div class="number">{len(self.size_changes)}</div><div class="label">Modified</div></div>
            <div class="summary-card"><div class="number">{unchanged_count}</div><div class="label">Unchanged</div></div>
            <div class="summary-card"><div class="number">{total_class_diffs}</div><div class="label">Class Diffs</div></div>
        </div>
        
        <div class="content">
            {quick_nav_html}
            {error_html}
            {no_diff_html}
            {naming_side_by_side_html}
            {version_changes_html}
            {only_in_1_html}
            {only_in_2_html}
            {size_changes_html}
            {unchanged_html}
            
            <a href="/" class="back-link">← Back to Home</a>
        </div>
    </div>
</body>
</html>"""
        return inject_report_section_controls(html_content)
    
    def save_report(self, filepath: str):
        """Save the text report to a file."""
        report = self.generate_report()
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(report)
        
        html_filepath = str(Path(filepath).with_suffix('.html'))
        html_report = self.generate_html_report()
        with open(html_filepath, 'w', encoding='utf-8') as f:
            f.write(html_report)


def run_uxf_comparison(folder1: str, folder2: str, session_id=None, log_callback=None, progress_callback=None, **options) -> tuple:
    """Main entry point for UXF comparison."""
    from report_naming import build_report_filename

    report_user = (
        options.get("report_user")
        or options.get("client_identity")
        or "unknown"
    )
    _rd = Path(str(options.get("report_dir") or "."))
    output_filename = build_report_filename(
        "uxf_patch_comparison", folder1, folder2, report_user, report_dir=_rd
    )
    
    comparer = UXFComparer(folder1, folder2)
    comparer.suppress_warnings = options.get('suppress_warnings', True)
    comparer.show_missing_only = options.get('show_missing_only', False)
    comparer.ignore_timestamp_only_differences = options.get('ignore_timestamp_only', True)
    
    if log_callback:
        comparer.set_log_callback(log_callback)
    if progress_callback:
        comparer.set_progress_callback(progress_callback)
    
    results = comparer.compare_folders()
    
    return comparer, results, output_filename
