#!/usr/bin/env python3
"""
Scan an environment (deployment) path for FOSS and Foundation JARs and read
Implementation-Version from each JAR's META-INF/MANIFEST.MF.
"""

from __future__ import annotations

import fnmatch
import html
import os
import zipfile
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from folder_compare import _path_exists

# (category, jar filename, display label)
FOUNDATION_JARS: List[Tuple[str, str, str]] = [
    ("Foundation", "AmdocsCore.jar", "Amdocs Core"),
    ("Foundation", "AmdocsProcMgrLiteClient.jar", "Amdocs Process Manager"),
    ("Foundation", "AmdocsSecurityManager.jar", "Amdocs Security Manager"),
    ("Foundation", "acfglib.jar", "Amdocs System Configuration"),
    ("Foundation", "obfuscation-utility.jar", "obfuscation-utility"),
]

FOSS_JARS: List[Tuple[str, str, str]] = [
    ("FOSS", "commons-fileupload.jar", "commons-fileupload"),
    ("FOSS", "commons-io.jar", "commons-io"),
    ("FOSS", "commons-logging.jar", "commons-logging"),
    ("FOSS", "esapi.jar", "esapi"),
    ("FOSS", "jxbrowser.jar", "jxbrowser"),
    ("FOSS", "jxbrowser-swing.jar", "jxbrowser-swing"),
    ("FOSS", "jxbrowser-win.jar", "jxbrowser-win"),
    ("FOSS", "log4j-1.2-api.jar", "log4j-1.2-api"),
    ("FOSS", "log4j-api.jar", "log4j-api"),
    ("FOSS", "log4j-core.jar", "log4j-core"),
    ("FOSS", "slf4j-api.jar", "slf4j-api"),
    ("FOSS", "slf4j-nop.jar", "slf4j-nop"),
]

ALL_CATALOG_JARS = FOUNDATION_JARS + FOSS_JARS


def parse_jar_queries(raw: str) -> List[str]:
    """Split comma/newline-separated JAR names (same format as CRM manifest diff)."""
    out: List[str] = []
    seen: set[str] = set()
    for part in (raw or "").replace("\n", ",").split(","):
        q = part.strip().strip('"').strip("'")
        if not q:
            continue
        key = q.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(q)
    return out


def _jar_query_file_name(query: str) -> str:
    q_name = os.path.basename(query.strip().lower().replace("\\", "/"))
    if "*" in q_name or "?" in q_name:
        return q_name if q_name.endswith(".jar") else q_name
    if not q_name.endswith(".jar"):
        return q_name + ".jar"
    return q_name


def _jar_basename_matches_query(basename_lower: str, query: str) -> bool:
    b = (basename_lower or "").lower().strip()
    if not b:
        return False
    q_file = _jar_query_file_name(query)
    if "*" in q_file or "?" in q_file:
        b_stem = b[:-4] if b.endswith(".jar") else b
        q_stem = q_file[:-4] if q_file.endswith(".jar") else q_file

        def _wm(pat: str, name: str) -> bool:
            return fnmatch.fnmatch(name, pat) or fnmatch.fnmatch(name.lower(), pat.lower())

        if _wm(q_file, b):
            return True
        if _wm(q_stem, b_stem):
            return True
        return False
    return b == q_file or q_file in b


def _collect_paths_for_query(jar_index: Dict[str, List[str]], query: str) -> List[str]:
    """Return every remote/local path whose basename matches the query (find_jar_version.sh style)."""
    paths: List[str] = []
    for basename, plist in jar_index.items():
        if _jar_basename_matches_query(basename, query):
            paths.extend(plist)
    return sorted(set(paths), key=lambda p: p.lower())


def _parse_manifest_implementation_version(text: str) -> Tuple[str, str]:
    merged: List[str] = []
    for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if ln.startswith(" ") and merged:
            merged[-1] += ln[1:]
        else:
            merged.append(ln)
    for ln in merged:
        if ln.lower().startswith("implementation-version:"):
            return ln.split(":", 1)[1].strip(), ""
    return "", "Implementation-Version not found"


