#!/usr/bin/env python3
"""Merge background /run comparison into builtin_server.py."""
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
path = ROOT / "builtin_server.py"
lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
worker_lines = (ROOT / "_extracted_worker.py").read_text(encoding="utf-8").splitlines(keepends=True)

ctx_unpack = """
        ctx = parse_run_comparison_qs(qs)
        global last_folder1_path, last_folder2_path
        if ctx.folder1:
            last_folder1_path = ctx.folder1
        if ctx.folder2:
            last_folder2_path = ctx.folder2
        folder1 = ctx.folder1
        folder2 = ctx.folder2
        mode = ctx.mode
        app_ascf = ctx.app_ascf
        app_crm = ctx.app_crm
        app_uxf = ctx.app_uxf
        app_dex = ctx.app_dex
        selected_apps = list(ctx.selected_apps)
        show_all_differences = ctx.show_all_differences
        ignore_timestamp_only = ctx.ignore_timestamp_only
        suppress_warnings = ctx.suppress_warnings
        jar_name = ctx.jar_name
        fast_crm_full = ctx.fast_crm_full
        crm_class_diff_jar = ctx.crm_class_diff_jar
        crm_manifest_diff_jar = ctx.crm_manifest_diff_jar
        crm_config_files_filter = ctx.crm_config_files_filter
        crm_explicit_size_paths = ctx.crm_explicit_size_paths
        compare_clfy_agent_zip = ctx.compare_clfy_agent_zip
        use_local_copy = ctx.use_local_copy
        keep_temp_folders = ctx.keep_temp_folders
        exclude_missing_folder2 = ctx.exclude_missing_folder2
        rename_folder2_only = ctx.rename_folder2_only
        rename_suffix = ctx.rename_suffix
        max_retries = ctx.max_retries
        retry_delay = ctx.retry_delay
        dex_nexus_previous = ctx.dex_nexus_previous
        dex_nexus_latest = ctx.dex_nexus_latest
        dex_webapp_ces_previous = ctx.dex_webapp_ces_previous
        dex_webapp_ces_latest = ctx.dex_webapp_ces_latest
        dex_playbook_pair = ctx.dex_playbook_pair
        dex_webapp_pair = ctx.dex_webapp_pair
        dex_nexus_mode = ctx.dex_nexus_mode
        crm_archive_mode = False
""".strip().splitlines(keepends=True)
ctx_unpack = ["        " + ln.strip() + "\n" for ln in ctx_unpack if ln.strip()]

new_worker = worker_lines[:2] + ctx_unpack + worker_lines[2:]

