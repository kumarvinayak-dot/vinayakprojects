#!/usr/bin/env python3
"""
WEF remote comparison over SSH.

Connects to a Unix/Linux host, inventories two deployment paths (old vs new),
and produces HTML/text reports similar to other comparison applications.
"""

from __future__ import annotations

import html
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional, TextIO

from ssh_environment_validate import _connect_ssh, _shell_quote

_TEXT_SECTION_LIMIT = 500
_HTML_SECTION_LIMIT = 2000


@dataclass
class WEFComparisonResult:
    relative_path: str
    status: str  # added, removed, modified
    size_old: Optional[int] = None
    size_new: Optional[int] = None
    details: str = ""


@dataclass
class WEFComparer:
    ssh_host: str
    ssh_username: str
    ssh_password: str
    old_path: str
    new_path: str
    ssh_port: int = 22

    results: list[WEFComparisonResult] = field(default_factory=list)
    comparison_error: Optional[str] = None
    show_missing_only: bool = False

    total_files_old: int = 0
    total_files_new: int = 0
    count_unchanged: int = 0
    processed_files: int = 0
    total_files: int = 0
    current_file: str = ""
    progress_percentage: float = 0.0
    comparison_start_time: Optional[float] = None

    def _log(self, msg: str, callback: Optional[Callable[[str, str], None]] = None) -> None:
        print(f"[WEF] {msg}", flush=True)
        if callback:
            callback(msg, "INFO")

    def _progress(
        self,
        current: str,
        done: int,
        total: int,
        callback: Optional[Callable[..., None]] = None,
    ) -> None:
        self.current_file = current
        self.processed_files = done
        self.total_files = max(total, 1)
        self.progress_percentage = min(100.0, (done / self.total_files) * 100.0)
        if callback:
            callback(current, done, total)

    @staticmethod
    def _normalize_unix_path(path: str) -> str:
        p = (path or "").strip().replace("\\", "/")
        while "//" in p:
            p = p.replace("//", "/")
        return p.rstrip("/") or "/"

    def _bucket_rows(self) -> tuple[list[WEFComparisonResult], list[WEFComparisonResult], list[WEFComparisonResult]]:
        removed: list[WEFComparisonResult] = []
        added: list[WEFComparisonResult] = []
        modified: list[WEFComparisonResult] = []
        for row in self.results:
            if row.status == "removed":
                removed.append(row)
            elif row.status == "added":
                added.append(row)
            elif row.status == "modified":
                modified.append(row)
        return removed, added, modified

    def _remote_list_files(
        self,
        session,
        root_path: str,
        *,
        log_callback: Optional[Callable[[str, str], None]] = None,
    ) -> dict[str, int]:
        """Return relative_path -> size for all regular files under root_path."""
        root = self._normalize_unix_path(root_path)
        q = _shell_quote(root)
        cmd = (
            f"if [ ! -d {q} ]; then echo '__WEF_ERR__:not_a_directory'; exit 2; fi; "
            f"if [ ! -r {q} ]; then echo '__WEF_ERR__:not_readable'; exit 2; fi; "
            f"find {q} -type f 2>/dev/null | while IFS= read -r f; do "
            f'sz=$(stat -c "%s" "$f" 2>/dev/null || echo 0); '
            f'rel="${{f#{root}/}}"; rel="${{rel#{root}}}"; '
            f'printf "%s\\t%s\\n" "$sz" "$rel"; '
            f"done"
        )
        self._log(f"Scanning remote path: {root}", log_callback)
        code, out, err = session.exec_command(cmd, timeout=3600)
        if code != 0:
            detail = (err or out or "remote find failed").strip()
            if "__WEF_ERR__:not_a_directory" in out:
                raise ValueError(f"Remote path is not a directory: {root}")
            if "__WEF_ERR__:not_readable" in out:
                raise ValueError(f"Remote path is not readable: {root}")
            raise ValueError(f"Remote scan failed for {root}: {detail}")

        files: dict[str, int] = {}
        for line in out.splitlines():
            line = line.strip()
            if not line or line.startswith("__WEF_ERR__"):
                continue
            if "\t" not in line:
                continue
            sz_s, rel = line.split("\t", 1)
            rel = rel.replace("\\", "/").lstrip("./")
            if not rel:
                continue
            try:
                sz = int(sz_s.strip())
            except ValueError:
                sz = 0
            files[rel] = sz
        self._log(f"Found {len(files):,} file(s) under {root}", log_callback)
        return files

    def compare(
        self,
        *,
        log_callback: Optional[Callable[[str, str], None]] = None,
        progress_callback: Optional[Callable[..., None]] = None,
    ) -> list[WEFComparisonResult]:
        self.comparison_start_time = time.time()
        self.results = []
        self.count_unchanged = 0
        self.comparison_error = None

        host = (self.ssh_host or "").strip()
        user = (self.ssh_username or "").strip()
        password = self.ssh_password or ""
        old_root = self._normalize_unix_path(self.old_path)
        new_root = self._normalize_unix_path(self.new_path)

        if not host:
            self.comparison_error = "SSH host / IP is required."
            return self.results
        if not user:
            self.comparison_error = "SSH username is required."
            return self.results
        if not password:
            self.comparison_error = "SSH password is required."
            return self.results
        if not old_root or old_root == "/":
            self.comparison_error = "Old build path on the Unix host is required."
            return self.results
        if not new_root or new_root == "/":
            self.comparison_error = "New build path on the Unix host is required."
            return self.results

        session = None
        try:
            self._log(f"Connecting to {user}@{host}:{self.ssh_port}…", log_callback)
            session = _connect_ssh(host, user, password, int(self.ssh_port or 22))
            self._progress("Connected — scanning old build path", 1, 4, progress_callback)
            files_old = self._remote_list_files(session, old_root, log_callback=log_callback)
            self.total_files_old = len(files_old)
            self._progress("Scanning new build path", 2, 4, progress_callback)
            files_new = self._remote_list_files(session, new_root, log_callback=log_callback)
            self.total_files_new = len(files_new)

            old_lower = {k.lower(): k for k in files_old}
            new_lower = {k.lower(): k for k in files_new}
            all_keys = sorted(set(old_lower.keys()) | set(new_lower.keys()))
            self.total_files = len(all_keys)
            self._progress("Comparing file inventories", 3, 4, progress_callback)

            count_added = 0
            count_removed = 0
            count_modified = 0

            for idx, key_l in enumerate(all_keys, start=1):
                ok = old_lower.get(key_l)
                nk = new_lower.get(key_l)
                rel = ok or nk or key_l
                if idx % 500 == 0 or idx == len(all_keys):
                    self._progress(rel, idx, len(all_keys), progress_callback)

                if ok and not nk:
                    count_removed += 1
                    self.results.append(
                        WEFComparisonResult(
                            relative_path=ok,
                            status="removed",
                            size_old=files_old[ok],
                            details="Only in old build",
                        )
                    )
                elif nk and not ok:
                    count_added += 1
                    self.results.append(
                        WEFComparisonResult(
                            relative_path=nk,
                            status="added",
                            size_new=files_new[nk],
                            details="Only in new build",
                        )
                    )
                else:
                    s_old = files_old[ok]
                    s_new = files_new[nk]
                    if s_old != s_new:
                        count_modified += 1
                        self.results.append(
                            WEFComparisonResult(
                                relative_path=f"{ok} -> {nk}",
                                status="modified",
                                size_old=s_old,
                                size_new=s_new,
                                details=f"Size: {s_old:,} -> {s_new:,} bytes",
                            )
                        )
                    else:
                        self.count_unchanged += 1

            self._progress("Comparison complete", 4, 4, progress_callback)
            self._log(
                f"Done: old={self.total_files_old:,} new={self.total_files_new:,} "
                f"added={count_added:,} removed={count_removed:,} modified={count_modified:,} "
                f"unchanged={self.count_unchanged:,}",
                log_callback,
            )
        except Exception as exc:
            self.comparison_error = str(exc)
            self._log(f"ERROR: {exc}", log_callback)
        finally:
            if session is not None:
                try:
                    session.close()
                except Exception:
                    pass
        return self.results

    def _write_text_section(
        self,
        out: TextIO,
        title: str,
        rows: list[WEFComparisonResult],
        *,
        limit: int = _TEXT_SECTION_LIMIT,
    ) -> None:
        if not rows:
            return
        out.write("-" * 80 + "\n")
        out.write(f"{title} ({len(rows):,})\n")
        out.write("-" * 80 + "\n")
        for r in rows[:limit]:
            out.write(f"  [{r.status}] {r.relative_path}\n")
            if r.details:
                out.write(f"         {r.details}\n")
        if len(rows) > limit:
            out.write(f"  … and {len(rows) - limit:,} more\n")
        out.write("\n")

    def _write_text_report(self, out: TextIO) -> None:
        out.write("=" * 80 + "\n")
        out.write("WEF REMOTE COMPARISON REPORT\n")
        out.write("=" * 80 + "\n")
        out.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        out.write(f"SSH Host: {self.ssh_host}:{self.ssh_port}\n")
        out.write(f"SSH User: {self.ssh_username}\n")
        out.write(f"Old Path: {self.old_path}\n")
        out.write(f"New Path: {self.new_path}\n\n")

        if self.comparison_error:
            out.write(f"ERROR: {self.comparison_error}\n")
            return

        removed, added, modified = self._bucket_rows()
        out.write("-" * 80 + "\n")
        out.write("SUMMARY\n")
        out.write("-" * 80 + "\n")
        out.write(f"Total files (old tree):  {self.total_files_old:,}\n")
        out.write(f"Total files (new tree):  {self.total_files_new:,}\n")
        out.write(f"Files added:             {len(added):,}\n")
        out.write(f"Files removed:           {len(removed):,}\n")
        out.write(f"Files modified (size):   {len(modified):,}\n")
        out.write(f"Files unchanged:         {self.count_unchanged:,}\n\n")

        self._write_text_section(out, "REMOVED", removed)
        self._write_text_section(out, "ADDED", added)
        self._write_text_section(out, "MODIFIED", modified)
        if self.count_unchanged and not self.show_missing_only:
            out.write("-" * 80 + "\n")
            out.write(f"UNCHANGED ({self.count_unchanged:,})\n")
            out.write("-" * 80 + "\n")
            out.write(
                "  Unchanged file paths are omitted from this text report for performance. "
                "See the HTML summary card for the unchanged count.\n\n"
            )
        out.write("=" * 80 + "\n")

    def generate_report(self) -> str:
        from io import StringIO

        buf = StringIO()
        self._write_text_report(buf)
        return buf.getvalue()

    def _render_table_rows(self, rows: list[WEFComparisonResult], limit: int = _HTML_SECTION_LIMIT) -> str:
        out: list[str] = []
        for r in rows[:limit]:
            esc = html.escape(r.relative_path)
            det = html.escape(r.details or "")
            so = f"{r.size_old:,}" if r.size_old is not None else "—"
            sn = f"{r.size_new:,}" if r.size_new is not None else "—"
            status_colors = {
                "added": "#166534",
                "removed": "#b91c1c",
                "modified": "#b45309",
            }
            color = status_colors.get(r.status, "#334155")
            out.append(
                f"<tr>"
                f"<td style='padding:8px 10px;border-bottom:1px solid #e2e8f0;font-weight:600;color:{color};'>"
                f"{html.escape(r.status)}</td>"
                f"<td style='padding:8px 10px;border-bottom:1px solid #e2e8f0;font-family:Consolas,monospace;font-size:12px;word-break:break-word;'>{esc}</td>"
                f"<td style='padding:8px 10px;border-bottom:1px solid #e2e8f0;text-align:right;'>{so}</td>"
                f"<td style='padding:8px 10px;border-bottom:1px solid #e2e8f0;text-align:right;'>{sn}</td>"
                f"<td style='padding:8px 10px;border-bottom:1px solid #e2e8f0;font-size:12px;color:#64748b;'>{det}</td>"
                f"</tr>"
            )
        if len(rows) > limit:
            out.append(
                f"<tr><td colspan='5' style='padding:12px;color:#64748b;font-style:italic;'>"
                f"… and {len(rows) - limit:,} more row(s)</td></tr>"
            )
        return "".join(out)

    def generate_html_report(self) -> str:
        if self.comparison_error:
            return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>WEF Comparison Error</title></head>