def _index_jars(base: Path) -> Dict[str, List[str]]:
    """Map lowercase jar filename -> list of full paths (single walk)."""
    index: Dict[str, List[str]] = {}
    try:
        for root, _, files in os.walk(str(base)):
            for name in files:
                if not name.lower().endswith(".jar"):
                    continue
                key = name.lower()
                full = str(Path(root) / name)
                index.setdefault(key, []).append(full)
    except OSError as exc:
        raise ValueError(f"Cannot scan environment path: {exc}") from exc
    return index


def _find_jar_in_index(index: Dict[str, List[str]], jar_name: str) -> str:
    target = jar_name.lower()
    if target in index:
        return index[target][0]
    partial = None
    for key, paths in index.items():
        if target in key and partial is None:
            partial = paths[0]
    return partial or ""


def read_implementation_version(jar_path: str) -> Tuple[str, str]:
    """Return (implementation_version, error_message) from a local JAR path."""
    if not jar_path:
        return "", "JAR not found"
    try:
        with open(jar_path, "rb") as f:
            return read_implementation_version_bytes(f.read())
    except Exception as exc:
        return "", f"Error reading JAR: {exc}"


def read_implementation_version_bytes(data: bytes) -> Tuple[str, str]:
    """Return (implementation_version, error_message) from JAR bytes."""
    if not data:
        return "", "Empty JAR data"
    try:
        with zipfile.ZipFile(BytesIO(data), "r") as zf:
            try:
                manifest = zf.read("META-INF/MANIFEST.MF")
            except KeyError:
                return "", "MANIFEST.MF not found"
        text = manifest.decode("utf-8", errors="replace")
        return _parse_manifest_implementation_version(text)
    except Exception as exc:
        return "", f"Error reading JAR: {exc}"


def _build_validation_rows(
    jar_index: Dict[str, List[str]],
    read_version_fn,
) -> Tuple[List[Dict[str, Any]], int, int, int]:
    rows: List[Dict[str, Any]] = []
    found_count = 0
    version_count = 0
    missing_count = 0

    for category, jar_file, label in ALL_CATALOG_JARS:
        jar_path = _find_jar_in_index(jar_index, jar_file)
        version, err = read_version_fn(jar_path) if jar_path else ("", "JAR not found")
        if jar_path and version:
            status = "OK"
            found_count += 1
            version_count += 1
        elif jar_path:
            status = err or "No version"
            found_count += 1
        else:
            status = "Not found"
            missing_count += 1
        rows.append(
            {
                "category": category,
                "jar_name": jar_file,
                "product": label,
                "implementation_version": version,
                "path": jar_path,
                "status": status,
            }
        )
    return rows, found_count, version_count, missing_count


def _build_query_validation_rows(
    jar_index: Dict[str, List[str]],
    queries: List[str],
    read_version_fn,
) -> Tuple[List[Dict[str, Any]], int, int, int]:
    """One row per JAR path found for each query (like find_jar_version.sh)."""
    rows: List[Dict[str, Any]] = []
    found_count = 0
    version_count = 0
    missing_count = 0

    for query in queries:
        paths = _collect_paths_for_query(jar_index, query)
        if not paths:
            missing_count += 1
            rows.append(
                {
                    "category": "CRM",
                    "query": query,
                    "product": query,
                    "jar_name": os.path.basename(_jar_query_file_name(query)),
                    "implementation_version": "",
                    "path": "",
                    "status": "Not found",
                }
            )
            continue
        for jar_path in paths:
            version, err = read_version_fn(jar_path)
            jar_base = os.path.basename(jar_path.replace("\\", "/"))
            if version:
                status = "OK"
                found_count += 1
                version_count += 1
            else:
                status = err or "No version"
                found_count += 1
            rows.append(
                {
                    "category": "CRM",
                    "query": query,
                    "product": query,
                    "jar_name": jar_base,
                    "implementation_version": version,
                    "path": jar_path,
                    "status": status,
                }
            )
    return rows, found_count, version_count, missing_count