helpers = Path(__file__).with_name("_merge_helpers_snippet.py")
helpers.write_text(
    r'''
from types import SimpleNamespace


def parse_run_comparison_qs(qs: dict) -> SimpleNamespace:
    """Parse /run query string into comparison options."""
    folder1 = normalize_build_path_for_windows(qs.get("folder1_path", [""])[0].strip())
    folder2 = normalize_build_path_for_windows(qs.get("folder2_path", [""])[0].strip())
    mode = qs.get("mode", ["full"])[0]
    if "naming_convention" in qs:
        mode = "naming"
    app_ascf = "app_ascf" in qs
    app_crm = "app_crm" in qs
    app_uxf = "app_uxf" in qs
    app_dex = "app_dex" in qs
    selected_apps = []
    if app_ascf:
        selected_apps.append("ASCF")
    if app_crm:
        selected_apps.append("CRM")
    if app_uxf:
        selected_apps.append("UXF/OMNI")
    if app_dex:
        selected_apps.append("DEX")
    _clfy_vals = qs.get("compare_clfy_agent_zip", ["0"])
    try:
        max_retries = int(qs.get("max_retries", ["3"])[0])
    except (ValueError, IndexError):
        max_retries = 3
    try:
        retry_delay = float(qs.get("retry_delay", ["1.0"])[0])
    except (ValueError, IndexError):
        retry_delay = 1.0
    dex_nexus_previous = qs.get("dex_nexus_previous_version", [""])[0].strip()
    dex_nexus_latest = qs.get("dex_nexus_latest_version", [""])[0].strip()
    dex_webapp_ces_previous = qs.get("dex_webapp_ces_previous_version", [""])[0].strip()
    dex_webapp_ces_latest = qs.get("dex_webapp_ces_latest_version", [""])[0].strip()
    dex_playbook_pair = bool(dex_nexus_previous and dex_nexus_latest)
    dex_webapp_pair = bool(dex_webapp_ces_previous and dex_webapp_ces_latest)
    dex_nexus_mode = app_dex and (dex_playbook_pair or dex_webapp_pair)
    return SimpleNamespace(
        folder1=folder1,
        folder2=folder2,
        mode=mode,
        app_ascf=app_ascf,
        app_crm=app_crm,
        app_uxf=app_uxf,
        app_dex=app_dex,
        selected_apps=selected_apps,
        show_all_differences=True,
        ignore_timestamp_only=True,
        suppress_warnings=True,
        jar_name=qs.get("jar_name", [""])[0].strip(),
        fast_crm_full="fast_crm_full" in qs,
        crm_class_diff_jar=qs.get("crm_class_diff_jar", [""])[0].strip(),
        crm_manifest_diff_jar=qs.get("crm_manifest_diff_jar", [""])[0].strip(),
        crm_config_files_filter=qs.get("crm_config_files_filter", [""])[0].strip(),
        crm_explicit_size_paths=qs.get("crm_explicit_size_paths", [""])[0].strip(),
        compare_clfy_agent_zip=str(_clfy_vals[-1]).strip().lower() in ("1", "on", "true", "yes"),
        use_local_copy="use_local_copy" in qs,
        keep_temp_folders="keep_temp_folders" in qs,
        exclude_missing_folder2="exclude_missing_folder2" in qs,
        rename_folder2_only="rename_folder2_only" in qs,
        rename_suffix=(qs.get("rename_suffix", ["_renamed"])[0] or "_renamed").strip() or "_renamed",
        max_retries=max_retries,
        retry_delay=retry_delay,
        dex_nexus_previous=dex_nexus_previous,
        dex_nexus_latest=dex_nexus_latest,
        dex_webapp_ces_previous=dex_webapp_ces_previous,
        dex_webapp_ces_latest=dex_webapp_ces_latest,
        dex_playbook_pair=dex_playbook_pair,
        dex_webapp_pair=dex_webapp_pair,
        dex_nexus_mode=dex_nexus_mode,
    )


class _ComparisonRunSink:
    """Captures HTTP responses from background comparison; stores redirect URL on session."""

    def __init__(self, session_progress: dict, handler_impl):
        self.session_progress = session_progress
        self._handler_impl = handler_impl
        self._status = 200
        self._body = b""

    def send_response(self, code):
        self._status = code

    def send_header(self, key, value):
        if key.lower() == "location":
            self.session_progress["result_url"] = value

    def end_headers(self):
        if self._status >= 400:
            self.session_progress["error_page"] = True

    def write(self, data):
        if isinstance(data, str):
            data = data.encode("utf-8")
        self._body += data
        if self._status >= 400:
            self.session_progress["error_html"] = self._body.decode("utf-8", errors="replace")

    @property
    def wfile(self):
        return self

    def __getattr__(self, item):
        return getattr(self._handler_impl, item)


def comparison_wait_page_html(session_id: str) -> bytes:
    import json as _json
    sid = html.escape(session_id or "")
    js_sid = _json.dumps(session_id)
    body = (
        f"""
    <div style="max-width:720px;margin:40px auto;padding:24px;background:#fff;border-radius:12px;border:1px solid #e2e8f0;box-shadow:0 4px 20px rgba(0,0,0,0.06);">
      <h2 style="margin:0 0 12px 0;color:#0f172a;">Comparison running in background</h2>
      <p style="color:#475569;line-height:1.6;margin:0 0 16px 0;">
        You can open <strong>Console Output</strong> on the home page or
        <a href="/reports" target="_blank" rel="noopener noreferrer">browse reports</a> in a new tab — the comparison keeps running.
      </p>
      <p style="font-size:13px;color:#64748b;">Session: <code>{sid}</code></p>
      <div style="margin:16px 0;background:#f1f5f9;border-radius:8px;height:24px;overflow:hidden;">
        <div id="wait-progress-bar" style="height:100%;width:0%;background:linear-gradient(90deg,#3b82f6,#6366f1);transition:width 0.3s;"></div>
      </div>
      <p id="wait-progress-text" style="font-size:14px;color:#334155;margin:8px 0;">Starting...</p>
      <p id="wait-current-file" style="font-size:12px;color:#64748b;word-break:break-all;"></p>
      <p style="margin:16px 0 0 0;"><a href="/">← Home</a></p>
    </div>
    <script>
      (function() {{
        var sessionId = """
        + js_sid
        + """;
        function poll() {{
          fetch('/progress?session_id=' + encodeURIComponent(sessionId))
            .then(function(r) {{ return r.json(); }})
            .then(function(d) {{
              var pct = d.percentage || 0;
              document.getElementById('wait-progress-bar').style.width = pct + '%';
              document.getElementById('wait-progress-text').textContent =
                (d.running ? 'Running: ' : 'Status: ') + pct.toFixed(1) + '%';
              document.getElementById('wait-current-file').textContent = d.current_file || '';
              if (d.result_url) {{ window.location.href = d.result_url; return; }}
              if (!d.running && d.error) {{
                document.getElementById('wait-progress-text').textContent = 'Error: ' + d.error;
                return;
              }}
              if (!d.running && !d.error) {{ window.location.href = '/'; return; }}
              setTimeout(poll, 1000);
            }})
            .catch(function() {{ setTimeout(poll, 2000); }});
        }}
        poll();
      }})();
    </script>
    """
    )
    return html_page("Comparison in progress", body)
'''.lstrip(),
    encoding="utf-8",
)
helpers_text = helpers.read_text(encoding="utf-8")