<body style="font-family:Segoe UI,sans-serif;padding:24px;">
<h1 style="color:#b91c1c;">WEF Comparison Error</h1>
<p>{html.escape(self.comparison_error)}</p>
<p><a href="/">← Back to Home</a></p></body></html>"""

        removed, added, modified = self._bucket_rows()

        th = (
            "padding:10px;text-align:left;background:#1e3a5f;color:#fff;"
            "font-size:13px;font-weight:600;"
        )

        def _table(title: str, rows: list[WEFComparisonResult], empty_msg: str) -> str:
            body = self._render_table_rows(rows) if rows else (
                f"<tr><td colspan='5' style='padding:14px;color:#64748b;font-style:italic;'>{empty_msg}</td></tr>"
            )
            return f"""
            <div style="margin:24px 0;">
              <h2 style="margin:0 0 10px 0;font-size:18px;color:#0f172a;">{html.escape(title)} ({len(rows):,})</h2>
              <div style="overflow-x:auto;border:1px solid #cbd5e1;border-radius:10px;">
                <table style="width:100%;border-collapse:collapse;min-width:720px;">
                  <thead><tr>
                    <th style="{th}">Status</th>
                    <th style="{th}">Relative path</th>
                    <th style="{th}text-align:right;">Old size</th>
                    <th style="{th}text-align:right;">New size</th>
                    <th style="{th}">Details</th>
                  </tr></thead>
                  <tbody>{body}</tbody>
                </table>
              </div>
            </div>"""

        unchanged_note = ""
        if self.count_unchanged and not self.show_missing_only:
            unchanged_note = (
                f'<p style="margin:16px 0;color:#64748b;font-size:13px;">'
                f"{self.count_unchanged:,} unchanged file(s) — listing omitted for performance.</p>"
            )

        return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>WEF Remote Comparison Report</title>
  <style>
    * {{ box-sizing: border-box; }}
    body {{ font-family: 'Segoe UI', system-ui, sans-serif; margin: 0; padding: 20px;
      background: linear-gradient(135deg, #0f172a 0%, #1e40af 100%); min-height: 100vh; }}
    .container {{ max-width: 1280px; margin: 0 auto; background: #fff; border-radius: 16px;
      box-shadow: 0 20px 60px rgba(0,0,0,0.35); overflow: hidden; }}
    .header {{ background: linear-gradient(135deg, #1e3a5f 0%, #2563eb 100%); color: #fff; padding: 28px 32px; }}
    .header h1 {{ margin: 0 0 8px 0; font-size: 26px; }}
    .subtitle {{ opacity: 0.92; font-size: 14px; line-height: 1.5; }}
    .meta {{ padding: 18px 32px; background: #f8fafc; border-bottom: 1px solid #e2e8f0; font-size: 13px; }}
    .meta dt {{ font-weight: 700; color: #1e3a5f; margin-top: 8px; }}
    .meta dd {{ margin: 4px 0 0 0; word-break: break-all; font-family: Consolas, monospace; font-size: 12px; }}
    .summary {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 12px;
      padding: 20px 32px; background: #eff6ff; }}
    .card {{ background: #fff; border-radius: 10px; padding: 14px; text-align: center; border: 1px solid #bfdbfe; }}
    .card .n {{ font-size: 26px; font-weight: 700; color: #1d4ed8; }}
    .card .l {{ font-size: 11px; color: #64748b; margin-top: 4px; text-transform: uppercase; letter-spacing: 0.04em; }}
    .content {{ padding: 8px 32px 32px; }}
    .back {{ display: inline-block; margin-top: 8px; padding: 10px 18px; background: #2563eb; color: #fff;
      text-decoration: none; border-radius: 8px; font-weight: 600; }}
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <h1>WEF Remote Comparison</h1>
      <div class="subtitle">SSH inventory compare — old vs new deployment paths on Unix/Linux</div>
    </div>
    <dl class="meta">
      <dt>SSH connection</dt><dd>{html.escape(self.ssh_username)}@{html.escape(self.ssh_host)}:{self.ssh_port}</dd>
      <dt>Old build path</dt><dd>{html.escape(self.old_path)}</dd>
      <dt>New build path</dt><dd>{html.escape(self.new_path)}</dd>
      <dt>Generated</dt><dd>{html.escape(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))}</dd>
    </dl>
    <div class="summary">
      <div class="card"><div class="n">{self.total_files_old:,}</div><div class="l">Files in old tree</div></div>
      <div class="card"><div class="n">{self.total_files_new:,}</div><div class="l">Files in new tree</div></div>
      <div class="card"><div class="n">{len(removed):,}</div><div class="l">Removed</div></div>
      <div class="card"><div class="n">{len(added):,}</div><div class="l">Added</div></div>
      <div class="card"><div class="n">{len(modified):,}</div><div class="l">Modified</div></div>
      <div class="card"><div class="n">{self.count_unchanged:,}</div><div class="l">Unchanged</div></div>
    </div>
    <div class="content">
      {_table("Removed (old build only)", removed, "No removed files.")}
      {_table("Added (new build only)", added, "No added files.")}
      {_table("Modified (size differs)", modified, "No modified files.")}
      {unchanged_note}
      <a href="/" class="back">← Back to Home</a>
    </div>
  </div>
</body>
</html>"""

    def save_report(self, filepath: str) -> None:
        nrows = len(self.results)
        self._log(f"Writing text report ({nrows:,} difference row(s))…")
        with open(filepath, "w", encoding="utf-8") as f:
            self._write_text_report(f)
        html_path = os.path.splitext(filepath)[0] + ".html"
        self._log("Writing HTML report…")
        html_body = self.generate_html_report()
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html_body)
        self._log(f"Reports saved: {filepath}, {html_path}")


def run_wef_comparison(
    ssh_host: str,
    ssh_username: str,
    ssh_password: str,
    old_path: str,
    new_path: str,
    *,
    ssh_port: int = 22,
    session_id=None,
    log_callback: Optional[Callable[[str, str], None]] = None,
    progress_callback: Optional[Callable[..., None]] = None,
    show_missing_only: bool = False,
    report_user: str = "",
    report_dir: str = "",
) -> tuple[WEFComparer, list[WEFComparisonResult], str]:
    from report_naming import build_report_filename

    comparer = WEFComparer(
        ssh_host=ssh_host,
        ssh_username=ssh_username,
        ssh_password=ssh_password,
        old_path=old_path,
        new_path=new_path,
        ssh_port=int(ssh_port or 22),
        show_missing_only=show_missing_only,
    )
    results = comparer.compare(log_callback=log_callback, progress_callback=progress_callback)
    _rd = Path(str(report_dir or "."))
    saved_name = build_report_filename(
        "wef_remote_comparison",
        old_path,
        new_path,
        report_user or "unknown",
        path_hint=ssh_host,
        suffix=".txt",
        report_dir=_rd,
    )
    return comparer, results, saved_name