def _validation_result(
    env_label: str,
    rows: List[Dict[str, Any]],
    found_count: int,
    version_count: int,
    missing_count: int,
    *,
    connection_mode: str = "local",
    ssh_host: Optional[str] = None,
    ssh_username: Optional[str] = None,
    ssh_port: Optional[int] = None,
    validation_mode: str = "catalog",
    jar_queries: Optional[List[str]] = None,
) -> Dict[str, Any]:
    summary: Dict[str, Any] = {
        "total": len(rows),
        "found": found_count,
        "with_version": version_count,
        "missing": missing_count,
    }
    if validation_mode == "jar_query":
        summary["queries"] = len(jar_queries or [])
        summary["validation_mode"] = "jar_query"
    else:
        summary["foundation_total"] = len(FOUNDATION_JARS)
        summary["foss_total"] = len(FOSS_JARS)
        summary["validation_mode"] = "catalog"
    return {
        "ok": True,
        "environment_path": env_label,
        "connection_mode": connection_mode,
        "validation_mode": validation_mode,
        "jar_queries": jar_queries or [],
        "ssh_host": ssh_host or "",
        "ssh_username": ssh_username or "",
        "ssh_port": ssh_port,
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "rows": rows,
        "summary": summary,
    }


def validate_environment_jars(
    environment_path: str,
    ssh_host: Optional[str] = None,
    ssh_username: Optional[str] = None,
    ssh_password: Optional[str] = None,
    ssh_port: int = 22,
    connection_mode: Optional[str] = None,
    jar_queries: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Validate JAR Implementation-Version remotely via SSH.
    Uses jar_queries when provided (CRM manifest diff names); otherwise FOSS/Foundation catalog.
    """
    queries = [q for q in (jar_queries or []) if q]
    return _validate_environment_jars_ssh(
        host=ssh_host or "",
        username=ssh_username or "",
        password=ssh_password or "",
        remote_path=environment_path,
        port=int(ssh_port or 22),
        jar_queries=queries if queries else None,
    )


def _validate_environment_jars_local(environment_path: str) -> Dict[str, Any]:
    env = (environment_path or "").strip()
    if not env:
        raise ValueError("Environment path is required.")
    if not _path_exists(env):
        raise ValueError(f"Environment path does not exist or is not accessible: {env}")

    base = Path(env)
    if not base.is_dir():
        raise ValueError(f"Environment path is not a directory: {env}")

    jar_index = _index_jars(base)
    rows, found_count, version_count, missing_count = _build_validation_rows(
        jar_index, read_implementation_version
    )
    return _validation_result(
        env, rows, found_count, version_count, missing_count, connection_mode="local"
    )


def _validate_environment_jars_ssh(
    host: str,
    username: str,
    password: str,
    remote_path: str,
    port: int = 22,
    jar_queries: Optional[List[str]] = None,
) -> Dict[str, Any]:
    from ssh_environment_validate import (
        read_remote_jar_manifest,
        validate_environment_jars_ssh,
    )

    jar_index, client, sftp = validate_environment_jars_ssh(
        host, username, password, remote_path, port=port
    )
    use_queries = bool(jar_queries)
    try:
        def read_version(jar_path: str) -> Tuple[str, str]:
            return read_remote_jar_manifest(client, sftp, jar_path)

        if use_queries:
            rows, found_count, version_count, missing_count = _build_query_validation_rows(
                jar_index, jar_queries or [], read_version
            )
            mode = "jar_query"
        else:
            rows, found_count, version_count, missing_count = _build_validation_rows(
                jar_index, read_version
            )
            mode = "catalog"
        env_label = f"{username}@{host}:{remote_path}"
        return _validation_result(
            env_label,
            rows,
            found_count,
            version_count,
            missing_count,
            connection_mode="ssh",
            ssh_host=host,
            ssh_username=username,
            ssh_port=port,
            validation_mode=mode,
            jar_queries=jar_queries if use_queries else None,
        )
    finally:
        if sftp:
            try:
                sftp.close()
            except Exception:
                pass
        try:
            client.close()
        except Exception:
            pass


def parse_validation_request(data: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize API JSON body for remote SSH validation."""
    path = (data.get("path") or data.get("environment_path") or "").strip()
    host = (data.get("host") or data.get("ssh_host") or data.get("ip") or "").strip()
    username = (data.get("username") or data.get("ssh_username") or "").strip()
    password = data.get("password") or data.get("ssh_password") or ""
    port_raw = data.get("port") or data.get("ssh_port") or 22
    jar_raw = (
        data.get("env_ssh_jar_names")
        or data.get("jar_names")
        or data.get("crm_manifest_diff_jar")
        or data.get("manifest_diff_jar")
        or ""
    )
    try:
        port = int(port_raw)
    except (TypeError, ValueError):
        port = 22

    queries = parse_jar_queries(str(jar_raw)) if jar_raw else []
    if not queries:
        raise ValueError(
            "Enter JAR name(s) to search in the Remote environment panel "
            "(e.g. AmdocsSecurityManager.jar)."
        )

    return {
        "mode": "ssh",
        "path": path,
        "host": host,
        "username": username,
        "password": password,
        "port": port,
        "jar_queries": queries,
        "use_jar_queries": True,
    }


def run_validation_from_request(data: Dict[str, Any]) -> Dict[str, Any]:
    """Run remote SSH validation from parsed API request dict."""
    req = parse_validation_request(data)
    return validate_environment_jars(
        req["path"],
        ssh_host=req["host"],
        ssh_username=req["username"],
        ssh_password=req["password"],
        ssh_port=req["port"],
        connection_mode="ssh",
        jar_queries=req["jar_queries"] if req["use_jar_queries"] else None,
    )


def render_environment_validation_html(result: Dict[str, Any]) -> str:
    """Standalone HTML report for environment JAR validation."""
    rows = result.get("rows") or []
    summary = result.get("summary") or {}
    query_mode = result.get("validation_mode") == "jar_query"
    if query_mode:
        thead = (
            "<tr><th>#</th><th>Search query</th><th>JAR name</th>"
            "<th>Implementation-Version</th><th>Path</th><th>Status</th></tr>"
        )
        tbody = "".join(
            f"<tr>"
            f"<td>{i}</td>"
            f"<td><code>{html.escape(r.get('query') or r.get('product') or '')}</code></td>"
            f"<td><code>{html.escape(r.get('jar_name', ''))}</code></td>"
            f"<td><strong>{html.escape(r.get('implementation_version') or '—')}</strong></td>"
            f"<td style='font-family:Consolas,monospace;font-size:11px;word-break:break-all;'>"
            f"{html.escape(r.get('path') or '—')}</td>"
            f"<td style='font-weight:600;'>{html.escape(r.get('status', ''))}</td>"
            f"</tr>"
            for i, r in enumerate(rows, start=1)
        )
        title = "Environment JAR Manifest Search (CRM)"
        summary_cards = f"""
      <div class="summary-card"><div class="n">{summary.get('queries', 0)}</div><div class="l">Search queries</div></div>
      <div class="summary-card"><div class="n">{summary.get('total', 0)}</div><div class="l">Result rows</div></div>
      <div class="summary-card"><div class="n">{summary.get('with_version', 0)}</div><div class="l">With version</div></div>
      <div class="summary-card"><div class="n">{summary.get('missing', 0)}</div><div class="l">Queries not found</div></div>"""
    else:
        thead = (
            "<tr><th>#</th><th>Category</th><th>Product</th><th>JAR name</th>"
            "<th>Implementation-Version</th><th>Path</th><th>Status</th></tr>"
        )
        tbody = "".join(
            f"<tr>"
            f"<td>{i}</td>"
            f"<td>{html.escape(r.get('category', ''))}</td>"
            f"<td>{html.escape(r.get('product', ''))}</td>"
            f"<td><code>{html.escape(r.get('jar_name', ''))}</code></td>"
            f"<td><strong>{html.escape(r.get('implementation_version') or '—')}</strong></td>"
            f"<td style='font-family:Consolas,monospace;font-size:11px;word-break:break-all;'>"
            f"{html.escape(r.get('path') or '—')}</td>"
            f"<td style='font-weight:600;'>{html.escape(r.get('status', ''))}</td>"
            f"</tr>"
            for i, r in enumerate(rows, start=1)
        )
        title = "Environment FOSS &amp; Foundation JAR Validation"
        summary_cards = f"""
      <div class="summary-card"><div class="n">{summary.get('total', 0)}</div><div class="l">Catalog JARs</div></div>
      <div class="summary-card"><div class="n">{summary.get('found', 0)}</div><div class="l">JARs found</div></div>
      <div class="summary-card"><div class="n">{summary.get('with_version', 0)}</div><div class="l">With version</div></div>
      <div class="summary-card"><div class="n">{summary.get('missing', 0)}</div><div class="l">Not found</div></div>"""

    queries_line = ""
    if query_mode and result.get("jar_queries"):
        queries_line = (
            "<div><strong>JAR queries:</strong> <code>"
            + html.escape(", ".join(result.get("jar_queries") or []))
            + "</code></div>"
        )

    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    body {{ font-family: 'Segoe UI', system-ui, sans-serif; margin: 0; padding: 20px;
      background: linear-gradient(135deg, #4c1d95 0%, #7c3aed 100%); min-height: 100vh; }}
    .container {{ max-width: 1100px; margin: 0 auto; background: #fff; border-radius: 16px;
      box-shadow: 0 20px 60px rgba(0,0,0,0.25); overflow: hidden; }}
    .header {{ background: linear-gradient(135deg, #5b21b6 0%, #7c3aed 100%); color: #fff; padding: 24px 28px; }}
    .header h1 {{ margin: 0 0 6px 0; font-size: 24px; }}
    .meta {{ padding: 16px 28px; background: #f5f3ff; font-size: 13px; color: #334155; border-bottom: 1px solid #ddd6fe; }}
    .summary {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; padding: 16px 28px; background: #faf5ff; }}
    .summary-card {{ background: #fff; border: 1px solid #e9d5ff; border-radius: 10px; padding: 12px; text-align: center; }}
    .summary-card .n {{ font-size: 22px; font-weight: 700; color: #6d28d9; }}
    .summary-card .l {{ font-size: 11px; color: #64748b; margin-top: 4px; }}
    .content {{ padding: 20px 28px 28px; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
    th, td {{ border: 1px solid #e2e8f0; padding: 10px; text-align: left; }}
    th {{ background: #ede9fe; color: #5b21b6; }}
    .back-link {{ display: inline-block; margin-top: 20px; padding: 10px 18px; background: #7c3aed;
      color: #fff; text-decoration: none; border-radius: 8px; font-weight: 600; }}
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <h1>{title}</h1>
      <p style="margin:0;opacity:0.95;">Implementation-Version from META-INF/MANIFEST.MF (remote environment scan)</p>
    </div>
    <div class="meta">
      <div><strong>Connection:</strong> SSH</div>
      <div><strong>Environment:</strong> <code>{html.escape(result.get('environment_path', ''))}</code></div>
      <div><strong>SSH host:</strong> {html.escape(result.get('ssh_host', ''))} (port {html.escape(str(result.get('ssh_port') or 22))})</div>
      <div><strong>SSH user:</strong> {html.escape(result.get('ssh_username', ''))}</div>
      {queries_line}
      <div><strong>Generated:</strong> {html.escape(result.get('generated', ''))}</div>
    </div>
    <div class="summary">
      {summary_cards}
    </div>
    <div class="content">
      <table>
        <thead>{thead}</thead>
        <tbody>{tbody}</tbody>
      </table>
      <a href="/" class="back-link">← Back to Home</a>
    </div>
  </div>
</body>
</html>"""