handler_methods = """
    def _send_comparison_wait_page(self, session_id: str):
        body = comparison_wait_page_html(session_id)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def _run_comparison_thread(handler_impl, qs_snap: dict, session_id: str):
        session_progress = get_session(session_id)
        if not session_progress:
            return
        sink = _ComparisonRunSink(session_progress, handler_impl)
        try:
            handler_impl._execute_run_comparison(sink, qs_snap, session_id, session_progress)
        except Exception as e:
            import traceback
            session_progress["error"] = str(e)
            add_session_log(session_id, f"Unhandled worker error: {e}", "ERROR")
            add_session_log(session_id, traceback.format_exc(), "ERROR")
        finally:
            session_progress["running"] = False
            comparison_progress["running"] = False

"""

# Add SimpleNamespace import
if not any("from types import SimpleNamespace" in ln for ln in lines[:40]):
    for i, ln in enumerate(lines[:40]):
        if ln.startswith("from typing"):
            lines.insert(i + 1, "from types import SimpleNamespace\n")
            break

# Insert helpers before AppHandler
class_idx = next(i for i, ln in enumerate(lines) if ln.startswith("class AppHandler"))
lines.insert(class_idx, helpers_text)

# Insert worker + methods before do_GET
do_get_idx = next(i for i, ln in enumerate(lines) if "    def do_GET(self)" in ln)
lines.insert(do_get_idx, "".join(new_worker) + handler_methods)

# Line-based replace: find timestamp line in /run
start_idx = None
for i, ln in enumerate(lines):
    if ln.strip() == 'timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")' and i > 4000:
        start_idx = i
        break
if start_idx is None:
    raise SystemExit("start_idx not found")

# Find end: clear_current_session() before "# Not found"
end_idx = None
for i in range(start_idx, len(lines)):
    if lines[i].strip() == "clear_current_session()" and i + 2 < len(lines):
        if "# Not found" in lines[i + 2] or "self.send_error(404)" in lines[i + 1]:
            end_idx = i
            break
if end_idx is None:
    for i in range(start_idx, len(lines)):
        if lines[i].strip() == "clear_current_session()":
            end_idx = i
    if end_idx is None:
        raise SystemExit("end_idx not found")

dispatch = '''            ctx = parse_run_comparison_qs(qs)
            if not ctx.selected_apps:
                body = """<div style="background:#fee2e2;border:2px solid #fca5a5;border-radius:12px;padding:20px;margin:20px 0;">
                    <h2 style="color:#dc2626;margin:0 0 15px 0;">❌ No Application Selected</h2>
                    <p style="color:#991b1b;">Please select at least one application (ASCF, CRM/AMSS, UXF/OMNI, or DEX) to compare.</p>
                </div>
                <p><a href="/" style="display:inline-block;padding:10px 20px;background:#3b82f6;color:white;text-decoration:none;border-radius:8px;">← Back to Main Page</a></p>
                """
                self.send_response(400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html_page("No Application Selected", body))
                return
            if len(ctx.selected_apps) > 1:
                body = f"""<div style="background:#fee2e2;border:2px solid #fca5a5;border-radius:12px;padding:20px;margin:20px 0;">
                    <h2 style="color:#dc2626;margin:0 0 15px 0;">❌ Multiple Applications Selected</h2>
                    <p style="color:#991b1b;">Please select only one application at a time.</p>
                    <p style="color:#991b1b;">Selected: {', '.join(ctx.selected_apps)}</p>
                </div>
                <p><a href="/" style="display:inline-block;padding:10px 20px;background:#3b82f6;color:white;text-decoration:none;border-radius:8px;">← Back to Main Page</a></p>
                """
                self.send_response(400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html_page("Multiple Applications Selected", body))
                return

            session_id = qs.get("session_id", [""])[0]
            if not session_id:
                session_id = str(uuid.uuid4())[:8]
            session_id, session_progress = get_or_create_session(session_id)
            with sessions_lock:
                if not session_progress.get("worker_started"):
                    session_progress["worker_started"] = True
                    session_progress["result_url"] = None
                    session_progress.pop("error_html", None)
                    session_progress["running"] = True
                    qs_snap = {k: list(v) for k, v in qs.items()}
                    handler_impl = AppHandler.__new__(AppHandler)
                    threading.Thread(
                        target=AppHandler._run_comparison_thread,
                        args=(handler_impl, qs_snap, session_id),
                        daemon=True,
                        name=f"comparison-{session_id}",
                    ).start()
            self._send_comparison_wait_page(session_id)
            return

'''.splitlines(keepends=True)

lines[start_idx : end_idx + 1] = dispatch
path.write_text("".join(lines), encoding="utf-8")
print(f"merged: removed lines {start_idx+1}-{end_idx+1}, inserted dispatch")
