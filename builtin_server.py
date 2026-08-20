#!/usr/bin/env python3
import sys
import os
import argparse
import subprocess
import time
from pathlib import Path
from datetime import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, quote, unquote
import html
import json
import socket
import ssl
from typing import Optional, Tuple
from types import SimpleNamespace

# Make src importable
PROJECT_ROOT = Path(__file__).parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from enterprise_security import load_dotenv  # noqa: E402

load_dotenv(str(PROJECT_ROOT))
os.environ.setdefault("CRM_PROJECT_ROOT", str(PROJECT_ROOT))

from folder_compare import FolderComparer, NamingConventionComparer, ArchiveContentsComparer, JarManifestComparer, _path_exists, _path_is_file, _path_is_dir  # noqa: E402
from crm_build_paths import get_crm_build_base_paths, normalize_build_path_for_windows  # noqa: E402
from report_naming import (  # noqa: E402
    allocate_report_filename,
    build_report_filename,
    comparison_path_slug,
    resolve_client_identity,
    report_timestamp_with_tz,
)
from report_storage import (  # noqa: E402
    MAX_BROWSE_REPORTS,
    is_allowed_report_name,
    list_report_files,
    resolve_report_path,
    resolve_report_save_dir,
)
from report_html_sections import inject_report_section_controls, report_collapse_extra_css  # noqa: E402
from client_temp_paths import (  # noqa: E402
    cleanup_compare_temp_before_run,
    clear_compare_client_context,
    comparison_space_status,
    estimate_comparison_space_bytes,
    format_bytes_human,
    min_compare_space_bytes,
    normalize_application_slug,
    resolve_client_machine_hostname,
    set_compare_client_context,
)
import uuid
import threading
import io
import sys
from collections import deque

# Session-based progress tracking for multi-user support
# Each user gets a unique session_id to track their own comparison
sessions_lock = threading.Lock()
comparison_sessions = {}  # session_id -> progress dict
copy_sessions = {}  # session_id -> copy progress dict
# Large CRM runs can emit many scan lines; a small deque makes the console look "stuck"
# when older progress lines are dropped and long post-processing phases print little.
SESSION_LOG_MAX_LINES = 25000
session_logs = {}  # session_id -> deque of log messages (bounded by SESSION_LOG_MAX_LINES)

# Dedicated storage for the last copy operation logs (simple and reliable)
last_copy_logs = []  # List of log messages from the most recent copy operation
last_copy_session_id = None  # Session ID of the most recent copy operation
last_folder1_path = ""
last_folder2_path = ""

# Thread-local storage for current session ID
_current_session = threading.local()

class SessionLogger:
    """Captures print output and stores it per-session"""
    def __init__(self, original_stdout):
        self.original_stdout = original_stdout
        self.max_lines_per_session = SESSION_LOG_MAX_LINES
    
    def write(self, message):
        # Always write to original stdout
        if self.original_stdout:
            self.original_stdout.write(message)
        
        # Also store in session log if we have a current session
        session_id = getattr(_current_session, 'id', None)
        if session_id and message.strip():
            self._log_to_session(session_id, message)
    
    def _log_to_session(self, session_id, message):
        with sessions_lock:
            if session_id not in session_logs:
                session_logs[session_id] = deque(maxlen=self.max_lines_per_session)
            
            # Add timestamp to log entries
            timestamp = datetime.now().strftime("%H:%M:%S")
            for line in message.strip().split('\n'):
                if line.strip():
                    session_logs[session_id].append(f"[{timestamp}] {line}")
    
    def flush(self):
        if self.original_stdout:
            self.original_stdout.flush()

def set_current_session(session_id):
    """Set the current session ID for this thread"""
    _current_session.id = session_id

def clear_current_session():
    """Clear the current session ID for this thread"""
    _current_session.id = None

def get_session_logs(session_id, last_n=None):
    """Get logs for a specific session"""
    with sessions_lock:
        if session_id not in session_logs:
            return []
        logs = list(session_logs[session_id])
        if last_n:
            return logs[-last_n:]
        return logs

def add_session_log(session_id, message, level="INFO"):
    """Add a log message to a specific session"""
    with sessions_lock:
        if session_id not in session_logs:
            session_logs[session_id] = deque(maxlen=SESSION_LOG_MAX_LINES)
        timestamp = datetime.now().strftime("%H:%M:%S")
        session_logs[session_id].append(f"[{timestamp}] [{level}] {message}")

def clear_session_logs(session_id):
    """Clear logs for a specific session"""
    with sessions_lock:
        if session_id in session_logs:
            session_logs[session_id].clear()

# Install session logger to capture print statements
_original_stdout = sys.stdout
_session_logger = SessionLogger(_original_stdout)
sys.stdout = _session_logger

_SESSION_ID_RE = __import__("re").compile(r"^[a-zA-Z0-9_-]{1,64}$")


def _sanitize_session_id(session_id: Optional[str]) -> Optional[str]:
    """Accept client session IDs when they match a safe token pattern."""
    if not session_id:
        return None
    sid = str(session_id).strip()
    if _SESSION_ID_RE.match(sid):
        return sid
    return None


def get_or_create_session(session_id=None):
    """Get existing session or create a new one"""
    with sessions_lock:
        clean_id = _sanitize_session_id(session_id)
        if clean_id and clean_id in comparison_sessions:
            return clean_id, comparison_sessions[clean_id]

        new_id = clean_id or str(uuid.uuid4())
        comparison_sessions[new_id] = {
            "running": False,
            "progress": 0,
            "total": 0,
            "current_file": "",
            "eta_seconds": 0,
            "error": None,
            "comparer": None,
            "stop_requested": False,
            "created_at": time.time()
        }
        return new_id, comparison_sessions[new_id]

def get_session(session_id):
    """Get session by ID or return None"""
    with sessions_lock:
        clean_id = _sanitize_session_id(session_id)
        if not clean_id:
            return None
        return comparison_sessions.get(clean_id)


def _application_from_qs(qs: dict) -> str:
    """Selected application label from /run query (ASCF, CRM, UXF/OMNI, DEX, WEF)."""
    if "app_ascf" in qs:
        return "ASCF"
    if "app_crm" in qs:
        return "CRM"
    if "app_uxf" in qs:
        return "UXF/OMNI"
    if "app_dex" in qs:
        return "DEX"
    if "app_wef" in qs:
        return "WEF"
    return ""


def _prepare_comparison_session(handler, qs: dict) -> tuple[str, dict]:
    """Register or update a comparison session before /run (avoids open-report 404 races)."""
    raw_sid = (qs.get("session_id", [""])[0] or "").strip()
    if not raw_sid:
        raw_sid = str(uuid.uuid4())[:8]
    session_id, session_progress = get_or_create_session(raw_sid)
    report_user = qs.get("report_user", [""])[0]
    client_host = (handler.client_address[0] or "")
    headers = {k: v for k, v in handler.headers.items()}
    session_progress["client_identity"] = resolve_client_identity(
        client_host,
        headers,
        report_user,
    )
    session_progress["client_host"] = client_host
    session_progress["client_machine_host"] = resolve_client_machine_hostname(
        client_host,
        headers,
        report_user,
    )
    session_progress["compare_application"] = _application_from_qs(qs)
    session_progress["request_base_url"] = _request_base_url_from_handler(handler)
    session_progress.setdefault("created_at", time.time())
    return session_id, session_progress


def request_client_identity(handler, qs_user: str = "") -> str:
    """Resolve client identity from an HTTP request."""
    from enterprise_security import secure_client_identity

    client_host = (handler.client_address[0] or "")
    headers = {k: v for k, v in handler.headers.items()}
    return secure_client_identity(
        client_host,
        headers,
        qs_user,
        resolve_fn=resolve_client_identity,
    )


def request_client_machine(handler, qs_user: str = "") -> str:
    """Resolve client machine hostname; remote clients cannot override."""
    from enterprise_security import secure_client_machine

    client_host = (handler.client_address[0] or "")
    headers = {k: v for k, v in handler.headers.items()}
    return secure_client_machine(
        client_host,
        headers,
        qs_user,
        resolve_fn=resolve_client_machine_hostname,
    )


def session_owned_by_client(session: dict | None, client_identity: str, client_host: str = "") -> bool:
    """True when this client may access or stop the given comparison session."""
    if not session:
        return False
    owner = str(session.get("client_identity") or "").strip()
    if owner:
        return owner.lower() == str(client_identity or "").strip().lower()
    sess_host = str(session.get("client_host") or "").strip()
    if sess_host and client_host:
        return sess_host == str(client_host).strip()
    return False


def _idle_progress_payload(session_id: str = "") -> dict:
    """Non-running progress response (no cross-session global fallback)."""
    return {
        "running": False,
        "progress": 0,
        "total": 0,
        "percentage": 0,
        "current_file": "",
        "eta_seconds": 0,
        "eta_formatted": "Calculating...",
        "error": None,
        "session_id": session_id or "none",
        "session_registered": False,
        "worker_started": False,
        "stop_requested": False,
        "result_url": None,
    }


def cleanup_old_sessions(max_age_seconds=3600):
    """Remove sessions older than max_age_seconds"""
    with sessions_lock:
        current_time = time.time()
        expired = [sid for sid, sess in comparison_sessions.items() 
                   if current_time - sess.get("created_at", 0) > max_age_seconds and not sess.get("running")]
        for sid in expired:
            del comparison_sessions[sid]
            session_logs.pop(sid, None)
        
        expired_copy = [sid for sid, sess in copy_sessions.items() 
                       if current_time - sess.get("start_time", 0) > max_age_seconds and not sess.get("running")]
        for sid in expired_copy:
            del copy_sessions[sid]

def get_or_create_copy_session(session_id=None):
    """Get existing copy session or create a new one"""
    with sessions_lock:
        if session_id and session_id in copy_sessions:
            return session_id, copy_sessions[session_id]
        
        new_id = session_id or str(uuid.uuid4())[:8]
        copy_sessions[new_id] = {
            "running": False,
            "progress": 0,
            "total": 0,
            "current_file": "",
            "eta_seconds": 0,
            "error": None,
            "start_time": None,
            "bytes_copied": 0,
            "total_bytes": 0,
            "failed_files": [],
            "skipped_count": 0,
            "stop_requested": False
        }
        return new_id, copy_sessions[new_id]

def get_copy_session(session_id):
    """Get copy session by ID"""
    with sessions_lock:
        return copy_sessions.get(session_id)

# Legacy global progress tracking (for backwards compatibility)
# These are still used but now point to the "default" session
comparison_progress = {
    "running": False,
    "progress": 0,
    "total": 0,
    "current_file": "",
    "eta_seconds": 0,
    "error": None,
    "comparer": None
}

# Legacy global copy progress tracking
copy_progress = {
    "running": False,
    "progress": 0,
    "total": 0,
    "current_file": "",
    "eta_seconds": 0,
    "error": None,
    "start_time": None,
    "bytes_copied": 0,
    "total_bytes": 0,
    "failed_files": [],
    "skipped_count": 0
}


def html_page(title: str, body_html: str) -> bytes:
    page = f"""
<!doctype html>
<html>
  <head>
    <meta charset=\"utf-8\" />
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
    <title>{html.escape(title)}</title>
    <style>
      :root {{ --crm-navy:#0f172a; --crm-slate:#1e3a5f; --accent:#1d4ed8; --accent-sky:#0ea5e9; --text:#0f172a; --muted:#64748b; --border:#cbd5e1; --card:#ffffff; }}
      * {{ box-sizing: border-box; }}
      body {{ font-family: system-ui, \"Segoe UI\", Arial, sans-serif; margin: 0; min-height: 100vh; background: linear-gradient(165deg, var(--crm-navy) 0%, var(--crm-slate) 18%, #e2e8f0 18%, #f8fafc 100%); color: var(--text); }}
      .container {{ max-width: 920px; margin: 0 auto; padding: 20px 16px 40px; }}
      h1 {{ margin: 0 0 4px 0; font-size: 1.45rem; font-weight: 700; letter-spacing: -0.02em; }}
      small {{ color: rgba(255,255,255,0.92); font-size: 0.85rem; line-height: 1.45; display: block; margin-top: 4px; }}
      .header-bar {{ background: linear-gradient(120deg, #0f172a 0%, #1e3a5f 45%, #0369a1 100%); color:#fff; border-radius: 16px; padding: 18px 20px; margin-bottom: 18px; box-shadow: 0 12px 40px rgba(15, 23, 42, 0.25); }}
      .header-bar h1 {{ color:#fff; }}
      .header-bar small {{ color: rgba(255,255,255,0.9); }}
      .card {{ background: var(--card); border: 1px solid var(--border); border-radius: 14px; padding: 18px; margin-top: 16px; box-shadow: 0 4px 24px rgba(15, 23, 42, 0.06); }}
      form {{ margin-top: 8px; }}
      label {{ display: block; margin-top: 10px; font-weight: 600; }}
      input[type=text], input[type=number] {{ width: 100%; padding: 10px 12px; border: 1px solid var(--border); border-radius: 8px; outline: none; background: #fff; }}
      input[type=text]:focus, input[type=number]:focus {{ border-color: var(--accent); box-shadow: 0 0 0 3px rgba(29, 78, 216, 0.15); }}
      input[type=radio], input[type=checkbox] {{ accent-color: var(--accent); }}
      select {{ width: 100%; padding: 10px 12px; border:1px solid var(--border); border-radius:8px; background:#fff; outline:none; }}
      select:focus {{ border-color: var(--accent); box-shadow: 0 0 0 3px rgba(13,110,253,0.15); }}
      .row {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
      .options {{ display: grid; grid-template-columns: repeat(2, minmax(240px, 1fr)); gap: 10px 16px; margin-top: 12px; }}
      .actions {{ margin-top: 16px; display: flex; gap: 10px; }}
      button {{ padding: 12px 20px; font-weight: 700; background: linear-gradient(90deg, var(--accent-sky) 0%, var(--accent) 100%); color: #fff; border: 0; border-radius: 12px; cursor: pointer; box-shadow: 0 6px 20px rgba(29, 78, 216, 0.28); transition: transform .05s ease, filter .15s ease; }}
      button:hover {{ filter: brightness(0.98); }}
      button:active {{ transform: translateY(1px); }}
      .btn-secondary {{ background: linear-gradient(90deg, #64748b 0%, #6b7280 100%); box-shadow: 0 4px 10px rgba(100,116,139,0.25); }}
      pre {{ white-space: pre-wrap; word-wrap: break-word; background: #f7f7f9; padding: 16px; border: 1px solid #eee; border-radius: 10px; }}
      a.button {{ display: inline-block; padding: 8px 12px; background: #0366d6; color: #fff; text-decoration: none; border-radius: 6px; }}
      a.button:hover {{ background: #024ea6; }}
      @media (max-width: 720px) {{ .row {{ grid-template-columns: 1fr; }} .options {{ grid-template-columns: 1fr; }} }}
    </style>
  </head>
  <body>
    <div class=\"container\">
    {body_html}
    </div>
  </body>
  </html>
    """
    return page.encode("utf-8")


def index_html(default_folder1: str = "", default_folder2: str = "") -> bytes:
    if not default_folder1:
        default_folder1 = last_folder1_path
    if not default_folder2:
        default_folder2 = last_folder2_path
    _SEC_BOOT = "___CRM_SECURITY_BOOTSTRAP___"
    body = f"""
    <div class=\"header-bar\" style=\"display:flex; align-items:flex-start; justify-content:space-between; gap:12px; flex-wrap:wrap;\"> 
      <div style=\"flex:1;min-width:200px;\">
        <h1>Build Verification (NAS) and Environment Validation (HOST IP) Automation Tool— ASCF/CRM/AMSS/UXF/OMNI/DEX/WEF</h1>
        <small>Select one application, compare old vs new build paths and Validate in the Env. Single URL: <code>/crm</code>, <code>/wef</code>. Built-in server.</small>
      </div>
      <div style=\"display:flex; gap:8px;\">
        <button type=\"button\" class=\"btn-secondary\" onclick=\"openConsoleModal()\" style=\"background:linear-gradient(90deg, #1e293b 0%, #334155 100%);\">📋 Console Output</button>
        <form method=\"post\" action=\"/stop\" onsubmit=\"stopMyComparison(); return false;\" style=\"margin:0;\"> 
          <button type=\"submit\" class=\"btn-secondary\">Stop and refresh</button>
        </form>
        <script>
          function stopMyComparison() {{
            var sessionId = sessionStorage.getItem('activeComparisonSessionId')
              || sessionStorage.getItem('comparisonSessionId') || '';
            if (!sessionId) {{
              alert('No comparison is running from this browser tab.');
              location.href = '/';
              return;
            }}
            fetch('/stop?session_id=' + encodeURIComponent(sessionId), {{method: 'POST'}})
              .then(function(r) {{
                if (r.status === 403) {{
                  alert('You can only stop comparisons started from this browser session.');
                }}
                try {{
                  sessionStorage.removeItem('comparisonRunsInOtherTab');
                  sessionStorage.removeItem('comparisonRunUrl');
                }} catch (e) {{}}
                setTimeout(function(){{ location.href='/'; }}, 300);
              }});
          }}
        </script>
      </div>
    </div>
    <!-- builtin-post-banner -->
    
    <!-- Console Output Modal -->
    <div id=\"console-modal\" style=\"display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.7); z-index:1000; padding:20px; box-sizing:border-box;\">
      <div style=\"background:#1e1e1e; border-radius:12px; height:100%; display:flex; flex-direction:column; max-width:1200px; margin:0 auto; box-shadow:0 10px 40px rgba(0,0,0,0.5);\">
        <div style=\"padding:16px 20px; background:linear-gradient(90deg, #1e293b 0%, #334155 100%); border-radius:12px 12px 0 0; display:flex; justify-content:space-between; align-items:center;\">
          <div style=\"display:flex; align-items:center; gap:12px;\">
            <span style=\"font-size:20px;\">📋</span>
            <h3 style=\"margin:0; color:#fff; font-size:16px;\">Console Output</h3>
            <span id=\"console-session-id\" style=\"background:#3b82f6; color:white; padding:4px 10px; border-radius:12px; font-size:11px; font-family:monospace;\">Session: -</span>
            <span id=\"console-line-count\" style=\"background:#22c55e; color:white; padding:4px 10px; border-radius:12px; font-size:11px;\">0 lines</span>
          </div>
          <div style=\"display:flex; gap:8px;\">
            <button onclick=\"refreshConsoleLogs()\" style=\"background:#3b82f6; color:white; border:none; padding:8px 16px; border-radius:6px; cursor:pointer; font-size:12px; font-weight:600;\">🔄 Refresh</button>
            <button onclick=\"clearConsoleLogs()\" style=\"background:#f59e0b; color:white; border:none; padding:8px 16px; border-radius:6px; cursor:pointer; font-size:12px; font-weight:600;\">🗑️ Clear</button>
            <button onclick=\"downloadConsoleLogs()\" style=\"background:#22c55e; color:white; border:none; padding:8px 16px; border-radius:6px; cursor:pointer; font-size:12px; font-weight:600;\">💾 Download</button>
            <button onclick=\"toggleAutoRefresh()\" id=\"auto-refresh-btn\" style=\"background:#8b5cf6; color:white; border:none; padding:8px 16px; border-radius:6px; cursor:pointer; font-size:12px; font-weight:600;\">▶️ Auto</button>
            <button onclick=\"closeConsoleModal()\" style=\"background:#ef4444; color:white; border:none; padding:8px 16px; border-radius:6px; cursor:pointer; font-size:12px; font-weight:600;\">✕ Close</button>
          </div>
        </div>
        <div id=\"console-error-banner\" style=\"display:none; background:#fee2e2; color:#991b1b; padding:12px 20px; border-left:4px solid #ef4444;\">
          <strong>⚠️ Error:</strong> <span id=\"console-error-msg\"></span>
        </div>
        <div id=\"console-output\" style=\"flex:1; overflow-y:auto; padding:16px; font-family:'Consolas', 'Monaco', monospace; font-size:12px; line-height:1.6; color:#d4d4d4; white-space:pre-wrap; word-break:break-all;\">
          <div style=\"color:#6b7280; text-align:center; padding:40px;\">
            Click \"Refresh\" to load console output...
          </div>
        </div>
        <div style=\"padding:12px 20px; background:#262626; border-radius:0 0 12px 12px; display:flex; justify-content:space-between; align-items:center; font-size:11px; color:#9ca3af;\">
          <div>💡 Tip: Enable \"Auto\" to automatically refresh logs every 2 seconds during comparison</div>
          <div id=\"console-timestamp\">Last updated: -</div>
        </div>
      </div>
    </div>
    
    <!-- Copy Logs Modal (dedicated for copy operations) -->
    <div id=\"copy-logs-modal\" style=\"display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.7); z-index:1000; padding:20px; box-sizing:border-box;\">
      <div style=\"background:#1e1e1e; border-radius:12px; height:100%; display:flex; flex-direction:column; max-width:1200px; margin:0 auto; box-shadow:0 10px 40px rgba(0,0,0,0.5);\">
        <div style=\"padding:16px 20px; background:linear-gradient(90deg, #4f46e5 0%, #7c3aed 100%); border-radius:12px 12px 0 0; display:flex; justify-content:space-between; align-items:center;\">
          <div style=\"display:flex; align-items:center; gap:12px;\">
            <span style=\"font-size:20px;\">📦</span>
            <h3 style=\"margin:0; color:#fff; font-size:16px;\">Copy Operation Logs</h3>
            <span id=\"copy-logs-session-id\" style=\"background:#22c55e; color:white; padding:4px 10px; border-radius:12px; font-size:11px; font-family:monospace;\">Session: -</span>
            <span id=\"copy-logs-line-count\" style=\"background:#f59e0b; color:white; padding:4px 10px; border-radius:12px; font-size:11px;\">0 lines</span>
          </div>
          <div style=\"display:flex; gap:8px;\">
            <button onclick=\"refreshCopyLogs()\" style=\"background:#3b82f6; color:white; border:none; padding:8px 16px; border-radius:6px; cursor:pointer; font-size:12px; font-weight:600;\">🔄 Refresh</button>
            <button onclick=\"downloadCopyLogs()\" style=\"background:#22c55e; color:white; border:none; padding:8px 16px; border-radius:6px; cursor:pointer; font-size:12px; font-weight:600;\">💾 Download</button>
            <button onclick=\"toggleCopyAutoRefresh()\" id=\"copy-auto-refresh-btn\" style=\"background:#8b5cf6; color:white; border:none; padding:8px 16px; border-radius:6px; cursor:pointer; font-size:12px; font-weight:600;\">▶️ Auto</button>
            <button onclick=\"closeCopyLogsModal()\" style=\"background:#ef4444; color:white; border:none; padding:8px 16px; border-radius:6px; cursor:pointer; font-size:12px; font-weight:600;\">✕ Close</button>
          </div>
        </div>
        <div id=\"copy-logs-output\" style=\"flex:1; overflow-y:auto; padding:16px; font-family:'Consolas', 'Monaco', monospace; font-size:12px; line-height:1.6; color:#d4d4d4; white-space:pre-wrap; word-break:break-all;\">
          <div style=\"color:#6b7280; text-align:center; padding:40px;\">
            Click \"Refresh\" to load copy operation logs...
          </div>
        </div>
        <div style=\"padding:12px 20px; background:#262626; border-radius:0 0 12px 12px; display:flex; justify-content:space-between; align-items:center; font-size:11px; color:#9ca3af;\">
          <div>💡 Tip: Copy logs show the most recent copy operation. Enable \"Auto\" to track progress in real-time.</div>
          <div id=\"copy-logs-timestamp\">Last updated: -</div>
        </div>
      </div>
    </div>

    <div class=\"card\" style=\"border-top:4px solid #1d4ed8;border-left:6px solid rgba(29,78,216,0.25);\">
    <!-- Backend still accepts naming/foss/jar_name/copy — controls removed from UI for CRM-focused workflow -->
    <form method=\"get\" action=\"/run\"> 
      <input type=\"hidden\" name=\"report_user\" id=\"report_user\" value=\"\" />
      <p id=\"report-user-hint\" style=\"margin:0 0 10px 0;font-size:12px;color:#64748b;display:none;\">Reports save to <strong id=\"report-user-label\"></strong> PC (<code>\\\\&lt;host&gt;\\c$\\crm_compare_reports</code>) as <em>first_report</em>, <em>second_report</em>, … per day until you delete them.</p>
      <div id=\"home-mode-toggles\" style=\"margin-bottom:14px;\">
        <div style=\"margin-bottom:14px;\">
          <label style=\"display:flex;align-items:center;gap:8px;font-size:13px;font-weight:600;cursor:pointer;\">
            <input type=\"checkbox\" id=\"build_compare_toggle\" /> Build Comparison
          </label>
          <p style=\"margin:4px 0 0 0;font-size:12px;color:#64748b;\">Enter old vs new build paths, select one application, configure comparison options, and run.</p>
        </div>
        <div style=\"margin-bottom:0;\">
          <label style=\"display:flex;align-items:center;gap:8px;font-size:13px;font-weight:600;cursor:pointer;\">
            <input type=\"checkbox\" id=\"env_path_toggle\" /> Environment Validation
          </label>
          <p style=\"margin:4px 0 0 0;font-size:12px;color:#64748b;\">Open remote SSH panel to search JAR manifest <code>Implementation-Version</code> on a deployment server. You can use this while a build comparison runs in another tab.</p>
        </div>
      </div>

      <div id=\"comparison-popup-blocked-banner\" style=\"display:none;margin:0 0 14px 0;padding:12px 14px;background:#fef3c7;border:1px solid #fcd34d;border-radius:10px;font-size:13px;color:#92400e;\">
        Pop-up blocked — comparison progress did not open in a new tab.
        <a id=\"comparison-popup-blocked-link\" href=\"#\" target=\"_blank\" rel=\"noopener noreferrer\" style=\"color:#b45309;font-weight:700;margin-left:6px;\">Open comparison progress</a>
        (progress will show on this page until you open the progress tab).
      </div>

      <div id=\"comparison-delegated-banner\" style=\"display:none;margin:0 0 14px 0;padding:12px 14px;background:#eff6ff;border:1px solid #93c5fd;border-radius:10px;font-size:13px;color:#1e40af;\">
        Build comparison is running in another tab.
        <a id=\"comparison-delegated-link\" href=\"#\" target=\"_blank\" rel=\"noopener noreferrer\" style=\"color:#1d4ed8;font-weight:700;margin-left:6px;\">View comparison progress</a>
        — this page stays available for environment validation. Use <strong>Console Output</strong> here for live logs (same session). The report opens automatically in the progress tab when finished.
      </div>

      <!-- Build comparison progress lives in the new tab opened by Run comparison; keep home page free for environment validation -->
      <div id=\"progress-container\" style=\"display:none; margin:0 0 20px 0; padding:16px; background:#f7f7f9; border-radius:10px; border:1px solid #eee;\">
        <h3 style=\"margin:0 0 10px 0;\">Build comparison in progress</h3>
        <div style=\"background:#e0e0e0; border-radius:10px; height:30px; position:relative; overflow:hidden;\">
          <div id=\"progress-bar\" style=\"background:linear-gradient(90deg, #0ea5e9 0%, #0d6efd 100%); height:100%; width:0%; transition:width 0.3s; display:flex; align-items:center; justify-content:center; color:white; font-weight:bold; font-size:14px;\">
            <span id=\"progress-text\">0%</span>
          </div>
        </div>
        <div style=\"margin-top:10px; font-size:14px; color:#666;\">
          <div><strong>Status:</strong> <span id=\"progress-status\">Starting...</span></div>
          <div><strong>Files processed:</strong> <span id=\"progress-files\">0 / 0</span></div>
          <div><strong>Current file:</strong> <span id=\"progress-current\" style=\"font-family:monospace; font-size:12px; word-break:break-all; display:block; margin-top:4px;\">-</span></div>
          <div><strong>Estimated time remaining:</strong> <span id=\"progress-eta\">Calculating...</span></div>
        </div>
        <div id=\"progress-error\" style=\"display:none; margin-top:10px; padding:10px; background:#fee; border:1px solid #fca5a5; border-radius:6px; color:#dc2626;\">
          <strong>Error:</strong> <span id=\"progress-error-msg\"></span>
        </div>
      </div>

      <div id=\"build-compare-panel\" style=\"display:none;\">
      <div style=\"padding:14px 16px;border-radius:12px;background:#f1f5f9;border:1px solid #e2e8f0;margin-bottom:14px;\">
        <div style=\"font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.06em;color:#1e3a5f;margin:0 0 10px 0;\">Build paths</div>
      <div id=\"nas-build-paths-section\">
      <div class=\"row\">
        <div>
          <label>Old build</label>
          <input type=\"text\" name=\"folder1_path\" value=\"{html.escape(default_folder1)}\" placeholder=\"UNC or local — baseline / previous build\" />
        </div>
        <div>
          <label>New build</label>
          <input type=\"text\" name=\"folder2_path\" value=\"{html.escape(default_folder2)}\" placeholder=\"UNC or local — current build\" />
        </div>
      </div>
      </div>

      <div id=\"crm-only-panel\" style=\"margin-top:12px;padding:14px;background:linear-gradient(135deg,#f0f9ff 0%,#ecfeff 100%);border:1px solid #7dd3fc;border-radius:12px;\">
        <h4 style=\"margin:0 0 8px 0;font-size:15px;color:#0369a1;\">CRM &amp; AMSS version base paths</h4>
        <p style=\"margin:0 0 10px 0;font-size:12px;color:#64748b;\">Load one list at a time (full-width table). CRM paths from server config. AMSS: <code>\\\\raaisilon\\cmprod\\pbg\\cmbuild\\AMSS</code>. <strong>Open in Explorer</strong> on <strong>localhost</strong>; otherwise path is copied.</p>
        <div style=\"display:flex;flex-wrap:wrap;gap:10px;margin-bottom:8px;\">
          <button type=\"button\" id=\"btn-crm-base-paths\" style=\"padding:10px 16px;font-weight:700;background:#0369a1;color:#fff;border:0;border-radius:10px;cursor:pointer;\">Load CRM base paths</button>
          <button type=\"button\" id=\"btn-amss-base-paths\" style=\"padding:10px 16px;font-weight:700;background:#0d9488;color:#fff;border:0;border-radius:10px;cursor:pointer;\">Load AMSS base paths</button>
        </div>
        <p id=\"crm-amss-paths-view-title\" style=\"display:none;margin:0 0 8px 0;font-size:14px;font-weight:700;color:#334155;\"></p>
        <p id=\"crm-base-paths-status\" style=\"margin:0 0 6px 0;font-size:12px;color:#64748b;display:none;\"></p>
        <div id=\"crm-base-paths-wrap\" style=\"display:none;width:100%;margin-bottom:12px;\">
          <div id=\"crm-base-paths-toolbar\" style=\"display:none;margin:0 0 6px 0;\">
            <button type=\"button\" id=\"btn-crm-toggle-expand\" style=\"padding:6px 10px;font-size:12px;font-weight:600;border:0;border-radius:8px;cursor:pointer;color:#fff;background:#64748b;\">Minimize table</button>
          </div>
          <div id=\"crm-base-paths-scroll\" style=\"overflow-x:auto;width:100%;\">
            <table style=\"width:100%;border-collapse:collapse;font-size:13px;background:#fff;border:1px solid #d0d7de;border-radius:8px;overflow:hidden;\">
              <thead><tr style=\"background:#ede9fe;\">
                <th style=\"text-align:left;padding:8px 10px;color:#4c1d95;border-bottom:1px solid #d0d7de;\">CRM VERSION</th>
                <th style=\"text-align:left;padding:8px 10px;color:#4c1d95;border-bottom:1px solid #d0d7de;\">Base path</th>
                <th style=\"padding:8px 10px;color:#4c1d95;border-bottom:1px solid #d0d7de;\">Actions</th>
              </tr></thead>
              <tbody id=\"crm-base-paths-tbody\"></tbody>
            </table>
          </div>
        </div>
        <p id=\"amss-base-paths-status\" style=\"margin:0 0 6px 0;font-size:12px;color:#64748b;display:none;\"></p>
        <div id=\"amss-base-paths-wrap\" style=\"display:none;width:100%;margin-bottom:12px;\">
          <div id=\"amss-base-paths-toolbar\" style=\"display:none;margin:0 0 6px 0;\">
            <button type=\"button\" id=\"btn-amss-toggle-expand\" style=\"padding:6px 10px;font-size:12px;font-weight:600;border:0;border-radius:8px;cursor:pointer;color:#fff;background:#64748b;\">Minimize table</button>
          </div>
          <div id=\"amss-base-paths-scroll\" style=\"overflow-x:auto;width:100%;\">
            <table style=\"width:100%;border-collapse:collapse;font-size:13px;background:#fff;border:1px solid #d0d7de;border-radius:8px;overflow:hidden;\">
              <thead><tr style=\"background:#ccfbf1;\">
                <th style=\"text-align:left;padding:8px 10px;color:#0f766e;border-bottom:1px solid #d0d7de;\">AMSS VERSION</th>
                <th style=\"text-align:left;padding:8px 10px;color:#0f766e;border-bottom:1px solid #d0d7de;\">Base path</th>
                <th style=\"padding:8px 10px;color:#0f766e;border-bottom:1px solid #d0d7de;\">Actions</th>
              </tr></thead>
              <tbody id=\"amss-base-paths-tbody\"></tbody>
            </table>
          </div>
        </div>
      </div>
      <div id=\"ascf-only-panel-paths\" style=\"display:none;margin-top:12px;padding:14px;background:linear-gradient(135deg,#faf5ff 0%,#f5f3ff 100%);border:1px solid #d8b4fe;border-radius:12px;\">
        <h4 style=\"margin:0 0 6px 0;font-size:15px;color:#6b21a8;\">ASCF version base paths</h4>
        <p style=\"margin:0 0 10px 0;font-size:12px;color:#64748b;\">Load known ASCF build roots. <strong>Open in Explorer</strong> works on <strong>localhost</strong>; remotely, path is copied.</p>
        <button type=\"button\" id=\"btn-ascf-base-paths\" style=\"padding:10px 16px;font-weight:700;background:#7c3aed;color:#fff;border:0;border-radius:10px;cursor:pointer;\">Load ASCF base paths</button>
        <p id=\"ascf-base-paths-status\" style=\"margin:8px 0 0 0;font-size:12px;color:#64748b;display:none;\"></p>
        <div id=\"ascf-base-paths-wrap\" style=\"display:none;margin-top:10px;\">
          <div id=\"ascf-base-paths-toolbar\" style=\"display:none;margin:0 0 6px 0;\">
            <button type=\"button\" id=\"btn-ascf-toggle-expand\" style=\"padding:6px 10px;font-size:12px;font-weight:600;border:0;border-radius:8px;cursor:pointer;color:#fff;background:#64748b;\">Expand table</button>
          </div>
          <div id=\"ascf-base-paths-scroll\" style=\"overflow-x:auto;\">
            <table style=\"width:100%;border-collapse:collapse;font-size:13px;background:#fff;border:1px solid #d0d7de;border-radius:8px;overflow:hidden;\">
              <thead><tr style=\"background:#ede9fe;\">
                <th style=\"text-align:left;padding:8px 10px;color:#4c1d95;border-bottom:1px solid #d0d7de;\">ASCF VERSION</th>
                <th style=\"text-align:left;padding:8px 10px;color:#4c1d95;border-bottom:1px solid #d0d7de;\">Base Path of the ASCF Version Build Location</th>
                <th style=\"padding:8px 10px;color:#4c1d95;border-bottom:1px solid #d0d7de;\">Actions</th>
              </tr></thead>
              <tbody id=\"ascf-base-paths-tbody\"></tbody>
            </table>
          </div>
        </div>
      </div>
      <div id=\"uxf-only-panel-paths\" style=\"display:none;margin-top:12px;padding:14px;background:linear-gradient(135deg,#f0fdf4 0%,#ecfdf5 100%);border:1px solid #86efac;border-radius:12px;\">
        <h4 style=\"margin:0 0 8px 0;font-size:15px;color:#166534;\">UXF &amp; OMNI version base paths</h4>
        <p style=\"margin:0 0 10px 0;font-size:12px;color:#64748b;\">Load one list at a time (full-width table). UXF: <code>\\\\raaisilon\\cmprod\\pbg\\cmbuild\\LS</code> or <code>\\\\raaisilon\\cmprod_dev\\pbg\\cmdev\\LS</code>. OMNI: <code>\\\\raaisilon\\cmprod\\pbg\\cmbuild\\CM_REP</code> (e.g. 9.3.1 → <code>v931</code>) or <code>\\\\raaisilon\\cmprod_dev\\pbg\\cmdev\\CM_REP</code>.</p>
        <div style=\"display:flex;flex-wrap:wrap;gap:10px;margin-bottom:8px;\">
          <button type=\"button\" id=\"btn-uxf-base-paths\" style=\"padding:10px 16px;font-weight:700;background:#059669;color:#fff;border:0;border-radius:10px;cursor:pointer;\">Load UXF base paths</button>
          <button type=\"button\" id=\"btn-omni-base-paths\" style=\"padding:10px 16px;font-weight:700;background:#2563eb;color:#fff;border:0;border-radius:10px;cursor:pointer;\">Load OMNI base paths</button>
        </div>
        <p id=\"uxf-omni-paths-view-title\" style=\"display:none;margin:0 0 8px 0;font-size:14px;font-weight:700;color:#334155;\"></p>
        <p id=\"uxf-base-paths-status\" style=\"margin:0 0 6px 0;font-size:12px;color:#64748b;display:none;\"></p>
        <div id=\"uxf-base-paths-wrap\" style=\"display:none;width:100%;margin-bottom:12px;\">
          <div id=\"uxf-base-paths-toolbar\" style=\"display:none;margin:0 0 6px 0;\">
            <button type=\"button\" id=\"btn-uxf-toggle-expand\" style=\"padding:6px 10px;font-size:12px;font-weight:600;border:0;border-radius:8px;cursor:pointer;color:#fff;background:#64748b;\">Minimize table</button>
          </div>
          <div id=\"uxf-base-paths-scroll\" class=\"uxf-omni-paths-scroll-full\" style=\"overflow-x:auto;width:100%;\">
            <table style=\"width:100%;border-collapse:collapse;font-size:13px;background:#fff;border:1px solid #d0d7de;border-radius:8px;overflow:hidden;\">
              <thead><tr style=\"background:#dcfce7;\">
                <th style=\"text-align:left;padding:8px 10px;color:#166534;border-bottom:1px solid #d0d7de;\">UXF VERSION</th>
                <th style=\"text-align:left;padding:8px 10px;color:#166534;border-bottom:1px solid #d0d7de;\">Environment</th>
                <th style=\"text-align:left;padding:8px 10px;color:#166534;border-bottom:1px solid #d0d7de;\">Base path</th>
                <th style=\"padding:8px 10px;color:#166534;border-bottom:1px solid #d0d7de;\">Actions</th>
              </tr></thead>
              <tbody id=\"uxf-base-paths-tbody\"></tbody>
            </table>
          </div>
        </div>
        <p id=\"omni-base-paths-status\" style=\"margin:0 0 6px 0;font-size:12px;color:#64748b;display:none;\"></p>
        <div id=\"omni-base-paths-wrap\" style=\"display:none;width:100%;margin-bottom:12px;\">
          <div id=\"omni-base-paths-toolbar\" style=\"display:none;margin:0 0 6px 0;\">
            <button type=\"button\" id=\"btn-omni-toggle-expand\" style=\"padding:6px 10px;font-size:12px;font-weight:600;border:0;border-radius:8px;cursor:pointer;color:#fff;background:#64748b;\">Minimize table</button>
          </div>
          <div id=\"omni-base-paths-scroll\" class=\"uxf-omni-paths-scroll-full\" style=\"overflow-x:auto;width:100%;\">
            <table style=\"width:100%;border-collapse:collapse;font-size:13px;background:#fff;border:1px solid #d0d7de;border-radius:8px;overflow:hidden;\">
              <thead><tr style=\"background:#dbeafe;\">
                <th style=\"text-align:left;padding:8px 10px;color:#1e40af;border-bottom:1px solid #d0d7de;\">OMNI VERSION</th>
                <th style=\"text-align:left;padding:8px 10px;color:#1e40af;border-bottom:1px solid #d0d7de;\">Environment</th>
                <th style=\"text-align:left;padding:8px 10px;color:#1e40af;border-bottom:1px solid #d0d7de;\">Base path</th>
                <th style=\"padding:8px 10px;color:#1e40af;border-bottom:1px solid #d0d7de;\">Actions</th>
              </tr></thead>
              <tbody id=\"omni-base-paths-tbody\"></tbody>
            </table>
          </div>
        </div>
        <p style=\"margin:0;font-size:12px;color:#64748b;\"><strong>Open in Explorer</strong> on <strong>localhost</strong>; remotely, path is copied.</p>
      </div>
      </div>

      <div style=\"padding:14px 16px;border-radius:12px;background:#f8fafc;border:1px solid #e2e8f0;margin-bottom:14px;\">
        <div style=\"font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.06em;color:#1e3a5f;margin:0 0 10px 0;\">Application</div>
      <label style=\"margin-top:0;\">Application (select one)</label>
      <div class=\"options\">
        <label><input type=\"checkbox\" name=\"app_ascf\" id=\"app_ascf\" /> ASCF</label>
        <label><input type=\"checkbox\" name=\"app_crm\" id=\"app_crm\" /> CRM/AMSS</label>
        <label><input type=\"checkbox\" name=\"app_uxf\" id=\"app_uxf\" /> UXF/OMNI</label>
        <label><input type=\"checkbox\" name=\"app_dex\" id=\"app_dex\" /> DEX</label>
        <label><input type=\"checkbox\" name=\"app_wef\" id=\"app_wef\" /> WEF</label>
      </div>
      <p style=\"margin:8px 0 0 0;font-size:12px;color:#64748b;\">ASCF, CRM/AMSS, UXF/OMNI, DEX, and WEF use different comparison engines — select one.</p>
      </div>

      <div id=\"ascf-only-panel\" style=\"display:none;padding:14px 16px;border-radius:12px;background:#faf5ff;border:1px solid #d8b4fe;margin-bottom:14px;\">
        <div style=\"font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.06em;color:#6b21a8;margin:0 0 10px 0;\">ASCF comparison mode</div>
        <div class=\"options\">
          <label><input type=\"radio\" name=\"mode\" value=\"full\" checked class=\"ascf-field\" /> Full content comparison</label>
          <label><input type=\"radio\" name=\"mode\" value=\"naming\" class=\"ascf-field\" /> Naming convention comparison</label>
          <label><input type=\"radio\" name=\"mode\" value=\"archives\" class=\"ascf-field\" /> Archives (.ear/.war/.zip/.rpm/.jar)</label>
          <label><input type=\"radio\" name=\"mode\" value=\"foss\" class=\"ascf-field\" /> FOSS/Foundation (JAR Implementation-Version)</label>
        </div>
        <p style=\"margin:8px 0 0 0;font-size:12px;color:#64748b;\">Select <strong>one</strong> ASCF comparison mode only (mutually exclusive).</p>
        <div class=\"row\" style=\"margin-top:10px;\">
          <div>
            <label>Jar name (FOSS/Foundation)</label>
            <input type=\"text\" name=\"jar_name\" class=\"ascf-field\" placeholder=\"e.g. some-lib.jar\" />
          </div>
          <div>
            <label>Foundation product</label>
            <select id=\"foss-product\" name=\"foss_product\" class=\"ascf-field\">
              <option value=\"\">Select Foundation Product</option>
              <option value=\"AmdocsCore.jar\">Amdocs Core</option>
              <option value=\"AmdocsProcMgrLiteClient.jar\">Amdocs Process Manager</option>
              <option value=\"AmdocsSecurityManager.jar\">Amdocs Security Manager</option>
              <option value=\"acfglib.jar\">Amdocs System Configuration</option>
              <option value=\"obfuscation-utility.jar\">obfuscation-utility</option>
            </select>
          </div>
          <div>
            <label>Foss product</label>
            <select id=\"foss-lib\" name=\"foss_lib\" class=\"ascf-field\">
              <option value=\"\">Select Foss Product</option>
              <option value=\"commons-fileupload.jar\">commons-fileupload</option>
              <option value=\"commons-io.jar\">commons-io</option>
              <option value=\"commons-logging.jar\">commons-logging</option>
              <option value=\"esapi.jar\">esapi</option>
              <option value=\"jxbrowser.jar\">jxbrowser</option>
              <option value=\"jxbrowser-swing.jar\">jxbrowser-swing</option>
              <option value=\"jxbrowser-win.jar\">jxbrowser-win</option>
              <option value=\"log4j-1.2-api.jar\">log4j-1.2-api</option>
              <option value=\"log4j-api.jar\">log4j-api</option>
              <option value=\"log4j-core.jar\">log4j-core</option>
              <option value=\"slf4j-api.jar\">slf4j-api</option>
              <option value=\"slf4j-nop.jar\">slf4j-nop</option>
            </select>
          </div>
        </div>
      </div>

      <div id=\"ascf-only-panel-options\" style=\"display:none;padding:14px 16px;border-radius:12px;background:linear-gradient(135deg,#faf5ff 0%,#f5f3ff 100%);border:1px solid #d8b4fe;margin-bottom:14px;\">
        <div style=\"font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.06em;color:#6b21a8;margin:0 0 10px 0;\">ASCF full — optional reports</div>
        <p style=\"margin:0 0 12px 0;font-size:12px;color:#64748b;\">Available when <strong>Full content comparison</strong> is selected. Same optional HTML report sections as CRM/AMSS.</p>
        <label>JAR names for class diff (comma-separated)</label>
        <input type=\"text\" name=\"ascf_class_diff_jar\" class=\"ascf-field\" placeholder=\"e.g. AmdocsCore.jar, lightsaber.jar\" />
        <div style=\"margin:12px 0;padding:12px 14px;background:#fff;border:1px solid #e9d5ff;border-radius:10px;\">
          <label style=\"display:block;margin-bottom:6px;\">File patterns for text / config diffs in HTML (comma-separated — section shown only when filled)</label>
          <input type=\"text\" name=\"ascf_config_files_filter\" class=\"ascf-field\" style=\"width:100%;box-sizing:border-box;\" placeholder=\"e.g. app.json, web.xml, *.yml\" />
          <p style=\"margin:8px 0 0 0;font-size:12px;color:#64748b;\">Empty = all changed .xml / .properties / .conf / .cfg in the build. With patterns, add other file types; excludes .jar/.war/.ear/.zip as the path leaf.</p>
        </div>
        <label>JAR names for manifest diff (comma-separated)</label>
        <input type=\"text\" name=\"ascf_manifest_diff_jar\" class=\"ascf-field\" placeholder=\"e.g. scala*.jar, commons-lang3*.jar\" />
        <label style=\"margin-top:12px;display:block;font-weight:600;\">Explicit file sizes to compare (comma-separated)</label>
        <input type=\"text\" name=\"ascf_explicit_size_paths\" class=\"ascf-field\" placeholder=\"e.g. AmdocsUIF.ear, *.war\" />
      </div>

      <div id=\"crm-only-panel-mode\" style=\"padding:14px 16px;border-radius:12px;background:#f8fafc;border:1px solid #e2e8f0;margin-bottom:14px;\">
        <div style=\"font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.06em;color:#1e3a5f;margin:0 0 10px 0;\">CRM/AMSS comparison mode</div>
      <input type=\"hidden\" name=\"mode\" value=\"full\" id=\"crm-mode-hidden\" class=\"crm-field\" />
      <p style=\"margin:8px 0 0 0;font-size:12px;color:#64748b;\">Mode: <strong>Full content</strong> comparison for CRM and AMSS builds.</p>
      <div class=\"options\" style=\"margin-top:8px;\">
        <label><input type=\"checkbox\" name=\"fast_crm_full\" checked class=\"crm-field\" /> Fast CRM/AMSS full compare (skip deep archive hashing)</label>
      </div>
      <div class=\"options\" style=\"margin-top:8px;\">
        <input type=\"hidden\" name=\"compare_clfy_agent_zip\" value=\"0\" class=\"crm-field\" />
        <label><input type=\"checkbox\" name=\"compare_clfy_agent_zip\" value=\"1\" class=\"crm-field\" /> Compare ClfyAgent .zip/.ear in SDK (fast size-only nested JAR/WAR/EAR)</label>
      </div>
      <p style=\"margin:10px 0 0 0;font-size:12px;color:#64748b;line-height:1.55;\">
        <strong>ClfyAgent only:</strong> enable <em>Compare ClfyAgent</em> and disable <em>Fast CRM/AMSS full compare</em>.
        <strong>Fast CRM only:</strong> enable <em>Fast CRM/AMSS full compare</em> and disable <em>Compare ClfyAgent</em>.
        Enable both to run the full fast tree compare plus ClfyAgent size-only nested archives.
      </p>
      </div>

      <div id=\"uxf-only-panel\" style=\"display:none;padding:14px 16px;border-radius:12px;background:#f0fdf4;border:1px solid #86efac;margin-bottom:14px;\">
        <div style=\"font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.06em;color:#166534;margin:0 0 10px 0;\">UXF/OMNI comparison</div>
        <p style=\"margin:0;font-size:12px;color:#64748b;\">Full folder comparison for UXF and OMNI builds.</p>
      </div>

      <div id=\"dex-only-panel\" style=\"display:none;padding:14px 16px;border-radius:12px;background:#fff7ed;border:1px solid #fdba74;margin-bottom:14px;\">
        <div style=\"font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.06em;color:#9a3412;margin:0 0 10px 0;\">DEX — Nexus artifacts</div>
        <p style=\"margin:0 0 12px 0;font-size:12px;color:#64748b;\">Enter versions for one or both products. <strong>Run comparison</strong> downloads from <code>illin3273</code>, compares artifacts, and opens an HTML report.</p>
        <p style=\"margin:0 0 8px 0;font-size:12px;font-weight:700;color:#9a3412;\">digital-playbook</p>
        <div class=\"row\">
          <div>
            <label>Previous Nexus version (digital-playbook)</label>
            <input type=\"text\" name=\"dex_nexus_previous_version\" id=\"dex_nexus_previous_version\" class=\"dex-field\" placeholder=\"e.g. 10.24.1.47\" />
          </div>
          <div>
            <label>Latest Nexus version (digital-playbook)</label>
            <input type=\"text\" name=\"dex_nexus_latest_version\" id=\"dex_nexus_latest_version\" class=\"dex-field\" placeholder=\"e.g. 10.24.1.50\" />
          </div>
        </div>
        <p style=\"margin:16px 0 8px 0;font-size:12px;font-weight:700;color:#9a3412;\">digital-webapp-ces</p>
        <div class=\"row\">
          <div>
            <label>Previous Nexus version (digital-webapp-ces)</label>
            <input type=\"text\" name=\"dex_webapp_ces_previous_version\" id=\"dex_webapp_ces_previous_version\" class=\"dex-field\" placeholder=\"e.g. 10.24.1.47\" />
          </div>
          <div>
            <label>Latest Nexus version (digital-webapp-ces)</label>
            <input type=\"text\" name=\"dex_webapp_ces_latest_version\" id=\"dex_webapp_ces_latest_version\" class=\"dex-field\" placeholder=\"e.g. 10.24.1.50\" />
          </div>
        </div>
        <div id=\"dex-nexus-url-preview\" style=\"margin-top:12px;padding:12px;background:#fff;border:1px solid #fed7aa;border-radius:10px;font-size:12px;color:#334155;display:none;\">
          <div style=\"font-weight:700;color:#9a3412;margin-bottom:8px;\">Nexus download URLs</div>
          <div id=\"dex-nexus-preview-playbook\"></div>
          <div id=\"dex-nexus-preview-webapp\" style=\"margin-top:12px;\"></div>
        </div>
      </div>

      <div id=\"wef-only-panel\" style=\"display:none;padding:14px 16px;border-radius:12px;background:#f0fdfa;border:1px solid #5eead4;margin-bottom:14px;\">
        <div style=\"font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.06em;color:#0f766e;margin:0 0 10px 0;\">WEF — remote Unix comparison</div>
        <p style=\"margin:0 0 12px 0;font-size:12px;color:#64748b;\">Connect to a Unix host over SSH, compare <strong>old</strong> and <strong>new</strong> build paths on the server, and open an HTML report of added, removed, and modified files.</p>
        <div class=\"row\">
          <div>
            <label>SSH host</label>
            <input type=\"text\" name=\"wef_ssh_host\" id=\"wef_ssh_host\" class=\"wef-field\" placeholder=\"hostname or IP\" />
          </div>
          <div>
            <label>SSH port</label>
            <input type=\"text\" name=\"wef_ssh_port\" id=\"wef_ssh_port\" class=\"wef-field\" value=\"22\" placeholder=\"22\" />
          </div>
        </div>
        <div class=\"row\">
          <div>
            <label>SSH username</label>
            <input type=\"text\" name=\"wef_ssh_username\" id=\"wef_ssh_username\" class=\"wef-field\" placeholder=\"Unix user\" autocomplete=\"username\" />
          </div>
          <div>
            <label>SSH password</label>
            <input type=\"password\" name=\"wef_ssh_password\" id=\"wef_ssh_password\" class=\"wef-field\" placeholder=\"password\" autocomplete=\"current-password\" />
          </div>
        </div>
        <div class=\"row\">
          <div>
            <label>Old build path (on Unix)</label>
            <input type=\"text\" name=\"wef_old_path\" id=\"wef_old_path\" class=\"wef-field\" placeholder=\"/path/to/old/build\" />
          </div>
          <div>
            <label>New build path (on Unix)</label>
            <input type=\"text\" name=\"wef_new_path\" id=\"wef_new_path\" class=\"wef-field\" placeholder=\"/path/to/new/build\" />
          </div>
        </div>
        <p style=\"margin:8px 0 0 0;font-size:12px;color:#64748b;\">NAS Old/New build fields above are not used for WEF. Leave them empty or ignore them.</p>
      </div>

      <div id=\"crm-only-panel-options\" style=\"padding:14px 16px;border-radius:12px;background:linear-gradient(135deg,#eff6ff 0%,#ecfeff 100%);border:1px solid #bfdbfe;margin-bottom:14px;\">
        <div style=\"font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.06em;color:#1e40af;margin:0 0 10px 0;\">CRM/AMSS full — optional reports</div>
          <div style=\"margin-bottom:14px;padding:12px 14px;background:#fff;border:1px solid #93c5fd;border-radius:10px;\">
            <p style=\"margin:0 0 10px 0;font-size:12px;font-weight:700;color:#1e40af;\">Nexus verification — crm-playbooks TAR (YAML under amdocs-version)</p>
            <p style=\"margin:0 0 10px 0;font-size:11px;color:#64748b;\">Enter both versions and leave Old/New build paths empty to run <strong>only</strong> this Nexus check. Downloads from <code>indlin2410</code> and compares <code>.yaml</code> files inside <code>amdocs-version/</code>.</p>
            <div class=\"row\">
              <div>
                <label>Previous Nexus version (crm-playbooks tar)</label>
                <input type=\"text\" name=\"crm_playbooks_nexus_previous\" id=\"crm_playbooks_nexus_previous\" class=\"crm-field\" placeholder=\"e.g. 10.24.1.6-4368\" />
              </div>
              <div>
                <label>Latest Nexus version (crm-playbooks tar)</label>
                <input type=\"text\" name=\"crm_playbooks_nexus_latest\" id=\"crm_playbooks_nexus_latest\" class=\"crm-field\" placeholder=\"e.g. 10.24.1.7-4410\" />
              </div>
            </div>
            <div id=\"crm-playbooks-nexus-url-preview\" style=\"margin-top:10px;padding:10px;background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;font-size:11px;display:none;\">
              <div style=\"font-weight:700;color:#1e40af;margin-bottom:6px;\">Nexus download URLs</div>
              <div id=\"crm-playbooks-nexus-preview-body\"></div>
            </div>
          </div>
          <div style=\"margin-bottom:12px;padding:12px 14px;background:#fff;border:1px solid #bae6fd;border-radius:10px;\">
            <label style=\"display:block;margin-bottom:6px;\">Find JAR for class (remote SSH grep on deployment)</label>
            <div style=\"display:flex;flex-wrap:wrap;gap:8px;align-items:center;\">
              <input type=\"text\" id=\"crm_class_lookup\" class=\"crm-field\" style=\"flex:1;min-width:220px;box-sizing:border-box;\" placeholder=\"e.g. com.clarify.billingmanager.igpublic.savebean.CreateCustomerAccountXB\" />
              <button type=\"button\" id=\"btn-crm-find-jar-for-class\" style=\"padding:10px 14px;font-weight:700;background:#2563eb;color:#fff;border:0;border-radius:8px;cursor:pointer;\">Find JAR</button>
              <button type=\"button\" id=\"btn-crm-use-class-jar-suggestion\" style=\"padding:10px 14px;font-weight:600;background:#e0f2fe;color:#1e40af;border:1px solid #93c5fd;border-radius:8px;cursor:pointer;display:none;\">Use in class diff</button>
            </div>
            <p id=\"crm-class-lookup-status\" style=\"margin:8px 0 0 0;font-size:12px;color:#64748b;display:none;\"></p>
            <p style=\"margin:6px 0 0 0;font-size:11px;color:#64748b;\">Uses SSH credentials from <strong>Environment Validation</strong> below (host, user, password, remote path). Runs <code>find . -name '*.jar' -exec grep -Hsli &lt;class&gt; {{}} \\;</code> on the server and lists matching JAR names to copy.</p>
          </div>
          <label>JAR names for class diff (comma-separated)</label>
          <input type=\"text\" name=\"crm_class_diff_jar\" id=\"crm_class_diff_jar\" class=\"crm-field\" placeholder=\"e.g. ClfyBillingManager.jar, ClfyCore.jar\" />
          <div style=\"margin:12px 0;padding:12px 14px;background:#fff;border:1px solid #bae6fd;border-radius:10px;\">
            <label style=\"display:block;margin-bottom:6px;\">File patterns for text / config diffs in HTML (comma-separated — section shown only when filled)</label>
            <input type=\"text\" name=\"crm_config_files_filter\" class=\"crm-field\" style=\"width:100%;box-sizing:border-box;\" placeholder=\"e.g. app.json, web.xml, *.yml\" />
            <p style=\"margin:8px 0 0 0;font-size:12px;color:#64748b;\">Empty = all changed .xml / .properties / .conf / .cfg in the build. With patterns, add other file types; excludes .jar/.war/.ear/.zip as the path leaf.</p>
          </div>
          <label>JAR names for manifest diff (comma-separated)</label>
          <input type=\"text\" name=\"crm_manifest_diff_jar\" class=\"crm-field\" placeholder=\"e.g. scala*.jar, commons-lang3*.jar\" />
      <label style=\"margin-top:12px;display:block;font-weight:600;\">Explicit file sizes to compare (comma-separated)</label>
      <input type=\"text\" name=\"crm_explicit_size_paths\" class=\"crm-field\" placeholder=\"e.g. ClfyAgent.ear, AmdocsCRM.ear, *.war\" />
      </div>


      <div class=\"actions\">
        <button type=\"submit\">Run comparison</button>
      </div>
      </div>
    </form>

      <div id=\"environment-path-panel\" style=\"display:none;margin-top:12px;padding:12px 14px;background:#fff;border:1px solid #cbd5e1;border-radius:10px;\">
        <div style=\"font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.06em;color:#1e3a5f;margin:0 0 8px 0;\">Remote environment (SSH)</div>
        <p style=\"margin:0 0 10px 0;font-size:12px;color:#64748b;\">Connect via SSH, search the remote deployment tree for JAR name(s), and read <code>Implementation-Version</code> from each match (same as <code>find_jar_version.sh</code>). Comma-separated names or wildcards (e.g. <code>AmdocsSecurityManager.jar</code>, <code>commons-io*.jar</code>).</p>
        <div style=\"display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:10px;margin-bottom:10px;\">
          <div>
            <label style=\"display:block;margin-bottom:4px;font-size:13px;\">Host / IP</label>
            <input type=\"text\" id=\"env_ssh_host\" placeholder=\"10.76.48.68\" style=\"width:100%;padding:10px;border:1px solid #cbd5e1;border-radius:8px;\" />
          </div>
          <div>
            <label style=\"display:block;margin-bottom:4px;font-size:13px;\">Username</label>
            <input type=\"text\" id=\"env_ssh_username\" placeholder=\"pcicrm1\" style=\"width:100%;padding:10px;border:1px solid #cbd5e1;border-radius:8px;\" />
          </div>
          <div>
            <label style=\"display:block;margin-bottom:4px;font-size:13px;\">Password</label>
            <input type=\"password\" id=\"env_ssh_password\" placeholder=\"SSH password\" autocomplete=\"off\" style=\"width:100%;padding:10px;border:1px solid #cbd5e1;border-radius:8px;\" />
          </div>
          <div>
            <label style=\"display:block;margin-bottom:4px;font-size:13px;\">Port</label>
            <input type=\"number\" id=\"env_ssh_port\" value=\"22\" min=\"1\" max=\"65535\" style=\"width:100%;padding:10px;border:1px solid #cbd5e1;border-radius:8px;\" />
          </div>
        </div>
        <div style=\"margin-bottom:10px;\">
          <label style=\"display:block;margin-bottom:4px;font-size:13px;\">Remote environment path</label>
          <input type=\"text\" id=\"env_ssh_remote_path\" placeholder=\"/opt/app/deploy or server deployment root\" style=\"width:100%;padding:10px;border:1px solid #cbd5e1;border-radius:8px;\" />
        </div>
        <div style=\"margin-bottom:10px;\">
          <label style=\"display:block;margin-bottom:4px;font-size:13px;\">JAR name(s) to search</label>
          <input type=\"text\" id=\"env_ssh_jar_names\" placeholder=\"e.g. AmdocsSecurityManager.jar or scala*.jar, commons-io.jar\" style=\"width:100%;padding:10px;border:1px solid #cbd5e1;border-radius:8px;\" />
          <p style=\"margin:4px 0 0 0;font-size:11px;color:#64748b;\">Lists every matching JAR under the remote path with manifest <code>Implementation-Version</code>.</p>
        </div>
        <div>
          <button type=\"button\" id=\"btn-validate-env-jars\" style=\"padding:10px 16px;font-weight:700;background:#7c3aed;color:#fff;border:0;border-radius:10px;cursor:pointer;\">Search environment &amp; validate JAR manifest</button>
        </div>
        <p id=\"env-jars-status\" style=\"margin:8px 0 0 0;font-size:12px;color:#64748b;display:none;\"></p>
      </div>
    </div>

    <div class=\"card\">
      <p style=\"margin:0; display:flex; gap:10px; flex-wrap:wrap;\">
        <a class=\"button\" id=\"browse-reports-link\" href=\"/reports\" target=\"_blank\" rel=\"noopener noreferrer\">Browse comparison reports</a>
        <a class=\"button\" id=\"foundations-btn\" href=\"/foundations\" style=\"display:none;background:#059669;\">Foundations Link</a>
        <a class=\"button\" id=\"admin-tool-btn\" href=\"/admin-tool\" style=\"display:none;background:#7c3aed;\">Administrative Tool</a>
      </p>
    </div>

    <div class=\"card\" id=\"ascf-copy-panel\" style=\"display:none;\">
      <div style=\"display:flex; justify-content:space-between; align-items:center; margin-bottom:10px;\">
        <h3 style=\"margin:0;\">Copy build folders</h3>
        <button type=\"button\" onclick=\"showCopyLogs()\" style=\"background:#6366f1; color:white; border:none; padding:6px 12px; border-radius:6px; cursor:pointer; font-size:12px;\">Copy logs</button>
      </div>
      <form id=\"copy-form\" method=\"post\" action=\"/copy\">
        <div class=\"options\">
          <label><input type=\"radio\" name=\"copy_source\" value=\"folder1\" checked /> Copy from Old build</label>
          <label><input type=\"radio\" name=\"copy_source\" value=\"folder2\" /> Copy from New build</label>
          <label>Destination path <input type=\"text\" name=\"dest_path\" placeholder=\"Enter destination path\" /></label>
          <label><input type=\"checkbox\" name=\"overwrite\" /> Overwrite if exists</label>
        </div>
        <input type=\"hidden\" name=\"folder1_path\" id=\"copy-folder1\" />
        <input type=\"hidden\" name=\"folder2_path\" id=\"copy-folder2\" />
        <div class=\"actions\"><button type=\"button\" id=\"copy-btn\" onclick=\"handleCopyClick()\">Copy</button></div>
      </form>
      <div id=\"copy-progress-container\" style=\"display:none; margin:20px 0; padding:16px; background:#f0fdf4; border-radius:10px; border:1px solid #86efac;\">
        <h3 style=\"margin:0 0 10px 0; color:#166534;\">Copying in progress...</h3>
        <div style=\"background:#dcfce7; border-radius:10px; height:30px; position:relative; overflow:hidden;\">
          <div id=\"copy-progress-bar\" style=\"background:linear-gradient(90deg, #22c55e 0%, #16a34a 100%); height:100%; width:0%; transition:width 0.3s; display:flex; align-items:center; justify-content:center; color:white; font-weight:bold; font-size:14px;\">
            <span id=\"copy-progress-text\">0%</span>
          </div>
        </div>
        <div style=\"margin-top:10px; font-size:14px; color:#166534;\">
          <div><strong>Status:</strong> <span id=\"copy-progress-status\">Starting...</span></div>
          <div><strong>Files copied:</strong> <span id=\"copy-progress-files\">0 / 0</span></div>
          <div><strong>Data copied:</strong> <span id=\"copy-progress-bytes\">0 B / 0 B</span></div>
          <div><strong>Current file:</strong> <span id=\"copy-progress-current\" style=\"font-family:monospace; font-size:12px; word-break:break-all; display:block; margin-top:4px;\">-</span></div>
          <div><strong>Estimated time remaining:</strong> <span id=\"copy-progress-eta\">Calculating...</span></div>
        </div>
        <div id=\"copy-progress-warning\" style=\"display:none; margin-top:10px; padding:10px; background:#fef3c7; border:1px solid #fcd34d; border-radius:6px; color:#92400e;\">
          <strong>Warning:</strong> <span id=\"copy-progress-skipped-count\">0</span> file(s) skipped.
          <div id=\"copy-progress-skipped-list\" style=\"margin-top:8px; font-size:12px; font-family:monospace; max-height:150px; overflow-y:auto;\"></div>
        </div>
        <div id=\"copy-progress-error\" style=\"display:none; margin-top:10px; padding:10px; background:#fee; border:1px solid #fca5a5; border-radius:6px; color:#dc2626;\">
          <strong>Error:</strong> <span id=\"copy-progress-error-msg\"></span>
        </div>
        <div id=\"copy-progress-success\" style=\"display:none; margin-top:10px; padding:10px; background:#dcfce7; border:1px solid #86efac; border-radius:6px; color:#166534;\">
          <strong>Success:</strong> <span id=\"copy-progress-success-msg\"></span>
        </div>
      </div>
    </div>

    <div id=\"startup-notice-modal\" style=\"display:none;position:fixed;inset:0;z-index:9999;background:rgba(15,23,42,0.55);align-items:center;justify-content:center;padding:16px;\">
      <div style=\"max-width:440px;width:100%;background:#fff;border-radius:14px;padding:22px 22px 18px;box-shadow:0 20px 50px rgba(15,23,42,0.35);border:1px solid #cbd5e1;\">
        <h3 style=\"margin:0 0 10px 0;font-size:17px;color:#0f172a;\">Before you compare</h3>
        <p id=\"startup-notice-text\" style=\"margin:0 0 16px 0;font-size:14px;line-height:1.55;color:#475569;\"></p>
        <button type=\"button\" id=\"startup-notice-ok\" style=\"width:100%;margin:0;\">Got it — start comparing</button>
      </div>
    </div>

    <script>
    {_SEC_BOOT}
    </script>
    <script>
      var startupNoticeShown = false;
      function showStartupNotice(d) {{
        if (startupNoticeShown || !d) return;
        startupNoticeShown = true;
        var modal = document.getElementById('startup-notice-modal');
        var text = document.getElementById('startup-notice-text');
        if (!modal || !text) return;
        var machine = (d.client_machine || d.identity || 'your PC').toUpperCase();
        var req = d.minimum_required_gb || 50;
        var freeGb = d.client_free_gb != null ? d.client_free_gb : null;
        var freeHuman = d.client_free_human || '';
        var spaceLine = d.client_c_accessible && freeGb != null
          ? 'Need <strong>' + req + ' GB</strong> free on <strong>' + machine + ' C:</strong> — you have <strong>' + freeGb + ' GB</strong> free.'
          : 'Keep <strong>' + req + ' GB</strong> free on <strong>' + machine + ' C:</strong> (could not read free space from server).';
        text.innerHTML = 'Packages copy to your PC (<code>\\\\' + machine + '\\c$\\crm_compare_temp</code>). '
          + spaceLine + ' '
          + 'Remote <code>\\\\inpnqbdnc04\\software</code> is used only if your C: drive is full. '
          + 'Enable <strong>Build Comparison</strong>, pick an application, and click <strong>Run comparison</strong>.';
        modal.style.display = 'flex';
      }}
      (function() {{
        var modal = document.getElementById('startup-notice-modal');
        var okBtn = document.getElementById('startup-notice-ok');
        function closeNotice() {{ if (modal) modal.style.display = 'none'; }}
        if (okBtn) okBtn.addEventListener('click', closeNotice);
        if (modal) modal.addEventListener('click', function(e) {{ if (e.target === modal) closeNotice(); }});
      }})();

      // Persist comparison paths locally so they survive navigation/reload.
      function refreshClientSpaceHint() {{
        var f1 = document.querySelector('input[name="folder1_path"]');
        var f2 = document.querySelector('input[name="folder2_path"]');
        var q = '';
        if (f1 && f1.value) q += 'folder1_path=' + encodeURIComponent(f1.value) + '&';
        if (f2 && f2.value) q += 'folder2_path=' + encodeURIComponent(f2.value) + '&';
        fetch('/api/comparison-space-check?quick=1&' + q)
          .then(function(r) {{ return r.json(); }})
          .then(function(d) {{
            var ident = (d && d.identity) ? d.identity : '';
            var inp = document.getElementById('report_user');
            var hint = document.getElementById('report-user-hint');
            var lbl = document.getElementById('report-user-label');
            if (inp && ident) inp.value = ident;
            if (hint && lbl && ident) {{
              lbl.textContent = ident;
              hint.style.display = 'block';
            }}
            var browse = document.getElementById('browse-reports-link');
            if (browse && ident) {{
              browse.href = '/reports?report_user=' + encodeURIComponent(ident);
            }}
            showStartupNotice(d);
          }})
          .catch(function() {{}});
      }}
      refreshClientSpaceHint();

      (function() {{
        var f1 = document.querySelector('input[name="folder1_path"]');
        var f2 = document.querySelector('input[name="folder2_path"]');
        if (!f1 || !f2) return;
        var k1 = 'pc_last_folder1_path';
        var k2 = 'pc_last_folder2_path';
        try {{
          var s1 = localStorage.getItem(k1);
          var s2 = localStorage.getItem(k2);
          if (s1 && !f1.value) f1.value = s1;
          if (s2 && !f2.value) f2.value = s2;
        }} catch (e) {{}}
        function persist() {{
          try {{
            localStorage.setItem(k1, f1.value || '');
            localStorage.setItem(k2, f2.value || '');
          }} catch (e) {{}}
        }}
        f1.addEventListener('input', persist);
        f2.addEventListener('input', persist);
        var runForm = document.querySelector('form[action="/run"]');
        if (runForm) runForm.addEventListener('submit', persist);
      }})();

      (function() {{
        var toggle = document.getElementById('build_compare_toggle');
        var panel = document.getElementById('build-compare-panel');
        if (!toggle || !panel) return;
        var kToggle = 'pc_build_compare_toggle';
        function clearBuildCompareSelections() {{
          ['app_ascf', 'app_crm', 'app_uxf', 'app_dex', 'app_wef'].forEach(function(id) {{
            var el = document.getElementById(id);
            if (el) el.checked = false;
          }});
          panel.querySelectorAll('input, select, textarea').forEach(function(el) {{
            if (el.name === 'folder1_path' || el.name === 'folder2_path') return;
            if (el.id && /^app_/.test(el.id)) return;
            if (el.tagName === 'SELECT') {{
              el.selectedIndex = 0;
            }} else if (el.type === 'checkbox' || el.type === 'radio') {{
              el.checked = el.defaultChecked;
            }} else if (el.type === 'hidden') {{
              el.value = el.defaultValue;
            }} else {{
              el.value = el.defaultValue || '';
            }}
          }});
          try {{ window.dispatchEvent(new CustomEvent('buildCompareCleared')); }} catch (e) {{}}
        }}
        function syncBuildPanel() {{
          panel.style.display = toggle.checked ? 'block' : 'none';
        }}
        toggle.checked = false;
        try {{ localStorage.removeItem(kToggle); }} catch (e) {{}}
        syncBuildPanel();
        toggle.addEventListener('change', function() {{
          if (!toggle.checked) clearBuildCompareSelections();
          syncBuildPanel();
        }});
      }})();

      (function() {{
        var toggle = document.getElementById('env_path_toggle');
        var panel = document.getElementById('environment-path-panel');
        if (!toggle || !panel) return;
        var kToggle = 'pc_env_path_toggle';
        function syncEnvPanel() {{
          panel.style.display = toggle.checked ? 'block' : 'none';
        }}
        toggle.checked = false;
        try {{ localStorage.removeItem(kToggle); }} catch (e) {{}}
        syncEnvPanel();
        toggle.addEventListener('change', function() {{
          toggle.disabled = false;
          syncEnvPanel();
          if (toggle.checked && panel) {{
            try {{ panel.scrollIntoView({{ behavior: 'smooth', block: 'start' }}); }} catch (e) {{}}
          }}
        }});
      }})();

      (function() {{
        var buildToggle = document.getElementById('build_compare_toggle');
        var envToggle = document.getElementById('env_path_toggle');
        if (!buildToggle || !envToggle) return;
        function keepModeTogglesEnabled() {{
          buildToggle.disabled = false;
          envToggle.disabled = false;
        }}
        keepModeTogglesEnabled();
        buildToggle.addEventListener('change', keepModeTogglesEnabled);
        envToggle.addEventListener('change', keepModeTogglesEnabled);
        setInterval(keepModeTogglesEnabled, 2000);
      }})();

      (function() {{
        var classLookup = document.getElementById('crm_class_lookup');
        var classLookupBtn = document.getElementById('btn-crm-find-jar-for-class');
        var classUseBtn = document.getElementById('btn-crm-use-class-jar-suggestion');
        var classStatus = document.getElementById('crm-class-lookup-status');
        var classDiffJar = document.getElementById('crm_class_diff_jar');
        var lastClassJarSuggestion = '';
        function setClassLookupStatus(msg, isErr) {{
          if (!classStatus) return;
          classStatus.textContent = msg || '';
          classStatus.style.display = msg ? 'block' : 'none';
          classStatus.style.color = isErr ? '#b91c1c' : '#0f766e';
        }}
        if (classLookupBtn) {{
          classLookupBtn.addEventListener('click', function() {{
            var cls = (classLookup && classLookup.value || '').trim();
            var sshHostEl = document.getElementById('env_ssh_host');
            var sshUserEl = document.getElementById('env_ssh_username');
            var sshPassEl = document.getElementById('env_ssh_password');
            var sshPortEl = document.getElementById('env_ssh_port');
            var sshPathEl = document.getElementById('env_ssh_remote_path');
            var sshHost = (sshHostEl && sshHostEl.value || '').trim();
            var sshUser = (sshUserEl && sshUserEl.value || '').trim();
            var sshPass = sshPassEl ? sshPassEl.value : '';
            var sshPort = parseInt((sshPortEl && sshPortEl.value) || '22', 10) || 22;
            var sshPath = (sshPathEl && sshPathEl.value || '').trim();
            if (!sshHost) {{
              setClassLookupStatus('Enter SSH host / IP in Environment Validation (e.g. 10.76.48.68).', true);
              return;
            }}
            if (!sshUser) {{
              setClassLookupStatus('Enter SSH username in Environment Validation (e.g. pcicrm1).', true);
              return;
            }}
            if (!sshPass) {{
              setClassLookupStatus('Enter SSH password in Environment Validation.', true);
              return;
            }}
            if (!sshPath) {{
              setClassLookupStatus('Enter remote deployment path in Environment Validation.', true);
              return;
            }}
            if (!cls) {{
              setClassLookupStatus('Enter a class name or FQCN (e.g. CreateCustomerAccountXB.class).', true);
              return;
            }}
            lastClassJarSuggestion = '';
            if (classUseBtn) classUseBtn.style.display = 'none';
            setClassLookupStatus('SSH to ' + sshHost + ' — searching JARs for ' + cls + '…', false);
            classLookupBtn.disabled = true;
            var payload = {{
              class_name: cls,
              host: sshHost,
              username: sshUser,
              password: sshPass,
              port: sshPort,
              path: sshPath
            }};
            function parseFindJarResponse(r) {{
              return r.text().then(function(t) {{
                var j = {{}};
                try {{ j = t ? JSON.parse(t) : {{}}; }} catch (pe) {{
                  throw new Error((r.status ? r.status + ': ' : '') + (t || r.statusText || 'Invalid server response'));
                }}
                return {{ ok: r.ok, j: j }};
              }});
            }}
            function findJarRequest() {{
              return fetch('/api/crm-find-jar-for-class', {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify(payload)
              }}).then(parseFindJarResponse);
            }}
            findJarRequest()
              .catch(function(err) {{
                var msg = err && err.message ? String(err.message) : String(err || '');
                if (msg.indexOf('Failed to fetch') < 0 && msg.indexOf('NetworkError') < 0 && !(err instanceof TypeError)) {{
                  throw err;
                }}
                var q = 'class_name=' + encodeURIComponent(cls)
                  + '&host=' + encodeURIComponent(sshHost)
                  + '&username=' + encodeURIComponent(sshUser)
                  + '&password=' + encodeURIComponent(sshPass)
                  + '&port=' + encodeURIComponent(String(sshPort))
                  + '&path=' + encodeURIComponent(sshPath);
                try {{
                  var tok = sessionStorage.getItem('crmAccessToken') || '';
                  if (tok) q += '&access_token=' + encodeURIComponent(tok);
                }} catch (e2) {{}}
                return fetch('/api/crm-find-jar-for-class?' + q, {{ method: 'GET' }}).then(parseFindJarResponse);
              }})
              .then(function(res) {{
                var j = res.j || {{}};
                if (!j.ok) {{
                  setClassLookupStatus(j.error || 'Class lookup failed.', true);
                  return;
                }}
                lastClassJarSuggestion = j.suggestions || '';
                var lines = (j.jars || []).map(function(row) {{
                  return row.jar + (row.container ? ' — ' + row.container : '');
                }});
                var hostNote = j.remote_host ? ' on ' + j.remote_host : '';
                setClassLookupStatus(
                  'Found' + hostNote + ': ' + (lines.length ? lines.join('; ') : j.suggestions),
                  false
                );
                if (classUseBtn && lastClassJarSuggestion) classUseBtn.style.display = 'inline-block';
              }})
              .catch(function(e) {{
                setClassLookupStatus('Lookup error: ' + (e && e.message ? e.message : e), true);
              }})
              .finally(function() {{ classLookupBtn.disabled = false; }});
          }});
        }}
        if (classUseBtn) {{
          classUseBtn.addEventListener('click', function() {{
            if (!lastClassJarSuggestion || !classDiffJar) return;
            var cur = (classDiffJar.value || '').trim();
            var parts = cur ? cur.split(',').map(function(s) {{ return s.trim(); }}).filter(Boolean) : [];
            lastClassJarSuggestion.split(',').forEach(function(jn) {{
              jn = (jn || '').trim();
              if (!jn) return;
              if (parts.map(function(p) {{ return p.toLowerCase(); }}).indexOf(jn.toLowerCase()) < 0) {{
                parts.push(jn);
              }}
            }});
            classDiffJar.value = parts.join(', ');
            setClassLookupStatus('Added to class diff JAR list: ' + classDiffJar.value, false);
          }});
        }}
        if (classLookup) {{
          classLookup.addEventListener('keydown', function(ev) {{
            if (ev.key === 'Enter') {{
              ev.preventDefault();
              if (classLookupBtn) classLookupBtn.click();
            }}
          }});
        }}
      }})();

      (function() {{
        var sshHost = document.getElementById('env_ssh_host');
        var sshUser = document.getElementById('env_ssh_username');
        var sshPass = document.getElementById('env_ssh_password');
        var sshPort = document.getElementById('env_ssh_port');
        var sshPath = document.getElementById('env_ssh_remote_path');
        var envJarNames = document.getElementById('env_ssh_jar_names');
        var btn = document.getElementById('btn-validate-env-jars');
        var statusEl = document.getElementById('env-jars-status');
        if (!btn) return;
        var kHost = 'pc_env_ssh_host';
        var kUser = 'pc_env_ssh_username';
        var kPort = 'pc_env_ssh_port';
        var kRemote = 'pc_env_ssh_remote_path';
        var kJarNames = 'pc_env_ssh_jar_names';
        function loadSaved() {{
          try {{
            if (sshHost && !sshHost.value) sshHost.value = localStorage.getItem(kHost) || '10.76.48.68';
            if (sshUser && !sshUser.value) sshUser.value = localStorage.getItem(kUser) || 'pcicrm1';
            if (sshPort && !sshPort.value) sshPort.value = localStorage.getItem(kPort) || '22';
            if (sshPath && !sshPath.value) sshPath.value = localStorage.getItem(kRemote) || '';
            if (envJarNames && !envJarNames.value) envJarNames.value = localStorage.getItem(kJarNames) || '';
          }} catch (e) {{}}
        }}
        function persistNonSecret() {{
          try {{
            if (sshHost) localStorage.setItem(kHost, sshHost.value || '');
            if (sshUser) localStorage.setItem(kUser, sshUser.value || '');
            if (sshPort) localStorage.setItem(kPort, sshPort.value || '22');
            if (sshPath) localStorage.setItem(kRemote, sshPath.value || '');
            if (envJarNames) localStorage.setItem(kJarNames, envJarNames.value || '');
          }} catch (e) {{}}
        }}
        loadSaved();
        [sshHost, sshUser, sshPort, sshPath, envJarNames].forEach(function(el) {{
          if (el) el.addEventListener('input', persistNonSecret);
        }});
        function setStatus(msg, isErr) {{
          if (!statusEl) return;
          statusEl.textContent = msg || '';
          statusEl.style.display = msg ? 'block' : 'none';
          statusEl.style.color = isErr ? '#b91c1c' : '#64748b';
        }}
        btn.addEventListener('click', function() {{
          var jarNames = (envJarNames && envJarNames.value || '').trim();
          var payload = {{
            mode: 'ssh',
            host: (sshHost && sshHost.value || '').trim(),
            username: (sshUser && sshUser.value || '').trim(),
            password: sshPass ? sshPass.value : '',
            port: parseInt((sshPort && sshPort.value) || '22', 10) || 22,
            path: (sshPath && sshPath.value || '').trim(),
            env_ssh_jar_names: jarNames,
            jar_names: jarNames
          }};
          if (!payload.host) {{ setStatus('Enter host / IP address.', true); return; }}
          if (!payload.username) {{ setStatus('Enter SSH username.', true); return; }}
          if (!payload.password) {{ setStatus('Enter SSH password.', true); return; }}
          if (!payload.path) {{ setStatus('Enter remote environment path.', true); return; }}
          if (!jarNames) {{
            setStatus('Enter JAR name(s) to search (e.g. AmdocsSecurityManager.jar).', true);
            return;
          }}
          setStatus('Connecting via SSH and searching environment for JAR manifest versions…', false);
          persistNonSecret();
          btn.disabled = true;
          fetch('/api/validate-environment-jars', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify(payload),
          }}).then(function(r) {{
            return r.text().then(function(text) {{
              var data = {{}};
              try {{ data = text ? JSON.parse(text) : {{}}; }} catch (e) {{}}
              return {{ ok: r.ok, data: data }};
            }});
          }}).then(function(res) {{
            btn.disabled = false;
            if (!res.ok || !res.data.ok) {{
              setStatus((res.data && res.data.error) || 'Validation failed.', true);
              return;
            }}
            var rows = res.data.rows || [];
            var summary = res.data.summary || {{}};
            var reportUrl = res.data.report_url || '';
            if (reportUrl) {{
              var opened = window.open(reportUrl, '_blank', 'noopener,noreferrer');
              if (opened) {{
                setStatus('HTML report opened in a new tab (' + (summary.total || rows.length) + ' result row(s)).', false);
              }} else {{
                setStatus('Report ready — allow pop-ups, then open: ' + reportUrl, false);
              }}
            }} else {{
              setStatus('Search completed (' + (summary.total || rows.length) + ' result row(s)).', false);
            }}
          }}).catch(function() {{
            btn.disabled = false;
            setStatus('Network error — could not validate environment.', true);
          }});
        }});
      }})();

      (function() {{
        var appAscf = document.getElementById('app_ascf');
        var appCrm = document.getElementById('app_crm');
        var appUxf = document.getElementById('app_uxf');
        var appDex = document.getElementById('app_dex');
        var appWef = document.getElementById('app_wef');
        var nasPaths = document.getElementById('nas-build-paths-section');
        var panels = {{
          crmPaths: document.getElementById('crm-only-panel'),
          ascfPaths: document.getElementById('ascf-only-panel-paths'),
          uxfPaths: document.getElementById('uxf-only-panel-paths'),
          crmMode: document.getElementById('crm-only-panel-mode'),
          crmOpts: document.getElementById('crm-only-panel-options'),
          ascf: document.getElementById('ascf-only-panel'),
          ascfOpts: document.getElementById('ascf-only-panel-options'),
          ascfCopy: document.getElementById('ascf-copy-panel'),
          uxf: document.getElementById('uxf-only-panel'),
          dex: document.getElementById('dex-only-panel'),
          wef: document.getElementById('wef-only-panel')
        }};
        var foundationsBtn = document.getElementById('foundations-btn');
        var adminToolBtn = document.getElementById('admin-tool-btn');
        function setPanelDisabled(root, off) {{
          if (!root) return;
          root.querySelectorAll('input, select, textarea, button').forEach(function(el) {{
            el.disabled = off;
          }});
        }}
        function exclusiveApp(changed) {{
          if (!changed || !changed.checked) return;
          [appAscf, appCrm, appUxf, appDex, appWef].forEach(function(b) {{
            if (b && b !== changed) b.checked = false;
          }});
        }}
        function refreshAscfFullOptions() {{
          var ascf = appAscf && appAscf.checked;
          var fullMode = compareForm ? compareForm.querySelector('input[name="mode"][value="full"]') : null;
          var showOpts = ascf && fullMode && fullMode.checked;
          if (panels.ascfOpts) panels.ascfOpts.style.display = showOpts ? '' : 'none';
          setPanelDisabled(panels.ascfOpts, !showOpts);
        }}
        function refreshAppUi() {{
          var ascf = appAscf && appAscf.checked;
          var crm = appCrm && appCrm.checked;
          var uxf = appUxf && appUxf.checked;
          var dex = appDex && appDex.checked;
          var wef = appWef && appWef.checked;
          if (panels.ascf) panels.ascf.style.display = ascf ? '' : 'none';
          if (panels.ascfCopy) panels.ascfCopy.style.display = ascf ? '' : 'none';
          if (panels.ascfPaths) panels.ascfPaths.style.display = ascf ? '' : 'none';
          if (panels.uxfPaths) panels.uxfPaths.style.display = uxf ? '' : 'none';
          if (panels.crmPaths) panels.crmPaths.style.display = crm ? '' : 'none';
          if (panels.crmMode) panels.crmMode.style.display = crm ? '' : 'none';
          if (panels.crmOpts) panels.crmOpts.style.display = crm ? '' : 'none';
          if (panels.uxf) panels.uxf.style.display = uxf ? '' : 'none';
          if (panels.dex) panels.dex.style.display = dex ? '' : 'none';
          if (panels.wef) panels.wef.style.display = wef ? '' : 'none';
          if (nasPaths) nasPaths.style.display = wef ? 'none' : '';
          setPanelDisabled(panels.ascf, !ascf);
          setPanelDisabled(panels.ascfPaths, !ascf);
          setPanelDisabled(panels.uxfPaths, !uxf);
          setPanelDisabled(panels.crmMode, !crm);
          setPanelDisabled(panels.crmOpts, !crm);
          setPanelDisabled(panels.uxf, !uxf);
          setPanelDisabled(panels.dex, !dex);
          setPanelDisabled(panels.wef, !wef);
          refreshAscfFullOptions();
          if (foundationsBtn) foundationsBtn.style.display = ascf ? '' : 'none';
          if (adminToolBtn) adminToolBtn.style.display = ascf ? '' : 'none';
        }}
        [appAscf, appCrm, appUxf, appDex, appWef].forEach(function(cb) {{
          if (!cb) return;
          cb.addEventListener('change', function() {{ exclusiveApp(cb); refreshAppUi(); }});
        }});
        if (compareForm) {{
          compareForm.querySelectorAll('input[name="mode"].ascf-field').forEach(function(r) {{
            r.addEventListener('change', refreshAscfFullOptions);
          }});
        }}
        refreshAppUi();
        window.addEventListener('buildCompareCleared', function() {{
          refreshAppUi();
          refreshDexNexusPreview();
        }});
        var compareForm = document.querySelector('form[action="/run"]');
        if (compareForm) compareForm.addEventListener('submit', function() {{ refreshAppUi(); }});
        var dexPrev = document.getElementById('dex_nexus_previous_version');
        var dexLatest = document.getElementById('dex_nexus_latest_version');
        var dexWebappPrev = document.getElementById('dex_webapp_ces_previous_version');
        var dexWebappLatest = document.getElementById('dex_webapp_ces_latest_version');
        var dexPreviewWrap = document.getElementById('dex-nexus-url-preview');
        var dexPreviewPlaybook = document.getElementById('dex-nexus-preview-playbook');
        var dexPreviewWebapp = document.getElementById('dex-nexus-preview-webapp');
        function dexReleaseTrain(v) {{
          var parts = (v || '').trim().split('.');
          if (parts.length < 3) return null;
          return parts.slice(0, 3).join('.');
        }}
        function dexProductUrls(v, mavenName) {{
          var full = (v || '').trim();
          var train = dexReleaseTrain(full);
          if (!train || !full || !/^\\d+(?:\\.\\d+)+$/.test(full)) return null;
          var base = 'https://illin3273:28081/nexus/content/repositories/amdocs-dex-ad-rpm-releases-' + train
            + '/com/amdocs/digital/' + mavenName + '/' + full + '/' + mavenName + '-' + full;
          if (mavenName === 'digital-playbook') {{
            return {{ jar: base + '.jar', tar: base + '.tar' }};
          }}
          return {{
            sources_jar: base + '-sources.jar',
            pom: base + '.pom',
            war: base + '.war'
          }};
        }}
        function dexUrlListHtml(title, prevV, latestV, mavenName) {{
          var prev = dexProductUrls(prevV, mavenName);
          var latest = dexProductUrls(latestV, mavenName);
          if (!prev && !latest) return '';
          var lines = '<div style="margin-bottom:10px;"><strong style="color:#9a3412;">' + title + '</strong></div>';
          function block(label, urls) {{
            if (!urls) return '<p style="margin:4px 0;color:#94a3b8;">' + label + ': enter version</p>';
            var s = '<p style="margin:4px 0 2px 0;font-weight:600;">' + label + '</p>';
            for (var k in urls) {{
              s += '<p style="margin:0 0 2px 0;font-size:11px;"><strong>' + k + ':</strong> <a href="' + urls[k] + '" target="_blank" rel="noopener" style="word-break:break-all;color:#2563eb;">' + urls[k] + '</a></p>';
            }}
            return s;
          }}
          lines += block('Previous', prev) + block('Latest', latest);
          return lines;
        }}
        function refreshDexNexusPreview() {{
          if (!dexPreviewWrap) return;
          if (!(appDex && appDex.checked)) {{ dexPreviewWrap.style.display = 'none'; return; }}
          var pb = dexUrlListHtml('digital-playbook', dexPrev ? dexPrev.value : '', dexLatest ? dexLatest.value : '', 'digital-playbook');
          var wb = dexUrlListHtml('digital-webapp-ces', dexWebappPrev ? dexWebappPrev.value : '', dexWebappLatest ? dexWebappLatest.value : '', 'digital-webapp-ces');
          if (dexPreviewPlaybook) dexPreviewPlaybook.innerHTML = pb;
          if (dexPreviewWebapp) dexPreviewWebapp.innerHTML = wb;
          dexPreviewWrap.style.display = (pb || wb) ? '' : 'none';
        }}
        [dexPrev, dexLatest, dexWebappPrev, dexWebappLatest].forEach(function(el) {{
          if (el) el.addEventListener('input', refreshDexNexusPreview);
        }});
        if (appDex) appDex.addEventListener('change', refreshDexNexusPreview);
        refreshDexNexusPreview();
        var crmPbPrev = document.getElementById('crm_playbooks_nexus_previous');
        var crmPbLatest = document.getElementById('crm_playbooks_nexus_latest');
        var crmPbPreviewWrap = document.getElementById('crm-playbooks-nexus-url-preview');
        var crmPbPreviewBody = document.getElementById('crm-playbooks-nexus-preview-body');
        function crmPlaybooksReleaseTrain(v) {{
          var parts = (v || '').trim().split('.');
          if (parts.length < 3) return null;
          return parts.slice(0, 3).join('.');
        }}
        function crmPlaybooksTarUrl(v) {{
          var full = (v || '').trim();
          var train = crmPlaybooksReleaseTrain(full);
          if (!train || !full || !/^\\d+(?:\\.\\d+)+(?:-\\d+)?$/.test(full)) return null;
          return 'https://indlin2410:28081/nexus/content/repositories/crm-v' + train
            + '-Releases/com/amdocs/core/crm/crm-playbooks/' + full + '/crm-playbooks-' + full + '.tar';
        }}
        function refreshCrmPlaybooksNexusPreview() {{
          if (!crmPbPreviewWrap) return;
          if (!(appCrm && appCrm.checked)) {{ crmPbPreviewWrap.style.display = 'none'; return; }}
          var prev = crmPlaybooksTarUrl(crmPbPrev ? crmPbPrev.value : '');
          var latest = crmPlaybooksTarUrl(crmPbLatest ? crmPbLatest.value : '');
          if (!prev && !latest) {{ crmPbPreviewWrap.style.display = 'none'; return; }}
          var html = '';
          function block(label, url) {{
            if (!url) return '<p style="margin:4px 0;color:#94a3b8;">' + label + ': enter version</p>';
            return '<p style="margin:4px 0 2px 0;font-weight:600;">' + label + '</p>'
              + '<p style="margin:0 0 6px 0;"><a href="' + url + '" target="_blank" rel="noopener" style="word-break:break-all;color:#2563eb;">' + url + '</a></p>';
          }}
          html += block('Previous TAR', prev) + block('Latest TAR', latest);
          if (crmPbPreviewBody) crmPbPreviewBody.innerHTML = html;
          crmPbPreviewWrap.style.display = '';
        }}
        [crmPbPrev, crmPbLatest].forEach(function(el) {{
          if (el) el.addEventListener('input', refreshCrmPlaybooksNexusPreview);
        }});
        if (appCrm) appCrm.addEventListener('change', refreshCrmPlaybooksNexusPreview);
        refreshCrmPlaybooksNexusPreview();
        var fossProduct = document.getElementById('foss-product');
        var fossLib = document.getElementById('foss-lib');
        function syncFossJar() {{
          if (!appAscf || !appAscf.checked) return;
          var jar = compareForm ? compareForm.querySelector('input[name="jar_name"]') : null;
          var fossMode = compareForm ? compareForm.querySelector('input[name="mode"][value="foss"]') : null;
          if (!jar || !fossMode || !fossMode.checked) return;
          if (fossLib && fossLib.value) jar.value = fossLib.value;
          else if (fossProduct && fossProduct.value) jar.value = fossProduct.value;
        }}
        if (fossProduct) fossProduct.addEventListener('change', syncFossJar);
        if (fossLib) fossLib.addEventListener('change', syncFossJar);
      }})();

      (function() {{
        var btn = document.getElementById('btn-crm-base-paths');
        var wrap = document.getElementById('crm-base-paths-wrap');
        var scroll = document.getElementById('crm-base-paths-scroll');
        var toolbar = document.getElementById('crm-base-paths-toolbar');
        var btnToggle = document.getElementById('btn-crm-toggle-expand');
        var tbody = document.getElementById('crm-base-paths-tbody');
        var statusEl = document.getElementById('crm-base-paths-status');
        var amssWrap = document.getElementById('amss-base-paths-wrap');
        var amssToolbar = document.getElementById('amss-base-paths-toolbar');
        var amssStatusEl = document.getElementById('amss-base-paths-status');
        var viewTitle = document.getElementById('crm-amss-paths-view-title');
        if (!btn || !wrap || !tbody) return;
        function hideAmssPaths() {{
          if (amssWrap) amssWrap.style.display = 'none';
          if (amssToolbar) amssToolbar.style.display = 'none';
          if (amssStatusEl) {{ amssStatusEl.textContent = ''; amssStatusEl.style.display = 'none'; }}
        }}
        function setMinimized(on) {{
          if (!scroll || !btnToggle) return;
          if (on) {{
            scroll.classList.add('crm-minimized');
            scroll.style.maxHeight = '140px';
            scroll.style.overflow = 'auto';
            scroll.style.border = '1px solid #d0d7de';
            scroll.style.borderRadius = '8px';
            scroll.style.background = '#fff';
            btnToggle.textContent = 'Expand table';
          }} else {{
            scroll.classList.remove('crm-minimized');
            scroll.style.maxHeight = 'none';
            scroll.style.overflow = 'visible';
            scroll.style.border = '';
            scroll.style.borderRadius = '';
            scroll.style.background = '';
            btnToggle.textContent = 'Minimize table';
          }}
        }}
        if (btnToggle && scroll) {{
          btnToggle.addEventListener('click', function () {{
            setMinimized(!scroll.classList.contains('crm-minimized'));
          }});
        }}
        function setStatus(msg, isErr) {{
          if (!statusEl) return;
          statusEl.textContent = msg || '';
          statusEl.style.display = msg ? 'block' : 'none';
          statusEl.style.color = isErr ? '#b91c1c' : '#64748b';
        }}
        function copyPathToClipboard(text) {{
          if (navigator.clipboard && navigator.clipboard.writeText) {{
            return navigator.clipboard.writeText(text);
          }}
          var ta = document.createElement('textarea');
          ta.value = text;
          ta.style.position = 'fixed';
          ta.style.left = '-9999px';
          document.body.appendChild(ta);
          ta.select();
          try {{
            document.execCommand('copy');
            return Promise.resolve();
          }} finally {{
            document.body.removeChild(ta);
          }}
        }}
        function openCrmPathInExplorer(path) {{
          return fetch('/api/open-path-in-explorer', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify({{ path: path }}),
          }}).then(function (r) {{
            return r.text().then(function (text) {{
              var data = {{}};
              try {{ data = text ? JSON.parse(text) : {{}}; }} catch (e) {{}}
              return {{ ok: r.ok, status: r.status, data: data }};
            }});
          }});
        }}
        function setBuildPath(fieldName, path) {{
          var input = document.querySelector('input[name=\"' + fieldName + '\"]');
          if (!input) return;
          input.value = path;
          input.dispatchEvent(new Event('input', {{ bubbles: true }}));
          setStatus('Filled ' + (fieldName === 'folder1_path' ? 'Old build' : 'New build') + ' with ' + path, false);
        }}

        btn.addEventListener('click', function () {{
          hideAmssPaths();
          if (viewTitle) {{ viewTitle.style.display = 'block'; viewTitle.style.color = '#0369a1'; viewTitle.textContent = 'CRM base paths'; }}
          setStatus('Loading…', false);
          fetch('/api/crm-build-base-paths')
            .then(function (r) {{ if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); }})
            .then(function (data) {{
              var rows = data.bases || [];
              tbody.innerHTML = '';
              rows.forEach(function (row) {{
                var path = (row.path || '').trim();
                var label = row.label || 'Path';
                var tr = document.createElement('tr');
                var tdL = document.createElement('td');
                tdL.style.padding = '8px 10px';
                tdL.style.borderBottom = '1px solid #e9d5ff';
                tdL.style.verticalAlign = 'top';
                tdL.style.color = '#334155';
                tdL.style.fontSize = '14px';
                tdL.textContent = label;
                var tdP = document.createElement('td');
                tdP.style.padding = '8px 10px';
                tdP.style.borderBottom = '1px solid #e9d5ff';
                tdP.style.fontFamily = 'Consolas,monospace';
                tdP.style.fontSize = '12px';
                tdP.style.wordBreak = 'break-all';
                tdP.textContent = path;
                var tdA = document.createElement('td');
                tdA.style.padding = '8px 10px';
                tdA.style.borderBottom = '1px solid #e9d5ff';
                tdA.style.whiteSpace = 'nowrap';
                var bOld = document.createElement('button');
                bOld.type = 'button';
                bOld.textContent = 'Use as Old';
                bOld.style.marginRight = '6px';
                bOld.style.padding = '6px 10px';
                bOld.style.fontSize = '12px';
                bOld.style.fontWeight = '600';
                bOld.style.border = '0';
                bOld.style.borderRadius = '8px';
                bOld.style.cursor = path ? 'pointer' : 'not-allowed';
                bOld.style.color = '#fff';
                bOld.style.background = '#0369a1';
                bOld.disabled = !path;
                if (path) bOld.addEventListener('click', function() {{ setBuildPath('folder1_path', path); }});
                var bNew = document.createElement('button');
                bNew.type = 'button';
                bNew.textContent = 'Use as New';
                bNew.style.marginRight = '6px';
                bNew.style.padding = '6px 10px';
                bNew.style.fontSize = '12px';
                bNew.style.fontWeight = '600';
                bNew.style.border = '0';
                bNew.style.borderRadius = '8px';
                bNew.style.cursor = path ? 'pointer' : 'not-allowed';
                bNew.style.color = '#fff';
                bNew.style.background = '#2563eb';
                bNew.disabled = !path;
                if (path) bNew.addEventListener('click', function() {{ setBuildPath('folder2_path', path); }});
                var bOpen = document.createElement('button');
                bOpen.type = 'button';
                bOpen.textContent = 'Open in Explorer';
                bOpen.style.padding = '6px 10px';
                bOpen.style.fontSize = '12px';
                bOpen.style.fontWeight = '600';
                bOpen.style.border = '0';
                bOpen.style.borderRadius = '8px';
                bOpen.style.cursor = path ? 'pointer' : 'not-allowed';
                bOpen.style.color = '#fff';
                bOpen.style.background = '#7c3aed';
                bOpen.disabled = !path;
                if (path) {{
                  bOpen.addEventListener('click', function () {{
                    openCrmPathInExplorer(path).then(function (res) {{
                      if (res.ok && res.data && res.data.ok) {{
                        setStatus('Opened in Windows Explorer: ' + path, false);
                        return;
                      }}
                      if (res.status === 403 || res.status === 501 || (res.data && res.data.fallback === 'copy')) {{
                        copyPathToClipboard(path).then(function () {{
                          setStatus('Path copied — paste into Explorer address bar (Win+E). ' + path, false);
                        }});
                        return;
                      }}
                      var err = (res.data && res.data.error) || 'Could not open folder.';
                      copyPathToClipboard(path).then(function () {{
                        setStatus(err + ' Path also copied: ' + path, true);
                      }});
                    }}).catch(function () {{
                      copyPathToClipboard(path).then(function () {{
                        setStatus('Network error — path copied: ' + path, true);
                      }});
                    }});
                  }});
                }}
                tdA.appendChild(bOld);
                tdA.appendChild(bNew);
                tdA.appendChild(bOpen);
                tr.appendChild(tdL);
                tr.appendChild(tdP);
                tr.appendChild(tdA);
                tbody.appendChild(tr);
              }});
              wrap.style.display = 'block';
              if (toolbar) toolbar.style.display = rows.length ? 'block' : 'none';
              setMinimized(false);
              if (viewTitle) viewTitle.textContent = 'CRM base paths (' + rows.length + ' versions)';
              setStatus(rows.length ? ('Loaded ' + rows.length + ' CRM path(s).') : 'No paths returned.', false);
            }})
            .catch(function () {{ setStatus('Could not load paths.', true); }});
        }});
      }})();

      (function() {{
        var btn = document.getElementById('btn-amss-base-paths');
        var wrap = document.getElementById('amss-base-paths-wrap');
        var scroll = document.getElementById('amss-base-paths-scroll');
        var toolbar = document.getElementById('amss-base-paths-toolbar');
        var btnToggle = document.getElementById('btn-amss-toggle-expand');
        var tbody = document.getElementById('amss-base-paths-tbody');
        var statusEl = document.getElementById('amss-base-paths-status');
        var crmWrap = document.getElementById('crm-base-paths-wrap');
        var crmToolbar = document.getElementById('crm-base-paths-toolbar');
        var crmStatusEl = document.getElementById('crm-base-paths-status');
        var viewTitle = document.getElementById('crm-amss-paths-view-title');
        if (!btn || !wrap || !tbody) return;
        function hideCrmPaths() {{
          if (crmWrap) crmWrap.style.display = 'none';
          if (crmToolbar) crmToolbar.style.display = 'none';
          if (crmStatusEl) {{ crmStatusEl.textContent = ''; crmStatusEl.style.display = 'none'; }}
        }}
        function setMinimized(on) {{
          if (!scroll || !btnToggle) return;
          if (on) {{
            scroll.style.maxHeight = '140px';
            scroll.style.overflow = 'auto';
            scroll.style.border = '1px solid #d0d7de';
            scroll.style.borderRadius = '8px';
            scroll.style.background = '#fff';
            btnToggle.textContent = 'Expand table';
          }} else {{
            scroll.style.maxHeight = 'none';
            scroll.style.overflow = 'visible';
            scroll.style.border = '';
            scroll.style.borderRadius = '';
            scroll.style.background = '';
            btnToggle.textContent = 'Minimize table';
          }}
        }}
        if (btnToggle && scroll) {{
          btnToggle.addEventListener('click', function() {{
            setMinimized(btnToggle.textContent === 'Minimize table');
          }});
        }}
        function setStatus(msg, isErr) {{
          if (!statusEl) return;
          statusEl.textContent = msg || '';
          statusEl.style.display = msg ? 'block' : 'none';
          statusEl.style.color = isErr ? '#b91c1c' : '#64748b';
        }}
        function copyPathToClipboard(text) {{
          if (navigator.clipboard && navigator.clipboard.writeText) {{
            return navigator.clipboard.writeText(text);
          }}
          var ta = document.createElement('textarea');
          ta.value = text;
          ta.style.position = 'fixed';
          ta.style.left = '-9999px';
          document.body.appendChild(ta);
          ta.select();
          try {{
            document.execCommand('copy');
            return Promise.resolve();
          }} finally {{
            document.body.removeChild(ta);
          }}
        }}
        function openPathInExplorer(path) {{
          return fetch('/api/open-path-in-explorer', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify({{ path: path }}),
          }}).then(function (r) {{
            return r.text().then(function (text) {{
              var data = {{}};
              try {{ data = text ? JSON.parse(text) : {{}}; }} catch (e) {{}}
              return {{ ok: r.ok, status: r.status, data: data }};
            }});
          }});
        }}
        function buildAmssPath(versionCode) {{
          return '\\\\\\\\raaisilon\\\\cmprod\\\\pbg\\\\cmbuild\\\\AMSS\\\\' + versionCode;
        }}
        var amssBaseRows = [
          {{ label: 'v9.0PMX10_1.1' }},
          {{ label: 'v9.3PMX10_1' }},
          {{ label: 'v10_00' }},
          {{ label: 'v800' }},
          {{ label: 'v900' }},
          {{ label: 'v901' }},
          {{ label: 'v901_TSP2' }},
          {{ label: 'v910' }},
          {{ label: 'v920' }},
          {{ label: 'v921' }},
          {{ label: 'v922' }},
          {{ label: 'v930' }},
          {{ label: 'v9221' }},
          {{ label: 'v9222' }},
          {{ label: 'v92201' }}
        ];
        function setBuildPath(fieldName, path) {{
          var input = document.querySelector('input[name=\"' + fieldName + '\"]');
          if (!input) return;
          input.value = path;
          input.dispatchEvent(new Event('input', {{ bubbles: true }}));
          setStatus('Filled ' + (fieldName === 'folder1_path' ? 'Old build' : 'New build') + ' with ' + path, false);
        }}
        btn.addEventListener('click', function () {{
          hideCrmPaths();
          if (viewTitle) {{ viewTitle.style.display = 'block'; viewTitle.style.color = '#0f766e'; viewTitle.textContent = 'AMSS base paths'; }}
          setStatus('Loading…', false);
          tbody.innerHTML = '';
          amssBaseRows.forEach(function (row) {{
            row.path = buildAmssPath(row.label);
            var tr = document.createElement('tr');
            var tdL = document.createElement('td');
            tdL.style.padding = '8px 10px';
            tdL.style.borderBottom = '1px solid #99f6e4';
            tdL.style.verticalAlign = 'top';
            tdL.style.color = '#334155';
            tdL.style.fontSize = '14px';
            tdL.textContent = row.label;
            var tdP = document.createElement('td');
            tdP.style.padding = '8px 10px';
            tdP.style.borderBottom = '1px solid #99f6e4';
            tdP.style.fontFamily = 'Consolas,monospace';
            tdP.style.fontSize = '12px';
            tdP.style.wordBreak = 'break-all';
            tdP.textContent = row.path;
            var tdA = document.createElement('td');
            tdA.style.padding = '8px 10px';
            tdA.style.borderBottom = '1px solid #99f6e4';
            tdA.style.whiteSpace = 'nowrap';
            var bOld = document.createElement('button');
            bOld.type = 'button';
            bOld.textContent = 'Use as Old';
            bOld.style.marginRight = '6px';
            bOld.style.padding = '6px 10px';
            bOld.style.fontSize = '12px';
            bOld.style.fontWeight = '600';
            bOld.style.border = '0';
            bOld.style.borderRadius = '8px';
            bOld.style.cursor = 'pointer';
            bOld.style.color = '#fff';
            bOld.style.background = '#0f766e';
            bOld.addEventListener('click', function() {{ setBuildPath('folder1_path', row.path); }});
            var bNew = document.createElement('button');
            bNew.type = 'button';
            bNew.textContent = 'Use as New';
            bNew.style.marginRight = '6px';
            bNew.style.padding = '6px 10px';
            bNew.style.fontSize = '12px';
            bNew.style.fontWeight = '600';
            bNew.style.border = '0';
            bNew.style.borderRadius = '8px';
            bNew.style.cursor = 'pointer';
            bNew.style.color = '#fff';
            bNew.style.background = '#2563eb';
            bNew.addEventListener('click', function() {{ setBuildPath('folder2_path', row.path); }});
            var bOpen = document.createElement('button');
            bOpen.type = 'button';
            bOpen.textContent = 'Open in Explorer';
            bOpen.style.padding = '6px 10px';
            bOpen.style.fontSize = '12px';
            bOpen.style.fontWeight = '600';
            bOpen.style.border = '0';
            bOpen.style.borderRadius = '8px';
            bOpen.style.cursor = 'pointer';
            bOpen.style.color = '#fff';
            bOpen.style.background = '#0d9488';
            bOpen.addEventListener('click', function () {{
              openPathInExplorer(row.path).then(function (res) {{
                if (res.ok && res.data && res.data.ok) {{
                  setStatus('Opened in Windows Explorer: ' + row.path, false);
                  return;
                }}
                if (res.status === 403 || res.status === 501 || (res.data && res.data.fallback === 'copy')) {{
                  copyPathToClipboard(row.path).then(function () {{
                    setStatus('Path copied — paste into Explorer address bar (Win+E). ' + row.path, false);
                  }});
                  return;
                }}
                var err = (res.data && res.data.error) || 'Could not open folder.';
                copyPathToClipboard(row.path).then(function () {{
                  setStatus(err + ' Path also copied: ' + row.path, true);
                }});
              }}).catch(function () {{
                copyPathToClipboard(row.path).then(function () {{
                  setStatus('Network error — path copied: ' + row.path, true);
                }});
              }});
            }});
            tdA.appendChild(bOld);
            tdA.appendChild(bNew);
            tdA.appendChild(bOpen);
            tr.appendChild(tdL);
            tr.appendChild(tdP);
            tr.appendChild(tdA);
            tbody.appendChild(tr);
          }});
          wrap.style.display = 'block';
          if (toolbar) toolbar.style.display = amssBaseRows.length ? 'block' : 'none';
          setMinimized(false);
          if (viewTitle) viewTitle.textContent = 'AMSS base paths (' + amssBaseRows.length + ' versions)';
          setStatus('Loaded ' + amssBaseRows.length + ' AMSS path(s).', false);
        }});
      }})();

      (function() {{
        var btn = document.getElementById('btn-ascf-base-paths');
        var wrap = document.getElementById('ascf-base-paths-wrap');
        var scroll = document.getElementById('ascf-base-paths-scroll');
        var toolbar = document.getElementById('ascf-base-paths-toolbar');
        var btnToggle = document.getElementById('btn-ascf-toggle-expand');
        var tbody = document.getElementById('ascf-base-paths-tbody');
        var statusEl = document.getElementById('ascf-base-paths-status');
        if (!btn || !wrap || !tbody) return;
        function setMinimized(on) {{
          if (!scroll || !btnToggle) return;
          if (on) {{
            scroll.style.maxHeight = '140px';
            scroll.style.overflow = 'auto';
            scroll.style.border = '1px solid #d0d7de';
            scroll.style.borderRadius = '8px';
            scroll.style.background = '#fff';
            btnToggle.textContent = 'Expand table';
          }} else {{
            scroll.style.maxHeight = '';
            scroll.style.overflow = 'auto';
            scroll.style.border = '';
            scroll.style.borderRadius = '';
            scroll.style.background = '';
            btnToggle.textContent = 'Minimize table';
          }}
        }}
        if (btnToggle && scroll) {{
          btnToggle.addEventListener('click', function() {{
            setMinimized(btnToggle.textContent === 'Minimize table');
          }});
        }}
        function setStatus(msg, isErr) {{
          if (!statusEl) return;
          statusEl.textContent = msg || '';
          statusEl.style.display = msg ? 'block' : 'none';
          statusEl.style.color = isErr ? '#b91c1c' : '#64748b';
        }}
        function copyPathToClipboard(text) {{
          if (navigator.clipboard && navigator.clipboard.writeText) {{
            return navigator.clipboard.writeText(text);
          }}
          var ta = document.createElement('textarea');
          ta.value = text;
          ta.style.position = 'fixed';
          ta.style.left = '-9999px';
          document.body.appendChild(ta);
          ta.select();
          try {{
            document.execCommand('copy');
            return Promise.resolve();
          }} finally {{
            document.body.removeChild(ta);
          }}
        }}
        function openPathInExplorer(path) {{
          return fetch('/api/open-path-in-explorer', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify({{ path: path }}),
          }}).then(function (r) {{
            return r.text().then(function (text) {{
              var data = {{}};
              try {{ data = text ? JSON.parse(text) : {{}}; }} catch (e) {{}}
              return {{ ok: r.ok, status: r.status, data: data }};
            }});
          }});
        }}
        function buildAscfPath(versionCode) {{
          return '\\\\\\\\inpnqascflb02\\\\uif_' + versionCode + '\\\\uif';
        }}
        var ascfBaseRows = [
          {{ label: '10250' }},
          {{ label: '10241' }},
          {{ label: '10240' }},
          {{ label: '10100' }},
          {{ label: '9301' }},
          {{ label: '9221' }},
          {{ label: '9005' }},
          {{ label: '9004' }},
          {{ label: '9003' }},
          {{ label: '9000' }},
          {{ label: '8203' }},
          {{ label: '8202' }},
          {{ label: '8201' }}
        ];
        function setBuildPath(fieldName, path) {{
          var input = document.querySelector('input[name=\"' + fieldName + '\"]');
          if (!input) return;
          input.value = path;
          input.dispatchEvent(new Event('input', {{ bubbles: true }}));
          setStatus('Filled ' + (fieldName === 'folder1_path' ? 'Old build' : 'New build') + ' with ' + path, false);
        }}
        btn.addEventListener('click', function () {{
          setStatus('Loading…', false);
          tbody.innerHTML = '';
          ascfBaseRows.forEach(function (row) {{
            row.path = buildAscfPath(row.label);
            var tr = document.createElement('tr');
            var tdL = document.createElement('td');
            tdL.style.padding = '8px 10px';
            tdL.style.borderBottom = '1px solid #e9d5ff';
            tdL.style.verticalAlign = 'top';
            tdL.style.color = '#334155';
            tdL.style.fontSize = '14px';
            tdL.textContent = row.label;
            var tdP = document.createElement('td');
            tdP.style.padding = '8px 10px';
            tdP.style.borderBottom = '1px solid #e9d5ff';
            tdP.style.fontFamily = 'Consolas,monospace';
            tdP.style.fontSize = '12px';
            tdP.style.wordBreak = 'break-all';
            tdP.textContent = row.path;
            var tdA = document.createElement('td');
            tdA.style.padding = '8px 10px';
            tdA.style.borderBottom = '1px solid #e9d5ff';
            tdA.style.whiteSpace = 'nowrap';
            var bOld = document.createElement('button');
            bOld.type = 'button';
            bOld.textContent = 'Use as Old';
            bOld.style.marginRight = '6px';
            bOld.style.padding = '6px 10px';
            bOld.style.fontSize = '12px';
            bOld.style.fontWeight = '600';
            bOld.style.border = '0';
            bOld.style.borderRadius = '8px';
            bOld.style.cursor = 'pointer';
            bOld.style.color = '#fff';
            bOld.style.background = '#6d28d9';
            bOld.addEventListener('click', function() {{ setBuildPath('folder1_path', row.path); }});
            var bNew = document.createElement('button');
            bNew.type = 'button';
            bNew.textContent = 'Use as New';
            bNew.style.marginRight = '6px';
            bNew.style.padding = '6px 10px';
            bNew.style.fontSize = '12px';
            bNew.style.fontWeight = '600';
            bNew.style.border = '0';
            bNew.style.borderRadius = '8px';
            bNew.style.cursor = 'pointer';
            bNew.style.color = '#fff';
            bNew.style.background = '#2563eb';
            bNew.addEventListener('click', function() {{ setBuildPath('folder2_path', row.path); }});
            var bOpen = document.createElement('button');
            bOpen.type = 'button';
            bOpen.textContent = 'Open in Explorer';
            bOpen.style.padding = '6px 10px';
            bOpen.style.fontSize = '12px';
            bOpen.style.fontWeight = '600';
            bOpen.style.border = '0';
            bOpen.style.borderRadius = '8px';
            bOpen.style.cursor = 'pointer';
            bOpen.style.color = '#fff';
            bOpen.style.background = '#7c3aed';
            bOpen.addEventListener('click', function () {{
              openPathInExplorer(row.path).then(function (res) {{
                if (res.ok && res.data && res.data.ok) {{
                  setStatus('Opened in Windows Explorer: ' + row.path, false);
                  return;
                }}
                if (res.status === 403 || res.status === 501 || (res.data && res.data.fallback === 'copy')) {{
                  copyPathToClipboard(row.path).then(function () {{
                    setStatus('Path copied — paste into Explorer address bar (Win+E). ' + row.path, false);
                  }});
                  return;
                }}
                var err = (res.data && res.data.error) || 'Could not open folder.';
                copyPathToClipboard(row.path).then(function () {{
                  setStatus(err + ' Path also copied: ' + row.path, true);
                }});
              }}).catch(function () {{
                copyPathToClipboard(row.path).then(function () {{
                  setStatus('Network error — path copied: ' + row.path, true);
                }});
              }});
            }});
            tdA.appendChild(bOld);
            tdA.appendChild(bNew);
            tdA.appendChild(bOpen);
            tr.appendChild(tdL);
            tr.appendChild(tdP);
            tr.appendChild(tdA);
            tbody.appendChild(tr);
          }});
          wrap.style.display = 'block';
          if (toolbar) toolbar.style.display = ascfBaseRows.length ? 'block' : 'none';
          setMinimized(true);
          setStatus('Loaded ' + ascfBaseRows.length + ' ASCF path(s). Table is minimized — click Expand table to see all rows.', false);
        }});
      }})();

      (function() {{
        var btn = document.getElementById('btn-uxf-base-paths');
        var wrap = document.getElementById('uxf-base-paths-wrap');
        var scroll = document.getElementById('uxf-base-paths-scroll');
        var toolbar = document.getElementById('uxf-base-paths-toolbar');
        var btnToggle = document.getElementById('btn-uxf-toggle-expand');
        var tbody = document.getElementById('uxf-base-paths-tbody');
        var statusEl = document.getElementById('uxf-base-paths-status');
        var omniWrap = document.getElementById('omni-base-paths-wrap');
        var omniToolbar = document.getElementById('omni-base-paths-toolbar');
        var omniStatusEl = document.getElementById('omni-base-paths-status');
        var viewTitle = document.getElementById('uxf-omni-paths-view-title');
        if (!btn || !wrap || !tbody) return;
        function hideOmniPaths() {{
          if (omniWrap) omniWrap.style.display = 'none';
          if (omniToolbar) omniToolbar.style.display = 'none';
          if (omniStatusEl) {{ omniStatusEl.textContent = ''; omniStatusEl.style.display = 'none'; }}
        }}
        function setMinimized(on) {{
          if (!scroll || !btnToggle) return;
          if (on) {{
            scroll.style.maxHeight = '140px';
            scroll.style.overflow = 'auto';
            scroll.style.border = '1px solid #d0d7de';
            scroll.style.borderRadius = '8px';
            scroll.style.background = '#fff';
            btnToggle.textContent = 'Expand table';
          }} else {{
            scroll.style.maxHeight = 'none';
            scroll.style.overflow = 'visible';
            scroll.style.border = '';
            scroll.style.borderRadius = '';
            scroll.style.background = '';
            btnToggle.textContent = 'Minimize table';
          }}
        }}
        if (btnToggle && scroll) {{
          btnToggle.addEventListener('click', function() {{
            setMinimized(btnToggle.textContent === 'Minimize table');
          }});
        }}
        function setStatus(msg, isErr) {{
          if (!statusEl) return;
          statusEl.textContent = msg || '';
          statusEl.style.display = msg ? 'block' : 'none';
          statusEl.style.color = isErr ? '#b91c1c' : '#64748b';
        }}
        function copyPathToClipboard(text) {{
          if (navigator.clipboard && navigator.clipboard.writeText) {{
            return navigator.clipboard.writeText(text);
          }}
          var ta = document.createElement('textarea');
          ta.value = text;
          ta.style.position = 'fixed';
          ta.style.left = '-9999px';
          document.body.appendChild(ta);
          ta.select();
          try {{
            document.execCommand('copy');
            return Promise.resolve();
          }} finally {{
            document.body.removeChild(ta);
          }}
        }}
        function openPathInExplorer(path) {{
          return fetch('/api/open-path-in-explorer', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify({{ path: path }}),
          }}).then(function (r) {{
            return r.text().then(function (text) {{
              var data = {{}};
              try {{ data = text ? JSON.parse(text) : {{}}; }} catch (e) {{}}
              return {{ ok: r.ok, status: r.status, data: data }};
            }});
          }});
        }}
        function buildUxfPath(versionCode) {{
          return '\\\\\\\\raaisilon\\\\cmprod\\\\pbg\\\\cmbuild\\\\LS\\\\' + versionCode;
        }}
        function buildUxfDevPath(versionCode) {{
          return '\\\\\\\\raaisilon\\\\cmprod_dev\\\\pbg\\\\cmdev\\\\LS\\\\' + versionCode;
        }}
        var uxfBaseRows = [
          {{ label: 'v9.0PMX10_1.1' }},
          {{ label: 'v9.22' }},
          {{ label: 'v9.22.1' }},
          {{ label: 'v9_20PMX' }},
          {{ label: 'v9_21_PMX_10.2' }},
          {{ label: 'v9_30_PMX10_2_1' }},
          {{ label: 'v9_30_PMX102_1' }},
          {{ label: 'v10.1' }},
          {{ label: 'v900' }},
          {{ label: 'v901' }},
          {{ label: 'v901_USCC' }},
          {{ label: 'v902' }},
          {{ label: 'v910' }},
          {{ label: 'v920' }},
          {{ label: 'v921' }},
          {{ label: 'v930' }},
          {{ label: 'v930_TSP2' }},
          {{ label: 'v931' }},
          {{ label: 'v1000' }},
          {{ label: 'v10.1', base: 'dev' }},
          {{ label: 'v10.1.1', base: 'dev' }},
          {{ label: 'v10.2', base: 'dev' }},
          {{ label: 'v10.3', base: 'dev' }},
          {{ label: 'v10.24', base: 'dev' }},
          {{ label: 'v10.24.1', base: 'dev' }},
          {{ label: 'v10.25', base: 'dev' }},
          {{ label: 'v930', base: 'dev' }},
          {{ label: 'v931', base: 'dev' }}
        ];
        function setBuildPath(fieldName, path) {{
          var input = document.querySelector('input[name=\"' + fieldName + '\"]');
          if (!input) return;
          input.value = path;
          input.dispatchEvent(new Event('input', {{ bubbles: true }}));
          setStatus('Filled ' + (fieldName === 'folder1_path' ? 'Old build' : 'New build') + ' with ' + path, false);
        }}
        btn.addEventListener('click', function () {{
          hideOmniPaths();
          if (viewTitle) {{ viewTitle.style.display = 'block'; viewTitle.style.color = '#166534'; viewTitle.textContent = 'UXF base paths'; }}
          setStatus('Loading…', false);
          tbody.innerHTML = '';
          uxfBaseRows.forEach(function (row) {{
            row.path = row.base === 'dev' ? buildUxfDevPath(row.label) : buildUxfPath(row.label);
            var tr = document.createElement('tr');
            var tdL = document.createElement('td');
            tdL.style.padding = '8px 10px';
            tdL.style.borderBottom = '1px solid #bbf7d0';
            tdL.style.verticalAlign = 'top';
            tdL.style.color = '#334155';
            tdL.style.fontSize = '14px';
            tdL.textContent = row.label;
            var tdEnv = document.createElement('td');
            tdEnv.style.padding = '8px 10px';
            tdEnv.style.borderBottom = '1px solid #bbf7d0';
            tdEnv.style.fontSize = '12px';
            tdEnv.style.color = '#64748b';
            tdEnv.textContent = row.base === 'dev' ? 'cmdev' : 'Production';
            var tdP = document.createElement('td');
            tdP.style.padding = '8px 10px';
            tdP.style.borderBottom = '1px solid #bbf7d0';
            tdP.style.fontFamily = 'Consolas,monospace';
            tdP.style.fontSize = '12px';
            tdP.style.wordBreak = 'break-all';
            tdP.textContent = row.path;
            var tdA = document.createElement('td');
            tdA.style.padding = '8px 10px';
            tdA.style.borderBottom = '1px solid #bbf7d0';
            tdA.style.whiteSpace = 'nowrap';
            var bOld = document.createElement('button');
            bOld.type = 'button';
            bOld.textContent = 'Use as Old';
            bOld.style.marginRight = '6px';
            bOld.style.padding = '6px 10px';
            bOld.style.fontSize = '12px';
            bOld.style.fontWeight = '600';
            bOld.style.border = '0';
            bOld.style.borderRadius = '8px';
            bOld.style.cursor = 'pointer';
            bOld.style.color = '#fff';
            bOld.style.background = '#047857';
            bOld.addEventListener('click', function() {{ setBuildPath('folder1_path', row.path); }});
            var bNew = document.createElement('button');
            bNew.type = 'button';
            bNew.textContent = 'Use as New';
            bNew.style.marginRight = '6px';
            bNew.style.padding = '6px 10px';
            bNew.style.fontSize = '12px';
            bNew.style.fontWeight = '600';
            bNew.style.border = '0';
            bNew.style.borderRadius = '8px';
            bNew.style.cursor = 'pointer';
            bNew.style.color = '#fff';
            bNew.style.background = '#2563eb';
            bNew.addEventListener('click', function() {{ setBuildPath('folder2_path', row.path); }});
            var bOpen = document.createElement('button');
            bOpen.type = 'button';
            bOpen.textContent = 'Open in Explorer';
            bOpen.style.padding = '6px 10px';
            bOpen.style.fontSize = '12px';
            bOpen.style.fontWeight = '600';
            bOpen.style.border = '0';
            bOpen.style.borderRadius = '8px';
            bOpen.style.cursor = 'pointer';
            bOpen.style.color = '#fff';
            bOpen.style.background = '#059669';
            bOpen.addEventListener('click', function () {{
              openPathInExplorer(row.path).then(function (res) {{
                if (res.ok && res.data && res.data.ok) {{
                  setStatus('Opened in Windows Explorer: ' + row.path, false);
                  return;
                }}
                if (res.status === 403 || res.status === 501 || (res.data && res.data.fallback === 'copy')) {{
                  copyPathToClipboard(row.path).then(function () {{
                    setStatus('Path copied — paste into Explorer address bar (Win+E). ' + row.path, false);
                  }});
                  return;
                }}
                var err = (res.data && res.data.error) || 'Could not open folder.';
                copyPathToClipboard(row.path).then(function () {{
                  setStatus(err + ' Path also copied: ' + row.path, true);
                }});
              }}).catch(function () {{
                copyPathToClipboard(row.path).then(function () {{
                  setStatus('Network error — path copied: ' + row.path, true);
                }});
              }});
            }});
            tdA.appendChild(bOld);
            tdA.appendChild(bNew);
            tdA.appendChild(bOpen);
            tr.appendChild(tdL);
            tr.appendChild(tdEnv);
            tr.appendChild(tdP);
            tr.appendChild(tdA);
            tbody.appendChild(tr);
          }});
          wrap.style.display = 'block';
          if (toolbar) toolbar.style.display = uxfBaseRows.length ? 'block' : 'none';
          setMinimized(false);
          if (viewTitle) viewTitle.textContent = 'UXF base paths (' + uxfBaseRows.length + ' versions)';
          setStatus('Loaded ' + uxfBaseRows.length + ' UXF path(s).', false);
        }});
      }})();

      (function() {{
        var btn = document.getElementById('btn-omni-base-paths');
        var wrap = document.getElementById('omni-base-paths-wrap');
        var scroll = document.getElementById('omni-base-paths-scroll');
        var toolbar = document.getElementById('omni-base-paths-toolbar');
        var btnToggle = document.getElementById('btn-omni-toggle-expand');
        var tbody = document.getElementById('omni-base-paths-tbody');
        var statusEl = document.getElementById('omni-base-paths-status');
        var uxfWrap = document.getElementById('uxf-base-paths-wrap');
        var uxfToolbar = document.getElementById('uxf-base-paths-toolbar');
        var uxfStatusEl = document.getElementById('uxf-base-paths-status');
        var viewTitle = document.getElementById('uxf-omni-paths-view-title');
        if (!btn || !wrap || !tbody) return;
        function hideUxfPaths() {{
          if (uxfWrap) uxfWrap.style.display = 'none';
          if (uxfToolbar) uxfToolbar.style.display = 'none';
          if (uxfStatusEl) {{ uxfStatusEl.textContent = ''; uxfStatusEl.style.display = 'none'; }}
        }}
        function setMinimized(on) {{
          if (!scroll || !btnToggle) return;
          if (on) {{
            scroll.style.maxHeight = '140px';
            scroll.style.overflow = 'auto';
            scroll.style.border = '1px solid #d0d7de';
            scroll.style.borderRadius = '8px';
            btnToggle.textContent = 'Expand table';
          }} else {{
            scroll.style.maxHeight = 'none';
            scroll.style.overflow = 'visible';
            scroll.style.border = '';
            scroll.style.borderRadius = '';
            btnToggle.textContent = 'Minimize table';
          }}
        }}
        if (btnToggle && scroll) {{
          btnToggle.addEventListener('click', function() {{
            setMinimized(btnToggle.textContent === 'Minimize table');
          }});
        }}
        function setStatus(msg, isErr) {{
          if (!statusEl) return;
          statusEl.textContent = msg || '';
          statusEl.style.display = msg ? 'block' : 'none';
          statusEl.style.color = isErr ? '#b91c1c' : '#64748b';
        }}
        function copyPathToClipboard(text) {{
          if (navigator.clipboard && navigator.clipboard.writeText) {{
            return navigator.clipboard.writeText(text);
          }}
          var ta = document.createElement('textarea');
          ta.value = text;
          ta.style.position = 'fixed';
          ta.style.left = '-9999px';
          document.body.appendChild(ta);
          ta.select();
          try {{
            document.execCommand('copy');
            return Promise.resolve();
          }} finally {{
            document.body.removeChild(ta);
          }}
        }}
        function openPathInExplorer(path) {{
          return fetch('/api/open-path-in-explorer', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify({{ path: path }}),
          }}).then(function (r) {{
            return r.text().then(function (text) {{
              var data = {{}};
              try {{ data = text ? JSON.parse(text) : {{}}; }} catch (e) {{}}
              return {{ ok: r.ok, status: r.status, data: data }};
            }});
          }});
        }}
        function omniRepFolder(versionLabel) {{
          return 'v' + String(versionLabel).replace(/\\./g, '');
        }}
        function buildOmniPath(versionLabel) {{
          return '\\\\\\\\raaisilon\\\\cmprod\\\\pbg\\\\cmbuild\\\\CM_REP\\\\' + omniRepFolder(versionLabel);
        }}
        function buildOmniDevPath(folderName) {{
          return '\\\\\\\\raaisilon\\\\cmprod_dev\\\\pbg\\\\cmdev\\\\CM_REP\\\\' + folderName;
        }}
        var omniBaseRows = [
          {{ label: '9.3.1' }},
          {{ label: '9.3.0' }},
          {{ label: '10.1.0' }},
          {{ label: '10.0.1' }},
          {{ label: '9.22.1' }},
          {{ label: '10.2' }},
          {{ label: '10.0.0' }},
          {{ label: '9.3.2' }},
          {{ label: '10.24' }},
          {{ label: '9.3PMX10_1', base: 'dev' }},
          {{ label: '9.3PMX10_1.1', base: 'dev' }},
          {{ label: 'v10.1', base: 'dev' }},
          {{ label: 'v10.2', base: 'dev' }},
          {{ label: 'v930', base: 'dev' }},
          {{ label: 'v931', base: 'dev' }},
          {{ label: 'v932', base: 'dev' }},
          {{ label: 'v933', base: 'dev' }},
          {{ label: 'v934', base: 'dev' }},
          {{ label: 'v1000_PMX102_1', base: 'dev' }},
          {{ label: 'v1001', base: 'dev' }}
        ];
        function setBuildPath(fieldName, path) {{
          var input = document.querySelector('input[name=\"' + fieldName + '\"]');
          if (!input) return;
          input.value = path;
          input.dispatchEvent(new Event('input', {{ bubbles: true }}));
          setStatus('Filled ' + (fieldName === 'folder1_path' ? 'Old build' : 'New build') + ' with ' + path, false);
        }}
        btn.addEventListener('click', function () {{
          hideUxfPaths();
          if (viewTitle) {{ viewTitle.style.display = 'block'; viewTitle.style.color = '#1e40af'; viewTitle.textContent = 'OMNI base paths'; }}
          setStatus('Loading…', false);
          tbody.innerHTML = '';
          omniBaseRows.forEach(function (row) {{
            row.path = row.base === 'dev' ? buildOmniDevPath(row.label) : buildOmniPath(row.label);
            var tr = document.createElement('tr');
            var tdL = document.createElement('td');
            tdL.style.padding = '8px 10px';
            tdL.style.borderBottom = '1px solid #bfdbfe';
            tdL.style.verticalAlign = 'top';
            tdL.style.color = '#334155';
            tdL.style.fontSize = '14px';
            tdL.textContent = row.label;
            var tdEnv = document.createElement('td');
            tdEnv.style.padding = '8px 10px';
            tdEnv.style.borderBottom = '1px solid #bfdbfe';
            tdEnv.style.fontSize = '12px';
            tdEnv.style.color = '#64748b';
            tdEnv.textContent = row.base === 'dev' ? 'cmdev' : 'Production';
            var tdP = document.createElement('td');
            tdP.style.padding = '8px 10px';
            tdP.style.borderBottom = '1px solid #bfdbfe';
            tdP.style.fontFamily = 'Consolas,monospace';
            tdP.style.fontSize = '12px';
            tdP.style.wordBreak = 'break-all';
            tdP.textContent = row.path;
            var tdA = document.createElement('td');
            tdA.style.padding = '8px 10px';
            tdA.style.borderBottom = '1px solid #bfdbfe';
            tdA.style.whiteSpace = 'nowrap';
            var bOld = document.createElement('button');
            bOld.type = 'button';
            bOld.textContent = 'Use as Old';
            bOld.style.marginRight = '6px';
            bOld.style.padding = '6px 10px';
            bOld.style.fontSize = '12px';
            bOld.style.fontWeight = '600';
            bOld.style.border = '0';
            bOld.style.borderRadius = '8px';
            bOld.style.cursor = 'pointer';
            bOld.style.color = '#fff';
            bOld.style.background = '#1d4ed8';
            bOld.addEventListener('click', function() {{ setBuildPath('folder1_path', row.path); }});
            var bNew = document.createElement('button');
            bNew.type = 'button';
            bNew.textContent = 'Use as New';
            bNew.style.marginRight = '6px';
            bNew.style.padding = '6px 10px';
            bNew.style.fontSize = '12px';
            bNew.style.fontWeight = '600';
            bNew.style.border = '0';
            bNew.style.borderRadius = '8px';
            bNew.style.cursor = 'pointer';
            bNew.style.color = '#fff';
            bNew.style.background = '#2563eb';
            bNew.addEventListener('click', function() {{ setBuildPath('folder2_path', row.path); }});
            var bOpen = document.createElement('button');
            bOpen.type = 'button';
            bOpen.textContent = 'Open in Explorer';
            bOpen.style.padding = '6px 10px';
            bOpen.style.fontSize = '12px';
            bOpen.style.fontWeight = '600';
            bOpen.style.border = '0';
            bOpen.style.borderRadius = '8px';
            bOpen.style.cursor = 'pointer';
            bOpen.style.color = '#fff';
            bOpen.style.background = '#059669';
            bOpen.addEventListener('click', function () {{
              openPathInExplorer(row.path).then(function (res) {{
                if (res.ok && res.data && res.data.ok) {{
                  setStatus('Opened in Windows Explorer: ' + row.path, false);
                  return;
                }}
                if (res.status === 403 || res.status === 501 || (res.data && res.data.fallback === 'copy')) {{
                  copyPathToClipboard(row.path).then(function () {{
                    setStatus('Path copied — paste into Explorer address bar (Win+E). ' + row.path, false);
                  }});
                  return;
                }}
                var err = (res.data && res.data.error) || 'Could not open folder.';
                copyPathToClipboard(row.path).then(function () {{
                  setStatus(err + ' Path also copied: ' + row.path, true);
                }});
              }}).catch(function () {{
                copyPathToClipboard(row.path).then(function () {{
                  setStatus('Network error — path copied: ' + row.path, true);
                }});
              }});
            }});
            tdA.appendChild(bOld);
            tdA.appendChild(bNew);
            tdA.appendChild(bOpen);
            tr.appendChild(tdL);
            tr.appendChild(tdEnv);
            tr.appendChild(tdP);
            tr.appendChild(tdA);
            tbody.appendChild(tr);
          }});
          wrap.style.display = 'block';
          if (toolbar) toolbar.style.display = omniBaseRows.length ? 'block' : 'none';
          setMinimized(false);
          if (viewTitle) viewTitle.textContent = 'OMNI base paths (' + omniBaseRows.length + ' versions)';
          setStatus('Loaded ' + omniBaseRows.length + ' OMNI path(s).', false);
        }});
      }})();

      // Session-based progress tracking for multi-user support
      // Each browser session gets a unique ID
      let progressInterval = null;
      let comparisonRunning = false;
      let currentSessionId = null;
      let comparisonRunTab = null;

      function reportAbsoluteUrl(url) {{
        if (!url) return '';
        var abs = String(url);
        if (abs.charAt(0) === '/') abs = window.location.origin + abs;
        return abs;
      }}

      function comparisonRunsInOtherTab() {{
        try {{ return sessionStorage.getItem('comparisonRunsInOtherTab') === '1'; }} catch (e) {{ return false; }}
      }}

      function setComparisonRunsInOtherTab(on) {{
        try {{
          if (on) sessionStorage.setItem('comparisonRunsInOtherTab', '1');
          else sessionStorage.removeItem('comparisonRunsInOtherTab');
        }} catch (e) {{}}
      }}

      function hideComparisonDelegatedStatus() {{
        var delegatedBanner = document.getElementById('comparison-delegated-banner');
        if (delegatedBanner) delegatedBanner.style.display = 'none';
      }}

      function showComparisonDelegatedStatus(runUrl) {{
        hideComparisonDelegatedStatus();
        var delegatedBanner = document.getElementById('comparison-delegated-banner');
        var delegatedLink = document.getElementById('comparison-delegated-link');
        if (delegatedBanner) delegatedBanner.style.display = 'block';
        if (delegatedLink && runUrl) delegatedLink.href = runUrl;
        var progressContainer = document.getElementById('progress-container');
        if (progressContainer) progressContainer.style.display = 'none';
        var blockedBanner = document.getElementById('comparison-popup-blocked-banner');
        if (blockedBanner) blockedBanner.style.display = 'none';
      }}

      function storeComparisonRunUrl(runUrl) {{
        try {{
          if (runUrl) sessionStorage.setItem('comparisonRunUrl', runUrl);
          else sessionStorage.removeItem('comparisonRunUrl');
        }} catch (e) {{}}
      }}

      function getStoredComparisonRunUrl(sessionId) {{
        try {{
          var stored = sessionStorage.getItem('comparisonRunUrl') || '';
          if (stored) return stored;
        }} catch (e) {{}}
        return sessionId ? ('/run?session_id=' + encodeURIComponent(sessionId)) : '';
      }}

      function delegateComparisonToOtherTab(runUrl) {{
        setComparisonRunsInOtherTab(true);
        storeComparisonRunUrl(runUrl);
        stopProgressTracking();
        showComparisonDelegatedStatus(runUrl);
      }}

      function openReportWhenReady(url) {{
        if (comparisonRunsInOtherTab()) return false;
        var abs = reportAbsoluteUrl(url);
        if (!abs) return false;
        if (comparisonRunTab && !comparisonRunTab.closed) {{
          try {{
            comparisonRunTab.location.href = abs;
            comparisonRunTab.focus();
            return true;
          }} catch (e) {{}}
        }}
        var w = null;
        try {{ w = window.open(abs, '_blank'); }} catch (e2) {{}}
        if (w) {{
          try {{ w.opener = null; w.focus(); }} catch (e3) {{}}
          return true;
        }}
        window.location.href = abs;
        return true;
      }}
      
      // Generate unique session ID for this browser tab
      function generateSessionId() {{
        return 'sess_' + Math.random().toString(36).substr(2, 9) + '_' + Date.now().toString(36);
      }}
      
      // Get or create session ID (stored in sessionStorage for tab isolation)
      function getSessionId() {{
        if (!currentSessionId) {{
          currentSessionId = sessionStorage.getItem('comparisonSessionId');
          if (!currentSessionId) {{
            currentSessionId = generateSessionId();
            sessionStorage.setItem('comparisonSessionId', currentSessionId);
          }}
        }}
        return currentSessionId;
      }}

      // Active comparison for this tab only (set when a comparison is started)
      function getActiveComparisonSessionId() {{
        return sessionStorage.getItem('activeComparisonSessionId')
          || sessionStorage.getItem('comparisonSessionId')
          || '';
      }}

      function setActiveComparisonSessionId(sessionId) {{
        if (!sessionId) return;
        currentSessionId = sessionId;
        sessionStorage.setItem('comparisonSessionId', sessionId);
        sessionStorage.setItem('activeComparisonSessionId', sessionId);
      }}
      
      // Generate new session ID for new comparison
      function newSession() {{
        currentSessionId = generateSessionId();
        setActiveComparisonSessionId(currentSessionId);
        setComparisonRunsInOtherTab(false);
        storeComparisonRunUrl('');
        hideComparisonDelegatedStatus();
        return currentSessionId;
      }}
      
      // Console Output Modal Functions
      let consoleAutoRefreshInterval = null;
      let consoleAutoRefreshEnabled = false;
      
      function openConsoleModal() {{
        document.getElementById('console-modal').style.display = 'block';
        document.body.style.overflow = 'hidden';
        refreshConsoleLogs();
      }}
      
      function closeConsoleModal() {{
        document.getElementById('console-modal').style.display = 'none';
        document.body.style.overflow = 'auto';
        if (consoleAutoRefreshInterval) {{
          clearInterval(consoleAutoRefreshInterval);
          consoleAutoRefreshInterval = null;
        }}
        consoleAutoRefreshEnabled = false;
        document.getElementById('auto-refresh-btn').textContent = '▶️ Auto';
        document.getElementById('auto-refresh-btn').style.background = '#8b5cf6';
      }}
      
      function refreshConsoleLogs() {{
        const sessionId = getActiveComparisonSessionId();
        console.log('[Console] Fetching logs for session:', sessionId);
        document.getElementById('console-session-id').textContent = 'Session: ' + (sessionId || 'none');
        if (!sessionId) {{
          document.getElementById('console-output').innerHTML = '<div style="color:#6b7280; text-align:center; padding:40px;">'
            + '<p>No active comparison session in this browser tab.</p>'
            + '<p style="font-size:12px; margin-top:10px;">Start a comparison to view console output for your session only.</p>'
            + '</div>';
          document.getElementById('console-line-count').textContent = '0 lines';
          return;
        }}
        
        fetch('/logs?session_id=' + encodeURIComponent(sessionId))
          .then(response => {{
            console.log('[Console] Response status:', response.status);
            if (response.status === 403) {{
              return {{
                logs: [],
                total_lines: 0,
                error: 'Access denied: this session belongs to another user.',
                timestamp: new Date().toLocaleString()
              }};
            }}
            return response.json();
          }})
          .then(data => {{
            console.log('[Console] Received data:', data);
            const outputDiv = document.getElementById('console-output');
            const errorBanner = document.getElementById('console-error-banner');
            
            // Show error banner if there's an error
            if (data.error) {{
              errorBanner.style.display = 'block';
              document.getElementById('console-error-msg').textContent = data.error;
            }} else {{
              errorBanner.style.display = 'none';
            }}
            
            // Update line count
            document.getElementById('console-line-count').textContent = data.total_lines + ' lines';
            
            // Update timestamp
            document.getElementById('console-timestamp').textContent = 'Last updated: ' + data.timestamp;
            
            // Display logs with syntax highlighting
            if (data.logs && data.logs.length > 0) {{
              let html = '';
              data.logs.forEach(line => {{
                let colorClass = '#d4d4d4';
                if (line.includes('[ERROR]') || line.includes('ERROR') || line.includes('Error:') || line.includes('Traceback')) {{
                  colorClass = '#ef4444';
                }} else if (line.includes('[WARNING]') || line.includes('WARNING') || line.includes('Warning:')) {{
                  colorClass = '#f59e0b';
                }} else if (line.includes('[DEBUG]')) {{
                  colorClass = '#60a5fa';
                }} else if (line.includes('[INFO]') || line.includes('✓') || line.includes('Complete') || line.includes('Success')) {{
                  colorClass = '#22c55e';
                }} else if (line.includes('Found') || line.includes('Scanning') || line.includes('Processing')) {{
                  colorClass = '#a78bfa';
                }}
                html += '<div style="color:' + colorClass + '; padding:2px 0; border-bottom:1px solid #333;">' + escapeHtml(line) + '</div>';
              }});
              outputDiv.innerHTML = html;
              // Auto-scroll to bottom
              outputDiv.scrollTop = outputDiv.scrollHeight;
            }} else {{
              outputDiv.innerHTML = '<div style="color:#6b7280; text-align:center; padding:40px;">' +
                '<p>No console output yet for your session: <strong>' + (sessionId || 'none') + '</strong></p>' +
                '<p style="font-size:12px; margin-top:10px;">Logs appear here only for comparisons started in this browser tab.</p>' +
                '</div>';
            }}
          }})
          .catch(err => {{
            document.getElementById('console-output').innerHTML = '<div style="color:#ef4444; padding:20px;">Failed to load logs: ' + err.message + '</div>';
          }});
      }}
      
      function clearConsoleLogs() {{
        const sessionId = getActiveComparisonSessionId();
        if (!sessionId) return;
        fetch('/logs?session_id=' + encodeURIComponent(sessionId) + '&clear=1')
          .then(() => refreshConsoleLogs());
      }}
      
      function downloadConsoleLogs() {{
        const sessionId = getActiveComparisonSessionId();
        if (!sessionId) return;
        fetch('/logs?session_id=' + encodeURIComponent(sessionId))
          .then(response => response.json())
          .then(data => {{
            const content = data.logs.join('\\n');
            const blob = new Blob([content], {{ type: 'text/plain' }});
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = 'console_' + sessionId + '_' + new Date().toISOString().slice(0,19).replace(/[:]/g, '-') + '.log';
            a.click();
            URL.revokeObjectURL(url);
          }});
      }}
      
      function toggleAutoRefresh() {{
        const btn = document.getElementById('auto-refresh-btn');
        if (consoleAutoRefreshEnabled) {{
          clearInterval(consoleAutoRefreshInterval);
          consoleAutoRefreshInterval = null;
          consoleAutoRefreshEnabled = false;
          btn.textContent = '▶️ Auto';
          btn.style.background = '#8b5cf6';
        }} else {{
          consoleAutoRefreshEnabled = true;
          consoleAutoRefreshInterval = setInterval(refreshConsoleLogs, 2000);
          btn.textContent = '⏸️ Auto';
          btn.style.background = '#059669';
          refreshConsoleLogs();
        }}
      }}
      
      function escapeHtml(text) {{
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
      }}
      
      // Close modal on Escape key
      document.addEventListener('keydown', function(e) {{
        if (e.key === 'Escape' && document.getElementById('console-modal').style.display !== 'none') {{
          closeConsoleModal();
        }}
        if (e.key === 'Escape' && document.getElementById('copy-logs-modal').style.display !== 'none') {{
          closeCopyLogsModal();
        }}
      }});
      
      // Copy Logs Modal Functions
      let copyAutoRefreshInterval = null;
      let copyAutoRefreshEnabled = false;
      
      function showCopyLogs() {{
        document.getElementById('copy-logs-modal').style.display = 'block';
        document.body.style.overflow = 'hidden';
        refreshCopyLogs();
      }}
      
      function closeCopyLogsModal() {{
        document.getElementById('copy-logs-modal').style.display = 'none';
        document.body.style.overflow = 'auto';
        if (copyAutoRefreshInterval) {{
          clearInterval(copyAutoRefreshInterval);
          copyAutoRefreshInterval = null;
        }}
        copyAutoRefreshEnabled = false;
        document.getElementById('copy-auto-refresh-btn').textContent = '▶️ Auto';
        document.getElementById('copy-auto-refresh-btn').style.background = '#8b5cf6';
      }}
      
      function refreshCopyLogs() {{
        console.log('[Copy Logs] Fetching logs from /copy_logs');
        
        fetch('/copy_logs')
          .then(response => response.json())
          .then(data => {{
            console.log('[Copy Logs] Received:', data);
            const outputDiv = document.getElementById('copy-logs-output');
            
            // Update session and line count
            document.getElementById('copy-logs-session-id').textContent = 'Session: ' + (data.session_id || 'none');
            document.getElementById('copy-logs-line-count').textContent = (data.total_lines || 0) + ' lines';
            document.getElementById('copy-logs-timestamp').textContent = 'Last updated: ' + (data.timestamp || new Date().toLocaleTimeString());
            
            if (data.logs && data.logs.length > 0) {{
              let html = '<div style="background:#1a365d; color:#90cdf4; padding:10px; margin-bottom:10px; border-radius:6px; font-weight:bold;">📦 COPY OPERATION LOGS</div>';
              data.logs.forEach(function(line) {{
                let color = '#d4d4d4';
                if (line.indexOf('[ERROR]') >= 0 || line.indexOf('ERROR') >= 0 || line.indexOf('FAILED') >= 0) color = '#ef4444';
                else if (line.indexOf('[WARN]') >= 0 || line.indexOf('WARNING') >= 0 || line.indexOf('Skipped') >= 0) color = '#f59e0b';
                else if (line.indexOf('[INFO]') >= 0 || line.indexOf('SUCCESS') >= 0 || line.indexOf('COMPLETED') >= 0) color = '#22c55e';
                else if (line.indexOf('===') >= 0 || line.indexOf('---') >= 0) color = '#60a5fa';
                html += '<div style="color:' + color + ';">' + escapeHtml(line) + '</div>';
              }});
              outputDiv.innerHTML = html;
              outputDiv.scrollTop = outputDiv.scrollHeight;
            }} else {{
              outputDiv.innerHTML = '<div style="color:#6b7280; text-align:center; padding:40px;">' +
                '<p>No copy logs available yet.</p>' +
                '<p style="font-size:12px; margin-top:10px;">Copy operation logs will appear here after you perform a copy.</p>' +
                '</div>';
            }}
          }})
          .catch(err => {{
            document.getElementById('copy-logs-output').innerHTML = '<div style="color:#ef4444; padding:20px;">Error fetching copy logs: ' + err.message + '</div>';
          }});
      }}
      
      function downloadCopyLogs() {{
        fetch('/copy_logs')
          .then(response => response.json())
          .then(data => {{
            const content = (data.logs || []).join('\\n');
            const blob = new Blob([content], {{ type: 'text/plain' }});
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = 'copy_logs_' + (data.session_id || 'unknown') + '_' + new Date().toISOString().slice(0,19).replace(/[:]/g, '-') + '.log';
            a.click();
            URL.revokeObjectURL(url);
          }});
      }}
      
      function toggleCopyAutoRefresh() {{
        const btn = document.getElementById('copy-auto-refresh-btn');
        if (copyAutoRefreshEnabled) {{
          clearInterval(copyAutoRefreshInterval);
          copyAutoRefreshInterval = null;
          copyAutoRefreshEnabled = false;
          btn.textContent = '▶️ Auto';
          btn.style.background = '#8b5cf6';
        }} else {{
          copyAutoRefreshEnabled = true;
          copyAutoRefreshInterval = setInterval(refreshCopyLogs, 1000);
          btn.textContent = '⏸️ Auto';
          btn.style.background = '#059669';
          refreshCopyLogs();
        }}
      }}
      
      function startProgressTracking() {{
        if (comparisonRunsInOtherTab()) return;
        hideComparisonDelegatedStatus();
        document.getElementById('progress-container').style.display = 'block';
        comparisonRunning = true;
        
        var envOpen = document.getElementById('env_path_toggle') && document.getElementById('env_path_toggle').checked;
        if (!envOpen) {{
          document.getElementById('progress-container').scrollIntoView({{ behavior: 'smooth', block: 'center' }});
        }}
        
        progressInterval = setInterval(updateProgress, 1000); // Update every second
      }}
      
      function stopProgressTracking() {{
        if (progressInterval) {{
          clearInterval(progressInterval);
          progressInterval = null;
        }}
        comparisonRunning = false;
      }}
      
      function updateProgress() {{
        if (comparisonRunsInOtherTab()) {{
          stopProgressTracking();
          return;
        }}
        const sessionId = getActiveComparisonSessionId();
        if (!sessionId) return;
        fetch('/progress?session_id=' + encodeURIComponent(sessionId))
          .then(response => response.json())
          .then(data => {{
            if (data.result_url) {{
              stopProgressTracking();
              document.getElementById('progress-status').textContent = 'Complete — opening report...';
              document.getElementById('progress-bar').style.width = '100%';
              document.getElementById('progress-text').textContent = '100%';
              openReportWhenReady(data.result_url);
              return;
            }}
            if (!data.running && comparisonRunning) {{
              if (!data.session_registered) {{
                return;
              }}
              stopProgressTracking();
              if (!data.error) {{
                document.getElementById('progress-status').textContent = 'Complete — waiting for report...';
                document.getElementById('progress-bar').style.width = '100%';
                document.getElementById('progress-text').textContent = '100%';
                openReportWhenReady('/open-report?session_id=' + encodeURIComponent(sessionId));
              }}
              return;
            }}
            
            // Update progress bar
            const percentage = data.percentage || 0;
            document.getElementById('progress-bar').style.width = percentage + '%';
            document.getElementById('progress-text').textContent = percentage.toFixed(1) + '%';
            
            // Update status
            document.getElementById('progress-status').textContent = data.running ? 'Running...' : 'Complete';
            document.getElementById('progress-files').textContent = data.progress + ' / ' + data.total;
            document.getElementById('progress-current').textContent = data.current_file || '-';
            document.getElementById('progress-eta').textContent = data.eta_formatted || 'Calculating...';
            
            // Show error if any
            if (data.error) {{
              document.getElementById('progress-error').style.display = 'block';
              document.getElementById('progress-error-msg').textContent = data.error;
              stopProgressTracking();
            }}
          }})
          .catch(err => {{
            console.error('Progress update failed:', err);
          }});
      }}
      
      function hideComparisonPopupBlockedBanner() {{
        var blockedBanner = document.getElementById('comparison-popup-blocked-banner');
        if (blockedBanner) blockedBanner.style.display = 'none';
      }}

      function showComparisonPopupBlockedBanner(runUrl) {{
        var blockedBanner = document.getElementById('comparison-popup-blocked-banner');
        var blockedLink = document.getElementById('comparison-popup-blocked-link');
        if (blockedBanner && blockedLink) {{
          blockedLink.href = runUrl;
          blockedLink.onclick = function() {{
            delegateComparisonToOtherTab(runUrl);
          }};
          blockedBanner.style.display = 'block';
        }} else {{
          alert('Allow pop-ups to open comparison progress in a new tab, or open manually:\\n' + runUrl);
        }}
      }}

      function openComparisonRunTabOnClick() {{
        // Must run synchronously on the submit click; async window.open often returns null
        // even when a tab opens (especially with noopener), which falsely triggers the banner.
        var comparisonRunTab = null;
        try {{ comparisonRunTab = window.open('about:blank', '_blank'); }} catch (e) {{}}
        if (!comparisonRunTab) return null;
        try {{
          comparisonRunTab.document.write(
            '<!DOCTYPE html><html><head><meta charset="utf-8"><title>Starting comparison</title></head>'
            + '<body style="font-family:Segoe UI,sans-serif;padding:24px;color:#334155;">'
            + '<p>Starting comparison…</p></body></html>'
          );
          comparisonRunTab.document.close();
        }} catch (e2) {{}}
        try {{ comparisonRunTab.opener = null; }} catch (e3) {{}}
        hideComparisonPopupBlockedBanner();
        return comparisonRunTab;
      }}

      function navigateComparisonRunTab(comparisonRunTab, runUrl) {{
        if (!comparisonRunTab) {{
          showComparisonPopupBlockedBanner(runUrl);
          return;
        }}
        hideComparisonPopupBlockedBanner();
        try {{
          comparisonRunTab.location.href = runUrl;
          comparisonRunTab.focus();
          delegateComparisonToOtherTab(runUrl);
        }} catch (e) {{
          showComparisonPopupBlockedBanner(runUrl);
        }}
      }}

      function resumeBackgroundComparisonIfNeeded() {{
        var sid = sessionStorage.getItem('activeComparisonSessionId')
          || sessionStorage.getItem('comparisonSessionId') || '';
        if (!sid || progressInterval) return;
        if (comparisonRunsInOtherTab()) {{
          fetch('/progress?session_id=' + encodeURIComponent(sid))
            .then(function(r) {{ return r.json(); }})
            .then(function(data) {{
              if (data && (data.worker_started || data.running || data.result_url)) {{
                showComparisonDelegatedStatus(getStoredComparisonRunUrl(sid));
              }} else {{
                setComparisonRunsInOtherTab(false);
                storeComparisonRunUrl('');
              }}
            }})
            .catch(function() {{}});
          return;
        }}
        fetch('/progress?session_id=' + encodeURIComponent(sid))
          .then(function(r) {{ return r.json(); }})
          .then(function(data) {{
            if (data && data.running) {{
              comparisonRunning = true;
              startProgressTracking();
            }}
          }})
          .catch(function() {{}});
      }}
      
      // Override form submission to track progress and add session ID
      const compareForm = document.querySelector('form[action=\"/run\"]');
      if (compareForm) {{
        compareForm.addEventListener('submit', function(e) {{
          e.preventDefault();
          
          // Generate new session ID for this comparison
          const sessionId = newSession();
          
          // Add session_id to form action URL
          const formData = new FormData(compareForm);
          const params = new URLSearchParams(formData);
          params.append('session_id', sessionId);
          var comparisonRunTab = openComparisonRunTabOnClick();
          var runUrl = '/run?' + params.toString();

          if (!comparisonRunTab) {{
            showComparisonPopupBlockedBanner(runUrl);
            return;
          }}

          // Navigate immediately — do not wait for disk-space scan (NAS folder walks can take minutes).
          navigateComparisonRunTab(comparisonRunTab, runUrl);

          fetch('/api/comparison-space-check?quick=1&' + params.toString())
            .then(function(r) {{ return r.json(); }})
            .catch(function() {{}});
        }});
      }}

      // Copy progress tracking - use var for global scope
      var copyProgressInterval = null;
      var copyRunning = false;
      
      // Make formatBytes global
      window.formatBytes = function(bytes) {{
        if (bytes === 0) return '0 B';
        var k = 1024;
        var sizes = ['B', 'KB', 'MB', 'GB'];
        var i = Math.floor(Math.log(bytes) / Math.log(k));
        return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
      }};
      
      // Make startCopyProgressTracking global
      window.startCopyProgressTracking = function() {{
        var root = document.getElementById('copy-progress-container');
        if (!root) return;
        var errEl = document.getElementById('copy-progress-error');
        var okEl = document.getElementById('copy-progress-success');
        var warnEl = document.getElementById('copy-progress-warning');
        var listEl = document.getElementById('copy-progress-skipped-list');
        var bar = document.getElementById('copy-progress-bar');
        var pct = document.getElementById('copy-progress-text');
        var st = document.getElementById('copy-progress-status');
        var copyBtn = document.getElementById('copy-btn');
        if (errEl) errEl.style.display = 'none';
        if (okEl) okEl.style.display = 'none';
        if (warnEl) warnEl.style.display = 'none';
        if (listEl) listEl.innerHTML = '';
        if (bar) bar.style.width = '0%';
        if (pct) pct.textContent = '0%';
        if (st) st.textContent = 'Starting...';
        if (copyBtn) {{ copyBtn.disabled = true; copyBtn.textContent = 'Copying...'; }}
        root.style.display = 'block';
        copyRunning = true;
        
        root.scrollIntoView({{ behavior: 'smooth', block: 'center' }});
        
        copyProgressInterval = setInterval(window.updateCopyProgress, 500); // Update every 500ms
      }};
      
      // Make stopCopyProgressTracking global
      window.stopCopyProgressTracking = function() {{
        if (copyProgressInterval) {{
          clearInterval(copyProgressInterval);
          copyProgressInterval = null;
        }}
        copyRunning = false;
        var copyBtn = document.getElementById('copy-btn');
        if (copyBtn) {{ copyBtn.disabled = false; copyBtn.textContent = 'Copy'; }}
      }};
      
      // Make updateCopyProgress global
      window.updateCopyProgress = function() {{
        fetch('/copy_progress')
          .then(response => response.json())
          .then(data => {{
            if (!document.getElementById('copy-progress-container')) return;
            if (!data.running && copyRunning) {{
              // Copy finished
              window.stopCopyProgressTracking();
              
              // Show warnings about skipped files
              if (data.skipped_count > 0 || (data.failed_files && data.failed_files.length > 0)) {{
                var warningDiv = document.getElementById('copy-progress-warning');
                warningDiv.style.display = 'block';
                document.getElementById('copy-progress-skipped-count').textContent = data.skipped_count || data.failed_files.length;
                
                // Show list of failed files
                var listDiv = document.getElementById('copy-progress-skipped-list');
                if (data.failed_files && data.failed_files.length > 0) {{
                  var html = '<ul style="margin:0;padding-left:20px;">';
                  data.failed_files.slice(0, 10).forEach(function(f) {{
                    html += '<li>' + (Array.isArray(f) ? f[0] + ' - ' + f[1] : f) + '</li>';
                  }});
                  if (data.failed_files.length > 10) {{
                    html += '<li>... and ' + (data.failed_files.length - 10) + ' more</li>';
                  }}
                  html += '</ul>';
                  listDiv.innerHTML = html;
                }}
              }}
              
              if (data.error && !data.message) {{
                document.getElementById('copy-progress-error').style.display = 'block';
                document.getElementById('copy-progress-error-msg').textContent = data.error;
              }} else {{
                document.getElementById('copy-progress-status').textContent = 'Complete!';
                document.getElementById('copy-progress-bar').style.width = '100%';
                document.getElementById('copy-progress-text').textContent = '100%';
                document.getElementById('copy-progress-success').style.display = 'block';
                document.getElementById('copy-progress-success-msg').textContent = data.message || 'Copy completed successfully!';
              }}
              return;
            }}
            
            // Update progress bar
            const percentage = data.percentage || 0;
            document.getElementById('copy-progress-bar').style.width = percentage + '%';
            document.getElementById('copy-progress-text').textContent = percentage.toFixed(1) + '%';
            
            // Update status
            document.getElementById('copy-progress-status').textContent = data.running ? 'Copying...' : 'Complete';
            document.getElementById('copy-progress-files').textContent = data.progress + ' / ' + data.total;
            document.getElementById('copy-progress-bytes').textContent = formatBytes(data.bytes_copied || 0) + ' / ' + formatBytes(data.total_bytes || 0);
            document.getElementById('copy-progress-current').textContent = data.current_file || '-';
            document.getElementById('copy-progress-eta').textContent = data.eta_formatted || 'Calculating...';
            
            // Show skipped count in real-time
            if (data.skipped_count > 0) {{
              document.getElementById('copy-progress-warning').style.display = 'block';
              document.getElementById('copy-progress-skipped-count').textContent = data.skipped_count;
            }}
            
            // Show error if fatal
            if (data.error && !data.running) {{
              document.getElementById('copy-progress-error').style.display = 'block';
              document.getElementById('copy-progress-error-msg').textContent = data.error;
              window.stopCopyProgressTracking();
            }}
          }})
          .catch(err => {{
            console.error('Copy progress update failed:', err);
          }});
      }};
      
      // Handle copy button click - make it global so onclick can call it
      window.handleCopyClick = function() {{
        if (!document.getElementById('copy-form')) return;
        console.log('Copy button clicked!');
        
        // Get folder paths from main form
        var f1 = document.querySelector('input[name=\"folder1_path\"]');
        var f2 = document.querySelector('input[name=\"folder2_path\"]');
        var cf1 = document.getElementById('copy-folder1');
        var cf2 = document.getElementById('copy-folder2');
        if (cf1) cf1.value = f1 ? f1.value : '';
        if (cf2) cf2.value = f2 ? f2.value : '';
        
        // Validate required fields
        var destPath = document.querySelector('input[name=\"dest_path\"]').value;
        var folder1 = document.getElementById('copy-folder1').value;
        var folder2 = document.getElementById('copy-folder2').value;
        var copySource = document.querySelector('input[name=\"copy_source\"]:checked').value;
        var srcPath = copySource === 'folder1' ? folder1 : folder2;
        
        if (!srcPath) {{
          alert('Please enter a source path in the comparison section above');
          return;
        }}
        if (!destPath) {{
          alert('Please enter a destination path');
          return;
        }}
        
        console.log('Starting copy progress tracking...');
        // Start progress tracking
        window.startCopyProgressTracking();
        
        // Submit via fetch to allow async copy
        const copyForm = document.getElementById('copy-form');
        const formData = new FormData(copyForm);
        console.log('Sending copy request to /copy_async...');
        fetch('/copy_async', {{
          method: 'POST',
          body: new URLSearchParams(formData)
        }})
        .then(response => {{
          console.log('Copy request response:', response.status);
          return response.json();
        }})
        .then(data => {{
          console.log('Copy started:', data);
        }})
        .catch(err => {{
          console.error('Copy request failed:', err);
          var pe = document.getElementById('copy-progress-error');
          var pem = document.getElementById('copy-progress-error-msg');
          if (pe) pe.style.display = 'block';
          if (pem) pem.textContent = 'Failed to start copy: ' + err.message;
          window.stopCopyProgressTracking();
        }});
      }};
    </script>
    """
    from enterprise_security import client_access_token_bootstrap_js

    body = body.replace(_SEC_BOOT, client_access_token_bootstrap_js())
    return html_page("Build Verification (NAS) and Environment Validation (HOST IP) Automation Tool— ASCF/CRM/AMSS/UXF/OMNI/DEX", body)


def list_reports(machine_host: str = "", limit: int | None = MAX_BROWSE_REPORTS):
    """Latest comparison reports on the client workstation (and legacy server copies)."""
    report_dir = resolve_report_save_dir(machine_host, PROJECT_ROOT)
    dirs = [report_dir]
    if report_dir.resolve() != PROJECT_ROOT.resolve():
        dirs.append(PROJECT_ROOT)
    return list_report_files(dirs, limit=limit)


def cleanup_old_reports(max_reports: int = 10) -> int:
    """Reports persist on the user's PC until manually deleted."""
    return 0


def _configure_enhanced_full_tree_comparer(
    comparer,
    *,
    class_diff_jar: str = "",
    manifest_diff_jar: str = "",
    config_files_filter: str = "",
    explicit_size_paths: str = "",
    use_local_copy: bool = False,
    keep_temp_folders: bool = False,
    max_retries: int = 3,
    retry_delay: float = 1.0,
    suppress_warnings: bool = True,
    show_missing_only: bool = False,
) -> None:
    """Shared full-tree comparer options (CRM/AMSS and ASCF optional report fields)."""
    comparer.use_local_copy = use_local_copy
    comparer.max_retries = max_retries
    comparer.retry_delay = retry_delay
    comparer.should_cleanup_temp_folders = not keep_temp_folders
    comparer.suppress_warnings = suppress_warnings
    comparer.show_missing_only = show_missing_only
    comparer.ignore_timestamp_only_differences = True
    comparer.fast_mode = True
    comparer.compare_archive_contents = False
    comparer.deep_nested_archives = False
    comparer.compare_clfy_agent_zip = False
    comparer.run_full_tree_compare = True
    comparer.class_diff_jar_name = (class_diff_jar or "").strip()
    comparer.manifest_diff_jar_name = (manifest_diff_jar or "").strip()
    comparer.config_files_filter_query = (config_files_filter or "").strip()
    comparer.explicit_size_compare_query = (explicit_size_paths or "").strip()


def parse_run_comparison_qs(qs: dict) -> SimpleNamespace:
    """Parse /run query string into comparison options."""
    folder1 = normalize_build_path_for_windows(qs.get("folder1_path", [""])[0].strip())
    folder2 = normalize_build_path_for_windows(qs.get("folder2_path", [""])[0].strip())
    mode = qs.get("mode", ["full"])[0]
    if mode == "naming" or "naming_convention" in qs:
        mode = "naming"
    app_ascf = "app_ascf" in qs
    app_crm = "app_crm" in qs
    app_uxf = "app_uxf" in qs
    app_dex = "app_dex" in qs
    app_wef = "app_wef" in qs
    selected_apps = []
    if app_ascf:
        selected_apps.append("ASCF")
    if app_crm:
        selected_apps.append("CRM")
    if app_uxf:
        selected_apps.append("UXF/OMNI")
    if app_dex:
        selected_apps.append("DEX")
    if app_wef:
        selected_apps.append("WEF")
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
    crm_playbooks_nexus_previous = qs.get("crm_playbooks_nexus_previous", [""])[0].strip()
    crm_playbooks_nexus_latest = qs.get("crm_playbooks_nexus_latest", [""])[0].strip()
    crm_playbooks_nexus_pair = bool(crm_playbooks_nexus_previous and crm_playbooks_nexus_latest)
    crm_playbooks_nexus_mode = app_crm and crm_playbooks_nexus_pair
    wef_ssh_host = qs.get("wef_ssh_host", [""])[0].strip()
    wef_ssh_username = qs.get("wef_ssh_username", [""])[0].strip()
    wef_ssh_password = qs.get("wef_ssh_password", [""])[0]
    wef_old_path = qs.get("wef_old_path", [""])[0].strip()
    wef_new_path = qs.get("wef_new_path", [""])[0].strip()
    try:
        wef_ssh_port = int(qs.get("wef_ssh_port", ["22"])[0] or 22)
    except (ValueError, IndexError):
        wef_ssh_port = 22
    wef_mode = app_wef
    return SimpleNamespace(
        folder1=folder1,
        folder2=folder2,
        mode=mode,
        app_ascf=app_ascf,
        app_crm=app_crm,
        app_uxf=app_uxf,
        app_dex=app_dex,
        app_wef=app_wef,
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
        ascf_class_diff_jar=qs.get("ascf_class_diff_jar", [""])[0].strip(),
        ascf_manifest_diff_jar=qs.get("ascf_manifest_diff_jar", [""])[0].strip(),
        ascf_config_files_filter=qs.get("ascf_config_files_filter", [""])[0].strip(),
        ascf_explicit_size_paths=qs.get("ascf_explicit_size_paths", [""])[0].strip(),
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
        crm_playbooks_nexus_previous=crm_playbooks_nexus_previous,
        crm_playbooks_nexus_latest=crm_playbooks_nexus_latest,
        crm_playbooks_nexus_pair=crm_playbooks_nexus_pair,
        crm_playbooks_nexus_mode=crm_playbooks_nexus_mode,
        wef_ssh_host=wef_ssh_host,
        wef_ssh_username=wef_ssh_username,
        wef_ssh_password=wef_ssh_password,
        wef_ssh_port=wef_ssh_port,
        wef_old_path=wef_old_path,
        wef_new_path=wef_new_path,
        wef_mode=wef_mode,
    )


def _request_base_url_from_handler(handler) -> str:
    """Absolute origin for the current request (http vs https, correct Host/port)."""
    host = (handler.headers.get("Host") or "localhost").strip() or "localhost"
    is_tls = False
    try:
        import ssl

        conn = getattr(handler, "connection", None) or getattr(handler, "request", None)
        is_tls = isinstance(conn, ssl.SSLSocket)
    except Exception:
        is_tls = False
    if not is_tls:
        try:
            srv_port = int(getattr(handler.server, "server_address", (None, 0))[1] or 0)
            is_tls = srv_port == int(os.environ.get("HTTPS_PORT", "5443"))
        except (TypeError, ValueError):
            is_tls = False
    scheme = "https" if is_tls else "http"
    return f"{scheme}://{host}"


def _finalize_result_url(session_progress: dict, location: str) -> str:
    """Absolute redirect URL with report_user so HTTPS /view finds client-saved reports."""
    loc = str(location or "").strip()
    if not loc:
        return loc
    base = str(session_progress.get("request_base_url") or "").strip()
    if base and loc.startswith("/"):
        loc = base.rstrip("/") + loc
    ident = str(
        session_progress.get("client_identity")
        or session_progress.get("client_machine_host")
        or ""
    ).strip()
    if ident and "report_user=" not in loc:
        loc += ("&" if "?" in loc else "?") + "report_user=" + quote(ident)
    try:
        file_q = parse_qs(urlparse(loc).query).get("file", [""])[0]
    except Exception:
        file_q = ""
    if (
        file_q.startswith("crm_full_comparison_")
        and file_q.lower().endswith(".html")
        and "#" not in loc
    ):
        loc += "#class-level-details"
    return loc


def _set_session_report_result_url(session_progress: dict, html_name: str) -> None:
    """Store finalized report URL on the session (for /progress polling)."""
    name = str(html_name or "").strip()
    if not name or not session_progress:
        return
    session_progress["result_url"] = _finalize_result_url(
        session_progress,
        f"/view?file={quote(name)}",
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
            self.session_progress["result_url"] = _finalize_result_url(
                self.session_progress, str(value or "")
            )

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
    js_sid = _json.dumps(session_id or "")
    body = f"""
    <div style="max-width:720px;margin:40px auto;padding:24px;background:#fff;border-radius:12px;border:1px solid #e2e8f0;box-shadow:0 4px 20px rgba(0,0,0,0.06);">
      <h2 style="margin:0 0 12px 0;color:#0f172a;">Comparison running in background</h2>
      <p style="color:#475569;line-height:1.6;margin:0 0 16px 0;">
        This tab shows progress and will open the report automatically when the comparison finishes.
        You can keep working on the home page or
        <a href="/reports" target="_blank" rel="noopener noreferrer">browse reports</a> in another tab.
      </p>
      <p style="font-size:13px;color:#64748b;">Session: <code>{sid}</code></p>
      <p id="wait-space-info" style="font-size:12px;color:#475569;margin:8px 0;line-height:1.5;"></p>
      <div style="margin:16px 0;padding:14px;background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;">
        <div style="font-size:13px;font-weight:600;color:#334155;margin-bottom:10px;">Comparison progress</div>
        <div style="background:#e2e8f0;border-radius:10px;height:30px;position:relative;overflow:hidden;">
          <div id="wait-progress-bar" style="background:linear-gradient(90deg,#0ea5e9,#3b82f6);height:100%;width:0%;min-width:0;transition:width 0.35s ease;display:flex;align-items:center;justify-content:center;color:#fff;font-weight:700;font-size:13px;">
            <span id="wait-progress-bar-label">0%</span>
          </div>
        </div>
        <p id="wait-progress-text" style="font-size:14px;color:#334155;margin:10px 0 6px 0;">Starting…</p>
        <div style="font-size:12px;color:#64748b;line-height:1.6;">
          <div><strong>Files processed:</strong> <span id="wait-progress-files">0 / 0</span></div>
          <div><strong>Estimated time remaining:</strong> <span id="wait-progress-eta">Calculating…</span></div>
          <div style="margin-top:4px;"><strong>Current step:</strong></div>
          <div id="wait-current-file" style="font-family:Consolas,monospace;font-size:11px;word-break:break-all;color:#475569;"></div>
        </div>
      </div>
      <p style="margin:16px 0 0 0;"><a href="/">← Home</a></p>
    </div>
    <script>
    (function() {{
      var sessionId = {js_sid};
      try {{
        if (!sessionId) {{
          var qsSid = new URLSearchParams(window.location.search).get('session_id');
          if (qsSid) sessionId = qsSid;
        }}
        if (sessionId) {{
          sessionStorage.setItem('comparisonSessionId', sessionId);
          sessionStorage.setItem('activeComparisonSessionId', sessionId);
          sessionStorage.setItem('comparisonRunsInOtherTab', '1');
        }}
      }} catch (e) {{}}
      (function() {{
        var p = new URLSearchParams(window.location.search);
        var minGb = p.get('min_c_drive_gb');
        var freeGb = p.get('client_free_c_gb');
        var machine = p.get('client_machine');
        var el = document.getElementById('wait-space-info');
        if (!el) return;
        if (machine && minGb) {{
          el.innerHTML = 'Copying packages to <strong>' + machine + '</strong> C: drive '
            + '(min <strong>' + minGb + ' GB</strong> free required'
            + (freeGb ? ', currently <strong>' + freeGb + ' GB</strong> free' : '')
            + '). Remote \\\\inpnqbdnc04\\software is used only if your PC is full.';
        }}
      }})();
      function reportAbsoluteUrl(url) {{
        if (!url) return '';
        var abs = url;
        if (abs.charAt(0) === '/') abs = window.location.origin + abs;
        return abs;
      }}
      function navigateToReport(url) {{
        var abs = reportAbsoluteUrl(url);
        if (!abs || reportOpened) return;
        reportOpened = true;
        var el = document.getElementById('wait-progress-text');
        if (el) {{
          el.innerHTML = 'Complete — opening report… '
            + '<a href="' + abs.replace(/"/g, '&quot;') + '" style="color:#2563eb;">Open report manually</a>';
        }}
        try {{ window.location.replace(abs); }} catch (e) {{
          try {{ window.location.href = abs; }} catch (e2) {{}}
        }}
      }}
      function redirectViaOpenReport() {{
        var target = '/open-report?session_id=' + encodeURIComponent(sessionId);
        var el = document.getElementById('wait-progress-text');
        if (el) el.textContent = 'Complete — preparing report…';
        window.location.href = target;
      }}
      function updateWaitProgress(d) {{
        var pct = Number(d.percentage);
        if (!isFinite(pct) || pct < 0) pct = 0;
        if (pct > 100) pct = 100;
        var bar = document.getElementById('wait-progress-bar');
        var barLabel = document.getElementById('wait-progress-bar-label');
        var textEl = document.getElementById('wait-progress-text');
        var filesEl = document.getElementById('wait-progress-files');
        var etaEl = document.getElementById('wait-progress-eta');
        var curEl = document.getElementById('wait-current-file');
        var shown = pct > 0 ? pct : (d.running ? 3 : 0);
        if (bar) bar.style.width = shown + '%';
        if (barLabel) barLabel.textContent = pct.toFixed(1) + '%';
        if (textEl && !reportOpened) {{
          var line = (d.running ? 'Running: ' : 'Status: ') + pct.toFixed(1) + '%';
          if (d.current_file) line += ' — ' + d.current_file;
          textEl.textContent = line;
        }}
        if (filesEl) {{
          filesEl.textContent = String(d.progress || 0) + ' / ' + String(d.total || 0);
        }}
        if (etaEl) etaEl.textContent = d.eta_formatted || 'Calculating…';
        if (curEl) curEl.textContent = d.current_file || '—';
      }}
      var reportOpened = false;
      var idleCompletePolls = 0;
      var maxIdleCompletePolls = 120;
      function poll() {{
        fetch('/progress?session_id=' + encodeURIComponent(sessionId))
          .then(function(r) {{ return r.json(); }})
          .then(function(d) {{
            updateWaitProgress(d);
            if (d.result_url && !reportOpened) {{
              navigateToReport(d.result_url);
              return;
            }}
            if (!d.running && d.error) {{
              var errEl = document.getElementById('wait-progress-text');
              if (errEl) errEl.textContent = 'Error: ' + d.error;
              return;
            }}
            if (!d.running && !d.error && !d.result_url) {{
              idleCompletePolls += 1;
              if (idleCompletePolls <= maxIdleCompletePolls) {{
                var finEl = document.getElementById('wait-progress-text');
                if (finEl) finEl.textContent = 'Finishing report (' + idleCompletePolls + 's)…';
                setTimeout(poll, 1000);
                return;
              }}
              if (!reportOpened) {{
                reportOpened = true;
                redirectViaOpenReport();
              }}
              return;
            }}
            idleCompletePolls = 0;
            if (!reportOpened) setTimeout(poll, 1000);
          }})
          .catch(function() {{ setTimeout(poll, 2000); }});
      }}
      poll();
    }})();
    </script>
    """
    return html_page("Comparison in progress", body)


def _crm_find_jar_for_class_api(
    class_name: str,
    *,
    ssh_host: str = "",
    ssh_username: str = "",
    ssh_password: str = "",
    ssh_port: int = 22,
    ssh_remote_path: str = "",
    folder2: str = "",
) -> tuple[int, dict]:
    """Run class→JAR lookup on remote deployment via SSH grep; returns (http_status, json_body_dict)."""
    from ssh_environment_validate import find_jars_containing_class_ssh

    class_name = str(class_name or "").strip()
    if not class_name:
        return 400, {
            "ok": False,
            "error": "Enter a class name or FQCN (e.g. CreateCustomerAccountXB.class).",
            "jars": [],
        }

    host = str(ssh_host or "").strip()
    username = str(ssh_username or "").strip()
    password = ssh_password or ""
    remote_path = str(ssh_remote_path or "").strip()
    try:
        port = int(ssh_port or 22)
    except (TypeError, ValueError):
        port = 22

    if not host and folder2:
        return 400, {
            "ok": False,
            "error": (
                "Find JAR now searches the remote deployment via SSH. "
                "Fill Host, Username, Password, and Remote path in the Environment Validation section "
                "(e.g. 10.76.48.68 / pcicrm1)."
            ),
            "jars": [],
        }

    result = find_jars_containing_class_ssh(
        host,
        username,
        password,
        remote_path,
        class_name,
        port=port,
    )
    status = 200 if result.get("ok") or result.get("error") else 500
    if not result.get("ok") and result.get("error"):
        status = 400 if "required" in str(result["error"]).lower() else 200
    return status, result


class AppHandler(BaseHTTPRequestHandler):

    def _client_host(self) -> str:
        return (self.client_address[0] or "").strip()

    def _is_https(self) -> bool:
        try:
            return isinstance(self.connection, ssl.SSLSocket)
        except Exception:
            return False

    def _enforce_security(self, method: str, parsed) -> bool:
        from enterprise_security import check_request_access, write_access_denied_response

        allowed, message, status = check_request_access(
            self._client_host(),
            parsed.path,
            parsed.query,
            method,
            {k: v for k, v in self.headers.items()},
            is_https=self._is_https(),
        )
        if not allowed:
            write_access_denied_response(self, message, status)
            return False
        return True

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        super().end_headers()
    
    def _generate_comparison_html_table(self, folder1, folder2, archives1, archives2, only_in_1, only_in_2, in_both, truly_new_in_1, truly_new_in_2, get_base_name_func):
        """Generate HTML comparison table view for any comparison type."""
        from pathlib import Path as PathLib
        
        # Get display names for the folders/files
        try:
            name1 = PathLib(folder1).name
            name2 = PathLib(folder2).name
        except:
            name1 = str(folder1).split('\\')[-1] if '\\' in str(folder1) else str(folder1).split('/')[-1]
            name2 = str(folder2).split('\\')[-1] if '\\' in str(folder2) else str(folder2).split('/')[-1]
        
        html_content = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Comparison Report - {name1} vs {name2}</title>
    <style>"""
        
        # Include all the CSS from the zip file comparison
        html_content += self._get_comparison_table_css()
        
        html_content += f"""
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div style="margin-bottom: 16px;">
                <a href="/" style="color: #fff; text-decoration: none; background: rgba(255,255,255,0.2); padding: 8px 16px; border-radius: 6px; display: inline-block; font-size: 14px; transition: all 0.2s;">
                    ← Back to Home
                </a>
            </div>
            <h1>📊 Naming Convention Comparison Report</h1>
            <div class="subtitle">Side-by-Side File Comparison</div>
            <div class="paths">
                <div class="path-label">📁 Old Build Path:</div>
                <div class="path-value">{html.escape(folder1)}</div>
                <div class="path-label">📁 New Build Path:</div>
                <div class="path-value">{html.escape(folder2)}</div>
            </div>
        </div>
        
        <div class="content">
            <!-- Navigation Menu -->
            <div class="nav-menu">
                <h3>📑 Quick Navigation</h3>
                <div class="nav-links">"""
        
        if truly_new_in_2 or truly_new_in_1:
            html_content += f"""
                    <a href="#additional-files" class="nav-link new">🆕 Additional/New Files ({len(truly_new_in_2) + len(truly_new_in_1)})</a>"""
        
        html_content += f"""
                    <a href="#comparison-table" class="nav-link" style="border-left-color: #9f7aea;">📊 Detailed Comparison Table</a>
                    <a href="#missing-folder2" class="nav-link missing">🗑️ Removed in New Build ({len(only_in_1)})</a>
                    <a href="#missing-folder1" class="nav-link added">✨ Added in New Build ({len(only_in_2)})</a>
                    <a href="#exact-matches" class="nav-link matches">✓ Exact Matches ({len(in_both)})</a>
                </div>
            </div>
            
            <div class="summary">
                <div class="stat-card info">
                    <div class="label">Total Files (Old Build)</div>
                    <div class="value">{len(archives1)}</div>
                </div>
                <div class="stat-card info">
                    <div class="label">Total Files (New Build)</div>
                    <div class="value">{len(archives2)}</div>
                </div>
                <div class="stat-card success">
                    <div class="label">Exact Matches</div>
                    <div class="value">{len(in_both)}</div>
                </div>
                <div class="stat-card warning">
                    <div class="label">Differences Found</div>
                    <div class="value">{len(only_in_1) + len(only_in_2)}</div>
                </div>
            </div>"""
        
        # Build the comparison table
        all_files = {}
        for name in archives1:
            all_files[name] = {'in_1': True, 'path_1': archives1[name], 'in_2': False, 'path_2': None}
        for name in archives2:
            if name in all_files:
                all_files[name]['in_2'] = True
                all_files[name]['path_2'] = archives2[name]
            else:
                all_files[name] = {'in_1': False, 'path_1': None, 'in_2': True, 'path_2': archives2[name]}
        
        for name in all_files:
            file_info = all_files[name]
            if file_info['in_1'] and file_info['in_2']:
                file_info['status'] = 'identical'
                file_info['status_text'] = '✓ Identical'
                file_info['row_class'] = 'identical'
            elif file_info['in_1'] and not file_info['in_2']:
                is_truly_new = name in truly_new_in_1
                if is_truly_new:
                    file_info['status'] = 'additional-left'
                    file_info['status_text'] = '🆕 Only in Left (New)'
                    file_info['row_class'] = 'new-file'
                else:
                    file_info['status'] = 'missing-right'
                    file_info['status_text'] = 'Only in Left'
                    file_info['row_class'] = 'only-left'
            else:
                is_truly_new = name in truly_new_in_2
                if is_truly_new:
                    file_info['status'] = 'additional-right'
                    file_info['status_text'] = '🆕 Only in Right (New)'
                    file_info['row_class'] = 'new-file'
                else:
                    file_info['status'] = 'missing-left'
                    file_info['status_text'] = 'Only in Right'
                    file_info['row_class'] = 'only-right'
        
        sorted_files = sorted(all_files.items(), key=lambda x: (
            0 if x[1]['status'] != 'identical' else 1,
            x[0].lower()
        ))
        
        html_content += f"""
            <!-- Comprehensive Comparison Table -->
            <div class="section" id="comparison-table">
                <div class="section-title">📊 Detailed Comparison Table</div>
                <div class="table-container">
                    <div style="background: #e0e7ff; padding: 15px; border-radius: 8px; margin-bottom: 15px; border-left: 4px solid #6366f1;">
                        <strong style="color: #3730a3;">💡 Side-by-Side Comparison (Beyond Compare Style)</strong>
                        <div style="color: #4338ca; font-size: 13px; margin-top: 8px;">
                            • <span style="background: #f0f9ff; padding: 2px 6px; border-radius: 3px;">Blue</span> = Identical files
                            • <span style="background: #fef2f2; padding: 2px 6px; border-radius: 3px;">Red</span> = Missing in right folder
                            • <span style="background: #f0fdf4; padding: 2px 6px; border-radius: 3px;">Green</span> = Missing in left folder
                            • <span style="background: #fffbeb; padding: 2px 6px; border-radius: 3px;">Yellow</span> = Additional/New files
                        </div>
                    </div>
                    <table class="comparison-table">
                        <thead>
                            <tr>
                                <th style="width: 30%">File Name</th>
                                <th class="center" style="width: 15%">Status</th>
                                <th style="width: 27.5%">Folder 1 (Left)</th>
                                <th style="width: 27.5%">Folder 2 (Right)</th>
                            </tr>
                        </thead>
                        <tbody>"""
        
        for name, info in sorted_files:
            status_class = info['status'].replace('-', '_')
            path1_cell = html.escape(info['path_1']) if info['path_1'] else '<span class="empty-cell">—</span>'
            path2_cell = html.escape(info['path_2']) if info['path_2'] else '<span class="empty-cell">—</span>'
            html_content += f'<tr class="{info["row_class"]}"><td class="filename" style="width: 30%; word-break: break-word;">{html.escape(name)}</td><td class="status" style="width: 15%;"><span class="status-badge {status_class}">{info["status_text"]}</span></td><td class="path" style="width: 27.5%; word-break: break-all;">{path1_cell}</td><td class="path" style="width: 27.5%; word-break: break-all;">{path2_cell}</td></tr>'
        
        html_content += f"""
                        </tbody>
                    </table>
                </div>
            </div>
            
            <!-- Additional/New Files Section -->"""
        
        if truly_new_in_2 or truly_new_in_1:
            html_content += f"""
            <div class="section" id="additional-files">
                <div class="section-title">🆕 Additional/New Files ({len(truly_new_in_2) + len(truly_new_in_1)} files)</div>
                <div class="archive-list">
                    <div style="background: #fffbeb; padding: 15px; border-radius: 8px; border-left: 4px solid #f59e0b; margin-bottom: 15px;">
                        <strong style="color: #92400e;">ℹ️ These are truly NEW files (not just version updates)</strong>
                    </div>"""
            
            if truly_new_in_2:
                html_content += f"""
                    <h4 style="color: #2d3748; margin: 15px 0 10px 0;">✅ Additional in New Build ({len(truly_new_in_2)} files)</h4>"""
                for name in sorted(truly_new_in_2):
                    path = archives2.get(name, '')
                    html_content += f"""
                    <div class="archive-item only-in-2">
                        <div class="archive-name"><span class="badge added">NEW</span> {html.escape(name)} <span class="highlight-new">ADDITIONAL</span></div>
                        <div class="archive-path">📍 {html.escape(path)}</div>
                    </div>"""
            
            if truly_new_in_1:
                html_content += f"""
                    <h4 style="color: #2d3748; margin: 15px 0 10px 0;">❌ Additional in Old Build ({len(truly_new_in_1)} files)</h4>"""
                for name in sorted(truly_new_in_1):
                    path = archives1.get(name, '')
                    html_content += f"""
                    <div class="archive-item only-in-1">
                        <div class="archive-name"><span class="badge removed">NEW</span> {html.escape(name)} <span class="highlight-new">ADDITIONAL</span></div>
                        <div class="archive-path">📍 {html.escape(path)}</div>
                    </div>"""
            
            html_content += """
                </div>
            </div>"""
        else:
            html_content += """
            <div class="section" id="additional-files" style="display: none;"></div>"""
        
        # Removed in New Build Section (Only in Folder 1 / Missing from Folder 2)
        html_content += f"""
            <div class="section" id="missing-folder2">
                <div class="section-title">🗑️ Removed in New Build ({len(only_in_1)} files)</div>
                <div class="archive-list">"""
        if only_in_1:
            for name in sorted(only_in_1):
                path = archives1.get(name, '')
                html_content += f"""
                    <div class="archive-item only-in-1">
                        <div class="archive-name"><span class="badge removed">Removed</span> {html.escape(name)}</div>
                        <div class="archive-path">📍 {html.escape(path)}</div>
                    </div>"""
        else:
            html_content += """
                    <div style="text-align: center; padding: 20px; color: #718096;">
                        No files were removed in the new build.
                    </div>"""
        html_content += """
                </div>
            </div>"""
        
        # Added in New Build Section (Only in Folder 2 / Missing from Folder 1)
        html_content += f"""
            <div class="section" id="missing-folder1">
                <div class="section-title">✨ Added in New Build ({len(only_in_2)} files)</div>
                <div class="archive-list">"""
        if only_in_2:
            for name in sorted(only_in_2):
                path = archives2.get(name, '')
                html_content += f"""
                    <div class="archive-item only-in-2">
                        <div class="archive-name"><span class="badge added">Added</span> {html.escape(name)}</div>
                        <div class="archive-path">📍 {html.escape(path)}</div>
                    </div>"""
        else:
            html_content += """
                    <div style="text-align: center; padding: 20px; color: #718096;">
                        No files were added in the new build.
                    </div>"""
        html_content += """
                </div>
            </div>"""
        
        # Exact Matches Section
        html_content += f"""
            <div class="section" id="exact-matches">
                <div class="section-title">✓ Exact Matches ({len(in_both)} files)</div>
                <div class="archive-list">"""
        if in_both:
            sorted_matches = sorted(in_both)
            show_count = min(50, len(sorted_matches))
            for name in sorted_matches[:show_count]:
                html_content += f"""
                    <div class="archive-item match">
                        <div class="archive-name"><span class="badge match">Match</span> {html.escape(name)}</div>
                    </div>"""
            
            if len(in_both) > 50:
                remaining = len(in_both) - 50
                html_content += f"""
                    <div class="expand-link" onclick="toggleMoreMatches()" style="cursor: pointer; padding: 10px; text-align: center; color: #4299e1; font-weight: bold;">
                        Click to show {remaining} more matching files <span class="icon">▼</span>
                    </div>
                    <div class="more-matches" id="moreMatches" style="display: none;">"""
                for name in sorted_matches[50:]:
                    html_content += f"""
                        <div class="archive-item match">
                            <div class="archive-name"><span class="badge match">Match</span> {html.escape(name)}</div>
                        </div>"""
                html_content += """
                    </div>
                    <script>
                        function toggleMoreMatches() {
                            var moreMatches = document.getElementById('moreMatches');
                            var icon = document.querySelector('.expand-link .icon');
                            if (moreMatches.style.display === 'none') {
                                moreMatches.style.display = 'block';
                                icon.textContent = '▲';
                            } else {
                                moreMatches.style.display = 'none';
                                icon.textContent = '▼';
                            }
                        }
                    </script>"""
        else:
            html_content += """
                    <div style="text-align: center; padding: 20px; color: #718096;">
                        No exact matches found.
                    </div>"""
        html_content += """
                </div>
            </div>
            
            <a href="/" class="back-link">← Back to Home</a>
        </div>
    </div>
</body>
</html>"""
        
        return inject_report_section_controls(html_content)
    
    def _generate_archive_comparison_html_table(self, archive1, archive2, members1, members2, only_in_1, only_in_2, in_both, content_differs, truly_new_in_1, truly_new_in_2, get_base_name_func, size_mismatches=None):
        """Generate HTML comparison table view specifically for archive member comparison."""
        if size_mismatches is None:
            size_mismatches = {}
        from pathlib import Path as PathLib
        
        # Get display names for the archives
        try:
            name1 = PathLib(archive1).name
            name2 = PathLib(archive2).name
        except:
            name1 = str(archive1).split('\\')[-1] if '\\' in str(archive1) else str(archive1).split('/')[-1]
            name2 = str(archive2).split('\\')[-1] if '\\' in str(archive2) else str(archive2).split('/')[-1]
        
        html_content = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Archive Comparison Report - {name1} vs {name2}</title>
    <style>"""
        
        # Include all the CSS from the zip file comparison
        html_content += self._get_comparison_table_css()
        
        html_content += f"""
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div style="margin-bottom: 16px;">
                <a href="/" style="color: #fff; text-decoration: none; background: rgba(255,255,255,0.2); padding: 8px 16px; border-radius: 6px; display: inline-block; font-size: 14px; transition: all 0.2s;">
                    ← Back to Home
                </a>
            </div>
            <h1>📦 Archive Contents Comparison Report</h1>
            <div class="subtitle">Side-by-Side Member Comparison</div>
            <div class="paths">
                <div class="path-label">Archive 1:</div>
                <div class="path-value">{html.escape(archive1)}</div>
                <div class="path-label">Archive 2:</div>
                <div class="path-value">{html.escape(archive2)}</div>
            </div>
        </div>
        
        <div class="content">
            <!-- Navigation Menu -->
            <div class="nav-menu">
                <h3>📑 Quick Navigation</h3>
                <div class="nav-links">"""
        
        if truly_new_in_2 or truly_new_in_1:
            html_content += f"""
                    <a href="#additional-files" class="nav-link new">🆕 Additional/New Files ({len(truly_new_in_2) + len(truly_new_in_1)})</a>"""
        
        if content_differs:
            html_content += f"""
                    <a href="#content-differs" class="nav-link warning">⚠️ Content Differs ({len(content_differs)})</a>"""
        
        # Always show size mismatches (even if 0)
        size_mismatch_count = len(size_mismatches)
        mismatch_color = "#f97316" if size_mismatch_count > 0 else "#94a3b8"
        print(f"[DEBUG] Adding size mismatches navigation link ({size_mismatch_count} files)")
        html_content += f"""
                    <a href="#size-mismatches" class="nav-link" style="border-left-color: {mismatch_color};">📏 Size Mismatches ({size_mismatch_count})</a>"""
        
        html_content += f"""
                    <a href="#comparison-table" class="nav-link" style="border-left-color: #9f7aea;">📊 Detailed Comparison Table</a>
                    <a href="#missing-archive2" class="nav-link missing">❌ Missing from Archive 2 ({len(only_in_1)})</a>
                    <a href="#missing-archive1" class="nav-link added">✅ Missing from Archive 1 ({len(only_in_2)})</a>
                    <a href="#exact-matches" class="nav-link matches">✓ Exact Matches ({len(in_both)})</a>
                </div>
            </div>
            
            <div class="summary">
                <div class="stat-card info">
                    <div class="label">Total Members (Archive 1)</div>
                    <div class="value">{len(members1)}</div>
                </div>
                <div class="stat-card info">
                    <div class="label">Total Members (Archive 2)</div>
                    <div class="value">{len(members2)}</div>
                </div>
                <div class="stat-card success">
                    <div class="label">Exact Matches</div>
                    <div class="value">{len(in_both)}</div>
                </div>"""
        
        # Always show size mismatches card (green if 0, orange if > 0)
        size_mismatch_count = len(size_mismatches)
        if size_mismatch_count > 0:
            html_content += f"""
                <div class="stat-card" style="background: linear-gradient(135deg, #f97316 0%, #ea580c 100%);">
                    <div class="label">Size Mismatches</div>
                    <div class="value"><a href="#size-mismatches" style="color: white; text-decoration: none;">{size_mismatch_count}</a></div>
                </div>"""
        else:
            html_content += f"""
                <div class="stat-card success">
                    <div class="label">Size Mismatches</div>
                    <div class="value">0</div>
                </div>"""
        
        html_content += f"""
                <div class="stat-card warning">
                    <div class="label">Differences Found</div>
                    <div class="value">{len(only_in_1) + len(only_in_2) + len(content_differs)}</div>
                </div>
            </div>"""
        
        # Additional/New Files Section (before comparison table)
        if truly_new_in_2 or truly_new_in_1:
            html_content += f"""
            <div id="additional-files" class="section">
                <h2>🆕 Additional/New Files</h2>
                <p class="section-description">Files that exist in one archive but have no matching base name in the other.</p>"""
            
            if truly_new_in_1:
                html_content += f"""
                <div class="subsection">
                    <h3>New in Archive 1 ({len(truly_new_in_1)} files)</h3>
                    <ul class="file-list">"""
            for name in sorted(truly_new_in_1):
                display_name = name.split('::')[-1] if '::' in name else name
                html_content += f'<li class="new-file">{html.escape(display_name)}</li>'
            html_content += '</ul></div>'
            
            if truly_new_in_2:
                html_content += f"""
                <div class="subsection">
                    <h3>New in Archive 2 ({len(truly_new_in_2)} files)</h3>
                    <ul class="file-list">"""
            for name in sorted(truly_new_in_2):
                display_name = name.split('::')[-1] if '::' in name else name
                html_content += f'<li class="new-file">{html.escape(display_name)}</li>'
            html_content += '</ul></div>'
            
            html_content += """
            </div>"""
        
        # Content Differs Section
        if content_differs:
            html_content += f"""
            <div id="content-differs" class="section">
                <h2>⚠️ Files with Different Content ({len(content_differs)})</h2>
                <p class="section-description">Files that exist in both archives with the same name but different content/metadata.</p>
                <ul class="file-list">"""
            for name in sorted(content_differs):
                display_name = name.split('::')[-1] if '::' in name else name
                html_content += f'<li class="content-diff">{html.escape(display_name)}</li>'
            html_content += '</ul></div>'
        
        # Size Mismatches Section - Always show (even if 0)
        size_mismatch_count = len(size_mismatches)
        print(f"[DEBUG] Generating HTML - size_mismatches has {size_mismatch_count} entries")
        print(f"[DEBUG] Adding size mismatches section to HTML (count={size_mismatch_count})")
        
        html_content += f"""
            <div id="size-mismatches" class="section">
                <h2>📏 Size Mismatches ({size_mismatch_count} files)</h2>
                <p class="section-description">Files that exist in both archives with the same name but different sizes.</p>"""
        
        if size_mismatch_count > 0:
            html_content += """
                <div style="overflow-x: auto;">
                    <table class="comparison-table">
                        <thead>
                            <tr>
                                <th style="width: 50%">File Name</th>
                                <th style="width: 25%; text-align: right;">Archive 1 Size</th>
                                <th style="width: 25%; text-align: right;">Archive 2 Size</th>
                            </tr>
                        </thead>
                        <tbody>"""
            for name in sorted(size_mismatches.keys()):
                display_name = name.split('::')[-1] if '::' in name else name
                size1, size2 = size_mismatches[name]
                # Format sizes nicely
                def format_size(size_bytes):
                    if size_bytes < 1024:
                        return f"{size_bytes} B"
                    elif size_bytes < 1024 * 1024:
                        return f"{size_bytes / 1024:.2f} KB"
                    elif size_bytes < 1024 * 1024 * 1024:
                        return f"{size_bytes / (1024 * 1024):.2f} MB"
                    else:
                        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"
                
                size1_str = format_size(size1)
                size2_str = format_size(size2)
                diff = abs(size1 - size2)
                diff_str = format_size(diff)
                
                html_content += f'<tr class="warning"><td>{html.escape(display_name)}</td><td style="text-align: right; font-family: monospace;">{size1_str}</td><td style="text-align: right; font-family: monospace;">{size2_str}</td></tr>'
                html_content += f'<tr class="warning" style="background: rgba(249, 115, 22, 0.05);"><td colspan="3" style="text-align: center; font-size: 0.85em; color: #f97316; padding: 4px;"><strong>Difference: {diff_str}</strong> ({"+" if size1 < size2 else "-"}{abs(((size2 - size1) / size1 * 100) if size1 > 0 else 0):.1f}%)</td></tr>'
            html_content += """
                        </tbody>
                    </table>
                </div>"""
        else:
            # No size mismatches found
            html_content += """
                <div style="text-align: center; padding: 40px 20px; background: linear-gradient(135deg, #10b981 0%, #059669 100%); border-radius: 8px; color: white;">
                    <div style="font-size: 48px; margin-bottom: 16px;">✓</div>
                    <div style="font-size: 20px; font-weight: bold; margin-bottom: 8px;">No Size Mismatches Detected</div>
                    <div style="font-size: 14px; opacity: 0.9;">All files in both archives have identical sizes</div>
                </div>"""
        
        html_content += """
            </div>"""
        
        # Build the comparison table
        all_files = {}
        for name in members1:
            all_files[name] = {'in_1': True, 'path_1': members1[name], 'in_2': False, 'path_2': None}
        for name in members2:
            if name in all_files:
                all_files[name]['in_2'] = True
                all_files[name]['path_2'] = members2[name]
            else:
                all_files[name] = {'in_1': False, 'path_1': None, 'in_2': True, 'path_2': members2[name]}
        
        for name in all_files:
            file_info = all_files[name]
            if file_info['in_1'] and file_info['in_2']:
                if name in content_differs:
                    file_info['status'] = 'content-differs'
                    file_info['status_text'] = '⚠️ Content Differs'
                    file_info['row_class'] = 'warning'
                else:
                    file_info['status'] = 'identical'
                    file_info['status_text'] = '✓ Identical'
                    file_info['row_class'] = 'identical'
            elif file_info['in_1'] and not file_info['in_2']:
                is_truly_new = name in truly_new_in_1
                if is_truly_new:
                    file_info['status'] = 'additional-left'
                    file_info['status_text'] = '🆕 Only in Left (New)'
                    file_info['row_class'] = 'new-file'
                else:
                    file_info['status'] = 'missing-right'
                    file_info['status_text'] = 'Only in Left'
                    file_info['row_class'] = 'only-left'
            else:  # in_2 but not in_1
                is_truly_new = name in truly_new_in_2
                if is_truly_new:
                    file_info['status'] = 'additional-right'
                    file_info['status_text'] = '🆕 Only in Right (New)'
                    file_info['row_class'] = 'new-file'
                else:
                    file_info['status'] = 'missing-left'
                    file_info['status_text'] = 'Only in Right'
                    file_info['row_class'] = 'only-right'
        
        # Generate the comparison table
        html_content += f"""
            <div id="comparison-table" class="section">
                <h2>📊 Detailed Comparison Table</h2>
                <p class="section-description">Complete side-by-side comparison of all members.</p>
                
                <div class="table-container">
                    <table class="comparison-table">
                        <thead>
                            <tr>
                                <th style="width: 40%">{html.escape(name1)}</th>
                                <th style="width: 20%; text-align: center;">Status</th>
                                <th style="width: 40%">{html.escape(name2)}</th>
                            </tr>
                        </thead>
                        <tbody>"""
        
        # Sort files: new files first, then missing, then identical
        sorted_files = sorted(all_files.items(), key=lambda x: (
            0 if 'new-file' in x[1]['row_class'] else
            1 if 'content-differs' in x[1].get('status', '') else
            2 if 'only-' in x[1]['row_class'] else 3,
            x[0]
        ))
        
        for name, file_info in sorted_files:
            display_name = name.split('::')[-1] if '::' in name else name
            row_class = file_info['row_class']
            status_text = file_info['status_text']
            
            left_cell = html.escape(display_name) if file_info['in_1'] else '<span class="empty-cell">—</span>'
            right_cell = html.escape(display_name) if file_info['in_2'] else '<span class="empty-cell">—</span>'
            
            html_content += f'<tr class="{row_class}"><td style="width: 40%; word-break: break-all; overflow-wrap: break-word;">{left_cell}</td><td class="status-cell" style="width: 20%;"><span class="status-badge">{status_text}</span></td><td style="width: 40%; word-break: break-all; overflow-wrap: break-word;">{right_cell}</td></tr>'
        
        html_content += '</tbody></table></div></div>'
        
        # Missing from Archive 2 Section
        html_content += f"""
            <div id="missing-archive2" class="section">
                <h2>❌ Missing from Archive 2 - Only in Archive 1 ({len(only_in_1)} members)</h2>"""
        if only_in_1:
            html_content += '<ul class="file-list">'
            for name in sorted(only_in_1):
                display_name = name.split('::')[-1] if '::' in name else name
                html_content += f'<li class="missing-right">{html.escape(display_name)}</li>'
            html_content += '</ul>'
        else:
            html_content += '<p class="empty-message">No members are missing from Archive 2.</p>'
        html_content += '</div>'
        
        # Missing from Archive 1 Section
        html_content += f"""
            <div id="missing-archive1" class="section">
                <h2>✅ Missing from Archive 1 - Only in Archive 2 ({len(only_in_2)} members)</h2>"""
        if only_in_2:
            html_content += '<ul class="file-list">'
            for name in sorted(only_in_2):
                display_name = name.split('::')[-1] if '::' in name else name
                html_content += f'<li class="missing-left">{html.escape(display_name)}</li>'
            html_content += '</ul>'
        else:
            html_content += '<p class="empty-message">No members are missing from Archive 1.</p>'
        html_content += '</div>'
        
        # Exact Matches Section
        visible_limit = 10
        html_content += f"""
            <div id="exact-matches" class="section">
                <h2>✓ Exact Matches ({len(in_both)} members)</h2>"""
        if in_both:
            if len(in_both) > visible_limit:
                remaining = len(in_both) - visible_limit
                html_content += f"""
                <p class="section-description">
                    Showing first {visible_limit} matches. 
                    <a href="javascript:void(0)" onclick="toggleMatches()" style="color: #3b82f6; text-decoration: underline; cursor: pointer; font-weight: bold;">
                        Click to show {remaining} more matching members
                    </a>
                </p>"""
            
            html_content += """
                <ul class="file-list">"""
            for idx, name in enumerate(sorted(in_both)):
                display_name = name.split('::')[-1] if '::' in name else name
                if idx < visible_limit:
                    html_content += f'<li class="match-file">{html.escape(display_name)}</li>'
                else:
                    html_content += f'<li class="match-file hidden-match" style="display: none;">{html.escape(display_name)}</li>'
            html_content += '</ul>'
            
            if len(in_both) > visible_limit:
                remaining = len(in_both) - visible_limit
                html_content += f"""
                <div style="text-align: center; margin-top: 20px;">
                    <button class="show-more-btn" onclick="toggleMatches()" style="background: linear-gradient(90deg, #3b82f6 0%, #2563eb 100%); color: white; border: none; padding: 12px 24px; border-radius: 8px; font-size: 14px; cursor: pointer; box-shadow: 0 4px 10px rgba(59,130,246,0.25);">
                        <span id="toggle-text">Show {remaining} more matching members</span>
                    </button>
                </div>
                <script>
                    function toggleMatches() {{
                        const hidden = document.querySelectorAll('.hidden-match');
                        const toggleText = document.getElementById('toggle-text');
                        const remainingCount = {remaining};
                        
                        let isHidden = hidden[0].style.display === 'none' || hidden[0].style.display === '';
                        
                        hidden.forEach(item => {{
                            item.style.display = isHidden ? 'list-item' : 'none';
                        }});
                        
                        toggleText.textContent = isHidden 
                            ? 'Hide ' + remainingCount + ' matching members' 
                            : 'Show ' + remainingCount + ' more matching members';
                        
                        // Scroll to the first hidden item when showing
                        if (isHidden && hidden.length > 0) {{
                            hidden[0].scrollIntoView({{ behavior: 'smooth', block: 'nearest' }});
                        }}
                    }}
                </script>"""
        else:
            html_content += """
                <p class="empty-message">No exact matches found.</p>"""
        html_content += """
            </div>
        </div>
    </div>
</body>
</html>"""
        
        return inject_report_section_controls(html_content)
    
    def _get_comparison_table_css(self):
        """Return the CSS for comparison table styling."""
        return """
        body { font-family: 'Segoe UI', system-ui, -apple-system, sans-serif; margin: 0; padding: 20px; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); min-height: 100vh; }
        .container { max-width: 1200px; margin: 0 auto; background: white; border-radius: 16px; box-shadow: 0 20px 60px rgba(0,0,0,0.3); overflow: hidden; }
        .header { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 30px; }
        .header h1 { margin: 0 0 10px 0; font-size: 28px; font-weight: 600; }
        .header .subtitle { opacity: 0.9; font-size: 14px; }
        .header .paths { margin-top: 15px; padding-top: 15px; border-top: 1px solid rgba(255,255,255,0.3); }
        .header .path-label { font-weight: 600; font-size: 12px; text-transform: uppercase; opacity: 0.8; margin-top: 8px; }
        .header .path-value { font-family: 'Consolas', monospace; font-size: 13px; margin-top: 4px; word-break: break-all; }
        .content { padding: 30px; }
        .summary { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 20px; margin-bottom: 30px; }
        .stat-card { background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%); color: white; padding: 20px; border-radius: 12px; box-shadow: 0 4px 15px rgba(0,0,0,0.1); }
        .stat-card.success { background: linear-gradient(135deg, #4facfe 0%, #00f2fe 100%); }
        .stat-card.warning { background: linear-gradient(135deg, #fa709a 0%, #fee140 100%); }
        .stat-card.info { background: linear-gradient(135deg, #30cfd0 0%, #330867 100%); }
        .stat-card .label { font-size: 13px; font-weight: 500; opacity: 0.9; text-transform: uppercase; letter-spacing: 0.5px; }
        .stat-card .value { font-size: 36px; font-weight: 700; margin-top: 8px; }
        .section { margin-bottom: 40px; }
        .section-title { font-size: 22px; font-weight: 600; color: #1a202c; margin-bottom: 20px; padding-bottom: 10px; border-bottom: 3px solid #667eea; }
        .nav-menu { background: #f7fafc; padding: 20px; border-radius: 8px; margin-bottom: 30px; }
        .nav-menu h3 { margin: 0 0 15px 0; font-size: 18px; color: #2d3748; }
        .nav-links { display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 12px; }
        .nav-link { display: block; padding: 12px 16px; background: white; border-left: 4px solid #667eea; border-radius: 6px; text-decoration: none; color: #2d3748; font-weight: 600; transition: all 0.2s; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
        .nav-link:hover { transform: translateX(4px); border-left-width: 6px; box-shadow: 0 2px 8px rgba(0,0,0,0.15); }
        .nav-link.missing { border-left-color: #f56565; }
        .nav-link.added { border-left-color: #48bb78; }
        .nav-link.new { border-left-color: #ed8936; }
        .nav-link.matches { border-left-color: #4299e1; }
        .back-link { display: inline-block; margin-top: 20px; padding: 10px 20px; background: #667eea; color: white; text-decoration: none; border-radius: 8px; font-weight: 600; transition: transform 0.2s; }
        .back-link:hover { transform: translateY(-2px); background: #5a67d8; }
        .comparison-table { width: 100%; border-collapse: collapse; border-spacing: 0; background: white; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.1); table-layout: fixed; }
        .comparison-table thead { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; }
        .comparison-table th { padding: 16px; text-align: left; font-weight: 600; font-size: 14px; text-transform: uppercase; letter-spacing: 0.5px; border-bottom: 2px solid #5a67d8; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .comparison-table th.center { text-align: center; }
        .comparison-table tbody tr { transition: all 0.2s; }
        .comparison-table tbody tr:hover { background: #f7fafc !important; transform: scale(1.01); box-shadow: 0 2px 8px rgba(0,0,0,0.1); }
        .comparison-table td { padding: 14px 16px; border-bottom: 1px solid #e2e8f0; font-family: 'Consolas', monospace; font-size: 12px; vertical-align: top; word-wrap: break-word; word-break: break-all; overflow-wrap: break-word; max-width: 0; }
        .comparison-table td.filename { font-weight: 600; color: #2d3748; word-wrap: break-word; word-break: break-word; overflow-wrap: break-word; }
        .comparison-table td.path { color: #718096; font-size: 11px; word-wrap: break-word; word-break: break-all; overflow-wrap: break-word; }
        .comparison-table td.status { text-align: center; font-weight: 600; min-width: 150px; white-space: normal; }
        .comparison-table td.status-cell { text-align: center; font-weight: 600; min-width: 150px; white-space: normal; }
        .comparison-table tr.identical { background: #f0f9ff; }
        .comparison-table tr.only-left { background: #fef2f2; }
        .comparison-table tr.only-right { background: #f0fdf4; }
        .comparison-table tr.new-file { background: #fffbeb; }
        .status-badge { display: inline-block; padding: 6px 14px; border-radius: 20px; font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px; white-space: nowrap; }
        .status-badge.identical { background: #dbeafe; color: #1e40af; }
        .status-badge.missing_left { background: #dcfce7; color: #166534; }
        .status-badge.missing_right { background: #fee2e2; color: #991b1b; }
        .status-badge.additional_right { background: #fef3c7; color: #92400e; }
        .status-badge.additional_left { background: #fef3c7; color: #92400e; }
        .table-container { background: #f7fafc; border-radius: 8px; padding: 20px; margin-bottom: 30px; overflow-x: auto; }
        .empty-cell { color: #cbd5e0; text-align: center; font-style: italic; }
        @media (max-width: 768px) { .summary { grid-template-columns: 1fr; } .nav-links { grid-template-columns: 1fr; } .comparison-table { font-size: 11px; } .comparison-table td.filename { max-width: 200px; } .comparison-table td.path { max-width: 150px; } }
        """ + report_collapse_extra_css()
    
    def _execute_run_comparison(self, sink, qs, session_id, session_progress):
        """Run comparison job (invoked from background thread)."""
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
        app_wef = ctx.app_wef
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
        ascf_class_diff_jar = ctx.ascf_class_diff_jar
        ascf_manifest_diff_jar = ctx.ascf_manifest_diff_jar
        ascf_config_files_filter = ctx.ascf_config_files_filter
        ascf_explicit_size_paths = ctx.ascf_explicit_size_paths
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
        crm_playbooks_nexus_previous = ctx.crm_playbooks_nexus_previous
        crm_playbooks_nexus_latest = ctx.crm_playbooks_nexus_latest
        crm_playbooks_nexus_pair = ctx.crm_playbooks_nexus_pair
        crm_playbooks_nexus_mode = ctx.crm_playbooks_nexus_mode
        wef_ssh_host = ctx.wef_ssh_host
        wef_ssh_username = ctx.wef_ssh_username
        wef_ssh_password = ctx.wef_ssh_password
        wef_ssh_port = ctx.wef_ssh_port
        wef_old_path = ctx.wef_old_path
        wef_new_path = ctx.wef_new_path
        wef_mode = ctx.wef_mode
        crm_archive_mode = False
        report_text = ""
        saved_name = None

        # Use the session already registered by /run (do not re-create from qs — avoids ID mismatch).
        if not session_progress:
            session_progress = get_session(session_id)
        if not session_progress:
            return
        
        # Set current session for logging (thread-local)
        set_current_session(session_id)
        clear_session_logs(session_id)  # Clear previous logs for this session
        add_session_log(session_id, f"=== Comparison Started ===", "INFO")
        add_session_log(session_id, f"Session ID: {session_id}", "INFO")
        compare_app = (
            str(session_progress.get("compare_application") or "").strip()
            or (selected_apps[0] if selected_apps else "")
        )
        if compare_app:
            session_progress["compare_application"] = compare_app
        add_session_log(session_id, f"Mode: {mode}", "INFO")
        if compare_app:
            add_session_log(
                session_id,
                f"Application: {compare_app} (old temp folders for this app will be removed first)",
                "INFO",
            )
        add_session_log(session_id, f"Folder 1: {folder1}", "INFO")
        add_session_log(session_id, f"Folder 2: {folder2}", "INFO")
        client_identity = (
            str(session_progress.get("client_identity") or "").strip()
            or str(qs.get("report_user", [""])[0] or "").strip()
            or "unknown"
        )
        add_session_log(session_id, f"Report device/user: {client_identity}", "INFO")
        machine_host = str(session_progress.get("client_machine_host") or client_identity or "")
        space_st = comparison_space_status(machine_host, folder1, folder2)
        add_session_log(
            session_id,
            f"Client C: min {space_st['minimum_required_human']} free on {machine_host} "
            f"(currently {space_st['client_free_human']}) — temp on workstation first",
            "INFO",
        )
        report_save_dir = resolve_report_save_dir(machine_host, PROJECT_ROOT)
        session_progress["report_save_dir"] = str(report_save_dir)
        add_session_log(session_id, f"Report save folder: {report_save_dir}", "INFO")
        ts_label, tz_label = report_timestamp_with_tz()
        add_session_log(session_id, f"Report timestamp ({tz_label}): {ts_label}", "INFO")

        def _report_path(name: str) -> Path:
            return report_save_dir / name

        def _report_fname(prefix: str, *, path_hint: str | None = None, suffix: str = ".txt") -> str:
            return allocate_report_filename(
                prefix,
                client_identity,
                report_save_dir,
                suffix=suffix,
            )

        # Start progress tracking (both session and legacy global)
        session_progress["running"] = True
        session_progress["error"] = None
        session_progress["stop_requested"] = False
        session_progress["created_at"] = time.time()
        
        # Also update legacy global for backwards compatibility (scoped to this session)
        comparison_progress["running"] = True
        comparison_progress["error"] = None
        comparison_progress["_session_id"] = session_id
        comparer = None  # Initialize to avoid reference errors

        # Validate application selection
        if not selected_apps:
            body = """<div style="background:#fee2e2;border:2px solid #fca5a5;border-radius:12px;padding:20px;margin:20px 0;">
                <h2 style="color:#dc2626;margin:0 0 15px 0;">❌ No Application Selected</h2>
                <p style="color:#991b1b;">Please select at least one application (ASCF, CRM/AMSS, UXF/OMNI, DEX, or WEF) to compare.</p>
            </div>
            <p><a href="/" style="display:inline-block;padding:10px 20px;background:#3b82f6;color:white;text-decoration:none;border-radius:8px;">← Back to Main Page</a></p>
            """
            comparison_progress["running"] = False
            sink.send_response(400)
            sink.send_header("Content-Type", "text/html; charset=utf-8")
            sink.end_headers()
            sink.write(html_page("No Application Selected", body))
            return

        # Check for multiple application selections
        if len(selected_apps) > 1:
            body = f"""<div style="background:#fee2e2;border:2px solid #fca5a5;border-radius:12px;padding:20px;margin:20px 0;">
                <h2 style="color:#dc2626;margin:0 0 15px 0;">❌ Multiple Applications Selected</h2>
                <p style="color:#991b1b;">Please select only one application at a time.</p>
                <p style="color:#991b1b;">Selected: {', '.join(selected_apps)}</p>
                <p style="color:#78350f;margin-top:10px;">ASCF, CRM/AMSS, UXF/OMNI, DEX, and WEF have different comparison logic and cannot be combined.</p>
            </div>
            <p><a href="/" style="display:inline-block;padding:10px 20px;background:#3b82f6;color:white;text-decoration:none;border-radius:8px;">← Back to Main Page</a></p>
            """
            comparison_progress["running"] = False
            sink.send_response(400)
            sink.send_header("Content-Type", "text/html; charset=utf-8")
            sink.end_headers()
            sink.write(html_page("Multiple Applications Selected", body))
            return

        # Track if this was originally a CRM archive request (for report naming)
        crm_archive_mode = False
        
        # CRM crm-playbooks Nexus YAML verification (Nexus versions only; build paths optional)
        if (
            app_crm
            and not app_ascf
            and not app_uxf
            and not app_dex
            and not app_wef
            and crm_playbooks_nexus_mode
        ):
            try:
                session_progress["running"] = True
                session_progress["progress"] = 0
                session_progress["total"] = 100
                session_progress["current_file"] = "Initializing crm-playbooks Nexus verification..."
                comparison_progress["running"] = True
                comparison_progress["progress"] = 0
                comparison_progress["total"] = 100
                comparison_progress["current_file"] = "Initializing crm-playbooks Nexus verification..."
                add_session_log(session_id, "=== CRM crm-playbooks Nexus verification ===", "INFO")
                add_session_log(
                    session_id,
                    f"Previous: {crm_playbooks_nexus_previous} | Latest: {crm_playbooks_nexus_latest}",
                    "INFO",
                )
                from crm_playbooks_nexus import run_crm_playbooks_nexus_comparison

                def crm_pb_log(message, level="INFO"):
                    add_session_log(session_id, message, level)

                def crm_pb_progress(current_file, progress=None, total=None):
                    file_str = current_file or "Processing..."
                    session_progress["current_file"] = file_str
                    comparison_progress["current_file"] = file_str
                    if progress is not None and total is not None and total > 0:
                        pct = 10 + int((progress / total) * 80)
                        session_progress["progress"] = min(pct, 95)
                        comparison_progress["progress"] = min(pct, 95)

                _meta, _diff, saved_name = run_crm_playbooks_nexus_comparison(
                    crm_playbooks_nexus_previous,
                    crm_playbooks_nexus_latest,
                    log_callback=crm_pb_log,
                    progress_callback=crm_pb_progress,
                    report_user=client_identity,
                    report_dir=str(report_save_dir),
                )
                session_progress["progress"] = 100
                session_progress["current_file"] = "Complete!"
                comparison_progress["progress"] = 100
                comparison_progress["current_file"] = "Complete!"
                add_session_log(session_id, f"Report saved: {saved_name}", "INFO")
                _set_session_report_result_url(session_progress, saved_name)
                comparison_progress["running"] = False
                session_progress["running"] = False
                sink.send_response(302)
                sink.send_header("Location", f"/view?file={quote(saved_name)}")
                sink.end_headers()
                return
            except Exception as e:
                import traceback
                add_session_log(session_id, f"crm-playbooks Nexus error: {e}", "ERROR")
                add_session_log(session_id, traceback.format_exc(), "ERROR")
                comparison_progress["running"] = False
                session_progress["running"] = False
                body = f"""<div style="background:#fee2e2;border:2px solid #fca5a5;border-radius:12px;padding:20px;">
                    <h2 style="color:#dc2626;">crm-playbooks Nexus verification failed</h2>
                    <p>{html.escape(str(e))}</p>
                    <p><a href="/">← Back</a></p></div>"""
                sink.send_response(500)
                sink.send_header("Content-Type", "text/html; charset=utf-8")
                sink.end_headers()
                sink.write(html_page("crm-playbooks Nexus Error", body))
                return

        # Route to CRM comparison - separate implementation with mode support
        if app_crm and not app_ascf and not app_uxf and not app_dex and not app_wef:
            # For archives mode, use ASCF-style deep archive comparison
            if mode == "archives":
                add_session_log(session_id, f"Using CRM archive mode with ASCF-style deep comparison", "INFO")
                # Fall through to ASCF archive comparison code below
                # This ensures CRM gets the same rich archive comparison as ASCF
                crm_archive_mode = True  # Track that this was a CRM request
                app_crm = False  # Clear CRM flag so we use ASCF archive logic
                # Continue to ASCF section which handles archive mode
            else:
                try:
                    add_session_log(session_id, f"Using CRM comparison module (mode: {mode})", "INFO")
                    add_session_log(session_id, "=== CRM Comparison Started ===", "INFO")
                    from crm_compare import run_crm_comparison, CRMFolderComparer, CRMNamingComparer, CRMArchiveComparer, CRMJarManifestComparer
                    
                    # Map mode to CRM comparison type
                    crm_mode = mode  # 'full', 'naming', 'foss'
                    run_full_tree_compare = crm_mode == "full" and fast_crm_full
                    crm_fast_enabled = run_full_tree_compare
                    if crm_mode == "full" and compare_clfy_agent_zip and not fast_crm_full:
                        add_session_log(
                            session_id,
                            "CRM ClfyAgent-only mode: skipping full build tree compare",
                            "INFO",
                        )
                    elif crm_mode == "full" and fast_crm_full and not compare_clfy_agent_zip:
                        add_session_log(
                            session_id,
                            "CRM Fast full compare only (ClfyAgent section disabled)",
                            "INFO",
                        )
                    elif crm_mode == "full" and not fast_crm_full and not compare_clfy_agent_zip:
                        add_session_log(
                            session_id,
                            "WARNING: Both Fast CRM and ClfyAgent compare are disabled — enable at least one",
                            "WARN",
                        )
                    add_session_log(
                        session_id,
                        f"CRM full comparison speed profile: {'FAST' if crm_fast_enabled else ('SKIPPED' if not run_full_tree_compare else 'DETAILED')}",
                        "INFO",
                    )
                    if crm_class_diff_jar:
                        add_session_log(session_id, f"CRM JAR class diff query: {crm_class_diff_jar}", "INFO")
                    if crm_manifest_diff_jar:
                        add_session_log(session_id, f"CRM JAR manifest diff query: {crm_manifest_diff_jar}", "INFO")
                    if crm_config_files_filter:
                        add_session_log(session_id, f"CRM config files report filter: {crm_config_files_filter}", "INFO")
                    if crm_explicit_size_paths:
                        add_session_log(session_id, f"CRM explicit size compare list: {crm_explicit_size_paths}", "INFO")
                    if not compare_clfy_agent_zip:
                        add_session_log(session_id, "ClfyAgent size-only compare disabled by user", "INFO")
                    
                    # Initialize progress tracking for CRM
                    session_progress["progress"] = 0
                    session_progress["total"] = 100
                    session_progress["current_file"] = "Initializing CRM comparison..."
                    
                    crm_comparer, results, saved_name = run_crm_comparison(
                        folder1, folder2,
                        mode=crm_mode,
                        jar_name=jar_name,
                        suppress_warnings=suppress_warnings,
                        show_missing_only=not show_all_differences,
                        ignore_timestamp_only=ignore_timestamp_only,
                        fast_mode=crm_fast_enabled,
                        run_full_tree_compare=run_full_tree_compare,
                        compare_archive_contents=not crm_fast_enabled if crm_mode == "full" else True,
                        deep_nested_archives=not crm_fast_enabled if crm_mode == "full" else True,
                        use_local_copy=False if crm_fast_enabled else True,
                        class_diff_jar_name=crm_class_diff_jar if crm_mode == "full" else "",
                        manifest_diff_jar_name=crm_manifest_diff_jar if crm_mode == "full" else "",
                        crm_config_files_filter=crm_config_files_filter if crm_mode == "full" else "",
                        crm_explicit_size_paths=crm_explicit_size_paths if crm_mode == "full" else "",
                        compare_clfy_agent_zip=compare_clfy_agent_zip if crm_mode == "full" else False,
                        compare_application=compare_app or "CRM",
                        report_user=client_identity,
                        report_dir=str(report_save_dir),
                        session_progress=session_progress,
                        comparison_progress=comparison_progress,
                    )
                    
                    # Update progress - generating report
                    session_progress["current_file"] = "Generating report..."
                    session_progress["progress"] = 85
                    
                    report_filepath = str(_report_path(saved_name))
                    
                    # Save report based on comparer type
                    if hasattr(crm_comparer, 'save_archive_report'):
                        crm_comparer.save_archive_report(report_filepath)
                    else:
                        crm_comparer.save_report(report_filepath)
                    cleaned = cleanup_old_reports(10)
                    if cleaned:
                        add_session_log(session_id, f"Report cleanup: deleted {cleaned} old report file(s)", "INFO")
                    
                    add_session_log(session_id, f"CRM report saved: {saved_name}", "INFO")
                    
                    # Redirect to HTML report (CRM saves .html only)
                    html_name = os.path.basename(saved_name)
                    if not html_name.lower().endswith(".html"):
                        html_name = os.path.basename(os.path.splitext(saved_name)[0] + ".html")
                    html_check_path = str(_report_path(html_name))
                    _set_session_report_result_url(session_progress, html_name)
                    if _path_exists(html_check_path):
                        # Mark BOTH session and global progress as complete
                        comparison_progress["running"] = False
                        session_progress["running"] = False
                        session_progress["progress"] = 100
                        session_progress["current_file"] = "Complete!"
                        add_session_log(session_id, "=== CRM Comparison Completed ===", "INFO")
                        sink.send_response(302)
                        sink.send_header("Location", f"/view?file={quote(html_name)}")
                        sink.end_headers()
                        return
                    add_session_log(
                        session_id,
                        f"HTML report path not visible yet ({html_check_path}); result_url set for auto-open retry",
                        "WARN",
                    )
                    comparison_progress["running"] = False
                    session_progress["running"] = False
                    session_progress["progress"] = 100
                    session_progress["current_file"] = "Complete!"
                    add_session_log(session_id, "=== CRM Comparison Completed ===", "INFO")
                    sink.send_response(302)
                    sink.send_header("Location", f"/view?file={quote(html_name)}")
                    sink.end_headers()
                    return
                except Exception as e:
                    import traceback
                    error_details = traceback.format_exc()
                    add_session_log(session_id, f"CRM comparison error: {str(e)}", "ERROR")
                    body = f"""<div style="background:#fee2e2;border:2px solid #fca5a5;border-radius:12px;padding:20px;margin:20px 0;">
                        <h2 style="color:#dc2626;margin:0 0 15px 0;">❌ CRM Comparison Failed</h2>
                        <p style="color:#991b1b;">{html.escape(str(e))}</p>
                        <details><summary>Technical Details</summary><pre style="background:#f5f5f5;padding:10px;overflow:auto;">{html.escape(error_details)}</pre></details>
                    </div>
                    <p><a href="/" style="display:inline-block;padding:10px 20px;background:#3b82f6;color:white;text-decoration:none;border-radius:8px;">← Back to Main Page</a></p>
                    """
                    # Mark BOTH session and global progress as stopped due to error
                    comparison_progress["running"] = False
                    session_progress["running"] = False
                    session_progress["error"] = str(e)
                    sink.send_response(500)
                    sink.send_header("Content-Type", "text/html; charset=utf-8")
                    sink.end_headers()
                    sink.write(html_page("CRM Comparison Error", body))
                    return
                # Fallback: Mark BOTH session and global progress as complete
                comparison_progress["running"] = False
                session_progress["running"] = False
                sink.send_response(302)
                sink.send_header("Location", "/")
                sink.end_headers()
                return

        # Route to UXF/OMNI comparison - separate implementation
        if app_uxf and not app_ascf and not app_crm and not app_dex and not app_wef:
            try:
                # Initialize BOTH session and global progress for UXF comparison
                # Session progress (for multi-user support)
                session_progress["running"] = True
                session_progress["progress"] = 0
                session_progress["total"] = 100
                session_progress["current_file"] = "Initializing UXF comparison..."
                session_progress["error"] = None
                session_progress["stop_requested"] = False
                
                # Global progress (for backwards compatibility with UI)
                comparison_progress["running"] = True
                comparison_progress["progress"] = 0
                comparison_progress["total"] = 100
                comparison_progress["current_file"] = "Initializing UXF comparison..."
                comparison_progress["error"] = None
                
                add_session_log(session_id, "=== UXF Comparison Started ===", "INFO")
                add_session_log(session_id, "Using UXF comparison module", "INFO")
                add_session_log(session_id, f"Old Build: {folder1}", "INFO")
                add_session_log(session_id, f"New Build: {folder2}", "INFO")
                from uxf_compare import UXFComparer, run_uxf_comparison
                
                # Create a logging callback for UXF comparer
                def uxf_log_callback(message, level="INFO"):
                    add_session_log(session_id, message, level)
                
                # Create a progress callback to update BOTH session and global progress
                def uxf_progress_callback(current_file, progress=None, total=None):
                    file_str = current_file or "Processing..."
                    # Update session progress
                    session_progress["current_file"] = file_str
                    # Update global progress (this is what UI reads!)
                    comparison_progress["current_file"] = file_str
                    
                    if progress is not None and total is not None and total > 0:
                        # Map progress to 10-80% range
                        pct = 10 + int((progress / total) * 70)
                        pct = min(pct, 80)
                        session_progress["progress"] = pct
                        comparison_progress["progress"] = pct
                        session_progress["total"] = 100
                        comparison_progress["total"] = 100
                
                # Update progress - starting scan
                session_progress["current_file"] = "Scanning archives..."
                session_progress["progress"] = 10
                comparison_progress["current_file"] = "Scanning archives..."
                comparison_progress["progress"] = 10
                
                uxf_comparer, results, saved_name = run_uxf_comparison(
                    folder1, folder2,
                    session_id=session_id,
                    log_callback=uxf_log_callback,
                    progress_callback=uxf_progress_callback,
                    suppress_warnings=suppress_warnings,
                    show_missing_only=not show_all_differences,
                    ignore_timestamp_only=ignore_timestamp_only,
                    report_user=client_identity,
                    report_dir=str(report_save_dir),
                )
                
                # Update progress - generating report
                session_progress["current_file"] = "Generating report..."
                session_progress["progress"] = 85
                comparison_progress["current_file"] = "Generating report..."
                comparison_progress["progress"] = 85
                
                report_filepath = str(_report_path(saved_name))
                uxf_comparer.save_report(report_filepath)
                cleaned = cleanup_old_reports(10)
                if cleaned:
                    add_session_log(session_id, f"Report cleanup: deleted {cleaned} old report file(s)", "INFO")
                
                # Final progress update
                session_progress["progress"] = 100
                session_progress["current_file"] = "Complete!"
                comparison_progress["progress"] = 100
                comparison_progress["current_file"] = "Complete!"
                
                add_session_log(session_id, f"UXF report saved: {saved_name}", "INFO")
                add_session_log(session_id, f"=== UXF Comparison Completed ===", "INFO")
                
                # Redirect to HTML report
                html_name = os.path.basename(os.path.splitext(saved_name)[0] + '.html')
                html_check_path = str(_report_path(html_name))
                if _path_exists(html_check_path):
                    _set_session_report_result_url(session_progress, html_name)
                    # Mark BOTH session and global progress as complete
                    comparison_progress["running"] = False
                    session_progress["running"] = False
                    sink.send_response(302)
                    sink.send_header("Location", f"/view?file={quote(html_name)}")
                    sink.end_headers()
                    return
            except Exception as e:
                import traceback
                error_details = traceback.format_exc()
                add_session_log(session_id, f"UXF comparison error: {str(e)}", "ERROR")
                body = f"""<div style="background:#fee2e2;border:2px solid #fca5a5;border-radius:12px;padding:20px;margin:20px 0;">
                    <h2 style="color:#dc2626;margin:0 0 15px 0;">❌ UXF Comparison Failed</h2>
                    <p style="color:#991b1b;">{html.escape(str(e))}</p>
                    <details><summary>Technical Details</summary><pre style="background:#f5f5f5;padding:10px;overflow:auto;">{html.escape(error_details)}</pre></details>
                </div>
                <p><a href="/" style="display:inline-block;padding:10px 20px;background:#3b82f6;color:white;text-decoration:none;border-radius:8px;">← Back to Main Page</a></p>
                """
                # Mark BOTH session and global progress as stopped due to error
                comparison_progress["running"] = False
                session_progress["running"] = False
                session_progress["error"] = str(e)
                sink.send_response(500)
                sink.send_header("Content-Type", "text/html; charset=utf-8")
                sink.end_headers()
                sink.write(html_page("UXF Comparison Error", body))
                return
            # Fallback: Mark BOTH session and global progress as complete
            comparison_progress["running"] = False
            session_progress["running"] = False
            sink.send_response(302)
            sink.send_header("Location", "/")
            sink.end_headers()
            return

        # Route to DEX comparison - separate implementation
        if app_dex and not app_ascf and not app_crm and not app_uxf and not app_wef:
            if not dex_nexus_mode:
                body = """<div style="background:#fee2e2;border:2px solid #fca5a5;border-radius:12px;padding:20px;margin:20px 0;">
                    <h2 style="color:#dc2626;margin:0 0 15px 0;">❌ DEX Input Required</h2>
                    <p style="color:#991b1b;">Enter <strong>previous</strong> and <strong>latest</strong> Nexus versions for <strong>digital-playbook</strong> and/or <strong>digital-webapp-ces</strong> (e.g. 10.24.1.47 and 10.24.1.50). Artifacts are downloaded from Nexus and compared automatically.</p>
                </div>
                <p><a href="/" style="display:inline-block;padding:10px 20px;background:#3b82f6;color:white;text-decoration:none;border-radius:8px;">← Back to Main Page</a></p>
                """
                comparison_progress["running"] = False
                sink.send_response(400)
                sink.send_header("Content-Type", "text/html; charset=utf-8")
                sink.end_headers()
                sink.write(html_page("DEX Input Required", body))
                return
            try:
                session_progress["running"] = True
                session_progress["progress"] = 0
                session_progress["total"] = 100
                session_progress["current_file"] = "Initializing DEX comparison..."
                session_progress["error"] = None
                session_progress["stop_requested"] = False
                comparison_progress["running"] = True
                comparison_progress["progress"] = 0
                comparison_progress["total"] = 100
                comparison_progress["current_file"] = "Initializing DEX comparison..."
                comparison_progress["error"] = None

                add_session_log(session_id, "=== DEX Comparison Started ===", "INFO")
                add_session_log(session_id, "Using DEX comparison module", "INFO")
                if dex_playbook_pair:
                    add_session_log(session_id, f"DEX digital-playbook previous: {dex_nexus_previous}", "INFO")
                    add_session_log(session_id, f"DEX digital-playbook latest: {dex_nexus_latest}", "INFO")
                if dex_webapp_pair:
                    add_session_log(session_id, f"DEX digital-webapp-ces previous: {dex_webapp_ces_previous}", "INFO")
                    add_session_log(session_id, f"DEX digital-webapp-ces latest: {dex_webapp_ces_latest}", "INFO")
                else:
                    add_session_log(session_id, f"Old Build: {folder1}", "INFO")
                    add_session_log(session_id, f"New Build: {folder2}", "INFO")
                from dex_compare import run_dex_comparison

                def dex_log_callback(message, level="INFO"):
                    add_session_log(session_id, message, level)

                def dex_progress_callback(current_file, progress=None, total=None):
                    file_str = current_file or "Processing..."
                    session_progress["current_file"] = file_str
                    comparison_progress["current_file"] = file_str
                    if progress is not None and total is not None and total > 0:
                        pct = 10 + int((progress / total) * 70)
                        pct = min(pct, 80)
                        session_progress["progress"] = pct
                        comparison_progress["progress"] = pct
                        session_progress["total"] = 100
                        comparison_progress["total"] = 100

                session_progress["current_file"] = "Downloading from Nexus..."
                session_progress["progress"] = 5
                comparison_progress["current_file"] = "Downloading from Nexus..."
                comparison_progress["progress"] = 5

                dex_comparer, results, saved_name = run_dex_comparison(
                    folder1,
                    folder2,
                    session_id=session_id,
                    log_callback=dex_log_callback,
                    progress_callback=dex_progress_callback,
                    suppress_warnings=suppress_warnings,
                    show_missing_only=not show_all_differences,
                    ignore_timestamp_only=ignore_timestamp_only,
                    previous_nexus_version=dex_nexus_previous,
                    latest_nexus_version=dex_nexus_latest,
                    webapp_ces_previous_version=dex_webapp_ces_previous,
                    webapp_ces_latest_version=dex_webapp_ces_latest,
                    report_user=client_identity,
                    report_dir=str(report_save_dir),
                )

                session_progress["current_file"] = "Generating report..."
                session_progress["progress"] = 85
                comparison_progress["current_file"] = "Generating report..."
                comparison_progress["progress"] = 85

                report_filepath = str(_report_path(saved_name))
                dex_comparer.save_report(report_filepath)
                cleaned = cleanup_old_reports(10)
                if cleaned:
                    add_session_log(session_id, f"Report cleanup: deleted {cleaned} old report file(s)", "INFO")

                session_progress["progress"] = 100
                session_progress["current_file"] = "Complete!"
                comparison_progress["progress"] = 100
                comparison_progress["current_file"] = "Complete!"

                add_session_log(session_id, f"DEX report saved: {saved_name}", "INFO")
                add_session_log(session_id, "=== DEX Comparison Completed ===", "INFO")

                html_name = os.path.basename(os.path.splitext(saved_name)[0] + ".html")
                html_check_path = str(_report_path(html_name))
                if _path_exists(html_check_path):
                    _set_session_report_result_url(session_progress, html_name)
                    comparison_progress["running"] = False
                    session_progress["running"] = False
                    sink.send_response(302)
                    sink.send_header("Location", f"/view?file={quote(html_name)}")
                    sink.end_headers()
                    return
            except Exception as e:
                import traceback
                error_details = traceback.format_exc()
                add_session_log(session_id, f"DEX comparison error: {str(e)}", "ERROR")
                body = f"""<div style="background:#fee2e2;border:2px solid #fca5a5;border-radius:12px;padding:20px;margin:20px 0;">
                    <h2 style="color:#dc2626;margin:0 0 15px 0;">❌ DEX Comparison Failed</h2>
                    <p style="color:#991b1b;">{html.escape(str(e))}</p>
                    <details><summary>Technical Details</summary><pre style="background:#f5f5f5;padding:10px;overflow:auto;">{html.escape(error_details)}</pre></details>
                </div>
                <p><a href="/" style="display:inline-block;padding:10px 20px;background:#3b82f6;color:white;text-decoration:none;border-radius:8px;">← Back to Main Page</a></p>
                """
                comparison_progress["running"] = False
                session_progress["running"] = False
                session_progress["error"] = str(e)
                sink.send_response(500)
                sink.send_header("Content-Type", "text/html; charset=utf-8")
                sink.end_headers()
                sink.write(html_page("DEX Comparison Error", body))
                return
            comparison_progress["running"] = False
            session_progress["running"] = False
            sink.send_response(302)
            sink.send_header("Location", "/")
            sink.end_headers()
            return

        # Route to WEF comparison — remote SSH inventory on Unix/Linux
        if app_wef and not app_ascf and not app_crm and not app_uxf and not app_dex:
            missing = []
            if not wef_ssh_host:
                missing.append("SSH host / IP")
            if not wef_ssh_username:
                missing.append("SSH username")
            if not wef_ssh_password:
                missing.append("SSH password")
            if not wef_old_path:
                missing.append("old build path on Unix")
            if not wef_new_path:
                missing.append("new build path on Unix")
            if missing:
                body = f"""<div style="background:#fee2e2;border:2px solid #fca5a5;border-radius:12px;padding:20px;margin:20px 0;">
                    <h2 style="color:#dc2626;margin:0 0 15px 0;">❌ WEF Input Required</h2>
                    <p style="color:#991b1b;">Please provide: {html.escape(', '.join(missing))}.</p>
                </div>
                <p><a href="/" style="display:inline-block;padding:10px 20px;background:#3b82f6;color:white;text-decoration:none;border-radius:8px;">← Back to Main Page</a></p>
                """
                comparison_progress["running"] = False
                sink.send_response(400)
                sink.send_header("Content-Type", "text/html; charset=utf-8")
                sink.end_headers()
                sink.write(html_page("WEF Input Required", body))
                return
            try:
                session_progress["running"] = True
                session_progress["progress"] = 0
                session_progress["total"] = 100
                session_progress["current_file"] = "Initializing WEF comparison..."
                session_progress["error"] = None
                session_progress["stop_requested"] = False
                comparison_progress["running"] = True
                comparison_progress["progress"] = 0
                comparison_progress["total"] = 100
                comparison_progress["current_file"] = "Initializing WEF comparison..."
                comparison_progress["error"] = None

                add_session_log(session_id, "=== WEF Comparison Started ===", "INFO")
                add_session_log(session_id, "Using WEF remote SSH comparison module", "INFO")
                add_session_log(session_id, f"SSH: {wef_ssh_username}@{wef_ssh_host}:{wef_ssh_port}", "INFO")
                add_session_log(session_id, f"Old path: {wef_old_path}", "INFO")
                add_session_log(session_id, f"New path: {wef_new_path}", "INFO")
                from wef_compare import run_wef_comparison

                def wef_log_callback(message, level="INFO"):
                    add_session_log(session_id, message, level)

                def wef_progress_callback(current_file, progress=None, total=None):
                    file_str = current_file or "Processing..."
                    session_progress["current_file"] = file_str
                    comparison_progress["current_file"] = file_str
                    if progress is not None and total is not None and total > 0:
                        pct = 10 + int((progress / total) * 80)
                        pct = min(pct, 90)
                        session_progress["progress"] = pct
                        comparison_progress["progress"] = pct
                        session_progress["total"] = 100
                        comparison_progress["total"] = 100

                session_progress["current_file"] = "Connecting via SSH..."
                session_progress["progress"] = 5
                comparison_progress["current_file"] = "Connecting via SSH..."
                comparison_progress["progress"] = 5

                wef_comparer, results, saved_name = run_wef_comparison(
                    wef_ssh_host,
                    wef_ssh_username,
                    wef_ssh_password,
                    wef_old_path,
                    wef_new_path,
                    ssh_port=wef_ssh_port,
                    session_id=session_id,
                    log_callback=wef_log_callback,
                    progress_callback=wef_progress_callback,
                    show_missing_only=not show_all_differences,
                    report_user=client_identity,
                    report_dir=str(report_save_dir),
                )

                if wef_comparer.comparison_error:
                    raise RuntimeError(wef_comparer.comparison_error)

                session_progress["current_file"] = "Generating report..."
                session_progress["progress"] = 92
                comparison_progress["current_file"] = "Generating report..."
                comparison_progress["progress"] = 92

                report_filepath = str(_report_path(saved_name))
                wef_comparer.save_report(report_filepath)
                cleaned = cleanup_old_reports(10)
                if cleaned:
                    add_session_log(session_id, f"Report cleanup: deleted {cleaned} old report file(s)", "INFO")

                session_progress["progress"] = 100
                session_progress["current_file"] = "Complete!"
                comparison_progress["progress"] = 100
                comparison_progress["current_file"] = "Complete!"

                add_session_log(session_id, f"WEF report saved: {saved_name}", "INFO")
                add_session_log(session_id, "=== WEF Comparison Completed ===", "INFO")

                html_name = os.path.basename(os.path.splitext(saved_name)[0] + ".html")
                html_check_path = str(_report_path(html_name))
                if _path_exists(html_check_path):
                    _set_session_report_result_url(session_progress, html_name)
                    comparison_progress["running"] = False
                    session_progress["running"] = False
                    sink.send_response(302)
                    sink.send_header("Location", f"/view?file={quote(html_name)}")
                    sink.end_headers()
                    return
            except Exception as e:
                import traceback
                error_details = traceback.format_exc()
                add_session_log(session_id, f"WEF comparison error: {str(e)}", "ERROR")
                body = f"""<div style="background:#fee2e2;border:2px solid #fca5a5;border-radius:12px;padding:20px;margin:20px 0;">
                    <h2 style="color:#dc2626;margin:0 0 15px 0;">❌ WEF Comparison Failed</h2>
                    <p style="color:#991b1b;">{html.escape(str(e))}</p>
                    <details><summary>Technical Details</summary><pre style="background:#f5f5f5;padding:10px;overflow:auto;">{html.escape(error_details)}</pre></details>
                </div>
                <p><a href="/" style="display:inline-block;padding:10px 20px;background:#3b82f6;color:white;text-decoration:none;border-radius:8px;">← Back to Main Page</a></p>
                """
                comparison_progress["running"] = False
                session_progress["running"] = False
                session_progress["error"] = str(e)
                sink.send_response(500)
                sink.send_header("Content-Type", "text/html; charset=utf-8")
                sink.end_headers()
                sink.write(html_page("WEF Comparison Error", body))
                return
            comparison_progress["running"] = False
            session_progress["running"] = False
            sink.send_response(302)
            sink.send_header("Location", "/")
            sink.end_headers()
            return

        # ASCF comparison - current working implementation (default behavior)
        # This is the original code that was working before
        add_session_log(session_id, "Using ASCF comparison module", "INFO")

        try:
            if mode == "naming":
                from pathlib import Path as P
                # Check if inputs are zip files - use fast method if so
                p1 = P(folder1)
                p2 = P(folder2)
                
                # Use safe path check functions to handle network paths and permissions
                try:
                    is_p1_archive = _path_exists(p1) and _path_is_file(p1) and p1.suffix.lower() in ['.zip', '.jar', '.war', '.ear']
                    is_p2_archive = _path_exists(p2) and _path_is_file(p2) and p2.suffix.lower() in ['.zip', '.jar', '.war', '.ear']
                except (PermissionError, OSError) as e:
                    print(f"[DEBUG] Permission error checking paths, falling back to folder comparison: {e}")
                    is_p1_archive = False
                    is_p2_archive = False
                
                if is_p1_archive and is_p2_archive:
                    # Fast comparison for zip files - no extraction needed
                    import zipfile
                    
                    # Quick archive listing
                    def list_archives_in_zip(zip_path):
                        import os
                        archives = {}
                        try:
                            with zipfile.ZipFile(zip_path, 'r') as zf:
                                for member in zf.namelist():
                                    if any(member.lower().endswith(ext) for ext in ['.jar', '.war', '.ear', '.zip']):
                                        filename = os.path.basename(member)
                                        if filename and not member.endswith('/'):
                                            archives[filename] = member
                        except Exception as e:
                            print(f"Error reading {zip_path}: {e}")
                        return archives
                    
                    archives1 = list_archives_in_zip(p1)
                    archives2 = list_archives_in_zip(p2)
                    
                    # Generate simple comparison report
                    saved_name = _report_fname("ascf_naming_convention_report")
                    set1 = set(archives1.keys())
                    set2 = set(archives2.keys())
                    only_in_1 = sorted(set1 - set2)
                    only_in_2 = sorted(set2 - set1)
                    in_both = sorted(set1 & set2)
                    
                    report_lines = []
                    report_lines.append("=" * 80)
                    report_lines.append("FAST ARCHIVE NAMING CONVENTION COMPARISON")
                    report_lines.append("=" * 80)
                    report_lines.append(f"Old Build Path: {folder1}")
                    report_lines.append(f"New Build Path: {folder2}")
                    report_lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                    report_lines.append("")
                    report_lines.append(f"Total archives in Old Build: {len(archives1)}")
                    report_lines.append(f"Total archives in New Build: {len(archives2)}")
                    report_lines.append(f"Exact matches: {len(in_both)}")
                    report_lines.append(f"Removed in New Build: {len(only_in_1)}")
                    report_lines.append(f"Added in New Build: {len(only_in_2)}")
                    report_lines.append("")
                    
                    if only_in_1:
                        report_lines.append("ARCHIVES ONLY IN FOLDER 1:")
                        report_lines.append("-" * 80)
                        for name in only_in_1:
                            report_lines.append(f"  [X] {name}")
                            report_lines.append(f"      Path: {archives1[name]}")
                        report_lines.append("")
                    
                    if only_in_2:
                        report_lines.append("ARCHIVES ONLY IN FOLDER 2:")
                        report_lines.append("-" * 80)
                        for name in only_in_2:
                            report_lines.append(f"  [+] {name}")
                            report_lines.append(f"      Path: {archives2[name]}")
                        report_lines.append("")
                    
                    if in_both:
                        report_lines.append(f"EXACT MATCHES ({len(in_both)} archives):")
                        report_lines.append("-" * 80)
                        for name in sorted(in_both)[:50]:
                            report_lines.append(f"  [OK] {name}")
                        if len(in_both) > 50:
                            report_lines.append(f"  ... and {len(in_both) - 50} more")
                    
                    report_text = "\n".join(report_lines)
                    
                    # Save report
                    report_filepath = str(_report_path(saved_name))
                    with open(report_filepath, 'w', encoding='utf-8') as f:
                        f.write(report_text)
                    
                    # Identify truly NEW files (not just version changes)
                    def get_base_name(filename):
                        """Extract base name without version/build numbers"""
                        import re
                        # Remove version patterns like -10.1.0.pb00_hf29.319
                        base = re.sub(r'-\d+\.\d+\.\d+\.[\w\d_.-]+\.(jar|war|ear|zip)$', '.\\1', filename)
                        # Remove date/build patterns
                        base = re.sub(r'_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}', '', base)
                        base = re.sub(r'_\d{8,}', '', base)
                        return base.lower()
                    
                    # Find truly additional files (base name doesn't exist in other folder)
                    bases_in_1 = {get_base_name(name) for name in only_in_1}
                    bases_in_2 = {get_base_name(name) for name in only_in_2}
                    
                    truly_new_in_2 = []
                    for name in only_in_2:
                        base = get_base_name(name)
                        if base not in bases_in_1:
                            truly_new_in_2.append(name)
                    
                    truly_new_in_1 = []
                    for name in only_in_1:
                        base = get_base_name(name)
                        if base not in bases_in_2:
                            truly_new_in_1.append(name)
                    
                    # Generate styled HTML report
                    html_content = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Archive Naming Convention Comparison</title>
    <style>
        * {{ box-sizing: border-box; }}
        body {{ font-family: 'Segoe UI', system-ui, -apple-system, sans-serif; margin: 0; padding: 20px; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); min-height: 100vh; }}
        .container {{ max-width: 1200px; margin: 0 auto; background: white; border-radius: 16px; box-shadow: 0 20px 60px rgba(0,0,0,0.3); overflow: hidden; }}
        .header {{ background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 30px; }}
        .header h1 {{ margin: 0 0 10px 0; font-size: 28px; font-weight: 600; }}
        .header .subtitle {{ opacity: 0.9; font-size: 14px; }}
        .header .paths {{ margin-top: 15px; padding-top: 15px; border-top: 1px solid rgba(255,255,255,0.3); }}
        .header .path-label {{ font-weight: 600; font-size: 12px; text-transform: uppercase; opacity: 0.8; margin-top: 8px; }}
        .header .path-value {{ font-family: 'Consolas', monospace; font-size: 13px; margin-top: 4px; word-break: break-all; }}
        .content {{ padding: 30px; }}
        .summary {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 20px; margin-bottom: 30px; }}
        .stat-card {{ background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%); color: white; padding: 20px; border-radius: 12px; box-shadow: 0 4px 15px rgba(0,0,0,0.1); }}
        .stat-card.success {{ background: linear-gradient(135deg, #4facfe 0%, #00f2fe 100%); }}
        .stat-card.warning {{ background: linear-gradient(135deg, #fa709a 0%, #fee140 100%); }}
        .stat-card.info {{ background: linear-gradient(135deg, #30cfd0 0%, #330867 100%); }}
        .stat-card .label {{ font-size: 13px; font-weight: 500; opacity: 0.9; text-transform: uppercase; letter-spacing: 0.5px; }}
        .stat-card .value {{ font-size: 36px; font-weight: 700; margin-top: 8px; }}
        .section {{ margin-bottom: 40px; }}
        .section-title {{ font-size: 22px; font-weight: 600; color: #1a202c; margin-bottom: 20px; padding-bottom: 10px; border-bottom: 3px solid #667eea; }}
        .archive-list {{ background: #f7fafc; border-radius: 8px; padding: 20px; }}
        .archive-item {{ background: white; padding: 15px; margin-bottom: 12px; border-radius: 8px; border-left: 4px solid #667eea; box-shadow: 0 2px 4px rgba(0,0,0,0.05); }}
        .archive-item.only-in-1 {{ border-left-color: #f56565; }}
        .archive-item.only-in-2 {{ border-left-color: #48bb78; }}
        .archive-item.match {{ border-left-color: #4299e1; }}
        .archive-name {{ font-family: 'Consolas', monospace; font-size: 14px; font-weight: 600; color: #2d3748; margin-bottom: 6px; }}
        .archive-path {{ font-family: 'Consolas', monospace; font-size: 12px; color: #718096; padding-left: 20px; }}
        .badge {{ display: inline-block; padding: 4px 12px; border-radius: 12px; font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; }}
        .badge.removed {{ background: #fed7d7; color: #c53030; }}
        .badge.added {{ background: #c6f6d5; color: #2f855a; }}
        .badge.match {{ background: #bee3f8; color: #2c5282; }}
        .back-link {{ display: inline-block; margin-top: 20px; padding: 10px 20px; background: #667eea; color: white; text-decoration: none; border-radius: 8px; font-weight: 600; transition: transform 0.2s; }}
        .back-link:hover {{ transform: translateY(-2px); background: #5a67d8; }}
        .empty-state {{ text-align: center; padding: 40px; color: #718096; }}
        .grid-2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 30px; }}
        .expand-link {{ display: block; text-align: center; padding: 15px; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; cursor: pointer; border-radius: 8px; font-weight: 600; margin-top: 15px; transition: all 0.3s; }}
        .expand-link:hover {{ transform: scale(1.02); box-shadow: 0 4px 15px rgba(102,126,234,0.4); }}
        .expand-link .icon {{ display: inline-block; margin-left: 8px; transition: transform 0.3s; }}
        .expand-link.expanded .icon {{ transform: rotate(180deg); }}
        .more-matches {{ display: none; margin-top: 15px; }}
        .more-matches.visible {{ display: block; }}
        .nav-menu {{ background: #f7fafc; padding: 20px; border-radius: 8px; margin-bottom: 30px; }}
        .nav-menu h3 {{ margin: 0 0 15px 0; font-size: 18px; color: #2d3748; }}
        .nav-links {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 12px; }}
        .nav-link {{ display: block; padding: 12px 16px; background: white; border-left: 4px solid #667eea; border-radius: 6px; text-decoration: none; color: #2d3748; font-weight: 600; transition: all 0.2s; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
        .nav-link:hover {{ transform: translateX(4px); border-left-width: 6px; box-shadow: 0 2px 8px rgba(0,0,0,0.15); }}
        .nav-link.missing {{ border-left-color: #f56565; }}
        .nav-link.added {{ border-left-color: #48bb78; }}
        .nav-link.new {{ border-left-color: #ed8936; }}
        .nav-link.matches {{ border-left-color: #4299e1; }}
        .highlight-new {{ background: linear-gradient(90deg, #ed8936 0%, #f6ad55 100%); color: white; padding: 3px 8px; border-radius: 4px; font-size: 11px; font-weight: 700; margin-left: 8px; }}
        /* Table-based comparison view */
        .comparison-table {{ width: 100%; border-collapse: separate; border-spacing: 0; background: white; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }}
        .comparison-table thead {{ background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; }}
        .comparison-table th {{ padding: 16px; text-align: left; font-weight: 600; font-size: 14px; text-transform: uppercase; letter-spacing: 0.5px; border-bottom: 2px solid #5a67d8; }}
        .comparison-table th.center {{ text-align: center; }}
        .comparison-table tbody tr {{ transition: all 0.2s; }}
        .comparison-table tbody tr:hover {{ background: #f7fafc; transform: scale(1.01); box-shadow: 0 2px 8px rgba(0,0,0,0.1); }}
        .comparison-table td {{ padding: 14px 16px; border-bottom: 1px solid #e2e8f0; font-family: 'Consolas', monospace; font-size: 13px; vertical-align: middle; }}
        .comparison-table td.filename {{ font-weight: 600; color: #2d3748; max-width: 400px; word-break: break-word; }}
        .comparison-table td.path {{ color: #718096; font-size: 11px; max-width: 300px; word-break: break-all; }}
        .comparison-table td.status {{ text-align: center; font-weight: 600; width: 150px; }}
        .comparison-table td.center {{ text-align: center; }}
        .comparison-table tr.identical {{ background: #f0f9ff; }}
        .comparison-table tr.only-left {{ background: #fef2f2; }}
        .comparison-table tr.only-right {{ background: #f0fdf4; }}
        .comparison-table tr.new-file {{ background: #fffbeb; }}
        .status-badge {{ display: inline-block; padding: 6px 14px; border-radius: 20px; font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px; white-space: nowrap; }}
        .status-badge.identical {{ background: #dbeafe; color: #1e40af; }}
        .status-badge.missing_left {{ background: #dcfce7; color: #166534; }}
        .status-badge.missing_right {{ background: #fee2e2; color: #991b1b; }}
        .status-badge.additional_left {{ background: #fef3c7; color: #92400e; }}
        .status-badge.additional_right {{ background: #fef3c7; color: #92400e; }}
        .file-icon {{ display: inline-block; width: 20px; height: 20px; margin-right: 8px; vertical-align: middle; }}
        .table-container {{ background: #f7fafc; border-radius: 8px; padding: 20px; margin-bottom: 30px; }}
        .table-header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 15px; }}
        .table-title {{ font-size: 18px; font-weight: 600; color: #1a202c; }}
        .table-count {{ background: #667eea; color: white; padding: 4px 12px; border-radius: 12px; font-size: 13px; font-weight: 600; }}
        .empty-cell {{ color: #cbd5e0; text-align: center; font-style: italic; }}
        @media (max-width: 768px) {{ .grid-2 {{ grid-template-columns: 1fr; }} .summary {{ grid-template-columns: 1fr; }} .nav-links {{ grid-template-columns: 1fr; }} .comparison-table {{ font-size: 11px; }} .comparison-table td.filename {{ max-width: 200px; }} .comparison-table td.path {{ max-width: 150px; }} }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
        <h1>🔍 Archive Naming Convention Comparison</h1>
        <div class="subtitle">Fast comparison completed in seconds • {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</div>
        <div class="paths">
            <div class="path-label">📁 Folder 1 (BN343)</div>
            <div class="path-value">{html.escape(folder1)}</div>
            <div class="path-label">📁 Folder 2 (BN344)</div>
            <div class="path-value">{html.escape(folder2)}</div>
        </div>
        </div>
        
        <div class="content">
        <!-- Navigation Menu -->
        <div class="nav-menu">
            <h3>📑 Quick Navigation</h3>
            <div class="nav-links">"""
                    
                    if truly_new_in_2 or truly_new_in_1:
                        html_content += f"""
                <a href="#additional-files" class="nav-link new">🆕 Additional/New Files ({len(truly_new_in_2) + len(truly_new_in_1)})</a>"""
                    
                    html_content += f"""
                <a href="#comparison-table" class="nav-link" style="border-left-color: #9f7aea;">📊 Detailed Comparison Table</a>
                <a href="#missing-folder2" class="nav-link missing">🗑️ Removed in New Build ({len(only_in_1)})</a>
                <a href="#missing-folder1" class="nav-link added">✨ Added in New Build ({len(only_in_2)})</a>
                <a href="#exact-matches" class="nav-link matches">✓ Exact Matches ({len(in_both)})</a>
            </div>
        </div>
        
        <div class="summary">
            <div class="stat-card info">
                <div class="label">Total Archives (Old Build)</div>
                <div class="value">{len(archives1)}</div>
            </div>
            <div class="stat-card info">
                <div class="label">Total Archives (New Build)</div>
                <div class="value">{len(archives2)}</div>
            </div>
            <div class="stat-card success">
                <div class="label">Exact Matches</div>
                <div class="value">{len(in_both)}</div>
            </div>
            <div class="stat-card warning">
                <div class="label">Differences Found</div>
                <div class="value">{len(only_in_1) + len(only_in_2)}</div>
            </div>
        </div>"""
                    
                    # Create comprehensive comparison table (Beyond Compare style)
                    # Build a unified list of all files with their status
                    all_files = {}
                    
                    # Add files from folder 1
                    for name in archives1:
                        all_files[name] = {'in_1': True, 'path_1': archives1[name], 'in_2': False, 'path_2': None}
                    
                    # Add files from folder 2
                    for name in archives2:
                        if name in all_files:
                            all_files[name]['in_2'] = True
                            all_files[name]['path_2'] = archives2[name]
                        else:
                            all_files[name] = {'in_1': False, 'path_1': None, 'in_2': True, 'path_2': archives2[name]}
                    
                    # Determine status for each file
                    for name in all_files:
                        file_info = all_files[name]
                        base = get_base_name(name)
                        
                        if file_info['in_1'] and file_info['in_2']:
                            file_info['status'] = 'identical'
                            file_info['status_text'] = '✓ Identical'
                            file_info['row_class'] = 'identical'
                        elif file_info['in_1'] and not file_info['in_2']:
                            # Check if it's truly new or just a version change
                            is_truly_new = name in truly_new_in_1
                            if is_truly_new:
                                file_info['status'] = 'additional-left'
                                file_info['status_text'] = '🆕 Only in Left (New)'
                                file_info['row_class'] = 'new-file'
                            else:
                                file_info['status'] = 'missing-right'
                                file_info['status_text'] = 'Only in Left'
                                file_info['row_class'] = 'only-left'
                        else:  # in_2 but not in_1
                            # Check if it's truly new or just a version change
                            is_truly_new = name in truly_new_in_2
                            if is_truly_new:
                                file_info['status'] = 'additional-right'
                                file_info['status_text'] = '🆕 Only in Right (New)'
                                file_info['row_class'] = 'new-file'
                            else:
                                file_info['status'] = 'missing-left'
                                file_info['status_text'] = 'Only in Right'
                                file_info['row_class'] = 'only-right'
                    
                    # Sort files: differences first, then matches
                    sorted_files = sorted(all_files.items(), key=lambda x: (
                        0 if x[1]['status'] != 'identical' else 1,  # Differences first
                        x[0].lower()  # Then alphabetically
                    ))
                    
                    html_content += f"""
        <!-- Comprehensive Comparison Table -->
        <div class="section" id="comparison-table">
            <div class="section-title">📊 Detailed Comparison Table</div>
            <div class="table-container">
                <div style="background: #e0e7ff; padding: 15px; border-radius: 8px; margin-bottom: 15px; border-left: 4px solid #6366f1;">
                    <strong style="color: #3730a3;">💡 Side-by-Side Comparison (Beyond Compare Style)</strong>
                    <div style="color: #4338ca; font-size: 13px; margin-top: 8px;">
                        • <span style="background: #f0f9ff; padding: 2px 6px; border-radius: 3px;">Blue</span> = Identical files
                        • <span style="background: #fef2f2; padding: 2px 6px; border-radius: 3px;">Red</span> = Missing in right folder
                        • <span style="background: #f0fdf4; padding: 2px 6px; border-radius: 3px;">Green</span> = Missing in left folder
                        • <span style="background: #fffbeb; padding: 2px 6px; border-radius: 3px;">Yellow</span> = Additional/New files
                    </div>
                </div>
                <table class="comparison-table">
                    <thead>
                        <tr>
                            <th>File Name</th>
                            <th class="center">Status</th>
                            <th>Folder 1 (Left)</th>
                            <th>Folder 2 (Right)</th>
                        </tr>
                    </thead>
                    <tbody>"""
                    
                    # Add table rows
                    for name, info in sorted_files:
                        status_class = info['status'].replace('-', '_')
                        html_content += f"""
                        <tr class="{info['row_class']}">
                            <td class="filename">{html.escape(name)}</td>
                            <td class="status">
                                <span class="status-badge {status_class}">{info['status_text']}</span>
                            </td>
                            <td class="path">
                                {html.escape(info['path_1']) if info['path_1'] else '<span class="empty-cell">—</span>'}
                            </td>
                            <td class="path">
                                {html.escape(info['path_2']) if info['path_2'] else '<span class="empty-cell">—</span>'}
                            </td>
                        </tr>"""
                    
                    html_content += """
                    </tbody>
                </table>
            </div>
        </div>"""
                    
                    # Additional/New Files Section
                    if truly_new_in_2 or truly_new_in_1:
                        html_content += f"""
        <div class="section" id="additional-files">
            <div class="section-title">🆕 Additional/New Files ({len(truly_new_in_2) + len(truly_new_in_1)} files)</div>
            <div class="archive-list">
                <div style="background: #fffbeb; padding: 15px; border-radius: 8px; border-left: 4px solid #f59e0b; margin-bottom: 15px;">
                    <strong style="color: #92400e;">ℹ️ These are truly NEW files (not just version updates)</strong>
                </div>"""
                        
                        if truly_new_in_2:
                            html_content += f"""
                <h4 style="color: #2d3748; margin: 15px 0 10px 0;">✅ Additional in Folder 2 ({len(truly_new_in_2)} files)</h4>"""
                            for name in truly_new_in_2:
                                path = archives2[name]
                                html_content += f"""
                <div class="archive-item only-in-2">
                    <div class="archive-name"><span class="badge added">NEW</span> {html.escape(name)} <span class="highlight-new">ADDITIONAL</span></div>
                    <div class="archive-path">📍 {html.escape(path)}</div>
                </div>"""
                        
                        if truly_new_in_1:
                            html_content += f"""
                <h4 style="color: #2d3748; margin: 15px 0 10px 0;">❌ Additional in Folder 1 ({len(truly_new_in_1)} files)</h4>"""
                            for name in truly_new_in_1:
                                path = archives1[name]
                                html_content += f"""
                <div class="archive-item only-in-1">
                    <div class="archive-name"><span class="badge removed">NEW</span> {html.escape(name)} <span class="highlight-new">ADDITIONAL</span></div>
                    <div class="archive-path">📍 {html.escape(path)}</div>
                </div>"""
                        
                        html_content += """
            </div>
        </div>"""
                    
                    # Only in Folder 1 section (MISSING from Folder 2)
                    if only_in_1:
                        html_content += f"""
        <div class="section" id="missing-folder2">
            <div class="section-title">🗑️ Removed in New Build ({len(only_in_1)} files)</div>
            <div class="archive-list">"""
                        for name in only_in_1:
                            path = archives1[name]
                            html_content += f"""
                <div class="archive-item only-in-1">
                    <div class="archive-name"><span class="badge removed">Removed</span> {html.escape(name)}</div>
                    <div class="archive-path">📍 {html.escape(path)}</div>
                </div>"""
                        html_content += """
            </div>
        </div>"""
                    
                    # Only in Folder 2 section (MISSING from Folder 1)
                    if only_in_2:
                        html_content += f"""
        <div class="section" id="missing-folder1">
            <div class="section-title">✨ Added in New Build ({len(only_in_2)} files)</div>
            <div class="archive-list">"""
                        for name in only_in_2:
                            path = archives2[name]
                            html_content += f"""
                <div class="archive-item only-in-2">
                    <div class="archive-name"><span class="badge added">Added</span> {html.escape(name)}</div>
                    <div class="archive-path">📍 {html.escape(path)}</div>
                </div>"""
                        html_content += """
            </div>
        </div>"""
                    
                    # Exact matches section (show first 50, expand for more)
                    if in_both:
                        show_count = min(50, len(in_both))
                        sorted_matches = sorted(in_both)
                        html_content += f"""
        <div class="section" id="exact-matches">
            <div class="section-title">✓ Exact Matches ({len(in_both)} files)</div>
            <div class="archive-list">"""
                        # Show first 50
                        for name in sorted_matches[:50]:
                            html_content += f"""
                <div class="archive-item match">
                    <div class="archive-name"><span class="badge match">Match</span> {html.escape(name)}</div>
                </div>"""
                        
                        # If more than 50, add expandable section
                        if len(in_both) > 50:
                            remaining = len(in_both) - 50
                            html_content += f"""
                <div class="expand-link" onclick="toggleMoreMatches()">
                    Click to show {remaining} more matching files <span class="icon">▼</span>
                </div>
                <div class="more-matches" id="moreMatches">"""
                            # Show remaining files
                            for name in sorted_matches[50:]:
                                html_content += f"""
                    <div class="archive-item match">
                        <div class="archive-name"><span class="badge match">Match</span> {html.escape(name)}</div>
                    </div>"""
                            html_content += """
                </div>"""
                        html_content += """
            </div>
        </div>"""
                    
                    html_content += f"""
        <a href="/" class="back-link">← Back to Home</a>
        </div>
    </div>
    
    <script>
        const remainingCount = {len(in_both) - 50 if len(in_both) > 50 else 0};
        
        function toggleMoreMatches() {{
        const moreSection = document.getElementById('moreMatches');
        const expandLink = document.querySelector('.expand-link');
        
        if (!moreSection || !expandLink) return;
        
        if (moreSection.classList.contains('visible')) {{
            // Collapse
            moreSection.classList.remove('visible');
            expandLink.classList.remove('expanded');
            expandLink.innerHTML = 'Click to show ' + remainingCount + ' more matching files <span class="icon">▼</span>';
            // Scroll back to the expand button
            expandLink.scrollIntoView({{ behavior: 'smooth', block: 'center' }});
        }} else {{
            // Expand
            moreSection.classList.add('visible');
            expandLink.classList.add('expanded');
            expandLink.innerHTML = 'Click to hide matching files <span class="icon">▼</span>';
            // Small delay to let the content render before scrolling
            setTimeout(function() {{
                moreSection.scrollIntoView({{ behavior: 'smooth', block: 'nearest' }});
            }}, 100);
        }}
        }}
    </script>
</body>
</html>"""
                    
                    # Use P (the local import) or recreate path
                    html_filename = os.path.splitext(saved_name)[0] + '.html'
                    html_filepath = str(_report_path(html_filename))
                    with open(html_filepath, 'w', encoding='utf-8') as f:
                        f.write(inject_report_section_controls(html_content))
                    
                    results = True  # Mark as successful
                else:
                    # Standard comparison for folders
                    comparer = NamingConventionComparer(folder1, folder2)
                    comparer.use_local_copy = use_local_copy
                    comparer.max_retries = max_retries
                    comparer.retry_delay = retry_delay
                    comparer.should_cleanup_temp_folders = not keep_temp_folders
                    comparer.suppress_warnings = suppress_warnings
                    comparison_progress["comparer"] = comparer
                    session_progress["comparer"] = comparer
                    results = comparer.compare_naming_conventions()
                    report_text = comparer.generate_naming_convention_report() if results is not None else "No results."
                    saved_name = _report_fname("ascf_naming_convention_report")
                    try:
                        report_filepath = str(_report_path(saved_name))
                        comparer.save_naming_convention_report(report_filepath)
                    except Exception:
                        pass
                    
                    # Generate HTML table view for folder naming convention comparison
                    if results is not None:
                        # Convert list values to strings (take first path if multiple exist)
                        archives1_raw = {**comparer.jar_files_folder1, **comparer.other_files_folder1}
                        archives2_raw = {**comparer.jar_files_folder2, **comparer.other_files_folder2}
                        
                        # NamingConventionComparer stores paths as lists, convert to strings
                        archives1 = {name: (paths[0] if isinstance(paths, list) and paths else paths) 
                                    for name, paths in archives1_raw.items()}
                        archives2 = {name: (paths[0] if isinstance(paths, list) and paths else paths) 
                                    for name, paths in archives2_raw.items()}
                        
                        # Debug: Check if data exists
                        if not archives1 and not archives2:
                            # Try to get all files if the specific dictionaries are empty
                            from pathlib import Path as PathLib
                            
                            def scan_folder_for_archives(folder_path):
                                """Scan folder for archive files"""
                                archives = {}
                                folder = PathLib(folder_path)
                                try:
                                    folder_exists = _path_exists(folder)
                                    folder_is_dir = folder_exists and _path_is_dir(folder)
                                except (PermissionError, OSError):
                                    folder_exists = False
                                    folder_is_dir = False
                                if folder_exists and folder_is_dir:
                                    for root, dirs, files in os.walk(str(folder)):
                                        for file in files:
                                            if any(file.lower().endswith(ext) for ext in ['.jar', '.war', '.ear', '.zip']):
                                                full_path = os.path.join(root, file)
                                                rel_path = os.path.relpath(full_path, str(folder))
                                                archives[file] = rel_path
                                return archives
                            
                            archives1 = scan_folder_for_archives(folder1)
                            archives2 = scan_folder_for_archives(folder2)
                        
                        # Reuse the same HTML generation logic
                        only_in_1 = sorted(set(archives1.keys()) - set(archives2.keys()))
                        only_in_2 = sorted(set(archives2.keys()) - set(archives1.keys()))
                        in_both = sorted(set(archives1.keys()) & set(archives2.keys()))
                        
                        # Identify truly NEW files
                        def get_base_name(filename):
                            import re
                            base = re.sub(r'-\d+\.\d+\.\d+\.[\w\d_.-]+\.(jar|war|ear|zip)$', r'.\1', filename)
                            base = re.sub(r'_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}', '', base)
                            base = re.sub(r'_\d{8,}', '', base)
                            return base.lower()
                        
                        bases_in_1 = {get_base_name(name) for name in only_in_1}
                        bases_in_2 = {get_base_name(name) for name in only_in_2}
                        
                        truly_new_in_2 = [name for name in only_in_2 if get_base_name(name) not in bases_in_1]
                        truly_new_in_1 = [name for name in only_in_1 if get_base_name(name) not in bases_in_2]
                        
                        # Generate HTML report (same structure as zip file comparison)
                        html_content = self._generate_comparison_html_table(
                            folder1, folder2, archives1, archives2, only_in_1, only_in_2, 
                            in_both, truly_new_in_1, truly_new_in_2, get_base_name
                        )
                        
                        html_filename = os.path.splitext(saved_name)[0] + '.html'
                        html_filepath = str(_report_path(html_filename))
                        with open(html_filepath, 'w', encoding='utf-8') as f:
                            f.write(html_content)
            elif mode == "archives":
                # Archive Contents Comparison with DEEP JAR Naming Convention Analysis
                # Goes inside nested WAR/EAR files to compare JAR naming conventions
                from pathlib import Path as P
                import zipfile
                import re
                from io import BytesIO
                
                p1 = P(folder1)
                p2 = P(folder2)
                
                print(f"[DEBUG] Archive comparison starting...")
                print(f"[DEBUG] Path 1: {folder1}, exists: {_path_exists(p1)}, is_file: {_path_is_file(p1)}")
                print(f"[DEBUG] Path 2: {folder2}, exists: {_path_exists(p2)}, is_file: {_path_is_file(p2)}")
                
                # Use CRM prefix if this was a CRM archive request
                report_prefix = "crm_archives_report" if crm_archive_mode else "ascf_archives_report"
                saved_name = _report_fname(report_prefix)
                
                # Supported archive extensions (including RPM)
                supported_extensions = ['.zip', '.jar', '.war', '.ear', '.rpm']
                
                # Helper to find archive files in a folder
                def find_archives_in_folder(folder_path):
                    """Find all archive files (.ear, .war, .jar, .rpm, .zip) in a folder"""
                    archives = []
                    try:
                        for root, dirs, files in os.walk(str(folder_path)):
                            for f in files:
                                if any(f.lower().endswith(ext) for ext in supported_extensions):
                                    archives.append(P(root) / f)
                    except Exception as e:
                        print(f"[DEBUG] Error scanning folder {folder_path}: {e}")
                    return archives
                
                # Check if paths are folders containing archives
                is_folder1 = _path_exists(p1) and not _path_is_file(p1)
                is_folder2 = _path_exists(p2) and not _path_is_file(p2)
                
                # Helper to get base name for matching (remove version numbers)
                def get_archive_base_name(filename):
                    """Extract base name without version/build numbers for matching archives"""
                    import re
                    base = filename
                    # Remove version patterns like -10.1.0.pb00_hf29.319.rpm
                    base = re.sub(r'-\d+\.\d+\.\d+\.[\w\d_.-]+\.(rpm|ear|war|jar|zip)$', '.\\1', base, flags=re.IGNORECASE)
                    # Remove date/build patterns
                    base = re.sub(r'_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}', '', base)
                    base = re.sub(r'_\d{8,}', '', base)
                    # Remove simple version numbers at end like -1.0.rpm
                    base = re.sub(r'-\d+(\.\d+)*\.(rpm|ear|war|jar|zip)$', '.\\2', base, flags=re.IGNORECASE)
                    return base.lower()
                
                # If folders, find matching archive pairs
                archive_pairs = []
                if is_folder1 and is_folder2:
                    print(f"[DEBUG] Both paths are folders, scanning for archive files...")
                    archives1 = find_archives_in_folder(p1)
                    archives2 = find_archives_in_folder(p2)
                    print(f"[DEBUG] Found {len(archives1)} archives in folder 1, {len(archives2)} in folder 2")
                    
                    # List all archives found
                    for a in archives1:
                        print(f"[DEBUG]   Folder 1: {a.name}")
                    for a in archives2:
                        print(f"[DEBUG]   Folder 2: {a.name}")
                    
                    # Match archives by exact name first
                    names1 = {a.name: a for a in archives1}
                    names2 = {a.name: a for a in archives2}
                    common_names = set(names1.keys()) & set(names2.keys())
                    print(f"[DEBUG] Common archive names (exact match): {len(common_names)}")
                    
                    for name in common_names:
                        archive_pairs.append((names1[name], names2[name]))
                    
                    # Match remaining archives by base name (for version differences)
                    unmatched1 = [a for a in archives1 if a.name not in common_names]
                    unmatched2 = [a for a in archives2 if a.name not in common_names]
                    
                    if unmatched1 and unmatched2:
                        print(f"[DEBUG] Attempting base name matching for {len(unmatched1)} vs {len(unmatched2)} unmatched archives...")
                        base_names1 = {get_archive_base_name(a.name): a for a in unmatched1}
                        base_names2 = {get_archive_base_name(a.name): a for a in unmatched2}
                        common_bases = set(base_names1.keys()) & set(base_names2.keys())
                        print(f"[DEBUG] Common archive names (base match): {len(common_bases)}")
                        
                        for base in common_bases:
                            a1 = base_names1[base]
                            a2 = base_names2[base]
                            print(f"[DEBUG]   Matched by base: {a1.name} <-> {a2.name}")
                            archive_pairs.append((a1, a2))
                    
                    print(f"[DEBUG] Total matched archive pairs: {len(archive_pairs)}")
                    
                    # Track unmatched archives for counting (archives only in one folder)
                    paired_archives1 = set(a1 for a1, a2 in archive_pairs)
                    paired_archives2 = set(a2 for a1, a2 in archive_pairs)
                    unpaired_in_folder1 = [a for a in archives1 if a not in paired_archives1]
                    unpaired_in_folder2 = [a for a in archives2 if a not in paired_archives2]
                    print(f"[DEBUG] Unpaired archives: {len(unpaired_in_folder1)} in folder 1, {len(unpaired_in_folder2)} in folder 2")
                
                # Direct archive-to-archive comparison OR folder with archives
                use_deep_comparison = (
                    (_path_exists(p1) and _path_is_file(p1) and p1.suffix.lower() in supported_extensions and
                     _path_exists(p2) and _path_is_file(p2) and p2.suffix.lower() in supported_extensions) or
                    len(archive_pairs) > 0
                )
                
                if use_deep_comparison:
                    
                    print(f"[DEBUG] Using DEEP archive comparison mode - scanning inside nested archives")
                    if archive_pairs:
                        print(f"[DEBUG] Comparing {len(archive_pairs)} archive pair(s) from folders")
                        for ap1, ap2 in archive_pairs[:5]:
                            print(f"[DEBUG]   - {ap1.name} ({ap1.suffix.lower()})")
                    else:
                        print(f"[DEBUG] Archive 1 type: {p1.suffix.lower()}")
                        print(f"[DEBUG] Archive 2 type: {p2.suffix.lower()}")
                    
                    archive_extensions = ['.jar', '.war', '.ear', '.zip']
                    
                    def get_base_name(filename):
                        """Extract base name without version/build numbers for matching"""
                        # Remove version patterns like -10.1.0.pb00_hf29.319.jar
                        base = re.sub(r'-\d+\.\d+\.\d+\.[\w\d_.-]+\.(jar|war|ear|zip|rpm)$', '.\\1', filename, flags=re.IGNORECASE)
                        # Remove date/build patterns
                        base = re.sub(r'_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}', '', base)
                        base = re.sub(r'_\d{8,}', '', base)
                        # Remove simple version numbers at end like -1.0.jar
                        base = re.sub(r'-\d+(\.\d+)*\.(jar|war|ear|zip|rpm)$', '.\\2', base, flags=re.IGNORECASE)
                        return base.lower()
                    
                    def scan_rpm_archive(rpm_path, prefix=""):
                        """
                        Scan an RPM file and extract all JARs and members.
                        RPM structure: .rpm -> .cpio.gz -> .cpio -> files (including ASCF/lib folders with JARs)
                        """
                        all_jars = {}
                        all_members = {}
                        import gzip
                        import tempfile
                        import shutil
                        
                        print(f"[DEBUG] Scanning RPM: {rpm_path}")
                        
                        def get_parent_folder(path):
                            """Extract parent folder name for display (e.g., ASCF, lib)"""
                            parts = path.replace('\\', '/').split('/')
                            # Find meaningful parent (skip . and empty)
                            for i, part in enumerate(parts):
                                if part and part != '.' and i < len(parts) - 1:
                                    if part.lower() in ['ascf', 'lib', 'web-inf', 'meta-inf', 'classes']:
                                        return part
                                    if parts[i+1] and os.path.splitext(parts[-1])[1].lower() == '.jar':
                                        return part
                            return os.path.basename(str(rpm_path))
                        
                        def scan_nested_zip_archive(data, parent_name, full_prefix):
                            """Recursively scan a nested ZIP/JAR/WAR/EAR archive"""
                            nested_jars = {}
                            nested_members = {}
                            try:
                                with zipfile.ZipFile(BytesIO(data), 'r') as zf:
                                    for info in zf.infolist():
                                        if info.is_dir():
                                            continue
                                        filename = os.path.basename(info.filename)
                                        nested_full_path = f"{full_prefix}{info.filename}"
                                        ext = os.path.splitext(filename)[1].lower()
                                        
                                        nested_members[nested_full_path] = (info.file_size, info.CRC)
                                        
                                        if ext == '.jar':
                                            folder = get_parent_folder(info.filename)
                                            nested_jars[filename] = {
                                                'full_path': nested_full_path,
                                                'size': info.file_size,
                                                'crc': info.CRC,
                                                'parent': f"{parent_name}/{folder}" if folder != parent_name else parent_name
                                            }
                                        
                                        # Go deeper into nested archives (including JARs containing JARs)
                                        if ext in ['.jar', '.war', '.ear', '.zip']:
                                            try:
                                                deeper_data = zf.read(info.filename)
                                                deeper_prefix = f"{nested_full_path}::"
                                                d_jars, d_members = scan_nested_zip_archive(deeper_data, filename, deeper_prefix)
                                                nested_jars.update(d_jars)
                                                nested_members.update(d_members)
                                            except Exception:
                                                pass
                            except Exception as e:
                                print(f"[DEBUG] Could not read nested archive: {e}")
                            return nested_jars, nested_members
                        
                        try:
                            # Method 1: Try using rpmfile library (best method)
                            try:
                                import rpmfile
                                print(f"[DEBUG] Using rpmfile library to read RPM")
                                with rpmfile.open(str(rpm_path)) as rpm:
                                    for member in rpm.getmembers():
                                        if member.isdir:
                                            continue
                                        
                                        filename = os.path.basename(member.name)
                                        full_path = f"{prefix}{member.name}" if prefix else member.name
                                        ext = os.path.splitext(filename)[1].lower()
                                        folder = get_parent_folder(member.name)
                                        
                                        all_members[full_path] = (member.size, 0)
                                        
                                        if ext == '.jar':
                                            all_jars[filename] = {
                                                'full_path': full_path,
                                                'size': member.size,
                                                'crc': 0,
                                                'parent': folder
                                            }
                                            print(f"[DEBUG] Found JAR: {filename} in {folder}")
                                        
                                        # Scan nested archives (WAR, EAR, ZIP, JAR with nested content)
                                        if ext in ['.jar', '.war', '.ear', '.zip']:
                                            try:
                                                nested_data = rpm.extractfile(member).read()
                                                nested_prefix = f"{full_path}::"
                                                n_jars, n_members = scan_nested_zip_archive(nested_data, filename, nested_prefix)
                                                all_jars.update(n_jars)
                                                all_members.update(n_members)
                                                print(f"[DEBUG] Scanned nested archive {filename}: found {len(n_jars)} JARs")
                                            except Exception as e:
                                                print(f"[DEBUG] Could not scan nested archive {filename}: {e}")
                                
                                print(f"[DEBUG] rpmfile scan complete: {len(all_jars)} JARs found")
                                return all_jars, all_members
                                
                            except ImportError:
                                print(f"[DEBUG] rpmfile not available, trying manual CPIO extraction")
                            
                            # Method 2: Manual RPM/CPIO parsing
                            # RPM files have a header followed by a gzip-compressed CPIO archive
                            print(f"[DEBUG] Attempting manual RPM parsing...")
                            
                            with open(str(rpm_path), 'rb') as f:
                                rpm_data = f.read()
                            
                            # Find the gzip signature (1f 8b) which marks the start of the CPIO payload
                            gzip_start = -1
                            for i in range(len(rpm_data) - 1):
                                if rpm_data[i] == 0x1f and rpm_data[i+1] == 0x8b:
                                    gzip_start = i
                                    break
                            
                            if gzip_start == -1:
                                print(f"[DEBUG] Could not find gzip signature in RPM")
                                return all_jars, all_members
                            
                            print(f"[DEBUG] Found gzip data at offset {gzip_start}")
                            
                            # Extract and decompress the CPIO data
                            gzip_data = rpm_data[gzip_start:]
                            try:
                                cpio_data = gzip.decompress(gzip_data)
                                print(f"[DEBUG] Decompressed CPIO data: {len(cpio_data)} bytes")
                            except Exception as e:
                                print(f"[DEBUG] Gzip decompression failed: {e}")
                                return all_jars, all_members
                            
                            # Parse CPIO archive (newc format)
                            # CPIO newc header: "070701" or "070702" magic
                            pos = 0
                            file_count = 0
                            
                            while pos < len(cpio_data) - 110:
                                # Check for CPIO magic
                                magic = cpio_data[pos:pos+6]
                                if magic not in [b'070701', b'070702']:
                                    pos += 1
                                    continue
                                
                                try:
                                    # Parse CPIO newc header (all values are hex strings)
                                    header = cpio_data[pos:pos+110].decode('ascii')
                                    if not header.startswith('0707'):
                                        pos += 1
                                        continue
                                    
                                    # Extract fields from header
                                    filesize = int(header[54:62], 16)
                                    namesize = int(header[94:102], 16)
                                    
                                    # Get filename
                                    name_start = pos + 110
                                    name_end = name_start + namesize - 1  # -1 for null terminator
                                    filename_full = cpio_data[name_start:name_end].decode('utf-8', errors='replace')
                                    
                                    # Check for trailer (end of archive)
                                    if filename_full == 'TRAILER!!!':
                                        break
                                    
                                    # Calculate data position (aligned to 4 bytes)
                                    name_padded = ((110 + namesize + 3) // 4) * 4
                                    data_start = pos + name_padded
                                    data_padded = ((filesize + 3) // 4) * 4
                                    
                                    # Skip directories
                                    if filesize > 0 and not filename_full.endswith('/'):
                                        filename = os.path.basename(filename_full)
                                        ext = os.path.splitext(filename)[1].lower()
                                        folder = get_parent_folder(filename_full)
                                        full_path = f"{prefix}{filename_full}" if prefix else filename_full
                                        
                                        all_members[full_path] = (filesize, 0)
                                        
                                        if ext == '.jar':
                                            all_jars[filename] = {
                                                'full_path': full_path,
                                                'size': filesize,
                                                'crc': 0,
                                                'parent': folder
                                            }
                                            file_count += 1
                                            if file_count <= 10:
                                                print(f"[DEBUG] Found JAR: {filename} in {folder} ({filesize} bytes)")
                                        
                                        # Scan nested ZIP archives
                                        if ext in ['.war', '.ear', '.zip'] and filesize > 0:
                                            try:
                                                nested_data = cpio_data[data_start:data_start+filesize]
                                                nested_prefix = f"{full_path}::"
                                                n_jars, n_members = scan_nested_zip_archive(nested_data, filename, nested_prefix)
                                                all_jars.update(n_jars)
                                                all_members.update(n_members)
                                                if n_jars:
                                                    print(f"[DEBUG] Scanned nested {filename}: {len(n_jars)} JARs")
                                            except Exception as e:
                                                pass
                                    
                                    # Move to next entry
                                    pos = data_start + data_padded
                                    
                                except Exception as e:
                                    pos += 1
                                    continue
                            
                            print(f"[DEBUG] Manual CPIO scan complete: {len(all_jars)} JARs found")
                            
                        except Exception as e:
                            print(f"[ERROR] Error reading RPM {rpm_path}: {e}")
                            import traceback
                            traceback.print_exc()
                        
                        return all_jars, all_members
                    
                    def scan_archive_deep(archive_path, prefix=""):
                        """
                        Recursively scan an archive and all nested archives inside it.
                        Returns dict of {full_path: {'name': filename, 'size': size, 'parent': parent_archive}}
                        """
                        all_jars = {}
                        all_members = {}
                        
                        # Nested archive extensions - INCLUDING JAR for JARs containing other JARs
                        nested_archive_exts = ['.jar', '.war', '.ear', '.zip']
                        
                        def scan_nested_archive_recursive(zf_or_data, parent_prefix, parent_name, depth=0, max_depth=10):
                            """Recursively scan nested archives up to max_depth levels"""
                            if depth >= max_depth:
                                return
                                
                            try:
                                # If zf_or_data is bytes, wrap in BytesIO and open as ZipFile
                                if isinstance(zf_or_data, bytes):
                                    with zipfile.ZipFile(BytesIO(zf_or_data), 'r') as nested_zf:
                                        scan_nested_archive_recursive(nested_zf, parent_prefix, parent_name, depth, max_depth)
                                    return
                                
                                # zf_or_data is a ZipFile object
                                for info in zf_or_data.infolist():
                                    if info.is_dir():
                                        continue
                                    
                                    filename = os.path.basename(info.filename)
                                    full_path = f"{parent_prefix}{info.filename}"
                                    ext = os.path.splitext(filename)[1].lower()
                                    
                                    # Record all members
                                    all_members[full_path] = (info.file_size, info.CRC)
                                    
                                    # If it's a JAR file, record it
                                    if ext == '.jar':
                                        all_jars[filename] = {
                                            'full_path': full_path,
                                            'size': info.file_size,
                                            'crc': info.CRC,
                                            'parent': parent_name
                                        }
                                    
                                    # Recursively scan nested archives (JAR, WAR, EAR, ZIP)
                                    if ext in nested_archive_exts:
                                        try:
                                            nested_data = zf_or_data.read(info.filename)
                                            nested_prefix = f"{full_path}::"
                                            print(f"[DEBUG] Scanning nested archive (depth {depth+1}): {info.filename}")
                                            scan_nested_archive_recursive(nested_data, nested_prefix, info.filename, depth + 1, max_depth)
                                        except Exception as e:
                                            print(f"[DEBUG] Could not scan nested archive {info.filename}: {e}")
                            except Exception as e:
                                print(f"[DEBUG] Error in nested scan: {e}")
                        
                        try:
                            with zipfile.ZipFile(str(archive_path), 'r') as zf:
                                archive_name = os.path.basename(str(archive_path))
                                initial_prefix = f"{prefix}" if prefix else ""
                                scan_nested_archive_recursive(zf, initial_prefix, archive_name, depth=0, max_depth=10)
                        except Exception as e:
                            print(f"[DEBUG] Deep zip scan failed for {archive_path}: {e}")
                            # Fallback for archives that Python zipfile cannot parse
                            # (e.g., corrupt/odd extra fields). Keep counts non-zero via `jar tf`.
                            try:
                                result = subprocess.run(
                                    ["jar", "tf", str(archive_path)],
                                    capture_output=True,
                                    text=True,
                                    timeout=300,
                                )
                                if result.returncode == 0 and result.stdout:
                                    archive_name = os.path.basename(str(archive_path))
                                    initial_prefix = f"{prefix}" if prefix else ""
                                    for line in result.stdout.splitlines():
                                        name = line.strip().replace("\\", "/")
                                        if not name or name.endswith("/"):
                                            continue
                                        full_path = f"{initial_prefix}{name}" if initial_prefix else name
                                        all_members[full_path] = (0, 0)
                                        filename = os.path.basename(name)
                                        if filename.lower().endswith(".jar"):
                                            all_jars[filename] = {
                                                "full_path": full_path,
                                                "size": 0,
                                                "crc": 0,
                                                "parent": archive_name,
                                            }
                                    print(
                                        f"[DEBUG] jar tf fallback for {archive_path.name}: "
                                        f"{len(all_jars)} JARs, {len(all_members)} members"
                                    )
                            except Exception as e2:
                                print(f"[DEBUG] jar tf fallback failed for {archive_path}: {e2}")
                        
                        return all_jars, all_members
                    
                    def scan_archive_shallow_with_jar(archive_path, prefix=""):
                        """Shallow archive listing via `jar tf` (no deep nested recursion)."""
                        all_jars = {}
                        all_members = {}
                        try:
                            result = subprocess.run(
                                ["jar", "tf", str(archive_path)],
                                capture_output=True,
                                text=True,
                                timeout=300,
                            )
                            if result.returncode != 0 or not result.stdout:
                                return all_jars, all_members
                            initial_prefix = f"{prefix}" if prefix else ""
                            archive_name = os.path.basename(str(archive_path))
                            for line in result.stdout.splitlines():
                                name = line.strip().replace("\\", "/")
                                if not name or name.endswith("/"):
                                    continue
                                full_path = f"{initial_prefix}{name}" if initial_prefix else name
                                all_members[full_path] = (0, 0)
                                filename = os.path.basename(name)
                                if filename.lower().endswith(".jar"):
                                    all_jars[filename] = {
                                        "full_path": full_path,
                                        "size": 0,
                                        "crc": 0,
                                        "parent": archive_name,
                                    }
                            print(
                                f"[DEBUG] Shallow jar scan for {archive_path.name}: "
                                f"{len(all_jars)} JARs, {len(all_members)} members"
                            )
                        except Exception as e:
                            print(f"[DEBUG] Shallow jar scan failed for {archive_path}: {e}")
                        return all_jars, all_members
                    
                    # Helper function to scan any archive type
                    def scan_any_archive(archive_path):
                        """Scan any supported archive type (ZIP, JAR, WAR, EAR, RPM)"""
                        ext = archive_path.suffix.lower()
                        if ext == '.rpm':
                            print(f"[DEBUG] Detected RPM file, using RPM scanner")
                            return scan_rpm_archive(archive_path)
                        else:
                            return scan_archive_deep(archive_path)
                    
                    # Scan archives deeply - either direct files or archive pairs from folders
                    jars1 = {}
                    jars2 = {}
                    members1 = {}
                    members2 = {}
                    scan_start_time = time.time()
                    
                    # Create a simple progress tracker class for archive scanning
                    class ArchiveScanProgress:
                        def __init__(self, total_archives):
                            self.processed_files = 0
                            self.total_files = total_archives * 2  # Both archives in each pair
                            self.current_file = "Starting..."
                            self.estimated_time_remaining = 0
                            self.start_time = time.time()
                            
                        def update(self, current_name, processed):
                            self.current_file = current_name
                            self.processed_files = processed
                            elapsed = time.time() - self.start_time
                            if self.processed_files > 0:
                                avg_time = elapsed / self.processed_files
                                remaining = self.total_files - self.processed_files
                                self.estimated_time_remaining = int(avg_time * remaining)
                    
                    if archive_pairs:
                        # Initialize progress tracker
                        progress_tracker = ArchiveScanProgress(len(archive_pairs))
                        comparison_progress["comparer"] = progress_tracker
                        session_progress["comparer"] = progress_tracker
                        
                        # Scan each archive pair from folders
                        pair_idx = 0
                        for ap1, ap2 in archive_pairs:
                            ext1 = ap1.suffix.lower()
                            ext2 = ap2.suffix.lower()
                            print(f"[DEBUG] Deep scanning archive pair: {ap1.name} ({ext1}) <-> {ap2.name} ({ext2})")
                            prefix1 = f"{ap1.name}::"
                            prefix2 = f"{ap2.name}::"
                            
                            # Update progress for archive 1
                            progress_tracker.update(f"Scanning: {ap1.name}", pair_idx * 2)
                            
                            # Use RPM scanner for RPM files
                            if ext1 == '.rpm':
                                print(f"[DEBUG] Using RPM scanner for {ap1.name}")
                                aj1, am1 = scan_rpm_archive(ap1)
                            else:
                                aj1, am1 = scan_archive_deep(ap1)
                            
                            # Update progress for archive 2
                            progress_tracker.update(f"Scanning: {ap2.name}", pair_idx * 2 + 1)
                                
                            if ext2 == '.rpm':
                                print(f"[DEBUG] Using RPM scanner for {ap2.name}")
                                aj2, am2 = scan_rpm_archive(ap2)
                            else:
                                aj2, am2 = scan_archive_deep(ap2)
                            
                            print(f"[DEBUG]   Archive 1 ({ap1.name}): {len(aj1)} nested JARs, {len(am1)} members")
                            print(f"[DEBUG]   Archive 2 ({ap2.name}): {len(aj2)} nested JARs, {len(am2)} members")
                            
                            # IMPORTANT: Add the top-level archive itself if it's a JAR file
                            # This fixes the issue where folders with standalone JARs show incorrect counts
                            if ext1 == '.jar':
                                try:
                                    jar_size = os.path.getsize(str(ap1))
                                    jars1[ap1.name] = {
                                        'full_path': ap1.name,
                                        'size': jar_size,
                                        'crc': 0,
                                        'parent': str(p1.name),
                                        'source_archive': 'folder'
                                    }
                                    print(f"[DEBUG]   Added top-level JAR: {ap1.name} ({jar_size} bytes)")
                                except:
                                    pass
                            
                            if ext2 == '.jar':
                                try:
                                    jar_size = os.path.getsize(str(ap2))
                                    jars2[ap2.name] = {
                                        'full_path': ap2.name,
                                        'size': jar_size,
                                        'crc': 0,
                                        'parent': str(p2.name),
                                        'source_archive': 'folder'
                                    }
                                    print(f"[DEBUG]   Added top-level JAR: {ap2.name} ({jar_size} bytes)")
                                except:
                                    pass
                            
                            # Add nested JARs with prefix to distinguish archives
                            for k, v in aj1.items():
                                unique_key = f"{ap1.name}::{k}"
                                v['full_path'] = prefix1 + v.get('full_path', k)
                                v['parent'] = ap1.name + '/' + v.get('parent', '')
                                v['source_archive'] = ap1.name
                                jars1[unique_key] = v
                            for k, v in aj2.items():
                                unique_key = f"{ap2.name}::{k}"
                                v['full_path'] = prefix2 + v.get('full_path', k)
                                v['parent'] = ap2.name + '/' + v.get('parent', '')
                                v['source_archive'] = ap2.name
                                jars2[unique_key] = v
                            for k, v in am1.items():
                                members1[prefix1 + k] = v
                            for k, v in am2.items():
                                members2[prefix2 + k] = v
                            
                            pair_idx += 1
                            progress_tracker.update(f"Completed: {ap1.name}", pair_idx * 2)
                            print(f"[DEBUG]   Total so far - JARs: {len(jars1)}/{len(jars2)}, Members: {len(members1)}/{len(members2)}")
                        
                        # Also add unpaired archives (archives only in one folder)
                        if 'unpaired_in_folder1' in dir() and unpaired_in_folder1:
                            print(f"[DEBUG] Adding {len(unpaired_in_folder1)} unpaired archives from folder 1...")
                            for ap in unpaired_in_folder1:
                                ext = ap.suffix.lower()
                                if ext == '.jar':
                                    try:
                                        jar_size = os.path.getsize(str(ap))
                                        jars1[ap.name] = {
                                            'full_path': ap.name,
                                            'size': jar_size,
                                            'crc': 0,
                                            'parent': str(p1.name),
                                            'source_archive': 'folder'
                                        }
                                        print(f"[DEBUG]   Added unpaired JAR from folder 1: {ap.name}")
                                    except:
                                        pass
                                else:
                                    # Scan non-JAR archives for their contents
                                    try:
                                        if ext == '.rpm':
                                            aj, am = scan_rpm_archive(ap)
                                        else:
                                            aj, am = scan_archive_deep(ap)
                                        prefix = f"{ap.name}::"
                                        for k, v in aj.items():
                                            unique_key = f"{ap.name}::{k}"
                                            v['full_path'] = prefix + v.get('full_path', k)
                                            v['source_archive'] = ap.name
                                            jars1[unique_key] = v
                                        for k, v in am.items():
                                            members1[prefix + k] = v
                                        print(f"[DEBUG]   Scanned unpaired archive from folder 1: {ap.name} ({len(aj)} JARs)")
                                    except:
                                        pass
                        
                        if 'unpaired_in_folder2' in dir() and unpaired_in_folder2:
                            print(f"[DEBUG] Adding {len(unpaired_in_folder2)} unpaired archives from folder 2...")
                            for ap in unpaired_in_folder2:
                                ext = ap.suffix.lower()
                                if ext == '.jar':
                                    try:
                                        jar_size = os.path.getsize(str(ap))
                                        jars2[ap.name] = {
                                            'full_path': ap.name,
                                            'size': jar_size,
                                            'crc': 0,
                                            'parent': str(p2.name),
                                            'source_archive': 'folder'
                                        }
                                        print(f"[DEBUG]   Added unpaired JAR from folder 2: {ap.name}")
                                    except:
                                        pass
                                else:
                                    # Scan non-JAR archives for their contents
                                    try:
                                        if ext == '.rpm':
                                            aj, am = scan_rpm_archive(ap)
                                        else:
                                            aj, am = scan_archive_deep(ap)
                                        prefix = f"{ap.name}::"
                                        for k, v in aj.items():
                                            unique_key = f"{ap.name}::{k}"
                                            v['full_path'] = prefix + v.get('full_path', k)
                                            v['source_archive'] = ap.name
                                            jars2[unique_key] = v
                                        for k, v in am.items():
                                            members2[prefix + k] = v
                                        print(f"[DEBUG]   Scanned unpaired archive from folder 2: {ap.name} ({len(aj)} JARs)")
                                    except:
                                        pass
                    else:
                        # Direct file-to-file comparison
                        progress_tracker = ArchiveScanProgress(1)
                        comparison_progress["comparer"] = progress_tracker
                        session_progress["comparer"] = progress_tracker
                        
                        progress_tracker.update(f"Scanning: {p1.name}", 0)
                        print(f"[DEBUG] Deep scanning Archive 1 ({p1.suffix.lower()})...")
                        jars1, members1 = scan_any_archive(p1)
                        
                        progress_tracker.update(f"Scanning: {p2.name}", 1)
                        print(f"[DEBUG] Deep scanning Archive 2 ({p2.suffix.lower()})...")
                        jars2, members2 = scan_any_archive(p2)

                        # Rebalance if one archive was deeply expanded but the other fell back
                        # to shallow listing (e.g., corrupt extra fields on one side).
                        m1 = len(members1)
                        m2 = len(members2)
                        small = min(m1, m2)
                        large = max(m1, m2)
                        if (
                            small > 0
                            and large >= 200000
                            and (large / small) >= 50
                            and p1.suffix.lower() != ".rpm"
                            and p2.suffix.lower() != ".rpm"
                        ):
                            print(
                                f"[DEBUG] Unbalanced deep scan detected ({m1} vs {m2}); "
                                "retrying both sides in shallow jar mode for stable reporting..."
                            )
                            jars1, members1 = scan_archive_shallow_with_jar(p1)
                            jars2, members2 = scan_archive_shallow_with_jar(p2)
                            print(
                                f"[DEBUG] Rebalanced shallow counts: "
                                f"{len(jars1)} / {len(jars2)} JARs, {len(members1)} / {len(members2)} members"
                            )
                        
                        progress_tracker.update("Analyzing results...", 2)
                    
                    print(f"[DEBUG] Archive 1: {len(jars1)} JARs found (including nested)")
                    print(f"[DEBUG] Archive 2: {len(jars2)} JARs found (including nested)")
                    print(f"[DEBUG] Archive 1: {len(members1)} total members")
                    print(f"[DEBUG] Archive 2: {len(members2)} total members")
                    
                    # JAR naming convention comparison
                    jar_set1 = set(jars1.keys())
                    jar_set2 = set(jars2.keys())
                    jars_only_in_1 = sorted(jar_set1 - jar_set2)
                    jars_only_in_2 = sorted(jar_set2 - jar_set1)
                    jars_in_both = sorted(jar_set1 & jar_set2)
                    
                    # Find truly new JARs (base name doesn't exist in other archive)
                    bases_in_1 = {get_base_name(name): name for name in jar_set1}
                    bases_in_2 = {get_base_name(name): name for name in jar_set2}
                    
                    truly_new_jars_in_1 = [name for name in jars_only_in_1 if get_base_name(name) not in bases_in_2]
                    truly_new_jars_in_2 = [name for name in jars_only_in_2 if get_base_name(name) not in bases_in_1]
                    
                    # Version changes (same base name, different full name)
                    jar_version_changes = []
                    for name1 in jars_only_in_1:
                        base = get_base_name(name1)
                        if base in bases_in_2:
                            name2 = bases_in_2[base]
                            if name2 in jars_only_in_2:
                                size1 = jars1[name1]['size']
                                size2 = jars2[name2]['size']
                                size_diff = size2 - size1
                                jar_version_changes.append({
                                    'old': name1, 'new': name2,
                                    'size1': size1, 'size2': size2,
                                    'diff': size_diff, 'diff_pct': (size_diff / size1 * 100) if size1 > 0 else 0
                                })
                    
                    # === Major Size Differences (for JARs in both archives) ===
                    SIZE_DIFF_THRESHOLD = 10240  # 10 KB
                    SIZE_DIFF_PCT_THRESHOLD = 5  # 5%
                    
                    major_size_diffs = []
                    for name in jars_in_both:
                        size1 = jars1[name]['size']
                        size2 = jars2[name]['size']
                        if size1 != size2:
                            diff = abs(size2 - size1)
                            diff_pct = (diff / size1 * 100) if size1 > 0 else 0
                            if diff >= SIZE_DIFF_THRESHOLD or diff_pct >= SIZE_DIFF_PCT_THRESHOLD:
                                major_size_diffs.append({
                                    'name': name, 'size1': size1, 'size2': size2,
                                    'diff': size2 - size1, 'diff_pct': diff_pct
                                })
                    
                    # Sort by absolute difference (largest first)
                    major_size_diffs.sort(key=lambda x: abs(x['diff']), reverse=True)
                    
                    # === All Files Analysis ===
                    set1 = set(members1.keys())
                    set2 = set(members2.keys())
                    only_in_1 = sorted(set1 - set2)
                    only_in_2 = sorted(set2 - set1)
                    in_both = sorted(set1 & set2)
                    
                    # Check for content differences in common files
                    content_differs = []
                    for name in in_both:
                        size1, crc1 = members1[name]
                        size2, crc2 = members2[name]
                        if size1 != size2 or crc1 != crc2:
                            content_differs.append(name)
                    
                    # Generate text report
                    report_lines = []
                    report_lines.append("=" * 80)
                    app_label = "CRM" if crm_archive_mode else "ASCF"
                    report_lines.append(f"{app_label} ARCHIVE CONTENTS COMPARISON REPORT")
                    report_lines.append("=" * 80)
                    report_lines.append(f"Archive 1: {folder1}")
                    report_lines.append(f"Archive 2: {folder2}")
                    report_lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                    report_lines.append("")
                    report_lines.append("=" * 80)
                    report_lines.append("JAR/ARCHIVE NAMING CONVENTION SUMMARY")
                    report_lines.append("=" * 80)
                    report_lines.append(f"Total JARs in Archive 1: {len(jars1)}")
                    report_lines.append(f"Total JARs in Archive 2: {len(jars2)}")
                    report_lines.append(f"Exact name matches: {len(jars_in_both)}")
                    report_lines.append(f"Version changes detected: {len(jar_version_changes)}")
                    report_lines.append(f"Truly new in Archive 1: {len(truly_new_jars_in_1)}")
                    report_lines.append(f"Truly new in Archive 2: {len(truly_new_jars_in_2)}")
                    report_lines.append(f"Major size differences: {len(major_size_diffs)}")
                    report_lines.append("")
                    
                    report_text = "\n".join(report_lines)
                    
                    # Save text report
                    report_filepath = str(_report_path(saved_name))
                    with open(report_filepath, 'w', encoding='utf-8') as f:
                        f.write(report_text)
                    print(f"[DEBUG] Text report saved: {report_filepath}")
                    
                    # Generate styled HTML report
                    html_content = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{app_label} Archive Contents & JAR Naming Comparison</title>
    <style>
        * {{ box-sizing: border-box; }}
        body {{ font-family: 'Segoe UI', system-ui, -apple-system, sans-serif; margin: 0; padding: 20px; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); min-height: 100vh; }}
        .container {{ max-width: 1400px; margin: 0 auto; background: white; border-radius: 16px; box-shadow: 0 20px 60px rgba(0,0,0,0.3); overflow: hidden; }}
        .header {{ background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 30px; }}
        .header h1 {{ margin: 0 0 10px 0; font-size: 28px; font-weight: 600; }}
        .header .subtitle {{ opacity: 0.9; font-size: 14px; }}
        .header .paths {{ margin-top: 15px; padding-top: 15px; border-top: 1px solid rgba(255,255,255,0.3); }}
        .header .path-label {{ font-weight: 600; font-size: 12px; text-transform: uppercase; opacity: 0.8; margin-top: 8px; }}
        .header .path-value {{ font-family: 'Consolas', monospace; font-size: 13px; margin-top: 4px; word-break: break-all; }}
        .content {{ padding: 30px; }}
        .nav-menu {{ background: linear-gradient(135deg, #f7fafc 0%, #edf2f7 100%); padding: 20px; border-radius: 12px; margin-bottom: 30px; }}
        .nav-menu h3 {{ margin: 0 0 15px 0; font-size: 18px; color: #2d3748; }}
        .nav-links {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 12px; }}
        .nav-link {{ display: block; padding: 14px 18px; background: white; border-left: 5px solid #667eea; border-radius: 8px; text-decoration: none; color: #2d3748; font-weight: 600; transition: all 0.2s; box-shadow: 0 2px 8px rgba(0,0,0,0.08); }}
        .nav-link:hover {{ transform: translateX(6px); box-shadow: 0 4px 12px rgba(0,0,0,0.15); }}
        .nav-link.size-diff {{ border-left-color: #ed8936; }}
        .nav-link.version {{ border-left-color: #9f7aea; }}
        .nav-link.new-jar {{ border-left-color: #48bb78; }}
        .nav-link.removed-jar {{ border-left-color: #f56565; }}
        .nav-link .count {{ float: right; background: #667eea; color: white; padding: 4px 12px; border-radius: 12px; font-size: 13px; }}
        .nav-link.size-diff .count {{ background: #ed8936; }}
        .nav-link.version .count {{ background: #9f7aea; }}
        .nav-link.new-jar .count {{ background: #48bb78; }}
        .nav-link.removed-jar .count {{ background: #f56565; }}
        .summary {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 15px; margin-bottom: 30px; }}
        .stat-card {{ background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 18px; border-radius: 12px; box-shadow: 0 4px 15px rgba(0,0,0,0.1); }}
        .stat-card.success {{ background: linear-gradient(135deg, #4facfe 0%, #00f2fe 100%); }}
        .stat-card.warning {{ background: linear-gradient(135deg, #fa709a 0%, #fee140 100%); }}
        .stat-card.info {{ background: linear-gradient(135deg, #30cfd0 0%, #330867 100%); }}
        .stat-card.danger {{ background: linear-gradient(135deg, #f5576c 0%, #f093fb 100%); }}
        .stat-card.purple {{ background: linear-gradient(135deg, #9f7aea 0%, #667eea 100%); }}
        .stat-card.orange {{ background: linear-gradient(135deg, #ed8936 0%, #f6ad55 100%); }}
        .stat-card .label {{ font-size: 11px; font-weight: 500; opacity: 0.9; text-transform: uppercase; letter-spacing: 0.5px; }}
        .stat-card .value {{ font-size: 32px; font-weight: 700; margin-top: 6px; }}
        .section {{ margin-bottom: 35px; scroll-margin-top: 20px; }}
        .section-title {{ font-size: 20px; font-weight: 600; color: #1a202c; margin-bottom: 15px; padding-bottom: 10px; border-bottom: 3px solid #667eea; display: flex; align-items: center; gap: 10px; }}
        .section-title.size-diff {{ border-bottom-color: #ed8936; }}
        .section-title.version {{ border-bottom-color: #9f7aea; }}
        .section-title.new {{ border-bottom-color: #48bb78; }}
        .section-title.removed {{ border-bottom-color: #f56565; }}
        .file-list {{ background: #f7fafc; border-radius: 8px; padding: 15px; max-height: 450px; overflow-y: auto; }}
        .file-item {{ font-family: 'Consolas', monospace; font-size: 12px; padding: 10px 14px; margin-bottom: 6px; background: white; border-radius: 6px; border-left: 4px solid #667eea; box-shadow: 0 1px 3px rgba(0,0,0,0.05); }}
        .file-item.removed {{ border-left-color: #f56565; background: #fff5f5; }}
        .file-item.added {{ border-left-color: #48bb78; background: #f0fff4; }}
        .file-item.changed {{ border-left-color: #ed8936; background: #fffaf0; }}
        .file-item.version {{ border-left-color: #9f7aea; background: #faf5ff; }}
        .file-item .size-info {{ color: #718096; font-size: 11px; margin-top: 4px; }}
        .file-item .size-change {{ font-weight: 600; }}
        .file-item .size-change.increase {{ color: #e53e3e; }}
        .file-item .size-change.decrease {{ color: #38a169; }}
        .badge {{ display: inline-block; padding: 3px 10px; border-radius: 10px; font-size: 10px; font-weight: 700; text-transform: uppercase; margin-left: 8px; }}
        .badge.increase {{ background: #fed7d7; color: #c53030; }}
        .badge.decrease {{ background: #c6f6d5; color: #276749; }}
        .back-link {{ display: inline-block; margin-top: 20px; padding: 12px 24px; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; text-decoration: none; border-radius: 8px; font-weight: 600; transition: all 0.3s; }}
        .back-link:hover {{ transform: translateY(-2px); box-shadow: 0 4px 15px rgba(102,126,234,0.4); }}
        .empty-state {{ text-align: center; padding: 40px; color: #718096; }}
        .subsection-title {{ font-size: 16px; font-weight: 600; color: #4a5568; margin: 20px 0 12px 0; }}
        /* Content Changes Table Styles */
        .content-diff-table {{ width: 100%; border-collapse: collapse; font-size: 12px; background: white; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.08); }}
        .content-diff-table th {{ background: linear-gradient(135deg, #ed8936 0%, #f6ad55 100%); color: white; padding: 14px 12px; text-align: left; font-weight: 600; font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; position: sticky; top: 0; z-index: 10; }}
        .content-diff-table td {{ padding: 12px; border-bottom: 1px solid #e2e8f0; vertical-align: middle; }}
        .content-diff-table tr:hover {{ background: #fffaf0; }}
        .content-diff-table tr:last-child td {{ border-bottom: none; }}
        .content-diff-table .file-name {{ font-family: 'Consolas', monospace; color: #2d3748; word-break: break-all; max-width: 400px; }}
        .content-diff-table .size-cell {{ font-family: 'Consolas', monospace; text-align: right; white-space: nowrap; color: #4a5568; }}
        .content-diff-table .change-cell {{ text-align: right; white-space: nowrap; font-weight: 600; }}
        .content-diff-table .change-cell.increase {{ color: #e53e3e; background: #fff5f5; }}
        .content-diff-table .change-cell.decrease {{ color: #38a169; background: #f0fff4; }}
        .content-diff-table .change-cell.same {{ color: #718096; background: #f7fafc; }}
        .table-container {{ background: #f7fafc; border-radius: 8px; padding: 15px; max-height: 600px; overflow-y: auto; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
        <h1>📦 {app_label} Archive Contents & JAR Naming Comparison</h1>
        <div class="subtitle">Completed at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</div>
        <div class="paths">
            <div class="path-label">📁 Archive 1</div>
            <div class="path-value">{html.escape(folder1)}</div>
            <div class="path-label">📁 Archive 2</div>
            <div class="path-value">{html.escape(folder2)}</div>
        </div>
        </div>
        <div class="content">
        <!-- Quick Navigation -->
        <div class="nav-menu">
            <h3>📑 Quick Navigation</h3>
            <div class="nav-links">"""
                    
                    if major_size_diffs:
                        html_content += f"""
                <a href="#size-diffs" class="nav-link size-diff">🔴 Major JAR Size Differences <span class="count">{len(major_size_diffs)}</span></a>"""
                    if jar_version_changes:
                        html_content += f"""
                <a href="#version-changes" class="nav-link version">🔄 JAR Version Changes <span class="count">{len(jar_version_changes)}</span></a>"""
                    if truly_new_jars_in_2:
                        html_content += f"""
                <a href="#new-jars" class="nav-link new-jar">✅ New JARs in Archive 2 <span class="count">{len(truly_new_jars_in_2)}</span></a>"""
                    if truly_new_jars_in_1:
                        html_content += f"""
                <a href="#removed-jars" class="nav-link removed-jar">❌ JARs Only in Archive 1 <span class="count">{len(truly_new_jars_in_1)}</span></a>"""
                    if content_differs:
                        html_content += f"""
                <a href="#content-diffs" class="nav-link">⚠️ All Files with Different Content <span class="count">{len(content_differs)}</span></a>"""
                    
                    # Add RPM structure link if comparing RPM files or folders with RPMs
                    has_rpm_files = (p1.suffix.lower() == '.rpm' or p2.suffix.lower() == '.rpm' or 
                                    (archive_pairs and any(ap1.suffix.lower() == '.rpm' for ap1, ap2 in archive_pairs)))
                    if has_rpm_files:
                        rpm_members1 = {k: v for k, v in members1.items() if '.rpm::' in k or k.endswith('.rpm')}
                        rpm_members2 = {k: v for k, v in members2.items() if '.rpm::' in k or k.endswith('.rpm')}
                        html_content += f"""
                <a href="#rpm-structure" class="nav-link" style="border-left-color:#805ad5;">📦 RPM CPIO Comparison <span class="count" style="background:#805ad5;">{len(rpm_members1) + len(rpm_members2):,}</span></a>"""
                    
                    html_content += f"""
            </div>
        </div>

        <!-- Summary Stats -->
        <div class="summary">
            <div class="stat-card info">
                <div class="label">Total JARs (Archive 1)</div>
                <div class="value">{len(jars1):,}</div>
            </div>
            <div class="stat-card info">
                <div class="label">Total JARs (Archive 2)</div>
                <div class="value">{len(jars2):,}</div>
            </div>
            <div class="stat-card orange">
                <div class="label">Major Size Diffs</div>
                <div class="value">{len(major_size_diffs):,}</div>
            </div>
            <div class="stat-card purple">
                <div class="label">Version Changes</div>
                <div class="value">{len(jar_version_changes):,}</div>
            </div>
            <div class="stat-card" style="background: linear-gradient(135deg, #48bb78 0%, #38a169 100%);">
                <div class="label">New in Archive 2</div>
                <div class="value">{len(truly_new_jars_in_2):,}</div>
            </div>
            <div class="stat-card danger">
                <div class="label">Only in Archive 1</div>
                <div class="value">{len(truly_new_jars_in_1):,}</div>
            </div>
        </div>"""
                    
                    # === RPM CPIO STRUCTURE SECTION (if comparing RPM files or folders with RPMs) ===
                    # Use the same has_rpm_files check as the navigation link
                    if has_rpm_files:
                        # Build structure tree from members
                        def build_structure_tree(members_dict):
                            """Build hierarchical structure from flat file paths"""
                            tree = {}
                            for path in members_dict.keys():
                                parts = path.replace('::', '/').split('/')
                                current = tree
                                for i, part in enumerate(parts):
                                    if part not in current:
                                        current[part] = {} if i < len(parts) - 1 else None
                                    if current[part] is not None:
                                        current = current[part]
                            return tree
                        
                        def render_tree_html(tree, indent=0):
                            """Render tree as nested HTML"""
                            output = ""
                            for name, subtree in sorted(tree.items()):
                                icon = "📦" if name.endswith(('.jar', '.war', '.ear')) else "📁" if subtree else "📄"
                                if name.endswith('.class'):
                                    icon = "☕"
                                elif 'MANIFEST' in name.upper():
                                    icon = "📋"
                                
                                escaped_name = html.escape(name)
                                output += f'<div style="margin-left:{indent*16}px;padding:3px 0;font-size:12px;font-family:Consolas,monospace;">'
                                output += f'{icon} {escaped_name}'
                                output += '</div>'
                                if subtree:
                                    output += render_tree_html(subtree, indent + 1)
                            return output
                        
                        # Find RPM-specific members
                        rpm_members1 = {k: v for k, v in members1.items() if '.rpm::' in k}
                        rpm_members2 = {k: v for k, v in members2.items() if '.rpm::' in k}
                        
                        # Find RPM content differences
                        rpm_only_in_1 = set(rpm_members1.keys()) - set(rpm_members2.keys())
                        rpm_only_in_2 = set(rpm_members2.keys()) - set(rpm_members1.keys())
                        rpm_in_both = set(rpm_members1.keys()) & set(rpm_members2.keys())
                        rpm_content_differs = []
                        for name in rpm_in_both:
                            s1, c1 = rpm_members1[name]
                            s2, c2 = rpm_members2[name]
                            if c1 != c2:
                                rpm_content_differs.append(name)
                        
                        html_content += f"""
        <div class="section" id="rpm-structure">
            <div class="section-title" style="border-bottom-color: #805ad5;">📦 RPM CPIO Comparison Results</div>
            <p style="color:#718096;margin-bottom:15px;">
                Comparison of RPM files (CPIO-based structure). Shows nested content including WAR/EAR/JAR files.
            </p>
            
            <!-- RPM Summary Stats -->
            <div style="display:grid; grid-template-columns:repeat(4, 1fr); gap:15px; margin-bottom:20px;">
                <div style="background:linear-gradient(135deg, #805ad5 0%, #6b46c1 100%); color:white; padding:15px; border-radius:10px;">
                    <div style="font-size:11px; opacity:0.9;">Total Files (Archive 1)</div>
                    <div style="font-size:28px; font-weight:700;">{len(rpm_members1):,}</div>
                </div>
                <div style="background:linear-gradient(135deg, #805ad5 0%, #6b46c1 100%); color:white; padding:15px; border-radius:10px;">
                    <div style="font-size:11px; opacity:0.9;">Total Files (Archive 2)</div>
                    <div style="font-size:28px; font-weight:700;">{len(rpm_members2):,}</div>
                </div>
                <div style="background:linear-gradient(135deg, #ed8936 0%, #dd6b20 100%); color:white; padding:15px; border-radius:10px;">
                    <div style="font-size:11px; opacity:0.9;">Content Changed</div>
                    <div style="font-size:28px; font-weight:700;">{len(rpm_content_differs):,}</div>
                </div>
                <div style="background:linear-gradient(135deg, #48bb78 0%, #38a169 100%); color:white; padding:15px; border-radius:10px;">
                    <div style="font-size:11px; opacity:0.9;">Only in New RPM</div>
                    <div style="font-size:28px; font-weight:700;">{len(rpm_only_in_2):,}</div>
                </div>
            </div>"""
                        
                        # RPM Content Changes Table
                        if rpm_content_differs:
                            html_content += f"""
            <h4 style="color:#805ad5; margin:20px 0 10px 0;">⚠️ RPM Content Changes ({len(rpm_content_differs):,} files)</h4>
            <div class="table-container" style="overflow-x:auto;">
                <table style="width:100%; border-collapse:collapse; font-size:13px;">
                    <thead>
                        <tr style="background:#f7fafc;">
                            <th style="padding:12px; text-align:left; border-bottom:2px solid #805ad5;">📄 File Path</th>
                            <th style="padding:12px; text-align:right; border-bottom:2px solid #805ad5;">📁 Old Size</th>
                            <th style="padding:12px; text-align:right; border-bottom:2px solid #805ad5;">📁 New Size</th>
                            <th style="padding:12px; text-align:right; border-bottom:2px solid #805ad5;">📊 Change</th>
                        </tr>
                    </thead>
                    <tbody>"""
                            for name in sorted(rpm_content_differs)[:100]:
                                s1, c1 = rpm_members1[name]
                                s2, c2 = rpm_members2[name]
                                diff = s2 - s1
                                diff_pct = ((s2 - s1) / s1 * 100) if s1 > 0 else 0
                                diff_class = "color:#e53e3e;" if diff > 0 else "color:#38a169;" if diff < 0 else "color:#718096;"
                                diff_sign = "+" if diff > 0 else ""
                                display_name = name.split('::')[-1] if '::' in name else name
                                html_content += f"""
                        <tr style="border-bottom:1px solid #e2e8f0;">
                            <td style="padding:10px; font-family:Consolas,monospace; font-size:12px;">{html.escape(display_name)}</td>
                            <td style="padding:10px; text-align:right;">{s1:,} bytes</td>
                            <td style="padding:10px; text-align:right;">{s2:,} bytes</td>
                            <td style="padding:10px; text-align:right; {diff_class} font-weight:600;">{diff_sign}{diff:,} bytes ({diff_sign}{diff_pct:.1f}%)</td>
                        </tr>"""
                            if len(rpm_content_differs) > 100:
                                html_content += f"""
                        <tr><td colspan="4" style="padding:10px; color:#718096; text-align:center;">... and {len(rpm_content_differs) - 100:,} more files</td></tr>"""
                            html_content += """
                    </tbody>
                </table>
            </div>"""
                        
                        # RPM New Files
                        if rpm_only_in_2:
                            html_content += f"""
            <h4 style="color:#48bb78; margin:20px 0 10px 0;">✅ New Files in RPM Archive 2 ({len(rpm_only_in_2):,} files)</h4>
            <div style="background:#f0fff4; border-radius:8px; padding:15px; max-height:300px; overflow:auto;">"""
                            for name in sorted(rpm_only_in_2)[:50]:
                                s2, c2 = rpm_members2[name]
                                display_name = name.split('::')[-1] if '::' in name else name
                                html_content += f"""
                <div style="padding:6px 10px; margin-bottom:4px; background:white; border-radius:4px; border-left:4px solid #48bb78; font-family:Consolas,monospace; font-size:12px;">
                    {html.escape(display_name)} <span style="color:#718096;">({s2:,} bytes)</span>
                </div>"""
                            if len(rpm_only_in_2) > 50:
                                html_content += f'<div style="color:#718096; padding:10px;">... and {len(rpm_only_in_2) - 50:,} more files</div>'
                            html_content += "</div>"
                        
                        # RPM Removed Files
                        if rpm_only_in_1:
                            html_content += f"""
            <h4 style="color:#e53e3e; margin:20px 0 10px 0;">❌ Removed Files from RPM Archive 1 ({len(rpm_only_in_1):,} files)</h4>
            <div style="background:#fff5f5; border-radius:8px; padding:15px; max-height:300px; overflow:auto;">"""
                            for name in sorted(rpm_only_in_1)[:50]:
                                s1, c1 = rpm_members1[name]
                                display_name = name.split('::')[-1] if '::' in name else name
                                html_content += f"""
                <div style="padding:6px 10px; margin-bottom:4px; background:white; border-radius:4px; border-left:4px solid #e53e3e; font-family:Consolas,monospace; font-size:12px;">
                    {html.escape(display_name)} <span style="color:#718096;">({s1:,} bytes)</span>
                </div>"""
                            if len(rpm_only_in_1) > 50:
                                html_content += f'<div style="color:#718096; padding:10px;">... and {len(rpm_only_in_1) - 50:,} more files</div>'
                            html_content += "</div>"
                        
                        # Tree structure view
                        html_content += f"""
            <h4 style="color:#805ad5; margin:20px 0 10px 0;">📁 RPM Structure Tree</h4>
            <div style="display:grid; grid-template-columns:1fr 1fr; gap:20px;">
                <div style="background:#f7fafc; border-radius:8px; padding:15px; max-height:400px; overflow:auto;">
                    <h5 style="margin:0 0 10px 0; color:#805ad5;">Archive 1 ({len(rpm_members1):,} files)</h5>"""
                        
                        tree1 = build_structure_tree(rpm_members1)
                        html_content += render_tree_html(dict(list(tree1.items())[:30]))
                        if len(tree1) > 30:
                            html_content += f'<div style="color:#718096;font-size:11px;margin-top:10px;">... and {len(tree1)-30} more top-level items</div>'
                        
                        html_content += f"""
                </div>
                <div style="background:#f7fafc; border-radius:8px; padding:15px; max-height:400px; overflow:auto;">
                    <h5 style="margin:0 0 10px 0; color:#805ad5;">Archive 2 ({len(rpm_members2):,} files)</h5>"""
                        
                        tree2 = build_structure_tree(rpm_members2)
                        html_content += render_tree_html(dict(list(tree2.items())[:30]))
                        if len(tree2) > 30:
                            html_content += f'<div style="color:#718096;font-size:11px;margin-top:10px;">... and {len(tree2)-30} more top-level items</div>'
                        
                        html_content += """
                </div>
            </div>
        </div>"""

                    # === MAJOR SIZE DIFFERENCES SECTION ===
                    if major_size_diffs:
                        html_content += f"""
        <div class="section" id="size-diffs">
            <div class="section-title size-diff">🔴 Major JAR Size Differences ({len(major_size_diffs)})</div>
            <p style="color:#718096;margin-bottom:15px;">JARs with size changes ≥ 10KB or ≥ 5% (found inside nested archives)</p>
            <div class="file-list">"""
                        for item in major_size_diffs:
                            diff_class = "increase" if item['diff'] > 0 else "decrease"
                            diff_sign = "+" if item['diff'] > 0 else ""
                            parent1 = jars1.get(item['name'], {}).get('parent', '')
                            parent2 = jars2.get(item['name'], {}).get('parent', '')
                            parent_info = f" (in {html.escape(parent1)})" if parent1 else ""
                            html_content += f"""
                <div class="file-item changed">
                    <strong>{html.escape(item['name'])}</strong>{parent_info}
                    <span class="badge {diff_class}">{diff_sign}{item['diff']:,} bytes ({diff_sign}{item['diff_pct']:.1f}%)</span>
                    <div class="size-info">
                        Archive 1: {item['size1']:,} bytes → Archive 2: {item['size2']:,} bytes
                    </div>
                </div>"""
                        html_content += """
            </div>
        </div>"""

                    # === JAR VERSION CHANGES SECTION ===
                    if jar_version_changes:
                        html_content += f"""
        <div class="section" id="version-changes">
            <div class="section-title version">🔄 JAR Version/Naming Changes ({len(jar_version_changes)})</div>
            <p style="color:#718096;margin-bottom:15px;">JARs with same base name but different versions (scanned from nested WAR/EAR files)</p>
            <div class="file-list">"""
                        for item in jar_version_changes:
                            diff_class = "increase" if item['diff'] > 0 else "decrease"
                            diff_sign = "+" if item['diff'] > 0 else ""
                            parent1 = jars1.get(item['old'], {}).get('parent', '')
                            parent2 = jars2.get(item['new'], {}).get('parent', '')
                            parent_info1 = f" <small style='color:#718096'>(in {html.escape(parent1)})</small>" if parent1 else ""
                            parent_info2 = f" <small style='color:#718096'>(in {html.escape(parent2)})</small>" if parent2 else ""
                            html_content += f"""
                <div class="file-item version">
                    <div style="margin-bottom:6px;">
                        <span style="color:#f56565;">❌ {html.escape(item['old'])}</span>{parent_info1}
                        <span style="color:#718096;margin:0 8px;">→</span>
                        <span style="color:#48bb78;">✅ {html.escape(item['new'])}</span>{parent_info2}
                    </div>
                    <div class="size-info">
                        Size: {item['size1']:,} → {item['size2']:,} bytes
                        <span class="size-change {diff_class}">({diff_sign}{item['diff']:,} bytes, {diff_sign}{item['diff_pct']:.1f}%)</span>
                    </div>
                </div>"""
                        html_content += """
            </div>
        </div>"""

                    # === NEW JARS SECTION ===
                    if truly_new_jars_in_2:
                        html_content += f"""
        <div class="section" id="new-jars">
            <div class="section-title new">✅ New JARs in Archive 2 ({len(truly_new_jars_in_2)})</div>
            <p style="color:#718096;margin-bottom:15px;">JARs that don't exist (even with different version) in Archive 1 - scanned from nested WAR/EAR files</p>
            <div class="file-list">"""
                        for name in truly_new_jars_in_2:
                            jar_info = jars2[name]
                            size = jar_info['size']
                            parent = jar_info.get('parent', '')
                            parent_info = f" <small style='color:#718096'>Found in: {html.escape(parent)}</small>" if parent else ""
                            html_content += f"""
                <div class="file-item added">
                    <strong>{html.escape(name)}</strong>{parent_info}
                    <div class="size-info">Size: {size:,} bytes</div>
                </div>"""
                        html_content += """
            </div>
        </div>"""

                    # === REMOVED JARS SECTION ===
                    if truly_new_jars_in_1:
                        html_content += f"""
        <div class="section" id="removed-jars">
            <div class="section-title removed">❌ JARs Only in Archive 1 ({len(truly_new_jars_in_1)})</div>
            <p style="color:#718096;margin-bottom:15px;">JARs that don't exist (even with different version) in Archive 2 - scanned from nested WAR/EAR files</p>
            <div class="file-list">"""
                        for name in truly_new_jars_in_1:
                            jar_info = jars1[name]
                            size = jar_info['size']
                            parent = jar_info.get('parent', '')
                            parent_info = f" <small style='color:#718096'>Found in: {html.escape(parent)}</small>" if parent else ""
                            html_content += f"""
                <div class="file-item removed">
                    <strong>{html.escape(name)}</strong>{parent_info}
                    <div class="size-info">Size: {size:,} bytes</div>
                </div>"""
                        html_content += """
            </div>
        </div>"""

                    # === ALL FILES WITH DIFFERENT CONTENT ===
                    # Helper function to extract file content from RPM CPIO structure
                    def extract_from_rpm_cpio(rpm_path, file_path):
                        """Extract file content from RPM's CPIO archive"""
                        try:
                            import gzip
                            
                            with open(str(rpm_path), 'rb') as f:
                                rpm_data = f.read()
                            
                            # Find gzip signature in RPM
                            gzip_start = rpm_data.find(b'\x1f\x8b')
                            if gzip_start == -1:
                                return None
                            
                            # Decompress CPIO data
                            gzip_data = rpm_data[gzip_start:]
                            try:
                                cpio_data = gzip.decompress(gzip_data)
                            except:
                                return None
                            
                            # Parse CPIO to find the file
                            pos = 0
                            while pos < len(cpio_data) - 110:
                                magic = cpio_data[pos:pos+6]
                                if magic not in [b'070701', b'070702']:
                                    pos += 1
                                    continue
                                
                                try:
                                    header = cpio_data[pos:pos+110].decode('ascii')
                                    if not header.startswith('0707'):
                                        pos += 1
                                        continue
                                    
                                    filesize = int(header[54:62], 16)
                                    namesize = int(header[94:102], 16)
                                    
                                    name_start = pos + 110
                                    name_end = name_start + namesize - 1
                                    filename = cpio_data[name_start:name_end].decode('utf-8', errors='replace')
                                    
                                    if filename == 'TRAILER!!!':
                                        break
                                    
                                    name_padded = ((110 + namesize + 3) // 4) * 4
                                    data_start = pos + name_padded
                                    data_padded = ((filesize + 3) // 4) * 4
                                    
                                    # Check if this is our file (handle path variations)
                                    clean_filename = filename.lstrip('./')
                                    if clean_filename == file_path or filename == file_path:
                                        return cpio_data[data_start:data_start+filesize]
                                    
                                    pos = data_start + data_padded
                                except:
                                    pos += 1
                                    continue
                            
                            return None
                        except Exception as e:
                            print(f"[DEBUG] Error extracting from RPM: {e}")
                            return None
                    
                    # Helper function to extract file content from nested archive path
                    def extract_nested_file_content(archive_path, nested_path):
                        """Extract file content from nested archive path like 'ASCF.war::WEB-INF/lib/SomeClass.class'
                        Also handles RPM CPIO structure and folder-based archive pairs."""
                        try:
                            parts = nested_path.split('::')
                            current_data = None
                            actual_archive_path = archive_path
                            
                            # Check if first part is an archive filename (folder-based comparison)
                            # Path format: "ArchiveName.ear::nested/path::file"
                            if len(parts) > 1 and any(parts[0].lower().endswith(ext) for ext in supported_extensions):
                                # First part is the archive name - need to find it in the folder
                                archive_name = parts[0]
                                if is_folder1 or is_folder2:
                                    # Find the actual archive file in the folder
                                    folder = p1 if 'folder1' in str(archive_path) or str(archive_path) == str(p1) else p2
                                    actual_archive_path = folder / archive_name
                                    if not _path_exists(actual_archive_path):
                                        # Try searching in the folder
                                        for root, dirs, files in os.walk(str(folder)):
                                            if archive_name in files:
                                                actual_archive_path = P(root) / archive_name
                                                break
                                parts = parts[1:]  # Remove archive name from parts
                            
                            ext = str(actual_archive_path).lower()
                            
                            # Check if outer archive is RPM
                            if ext.endswith('.rpm'):
                                # First part is the file path within CPIO
                                first_part = parts[0]
                                rpm_content = extract_from_rpm_cpio(actual_archive_path, first_part)
                                
                                if rpm_content is None:
                                    return None
                                
                                if len(parts) == 1:
                                    return rpm_content
                                
                                # Continue navigating nested ZIP archives
                                current_data = rpm_content
                                parts = parts[1:]  # Skip the first part we already extracted
                            else:
                                # Read the outer archive (ZIP-based)
                                with open(str(actual_archive_path), 'rb') as f:
                                    current_data = f.read()
                            
                            # Navigate through nested archives (ZIP/JAR/WAR/EAR)
                            for i, part in enumerate(parts):
                                if i == len(parts) - 1:
                                    # This is the final file
                                    try:
                                        with zipfile.ZipFile(BytesIO(current_data), 'r') as zf:
                                            return zf.read(part)
                                    except:
                                        # Maybe it's directly the content
                                        return current_data if i == 0 else None
                                else:
                                    # This is an intermediate archive
                                    with zipfile.ZipFile(BytesIO(current_data), 'r') as zf:
                                        current_data = zf.read(part)
                            
                            return None
                        except Exception as e:
                            print(f"[DEBUG] Error extracting {nested_path}: {e}")
                            return None
                    
                    # Get decompiler for class files
                    from folder_compare import _get_decompiler, _decompile_class_file
                    dec_type, dec_path = _get_decompiler()
                    if dec_type:
                        print(f"[DEBUG] Decompiler available: {dec_type} at {dec_path}")
                    
                    if content_differs:
                        # Separate files by type for better organization
                        manifest_files = [f for f in content_differs if 'MANIFEST.MF' in f.upper() or 'META-INF' in f.upper()]
                        class_files = [f for f in content_differs if f.lower().endswith('.class')]
                        jar_files = [f for f in content_differs if f.lower().endswith('.jar')]
                        other_files = [f for f in content_differs if f not in manifest_files and f not in class_files and f not in jar_files]
                        # Keep HTML generation bounded even for very large diffs.
                        MAX_HTML_ROWS = 100
                        MAX_CLASS_DECOMPILATIONS = 8
                        MAX_TEXT_DIFFS = 12
                        class_decomp_done = 0
                        text_diff_done = 0
                        skip_heavy_inline_diffs = len(content_differs) > 5000
                        
                        html_content += f"""
        <div class="section" id="content-diffs">
            <div class="section-title">⚠️ All Files with Different Content ({len(content_differs):,})</div>
            <p style="color:#718096;margin-bottom:15px;">
                📦 JAR files: {len(jar_files)} | 
                ☕ Class files: {len(class_files)} | 
                📋 Manifest files: {len(manifest_files)} | 
                📄 Other files: {len(other_files)}
                {' | 🔧 <strong style="color:#48bb78;">Decompiler: ' + dec_type.upper() + '</strong>' if dec_type else ' | ⚠️ <em>No decompiler - install CFR for Java source diff</em>'}
            </p>
            {'<p style="color:#9a3412;margin:-4px 0 15px 0;font-size:12px;">Inline source diffs are auto-limited for performance on large comparisons.</p>' if skip_heavy_inline_diffs else ''}
            
            <div class="table-container">
                <table class="content-diff-table">
                    <thead>
                        <tr>
                            <th style="width: 40%;">📄 File Path</th>
                            <th style="width: 15%;">📁 Old Size</th>
                            <th style="width: 15%;">📁 New Size</th>
                            <th style="width: 15%;">📊 Change</th>
                            <th style="width: 15%;">🔍 Details</th>
                        </tr>
                    </thead>
                    <tbody>"""
                        
                        # Process files with content diff (bounded for performance)
                        row_id = 0
                        for name in content_differs[:MAX_HTML_ROWS]:
                            s1, c1 = members1[name]
                            s2, c2 = members2[name]
                            diff = s2 - s1
                            diff_class = "increase" if diff > 0 else "decrease" if diff < 0 else "same"
                            diff_sign = "+" if diff > 0 else ""
                            pct_change = ((s2 - s1) / s1 * 100) if s1 > 0 else 0
                            pct_str = f" ({diff_sign}{pct_change:.1f}%)"
                            
                            # Determine file type icon
                            if name.lower().endswith('.class'):
                                icon = "☕"
                                file_type = "class"
                            elif name.lower().endswith('.jar'):
                                icon = "📦"
                                file_type = "jar"
                            elif 'MANIFEST.MF' in name.upper() or name.lower().endswith('.manifest'):
                                icon = "📋"
                                file_type = "manifest"
                            elif name.lower().endswith(('.xml', '.properties', '.txt', '.cfg', '.mf', '.inf')):
                                icon = "📄"
                                file_type = "text"
                            else:
                                icon = "📄"
                                file_type = "other"
                            
                            row_id += 1
                            
                            # For class files and manifest files, try to show content diff
                            # Note: class files need decompiler, but manifest/text files don't
                            content_diff_html = ""
                            if (
                                file_type == 'class'
                                and dec_type
                                and not skip_heavy_inline_diffs
                                and class_decomp_done < MAX_CLASS_DECOMPILATIONS
                            ):
                                # Class files require decompiler
                                try:
                                    content1 = extract_nested_file_content(p1, name)
                                    content2 = extract_nested_file_content(p2, name)
                                    
                                    if content1 and content2:
                                        class_name = os.path.basename(name).replace('.class', '')
                                        ok1, src1, err1 = _decompile_class_file(content1, class_name, dec_type, dec_path)
                                        ok2, src2, err2 = _decompile_class_file(content2, class_name, dec_type, dec_path)
                                        
                                        if ok1 and ok2 and src1 != src2:
                                            class_decomp_done += 1
                                            # Generate line-by-line diff with color highlighting
                                            import difflib
                                            lines1 = src1.splitlines()[:50]  # Limit to 50 lines for performance
                                            lines2 = src2.splitlines()[:50]
                                            
                                            # Build highlighted diff for old version (red for removed)
                                            old_diff_html = ""
                                            new_diff_html = ""
                                            
                                            # Process old version - show removed lines in red
                                            for line in lines1:
                                                escaped_line = html.escape(line)
                                                if line not in lines2:
                                                    old_diff_html += f'<div style="background:#5c1f1f; color:#fca5a5; padding:2px 6px; margin:1px 0; border-radius:3px; border-left:3px solid #ef4444;">- {escaped_line}</div>'
                                                else:
                                                    old_diff_html += f'<div style="color:#d4d4d4; padding:2px 6px;">{escaped_line}</div>'
                                            
                                            # Process new version - show added lines in green
                                            for line in lines2:
                                                escaped_line = html.escape(line)
                                                if line not in lines1:
                                                    new_diff_html += f'<div style="background:#1f3d1f; color:#86efac; padding:2px 6px; margin:1px 0; border-radius:3px; border-left:3px solid #22c55e;">+ {escaped_line}</div>'
                                                else:
                                                    new_diff_html += f'<div style="color:#d4d4d4; padding:2px 6px;">{escaped_line}</div>'
                                            
                                            content_diff_html = f'''
                                            <div id="diff-{row_id}" style="display:none; margin-top:10px; background:#1e1e1e; border-radius:8px; padding:15px; max-height:500px; overflow:auto;">
                                                <div style="display:grid; grid-template-columns:1fr 1fr; gap:20px;">
                                                    <div style="border:1px solid #374151; border-radius:6px; overflow:hidden;">
                                                        <div style="background:#991b1b; color:white; font-weight:bold; padding:10px; font-size:12px;">❌ Old Version (Removed in Red)</div>
                                                        <div style="padding:10px; font-size:11px; font-family:Consolas,monospace; max-height:350px; overflow:auto;">{old_diff_html}</div>
                                                    </div>
                                                    <div style="border:1px solid #374151; border-radius:6px; overflow:hidden;">
                                                        <div style="background:#166534; color:white; font-weight:bold; padding:10px; font-size:12px;">✅ New Version (Added in Green)</div>
                                                        <div style="padding:10px; font-size:11px; font-family:Consolas,monospace; max-height:350px; overflow:auto;">{new_diff_html}</div>
                                                    </div>
                                                </div>
                                            </div>'''
                                except Exception as e:
                                    print(f"[DEBUG] Error processing class diff for {name}: {e}")
                            elif (
                                file_type in ['manifest', 'text']
                                and not skip_heavy_inline_diffs
                                and text_diff_done < MAX_TEXT_DIFFS
                            ):
                                # Show text diff for manifest/text files (no decompiler needed)
                                try:
                                    content1 = extract_nested_file_content(p1, name)
                                    content2 = extract_nested_file_content(p2, name)
                                    
                                    if content1 and content2:
                                        text1 = content1.decode('utf-8', errors='replace')[:4000]
                                        text2 = content2.decode('utf-8', errors='replace')[:4000]
                                        if text1 != text2:
                                            text_diff_done += 1
                                            # Generate line-by-line diff with color highlighting
                                            import difflib
                                            lines1 = text1.splitlines(keepends=True)
                                            lines2 = text2.splitlines(keepends=True)
                                            
                                            # Build highlighted diff for old version (red for removed)
                                            old_diff_html = ""
                                            new_diff_html = ""
                                            
                                            differ = difflib.unified_diff(lines1, lines2, lineterm='')
                                            diff_lines = list(differ)
                                            
                                            # Process old version - show removed lines in red
                                            for line in lines1:
                                                escaped_line = html.escape(line.rstrip('\n\r'))
                                                if line not in lines2:
                                                    old_diff_html += f'<div style="background:#5c1f1f; color:#fca5a5; padding:2px 6px; margin:1px 0; border-radius:3px; border-left:3px solid #ef4444;">- {escaped_line}</div>'
                                                else:
                                                    old_diff_html += f'<div style="color:#d4d4d4; padding:2px 6px;">{escaped_line}</div>'
                                            
                                            # Process new version - show added lines in green
                                            for line in lines2:
                                                escaped_line = html.escape(line.rstrip('\n\r'))
                                                if line not in lines1:
                                                    new_diff_html += f'<div style="background:#1f3d1f; color:#86efac; padding:2px 6px; margin:1px 0; border-radius:3px; border-left:3px solid #22c55e;">+ {escaped_line}</div>'
                                                else:
                                                    new_diff_html += f'<div style="color:#d4d4d4; padding:2px 6px;">{escaped_line}</div>'
                                            
                                            content_diff_html = f'''
                                            <div id="diff-{row_id}" style="display:none; margin-top:10px; background:#1e1e1e; border-radius:8px; padding:15px; max-height:500px; overflow:auto;">
                                                <div style="display:grid; grid-template-columns:1fr 1fr; gap:20px;">
                                                    <div style="border:1px solid #374151; border-radius:6px; overflow:hidden;">
                                                        <div style="background:#991b1b; color:white; font-weight:bold; padding:10px; font-size:12px;">❌ Old Version (Removed in Red)</div>
                                                        <div style="padding:10px; font-size:11px; font-family:Consolas,monospace; max-height:350px; overflow:auto;">{old_diff_html}</div>
                                                    </div>
                                                    <div style="border:1px solid #374151; border-radius:6px; overflow:hidden;">
                                                        <div style="background:#166534; color:white; font-weight:bold; padding:10px; font-size:12px;">✅ New Version (Added in Green)</div>
                                                        <div style="padding:10px; font-size:11px; font-family:Consolas,monospace; max-height:350px; overflow:auto;">{new_diff_html}</div>
                                                    </div>
                                                </div>
                                            </div>'''
                                except Exception as e:
                                    print(f"[DEBUG] Error processing text diff for {name}: {e}")
                            
                            # Create expandable button if we have content diff
                            expand_btn = ""
                            if content_diff_html:
                                expand_btn = f'''<button onclick="var d=document.getElementById('diff-{row_id}');d.style.display=d.style.display==='none'?'block':'none';this.textContent=d.style.display==='none'?'View Diff':'Hide'" style="background:#667eea;color:white;border:none;padding:6px 12px;border-radius:6px;cursor:pointer;font-size:11px;">View Diff</button>'''
                            else:
                                expand_btn = f'<span style="color:#9ca3af;font-size:11px;">-</span>'
                            
                            html_content += f"""
                        <tr>
                            <td class="file-name">{icon} {html.escape(name)}</td>
                            <td class="size-cell">{s1:,}</td>
                            <td class="size-cell">{s2:,}</td>
                            <td class="change-cell {diff_class}">{diff_sign}{diff:,}{pct_str}</td>
                            <td style="text-align:center;">{expand_btn}</td>
                        </tr>"""
                            if content_diff_html:
                                html_content += f"""
                        <tr><td colspan="5" style="padding:0;border:none;">{content_diff_html}</td></tr>"""
                        
                        html_content += """
                    </tbody>
                </table>"""
                        if len(content_differs) > MAX_HTML_ROWS:
                            more_count = len(content_differs) - MAX_HTML_ROWS
                            html_content += f"""
                <div style="padding:12px; text-align:center; color:#718096; font-size:12px; background:#f7fafc; border-radius:0 0 8px 8px;">
                    <a href="javascript:void(0)" id="content-diff-more-link"
                       onclick="var el=document.getElementById('content-diff-more-rows'); var lk=document.getElementById('content-diff-more-link'); var show=(el.style.display==='none'); el.style.display=show?'block':'none'; lk.textContent=show?'Hide extra files':'... and {more_count:,} more files with different content';"
                       style="color:#2563eb; text-decoration:underline; font-weight:600;">
                        ... and {more_count:,} more files with different content
                    </a>
                </div>
                <div id="content-diff-more-rows" style="display:none; margin-top:10px;">
                    <table class="content-diff-table">
                        <thead>
                            <tr>
                                <th style="width: 40%;">📄 File Path</th>
                                <th style="width: 15%;">📁 Old Size</th>
                                <th style="width: 15%;">📁 New Size</th>
                                <th style="width: 15%;">📊 Change</th>
                                <th style="width: 15%;">🔍 Details</th>
                            </tr>
                        </thead>
                        <tbody>"""
                            for name in content_differs[MAX_HTML_ROWS:]:
                                s1, c1 = members1[name]
                                s2, c2 = members2[name]
                                diff = s2 - s1
                                diff_class = "increase" if diff > 0 else "decrease" if diff < 0 else "same"
                                diff_sign = "+" if diff > 0 else ""
                                pct_change = ((s2 - s1) / s1 * 100) if s1 > 0 else 0
                                pct_str = f" ({diff_sign}{pct_change:.1f}%)"
                                if name.lower().endswith('.class'):
                                    icon = "☕"
                                elif name.lower().endswith('.jar'):
                                    icon = "📦"
                                elif 'MANIFEST.MF' in name.upper() or name.lower().endswith('.manifest'):
                                    icon = "📋"
                                else:
                                    icon = "📄"
                                html_content += f"""
                            <tr>
                                <td class="file-name">{icon} {html.escape(name)}</td>
                                <td class="size-cell">{s1:,}</td>
                                <td class="size-cell">{s2:,}</td>
                                <td class="change-cell {diff_class}">{diff_sign}{diff:,}{pct_str}</td>
                                <td style="text-align:center;"><span style="color:#9ca3af;font-size:11px;">-</span></td>
                            </tr>"""
                            html_content += """
                        </tbody>
                    </table>
                </div>"""
                        html_content += """
            </div>
        </div>"""

                    # === EMPTY STATE ===
                    if not major_size_diffs and not jar_version_changes and not truly_new_jars_in_1 and not truly_new_jars_in_2 and not content_differs:
                        html_content += """
        <div class="empty-state">
            <h2>✅ Archives are Identical!</h2>
            <p>No differences found between the two archives.</p>
        </div>"""

                    html_content += """
        <a href="/" class="back-link">← Back to Home</a>
        </div>
    </div>
</body>
</html>"""
                    
                    # Save HTML report
                    html_filename = os.path.splitext(saved_name)[0] + '.html'
                    html_filepath = str(_report_path(html_filename))
                    with open(html_filepath, 'w', encoding='utf-8') as f:
                        f.write(inject_report_section_controls(html_content))
                    print(f"[DEBUG] HTML report saved: {html_filepath}")
                    
                    results = True  # Mark as successful for redirect
                else:
                    # Use the full ArchiveContentsComparer for folders
                    print(f"[DEBUG] Using full archive comparer for folders")
                    comparer = ArchiveContentsComparer(folder1, folder2)
                    comparer.use_local_copy = use_local_copy
                    comparer.max_retries = max_retries
                    comparer.retry_delay = retry_delay
                    comparer.should_cleanup_temp_folders = not keep_temp_folders
                    comparer.suppress_warnings = suppress_warnings
                    comparison_progress["comparer"] = comparer
                    session_progress["comparer"] = comparer
                    results = comparer.compare_archives()
                    report_text = comparer.generate_archive_report() if results is not None else "No results."
                    
                    report_filepath = str(_report_path(saved_name))
                    comparer.save_archive_report(report_filepath)
                    print(f"[DEBUG] Archive report saved: {report_filepath}")
            elif mode == "foss":
                if not jar_name:
                    body = "<p style=\"color:#900\">Please enter a JAR name for FOSS/Foundation mode.</p>" + index_html().decode("utf-8")
                    sink.send_response(400)
                    sink.send_header("Content-Type", "text/html; charset=utf-8")
                    sink.end_headers()
                    sink.write(html_page("Invalid Input", body))
                    comparison_progress["running"] = False
                    return
                comparer = JarManifestComparer(folder1, folder2, jar_name)
                comparison_progress["comparer"] = comparer
                session_progress["comparer"] = comparer
                _ = comparer.compare_versions()
                report_text = comparer.generate_report()
                saved_name = _report_fname("ascf_foss_foundation_report")
                try:
                    report_filepath = str(_report_path(saved_name))
                    comparer.save_report(report_filepath)
                except Exception:
                    pass
            else:
                from crm_compare import CRMFolderComparer

                print("[DEBUG] ASCF full content comparison (enhanced report engine)")
                if ascf_class_diff_jar:
                    add_session_log(session_id, f"ASCF JAR class diff query: {ascf_class_diff_jar}", "INFO")
                if ascf_manifest_diff_jar:
                    add_session_log(session_id, f"ASCF JAR manifest diff query: {ascf_manifest_diff_jar}", "INFO")
                if ascf_config_files_filter:
                    add_session_log(session_id, f"ASCF config files report filter: {ascf_config_files_filter}", "INFO")
                if ascf_explicit_size_paths:
                    add_session_log(session_id, f"ASCF explicit size compare list: {ascf_explicit_size_paths}", "INFO")

                comparer = CRMFolderComparer(folder1, folder2)
                _configure_enhanced_full_tree_comparer(
                    comparer,
                    class_diff_jar=ascf_class_diff_jar,
                    manifest_diff_jar=ascf_manifest_diff_jar,
                    config_files_filter=ascf_config_files_filter,
                    explicit_size_paths=ascf_explicit_size_paths,
                    use_local_copy=use_local_copy,
                    keep_temp_folders=keep_temp_folders,
                    max_retries=max_retries,
                    retry_delay=retry_delay,
                    suppress_warnings=suppress_warnings,
                    show_missing_only=not show_all_differences,
                )
                comparer.exclude_missing_from_folder2 = exclude_missing_folder2
                comparer.rename_folder2_only_files = rename_folder2_only
                comparer.folder2_rename_suffix = rename_suffix
                comparison_progress["comparer"] = comparer
                session_progress["comparer"] = comparer
                results = comparer.compare_folders()
                report_text = comparer.generate_report() if results is not None else "No results."
                saved_name = _report_fname("ascf_patch_comparison")
                try:
                    report_filepath = str(_report_path(saved_name))
                    comparer.save_report(report_filepath)
                except Exception:
                    pass

                if results and hasattr(comparer, "generate_html_report"):
                    try:
                        html_content = comparer.generate_html_report()
                        html_filename = os.path.splitext(saved_name)[0] + ".html"
                        html_filepath = str(_report_path(html_filename))
                        with open(html_filepath, "w", encoding="utf-8") as f:
                            f.write(html_content)
                        print(f"[DEBUG] HTML report saved using CRMFolderComparer.generate_html_report(): {html_filepath}")
                    except Exception as html_err:
                        print(f"[DEBUG] Error generating HTML report: {html_err}")
            
            # Copy error from comparer if any
            if comparer and hasattr(comparer, 'comparison_error') and comparer.comparison_error:
                comparison_progress["error"] = comparer.comparison_error

            # Save
            # Reports saved via comparer.save_* methods; keep saved_name as-is

            # Keep only latest report set before redirecting.
            if saved_name:
                cleanup_old_reports(10)
            # Redirect to HTML report if present
            html_name = os.path.basename(os.path.splitext(saved_name)[0] + '.html') if saved_name else None
            html_check_path = str(_report_path(html_name)) if html_name else None
            print(f"[DEBUG] Checking for HTML report: {html_check_path}")
            print(f"[DEBUG] HTML file exists: {_path_exists(html_check_path) if html_check_path else False}")
            if html_check_path and _path_exists(html_check_path):
                print(f"[DEBUG] Redirecting to HTML report: /view?file={html_name}")
                _set_session_report_result_url(session_progress, html_name)
                sink.send_response(302)
                sink.send_header("Location", f"/view?file={quote(html_name)}")
                sink.end_headers()
                return
            else:
                # Fallback: show inline text
                print(f"[DEBUG] HTML file not found, showing text report instead")
                link_html = (
                    f"<a class=\"button\" href=\"/download?file={quote(saved_name)}\">Download report</a>"
                    if saved_name else ""
                )
                body = f"""
                <div style=\"margin-bottom:12px\"><a href=\"/\">← Back</a></div>
                <div style=\"margin: 8px 0 16px\">{link_html}</div>
                <pre>{html.escape(report_text or '')}</pre>
                """
                sink.send_response(200)
                sink.send_header("Content-Type", "text/html; charset=utf-8")
                sink.end_headers()
                sink.write(html_page("Report", body))
                return
        except Exception as e:
            import traceback
            error_details = traceback.format_exc()
            print(f"ERROR in comparison: {str(e)}")
            print(f"Traceback:\n{error_details}")
            
            comparison_progress["error"] = str(e)
            
            # Log error to session
            if 'session_id' in dir() and session_id:
                add_session_log(session_id, f"ERROR: {str(e)}", "ERROR")
                add_session_log(session_id, f"Traceback:", "ERROR")
                for line in error_details.split('\n'):
                    if line.strip():
                        add_session_log(session_id, line, "ERROR")
                if 'session_progress' in dir():
                    session_progress["error"] = str(e)
            
            error_html = f"<p style=\"color:#900\"><strong>Error:</strong> {html.escape(str(e))}</p>"
            error_html += f"<details><summary>Technical Details</summary><pre style=\"background:#f5f5f5;padding:10px;overflow:auto;\">{html.escape(error_details)}</pre></details>"
            body = error_html + index_html().decode("utf-8")
            
            sink.send_response(500)
            sink.send_header("Content-Type", "text/html; charset=utf-8")
            sink.end_headers()
            sink.write(html_page("Error", body))
            return
        finally:
            if 'session_progress' in dir():
                session_progress["running"] = False
                session_progress["comparer"] = None
            if comparison_progress.get("_session_id") == session_id:
                comparison_progress["running"] = False
                comparison_progress["comparer"] = None
            
            # Log completion and clear current session
            if 'session_id' in dir() and session_id:
                if session_progress.get("error"):
                    add_session_log(session_id, f"=== Comparison Failed ===", "ERROR")
                    add_session_log(session_id, f"Error: {session_progress.get('error')}", "ERROR")
                else:
                    add_session_log(session_id, f"=== Comparison Completed Successfully ===", "INFO")
                clear_current_session()

    def _send_open_report_redirect(self, session_id: str):
        """302 to report when ready; otherwise a short auto-refresh wait page."""
        session = get_session(session_id) if session_id else None
        if not session:
            sid_q = quote(session_id or "")
            open_url = f"/open-report?session_id={sid_q}"
            body = html_page(
                "Waiting for comparison session",
                f"""
                <p style="font-size:15px;color:#334155;">The comparison session is not registered yet (the run tab may still be loading).</p>
                <p style="font-size:13px;color:#64748b;">Session: <code>{html.escape(session_id or '')}</code></p>
                <p style="font-size:13px;color:#64748b;">If you just clicked <strong>Run comparison</strong>, allow pop-ups or use the manual link on the home page, then wait a few seconds.</p>
                <p><a href="{open_url}">Check again</a> · <a href="/">Home</a></p>
                <script>
                  setTimeout(function() {{
                    window.location.replace({json.dumps(open_url)});
                  }}, 2000);
                </script>
                """,
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        qs_user = ""
        client_id = request_client_identity(self, qs_user)
        client_host = (self.client_address[0] or "")
        if not session_owned_by_client(session, client_id, client_host):
            self.send_error(403, "Access denied for this comparison session")
            return
        ru = session.get("result_url")
        if ru:
            loc = str(ru)
            base = str(session.get("request_base_url") or "").strip()
            if not base:
                base = _request_base_url_from_handler(self)
            if loc.startswith("/"):
                loc = _finalize_result_url(session, base.rstrip("/") + loc)
            else:
                loc = _finalize_result_url(session, loc)
            self.send_response(302)
            self.send_header("Location", loc)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        err = session.get("error")
        if err:
            body = html_page(
                "Comparison failed",
                f"<p style='color:#991b1b;'>{html.escape(str(err))}</p><p><a href='/'>← Home</a></p>",
            )
            self.send_response(500)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        running = bool(session.get("running"))
        refresh_ms = 2000 if running else 3000
        sid_q = quote(session_id or "")
        open_url = f"/open-report?session_id={sid_q}"
        body = html_page(
            "Opening report",
            f"""
            <p style="font-size:15px;color:#334155;">Comparison {'running' if running else 'finishing'} — opening the HTML report when ready…</p>
            <p style="font-size:13px;color:#64748b;">Session: <code>{html.escape(session_id or '')}</code></p>
            <p><a href="{open_url}">Refresh now</a> · <a href="/">Home</a></p>
            <script>
              setTimeout(function() {{
                window.location.replace({json.dumps(open_url)});
              }}, {refresh_ms});
            </script>
            """,
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

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
        machine = (
            str(session_progress.get("client_machine_host") or "").strip()
            or str(session_progress.get("client_identity") or "").strip()
        )
        app_label = str(session_progress.get("compare_application") or "").strip()
        app_slug = normalize_application_slug(app_label)
        set_compare_client_context(
            machine,
            str(session_progress.get("client_identity") or ""),
            application=app_label,
        )
        try:
            if machine:
                add_session_log(
                    session_id,
                    f"Temp/copy target: client workstation \\\\{machine}\\c$\\crm_compare_temp"
                    + (f"\\{app_slug}" if app_slug else "")
                    + " (remote fallback only if full)",
                    "INFO",
                )
            pre = cleanup_compare_temp_before_run(machine, application=app_label)
            if pre.get("removed"):
                add_session_log(
                    session_id,
                    f"Cleaned old compare temp for {pre.get('application') or app_label or 'comparison'} "
                    f"on {pre.get('client_machine') or machine}: "
                    f"{pre['removed']} item(s), ~{pre.get('freed_human', '0 B')} freed",
                    "INFO",
                )
            elif pre.get("errors"):
                add_session_log(
                    session_id,
                    "Compare temp cleanup: some paths could not be removed (run continues)",
                    "WARN",
                )
            handler_impl._execute_run_comparison(sink, qs_snap, session_id, session_progress)
        except Exception as e:
            import traceback
            session_progress["error"] = str(e)
            add_session_log(session_id, f"Unhandled worker error: {e}", "ERROR")
            add_session_log(session_id, traceback.format_exc(), "ERROR")
        finally:
            clear_compare_client_context()
            session_progress["running"] = False
            if comparison_progress.get("_session_id") == session_id:
                comparison_progress["running"] = False

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        if not self._enforce_security("GET", parsed):
            return
        path = parsed.path

        if path == "/api/crm-build-base-paths":
            data = {"bases": get_crm_build_base_paths()}
            body = json.dumps(data).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/api/client-identity":
            ident = resolve_client_identity(
                (self.client_address[0] or ""),
                {k: v for k, v in self.headers.items()},
                "",
            )
            machine = resolve_client_machine_hostname(
                (self.client_address[0] or ""),
                {k: v for k, v in self.headers.items()},
                "",
            )
            data = {
                "identity": ident,
                "client_host": self.client_address[0] or "",
                "client_machine": machine,
                "timezone": report_timestamp_with_tz()[1],
                "minimum_required_gb": round(min_compare_space_bytes() / (1024**3), 1),
            }
            body = json.dumps(data).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/api/comparison-space-check":
            qs = parse_qs(parsed.query)
            folder1 = normalize_build_path_for_windows(qs.get("folder1_path", [""])[0].strip())
            folder2 = normalize_build_path_for_windows(qs.get("folder2_path", [""])[0].strip())
            ident = resolve_client_identity(
                (self.client_address[0] or ""),
                {k: v for k, v in self.headers.items()},
                qs.get("report_user", [""])[0],
            )
            machine = resolve_client_machine_hostname(
                (self.client_address[0] or ""),
                {k: v for k, v in self.headers.items()},
                qs.get("report_user", [""])[0],
            )
            quick = str(qs.get("quick", ["0"])[0]).strip().lower() in ("1", "true", "yes", "on")
            status = comparison_space_status(machine, folder1, folder2, quick=quick)
            status["identity"] = ident
            status["client_free_gb"] = round(
                int(status.get("client_free_bytes") or 0) / (1024**3), 1
            )
            pre_sid = (qs.get("session_id", [""])[0] or "").strip()
            if pre_sid:
                reg_sid, _ = _prepare_comparison_session(self, qs)
                status["session_id"] = reg_sid
                status["session_registered"] = True
            body = json.dumps(status).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/api/crm-find-jar-for-class":
            qs = parse_qs(parsed.query)
            class_name = str(
                qs.get("class_name", qs.get("class", [""]))[0] or ""
            ).strip()
            try:
                ssh_port = int(qs.get("port", qs.get("ssh_port", ["22"]))[0] or 22)
            except (ValueError, TypeError):
                ssh_port = 22
            try:
                status, payload = _crm_find_jar_for_class_api(
                    class_name,
                    ssh_host=str(qs.get("host", qs.get("ssh_host", [""]))[0] or "").strip(),
                    ssh_username=str(
                        qs.get("username", qs.get("ssh_username", [""]))[0] or ""
                    ).strip(),
                    ssh_password=str(
                        qs.get("password", qs.get("ssh_password", [""]))[0] or ""
                    ),
                    ssh_port=ssh_port,
                    ssh_remote_path=str(
                        qs.get("path", qs.get("remote_path", qs.get("ssh_remote_path", [""])))[0]
                        or ""
                    ).strip(),
                    folder2=normalize_build_path_for_windows(
                        qs.get("folder2_path", qs.get("build_path", [""]))[0].strip()
                    ),
                )
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                err = json.dumps(
                    {"ok": False, "error": str(e), "jars": []}
                ).encode("utf-8")
                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(err)))
                self.end_headers()
                self.wfile.write(err)
            return

        if path in ("/", "/crm", "/ascf", "/uxf", "/dex", "/wef"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(index_html())
            return
        if path == "//crm":
            self.send_response(302)
            self.send_header("Location", "/crm")
            self.end_headers()
            return

        if path == "/progress":
            # Return progress status as JSON
            # Supports session_id parameter for multi-user isolation
            qs = parse_qs(parsed.query)
            session_id = qs.get("session_id", [""])[0]
            client_id = request_client_identity(self, qs.get("report_user", [""])[0])
            client_host = (self.client_address[0] or "")

            if not session_id:
                status = _idle_progress_payload("")
                response = json.dumps(status).encode('utf-8')
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)
                return

            session = get_session(session_id)
            if session and not session_owned_by_client(session, client_id, client_host):
                status = _idle_progress_payload(session_id)
                status["error"] = "Access denied for this comparison session"
                response = json.dumps(status).encode('utf-8')
                self.send_response(403)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)
                return

            progress_data = session if session else _idle_progress_payload(session_id)
            
            status = {
                "running": progress_data.get("running", False),
                "progress": 0,
                "total": 0,
                "percentage": 0,
                "current_file": "",
                "eta_seconds": 0,
                "eta_formatted": "Calculating...",
                "error": progress_data.get("error"),
                "session_id": session_id,
                "session_registered": session is not None,
                "worker_started": bool(progress_data.get("worker_started")),
                "stop_requested": progress_data.get("stop_requested", False),
                "result_url": progress_data.get("result_url"),
            }
            ru = status.get("result_url")
            if ru:
                base = str(progress_data.get("request_base_url") or "").strip()
                if not base:
                    base = _request_base_url_from_handler(self)
                loc = str(ru)
                if loc.startswith("/"):
                    loc = base.rstrip("/") + loc
                status["result_url"] = _finalize_result_url(progress_data, loc)
            
            comparer = progress_data.get("comparer")
            if comparer and hasattr(comparer, 'processed_files'):
                status["progress"] = comparer.processed_files
                status["total"] = comparer.total_files
                status["current_file"] = getattr(comparer, 'current_file', '') or str(
                    progress_data.get("current_file") or ""
                )
                status["eta_seconds"] = getattr(comparer, 'estimated_time_remaining', 0)

                if comparer.total_files > 0:
                    status["percentage"] = round(
                        (comparer.processed_files / comparer.total_files) * 100, 1
                    )
                elif progress_data.get("running"):
                    status["percentage"] = 3.0
                    if not status["current_file"]:
                        status["current_file"] = "Starting comparison…"
            elif progress_data.get("total"):
                status["progress"] = int(progress_data.get("progress") or 0)
                status["total"] = int(progress_data.get("total") or 0)
                status["current_file"] = str(progress_data.get("current_file") or "")
                status["eta_seconds"] = int(progress_data.get("eta_seconds") or 0)
                if status["total"] > 0:
                    status["percentage"] = round((status["progress"] / status["total"]) * 100, 1)
            elif progress_data.get("running"):
                status["current_file"] = str(progress_data.get("current_file") or "Starting comparison…")
                status["percentage"] = 3.0

            eta = status["eta_seconds"]
            if eta > 0:
                if eta < 60:
                    status["eta_formatted"] = f"{eta}s"
                elif eta < 3600:
                    status["eta_formatted"] = f"{eta//60}m {eta%60}s"
                else:
                    status["eta_formatted"] = f"{eta//3600}h {(eta%3600)//60}m"
            elif status.get("running"):
                status["eta_formatted"] = "Calculating..."
            
            response = json.dumps(status).encode('utf-8')
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)
            return

        if path == "/open-report":
            qs_open = parse_qs(parsed.query)
            open_sid = qs_open.get("session_id", [""])[0]
            self._send_open_report_redirect(open_sid)
            return
        
        # Dedicated endpoint for copy operation logs
        if path == "/copy_logs":
            global last_copy_logs, last_copy_session_id
            
            response_data = {
                "session_id": last_copy_session_id or "none",
                "logs": last_copy_logs,
                "total_lines": len(last_copy_logs),
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }
            
            print(f"[DEBUG /copy_logs] Returning {len(last_copy_logs)} logs for copy session '{last_copy_session_id}'")
            
            response = json.dumps(response_data).encode('utf-8')
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)
            return

        # Debug endpoint to list all sessions (localhost only; also gated in security module)
        if path == "/debug_sessions":
            dbg_host = (self.client_address[0] or "").strip()
            if dbg_host not in ("127.0.0.1", "::1", "localhost", ""):
                self.send_error(403, "Debug sessions listing is only available on localhost")
                return
            with sessions_lock:
                all_sessions = {sid: len(logs) for sid, logs in session_logs.items()}
            response_data = {
                "message": "Debug: All sessions with log counts",
                "sessions": all_sessions,
                "total_sessions": len(all_sessions)
            }
            response = json.dumps(response_data, indent=2).encode('utf-8')
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)
            return

        if path == "/logs":
            # Return session logs as JSON
            qs = parse_qs(parsed.query)
            session_id = qs.get("session_id", [""])[0]
            last_n = qs.get("last_n", [""])[0]
            last_n = int(last_n) if last_n.isdigit() else None
            clear = qs.get("clear", [""])[0] == "1"
            client_id = request_client_identity(self, qs.get("report_user", [""])[0])
            client_host = (self.client_address[0] or "")

            if not session_id:
                response_data = {
                    "session_id": "none",
                    "logs": [],
                    "total_lines": 0,
                    "error": "session_id is required",
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
                response = json.dumps(response_data).encode('utf-8')
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)
                return

            session = get_session(session_id)
            if session and not session_owned_by_client(session, client_id, client_host):
                response_data = {
                    "session_id": session_id,
                    "logs": [],
                    "total_lines": 0,
                    "error": "Access denied for this comparison session",
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
                response = json.dumps(response_data).encode('utf-8')
                self.send_response(403)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)
                return
            
            if clear:
                clear_session_logs(session_id)
            
            logs = get_session_logs(session_id, last_n)
            
            error = session.get("error") if session else None
            
            response_data = {
                "session_id": session_id,
                "logs": logs,
                "total_lines": len(logs),
                "error": error,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }
            
            response = json.dumps(response_data).encode('utf-8')
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)
            return
        
        if path == "/copy_progress":
            # Return copy progress status as JSON
            # Note: time is imported at module level
            
            status = {
                "running": copy_progress["running"],
                "progress": copy_progress["progress"],
                "total": copy_progress["total"],
                "percentage": 0,
                "current_file": copy_progress["current_file"],
                "eta_seconds": copy_progress["eta_seconds"],
                "eta_formatted": "Calculating...",
                "error": copy_progress.get("error"),
                "message": copy_progress.get("message", ""),
                "bytes_copied": copy_progress.get("bytes_copied", 0),
                "total_bytes": copy_progress.get("total_bytes", 0),
                "skipped_count": copy_progress.get("skipped_count", 0),
                "failed_files": copy_progress.get("failed_files", [])[:10]  # Only send first 10 to UI
            }
            
            if copy_progress["total"] > 0:
                status["percentage"] = round((copy_progress["progress"] / copy_progress["total"]) * 100, 1)
            
            # Calculate ETA based on elapsed time and progress
            if copy_progress["start_time"] and copy_progress["progress"] > 0:
                elapsed = time.time() - copy_progress["start_time"]
                remaining = copy_progress["total"] - copy_progress["progress"]
                if copy_progress["progress"] > 0:
                    eta = int((elapsed / copy_progress["progress"]) * remaining)
                    status["eta_seconds"] = eta
                    if eta < 60:
                        status["eta_formatted"] = f"{eta}s"
                    elif eta < 3600:
                        status["eta_formatted"] = f"{eta//60}m {eta%60}s"
                    else:
                        status["eta_formatted"] = f"{eta//3600}h {(eta%3600)//60}m"
            
            response = json.dumps(status).encode('utf-8')
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)
            return
        
        if path == "/reports":
            qs_reports = parse_qs(parsed.query)
            report_user_q = qs_reports.get("report_user", [""])[0]
            machine = resolve_client_machine_hostname(
                (self.client_address[0] or ""),
                {k: v for k, v in self.headers.items()},
                report_user_q,
            )
            entries = list_reports(machine, limit=MAX_BROWSE_REPORTS)
            ru_param = quote(report_user_q) if report_user_q else quote(machine)
            def _fmt(p):
                try:
                    from datetime import datetime as _dt
                    return _dt.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    return ""
            def _open_name(p):
                html_p = p.with_suffix('.html')
                return html_p.name if html_p.exists() else p.name
            def _view_href(p):
                on = _open_name(p)
                frag = (
                    "#class-level-details"
                    if on.startswith("crm_full_comparison_") and on.lower().endswith(".html")
                    else ""
                )
                return f"/view?file={quote(on)}&report_user={ru_param}{frag}"
            links = "\n".join(
                f"<li><a href=\"{_view_href(p)}\" target=\"_blank\" rel=\"noopener noreferrer\">{html.escape(_open_name(p))}</a>"
                f" <small style=\"color:#666\"> — saved {_fmt(p)}</small></li>" for p in entries
            ) or "<li>No reports found.</li>"
            body = f"""
            <h2>Comparison reports</h2>
            <p style=\"color:#555;max-width:52em;\">Latest {MAX_BROWSE_REPORTS} reports on your PC (<code>\\\\{html.escape(machine)}\\c$\\crm_compare_reports</code>), named by date and run order (first_report, second_report, …). Reports stay until you delete them.</p>
            <p><a href=\"/\">← Back</a></p>
            <ul>{links}</ul>
            """
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html_page("Reports", body))
            return


        if path == "/foundations":
            body = """
            <h2>Foundations Link</h2>
            <p><a href=\"/\">← Back</a></p>
            
            <!-- Section 1: Prepare Foundation Link -->
            <div style=\"background:#f0fdf4; border:1px solid #059669; border-radius:8px; padding:20px; margin:20px 0;\">
                <h3 style=\"color:#059669; margin-top:0;\">📋 Step 1: Prepare ASCF Foundation Link</h3>
                <form method=\"post\" action=\"/foundations/prepare\">
                    <div style=\"margin-bottom:15px;\">
                        <label style=\"display:block; font-weight:600; margin-bottom:8px; color:#065f46;\">ASCF Product Version</label>
                        <select name=\"ascf_version\" id=\"ascf_version_prepare\" required style=\"width:100%; padding:10px 12px; border:1px solid #059669; border-radius:8px; background:#fff; font-size:14px; cursor:pointer;\">
                            <option value=\"\">-- Select ASCF Product Version --</option>
                            <option value=\"10.25.0.pb00\">10.25.0.pb00</option>
                            <option value=\"10.24.0.pb01\">10.24.0.pb01</option>
                            <option value=\"10.24.0.pb00\">10.24.0.pb00</option>
                            <option value=\"10.1.0.pb01\">10.1.0.pb01</option>
                            <option value=\"10.1.0.pb00\">10.1.0.pb00</option>
                            <option value=\"9.22.0.pb01\">9.22.0.pb01</option>
                            <option value=\"9.22.0.pb00\">9.22.0.pb00</option>
                            <option value=\"9.3.0.pb01\">9.3.0.pb01</option>
                            <option value=\"9.0.0.pb05\">9.0.0.pb05</option>
                            <option value=\"9.0.0.pb04\">9.0.0.pb04</option>
                            <option value=\"9.0.0.pb03\">9.0.0.pb03</option>
                            <option value=\"9.0.0.pb02\">9.0.0.pb02</option>
                            <option value=\"9.0.0.pb01\">9.0.0.pb01</option>
                            <option value=\"9.0.0.pb00\">9.0.0.pb00</option>
                            <option value=\"8.2.0.pb01\">8.2.0.pb01</option>
                            <option value=\"8.2.0.pb02\">8.2.0.pb02</option>
                            <option value=\"8.2.0.pb03\">8.2.0.pb03</option>
                        </select>
                    </div>
                    <div style=\"margin-bottom:15px;\">
                        <label style=\"display:block; font-weight:600; margin-bottom:8px; color:#065f46;\">ASCF HF Number (for this release)</label>
                        <input type=\"text\" name=\"hf_number\" id=\"hf_number_prepare\" required placeholder=\"Enter HF number (e.g., 09)\" pattern=\"[0-9]{2}\" title=\"Please enter a 2-digit HF number (e.g., 01, 09, 12)\" style=\"width:100%; padding:10px 12px; border:1px solid #059669; border-radius:8px; background:#fff; font-size:14px;\" />
                        <small style=\"color:#6b7280; display:block; margin-top:4px;\">Enter the new HF number (2 digits). The previous HF's download.html will be copied and renamed.</small>
                    </div>
                    <div style=\"margin-top:15px;\">
                        <button type=\"submit\" style=\"background:linear-gradient(90deg, #059669 0%, #10b981 100%); color:white; border:none; padding:12px 24px; border-radius:8px; font-weight:600; cursor:pointer; font-size:14px;\">Prepare Foundation Link</button>
                    </div>
                </form>
            </div>
            
            <!-- Section 2: Update Foundation Link -->
            <div style=\"background:#eff6ff; border:1px solid #3b82f6; border-radius:8px; padding:20px; margin:20px 0;\">
                <h3 style=\"color:#1d4ed8; margin-top:0;\">📝 Step 2: Update Foundation Link</h3>
                <p style=\"color:#6b7280; margin-bottom:15px;\">After preparing the foundation link, update the HF paths, month, and creation date inside the file.</p>
                <form method=\"post\" action=\"/foundations/update\">
                    <div style=\"margin-bottom:15px;\">
                        <label style=\"display:block; font-weight:600; margin-bottom:8px; color:#1e40af;\">ASCF Product Version</label>
                        <select name=\"ascf_version\" id=\"ascf_version_update\" required style=\"width:100%; padding:10px 12px; border:1px solid #3b82f6; border-radius:8px; background:#fff; font-size:14px; cursor:pointer;\">
                            <option value=\"\">-- Select ASCF Product Version --</option>
                            <option value=\"10.25.0.pb00\">10.25.0.pb00</option>
                            <option value=\"10.24.0.pb01\">10.24.0.pb01</option>
                            <option value=\"10.24.0.pb00\">10.24.0.pb00</option>
                            <option value=\"10.1.0.pb01\">10.1.0.pb01</option>
                            <option value=\"10.1.0.pb00\">10.1.0.pb00</option>
                            <option value=\"9.22.0.pb01\">9.22.0.pb01</option>
                            <option value=\"9.22.0.pb00\">9.22.0.pb00</option>
                            <option value=\"9.3.0.pb01\">9.3.0.pb01</option>
                            <option value=\"9.0.0.pb05\">9.0.0.pb05</option>
                            <option value=\"9.0.0.pb04\">9.0.0.pb04</option>
                            <option value=\"9.0.0.pb03\">9.0.0.pb03</option>
                            <option value=\"9.0.0.pb02\">9.0.0.pb02</option>
                            <option value=\"9.0.0.pb01\">9.0.0.pb01</option>
                            <option value=\"9.0.0.pb00\">9.0.0.pb00</option>
                            <option value=\"8.2.0.pb01\">8.2.0.pb01</option>
                            <option value=\"8.2.0.pb02\">8.2.0.pb02</option>
                            <option value=\"8.2.0.pb03\">8.2.0.pb03</option>
                        </select>
                    </div>
                    <div style=\"margin-bottom:15px;\">
                        <label style=\"display:block; font-weight:600; margin-bottom:8px; color:#1e40af;\">ASCF HF Number (current)</label>
                        <input type=\"text\" name=\"hf_number\" id=\"hf_number_update\" required placeholder=\"Enter current HF number (e.g., 09)\" pattern=\"[0-9]{2}\" title=\"Please enter a 2-digit HF number (e.g., 01, 09, 12)\" style=\"width:100%; padding:10px 12px; border:1px solid #3b82f6; border-radius:8px; background:#fff; font-size:14px;\" />
                        <small style=\"color:#6b7280; display:block; margin-top:4px;\">Enter the HF number of the file to update.</small>
                    </div>
                    <div style=\"margin-bottom:15px;\">
                        <label style=\"display:block; font-weight:600; margin-bottom:8px; color:#1e40af;\">Month to Update</label>
                        <select name=\"month\" id=\"month_update\" required style=\"width:100%; padding:10px 12px; border:1px solid #3b82f6; border-radius:8px; background:#fff; font-size:14px; cursor:pointer;\">
                            <option value=\"\">-- Select Month --</option>
                            <option value=\"Jan\">Jan</option>
                            <option value=\"Feb\">Feb</option>
                            <option value=\"Mar\">Mar</option>
                            <option value=\"Apr\">Apr</option>
                            <option value=\"May\">May</option>
                            <option value=\"Jun\">Jun</option>
                            <option value=\"Jul\">Jul</option>
                            <option value=\"Aug\">Aug</option>
                            <option value=\"Sep\">Sep</option>
                            <option value=\"Oct\">Oct</option>
                            <option value=\"Nov\">Nov</option>
                            <option value=\"Dec\">Dec</option>
                        </select>
                    </div>
                    <div style=\"margin-bottom:15px;\">
                        <label style=\"display:block; font-weight:600; margin-bottom:8px; color:#1e40af;\">Year</label>
                        <input type=\"text\" name=\"year\" id=\"year_update\" required placeholder=\"Enter year (e.g., 2026)\" pattern=\"[0-9]{4}\" title=\"Please enter a 4-digit year\" style=\"width:100%; padding:10px 12px; border:1px solid #3b82f6; border-radius:8px; background:#fff; font-size:14px;\" />
                    </div>
                    <div style=\"margin-bottom:15px;\">
                        <label style=\"display:block; font-weight:600; margin-bottom:8px; color:#1e40af;\">ASCFDesignerUpdateSite Creation Date</label>
                        <input type=\"text\" name=\"creation_date\" id=\"creation_date_update\" required placeholder=\"Enter date (e.g., 20260215)\" pattern=\"[0-9]{8}\" title=\"Please enter date in YYYYMMDD format\" style=\"width:100%; padding:10px 12px; border:1px solid #3b82f6; border-radius:8px; background:#fff; font-size:14px;\" />
                        <small style=\"color:#6b7280; display:block; margin-top:4px;\">Enter the creation date in YYYYMMDD format (e.g., 20260215 for Feb 15, 2026).</small>
                    </div>
                    <div style=\"margin-top:15px; display:flex; gap:10px; flex-wrap:wrap;\">
                        <button type=\"submit\" style=\"background:linear-gradient(90deg, #3b82f6 0%, #1d4ed8 100%); color:white; border:none; padding:12px 24px; border-radius:8px; font-weight:600; cursor:pointer; font-size:14px;\">Update Foundation Link</button>
                        <a href=\"/foundations/update-index\" style=\"background:linear-gradient(90deg, #f59e0b 0%, #d97706 100%); color:white; border:none; padding:12px 24px; border-radius:8px; font-weight:600; cursor:pointer; font-size:14px; text-decoration:none; display:inline-block;\">Update Index.html</a>
                        <a href=\"/foundations/update-javadoc\" style=\"background:linear-gradient(90deg, #0d9488 0%, #0f766e 100%); color:white; border:none; padding:12px 24px; border-radius:8px; font-weight:600; cursor:pointer; font-size:14px; text-decoration:none; display:inline-block;\">Update Javadoc.html</a>
                        <a href=\"/foundations/update-api\" style=\"background:linear-gradient(90deg, #8b5cf6 0%, #7c3aed 100%); color:white; border:none; padding:12px 24px; border-radius:8px; font-weight:600; cursor:pointer; font-size:14px; text-decoration:none; display:inline-block;\">Update API manually</a>
                    </div>
                </form>
            </div>
            """
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html_page("Foundations Link", body))
            return

        if path == "/foundations/prepare" and self.command == "GET":
            # Redirect GET requests to the main foundations page
            self.send_response(302)
            self.send_header("Location", "/foundations")
            self.end_headers()
            return
        
        if path == "/foundations/update-index":
            body = """
            <h2>Update Index.html</h2>
            <p><a href=\"/foundations\">← Back to Foundations</a></p>
            
            <div style=\"background:#fefce8; border:1px solid #f59e0b; border-radius:8px; padding:20px; margin:20px 0;\">
                <h3 style=\"color:#d97706; margin-top:0;\">📝 Update Index.html Automatically</h3>
                <p style=\"color:#92400e;\">This will automatically add a new hotfix entry to the index.html file in the ReleaseNotes folder.</p>
                
                <form method=\"post\" action=\"/foundations/update-index\">
                    <div style=\"margin-bottom:15px;\">
                        <label style=\"display:block; font-weight:600; margin-bottom:8px; color:#b45309;\">ASCF Product Version</label>
                        <select name=\"ascf_version\" required style=\"width:100%; padding:10px 12px; border:1px solid #f59e0b; border-radius:8px; background:#fff; font-size:14px; cursor:pointer;\">
                            <option value=\"\">-- Select ASCF Product Version --</option>
                            <option value=\"10.25.0.pb00\">10.25.0.pb00</option>
                            <option value=\"10.24.0.pb01\">10.24.0.pb01</option>
                            <option value=\"10.24.0.pb00\">10.24.0.pb00</option>
                            <option value=\"10.1.0.pb01\">10.1.0.pb01</option>
                            <option value=\"10.1.0.pb00\">10.1.0.pb00</option>
                            <option value=\"9.22.0.pb01\">9.22.0.pb01</option>
                            <option value=\"9.22.0.pb00\">9.22.0.pb00</option>
                            <option value=\"9.3.0.pb01\">9.3.0.pb01</option>
                            <option value=\"9.0.0.pb05\">9.0.0.pb05</option>
                            <option value=\"9.0.0.pb04\">9.0.0.pb04</option>
                            <option value=\"9.0.0.pb03\">9.0.0.pb03</option>
                            <option value=\"9.0.0.pb02\">9.0.0.pb02</option>
                            <option value=\"9.0.0.pb01\">9.0.0.pb01</option>
                            <option value=\"9.0.0.pb00\">9.0.0.pb00</option>
                            <option value=\"8.2.0.pb01\">8.2.0.pb01</option>
                            <option value=\"8.2.0.pb02\">8.2.0.pb02</option>
                            <option value=\"8.2.0.pb03\">8.2.0.pb03</option>
                        </select>
                    </div>
                    <div style=\"margin-bottom:15px;\">
                        <label style=\"display:block; font-weight:600; margin-bottom:8px; color:#b45309;\">New HF Number</label>
                        <input type=\"text\" name=\"hf_number\" required placeholder=\"Enter new HF number (e.g., 09)\" pattern=\"[0-9]{2}\" title=\"Please enter a 2-digit HF number\" style=\"width:100%; padding:10px 12px; border:1px solid #f59e0b; border-radius:8px; background:#fff; font-size:14px;\" />
                        <small style=\"color:#92400e; display:block; margin-top:4px;\">The previous HF entry will be found automatically and new entry added below it.</small>
                    </div>
                    <div style=\"margin-bottom:15px;\">
                        <label style=\"display:block; font-weight:600; margin-bottom:8px; color:#b45309;\">Month</label>
                        <select name=\"month\" required style=\"width:100%; padding:10px 12px; border:1px solid #f59e0b; border-radius:8px; background:#fff; font-size:14px; cursor:pointer;\">
                            <option value=\"\">-- Select Month --</option>
                            <option value=\"Jan\">Jan</option>
                            <option value=\"Feb\">Feb</option>
                            <option value=\"Mar\">Mar</option>
                            <option value=\"Apr\">Apr</option>
                            <option value=\"May\">May</option>
                            <option value=\"Jun\">Jun</option>
                            <option value=\"Jul\">Jul</option>
                            <option value=\"Aug\">Aug</option>
                            <option value=\"Sep\">Sep</option>
                            <option value=\"Oct\">Oct</option>
                            <option value=\"Nov\">Nov</option>
                            <option value=\"Dec\">Dec</option>
                        </select>
                    </div>
                    <div style=\"margin-bottom:15px;\">
                        <label style=\"display:block; font-weight:600; margin-bottom:8px; color:#b45309;\">Year</label>
                        <input type=\"text\" name=\"year\" required placeholder=\"Enter year (e.g., 2026)\" pattern=\"[0-9]{4}\" title=\"Please enter a 4-digit year\" style=\"width:100%; padding:10px 12px; border:1px solid #f59e0b; border-radius:8px; background:#fff; font-size:14px;\" />
                    </div>
                    <div style=\"margin-top:15px;\">
                        <button type=\"submit\" style=\"background:linear-gradient(90deg, #f59e0b 0%, #d97706 100%); color:white; border:none; padding:12px 24px; border-radius:8px; font-weight:600; cursor:pointer; font-size:14px;\">Update Index.html</button>
                    </div>
                </form>
            </div>
            
            <div style=\"background:#fff; border:1px solid #fcd34d; border-radius:8px; padding:15px; margin-top:15px;\">
                <h4 style=\"color:#b45309; margin-top:0;\">ℹ️ What this does:</h4>
                <ul style=\"color:#92400e; margin:10px 0; padding-left:20px;\">
                    <li>Opens index.html at <code style=\"background:#fffbeb; padding:2px 6px; border-radius:4px;\">\\\\usstlfounds01\\ascf\\userguides\\ReleaseNotes\\index.html</code></li>
                    <li>Finds the previous HF entry (e.g., 10.24.0.pb01_hf08)</li>
                    <li>Adds the new HF entry below it (e.g., 10.24.0.pb01_hf09)</li>
                </ul>
            </div>
            """
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html_page("Update Index.html", body))
            return
        
        if path == "/foundations/update-javadoc" and self.command != "POST":
            body = """
            <h2>Update Javadoc.html</h2>
            <p><a href=\"/foundations\">← Back to Foundations</a></p>
            
            <div style=\"background:#f0fdfa; border:1px solid #0d9488; border-radius:8px; padding:20px; margin:20px 0;\">
                <h3 style=\"color:#0f766e; margin-top:0;\">📝 Update Javadoc.html Automatically</h3>
                <p style=\"color:#115e59;\">This will add a new hotfix entry to the version Javadoc file (e.g. 9_Javadoc.html in Version9\\API).</p>
                
                <form method=\"post\" action=\"/foundations/update-javadoc\">
                    <div style=\"margin-bottom:15px;\">
                        <label style=\"display:block; font-weight:600; margin-bottom:8px; color:#0f766e;\">ASCF Product Version</label>
                        <select name=\"ascf_version\" required style=\"width:100%; padding:10px 12px; border:1px solid #0d9488; border-radius:8px; background:#fff; font-size:14px; cursor:pointer;\">
                            <option value=\"\">-- Select ASCF Product Version --</option>
                            <option value=\"10.25.0.pb00\">10.25.0.pb00</option>
                            <option value=\"10.24.0.pb01\">10.24.0.pb01</option>
                            <option value=\"10.24.0.pb00\">10.24.0.pb00</option>
                            <option value=\"10.1.0.pb01\">10.1.0.pb01</option>
                            <option value=\"10.1.0.pb00\">10.1.0.pb00</option>
                            <option value=\"9.22.0.pb01\">9.22.0.pb01</option>
                            <option value=\"9.22.0.pb00\">9.22.0.pb00</option>
                            <option value=\"9.3.0.pb01\">9.3.0.pb01</option>
                            <option value=\"9.0.0.pb05\">9.0.0.pb05</option>
                            <option value=\"9.0.0.pb04\">9.0.0.pb04</option>
                            <option value=\"9.0.0.pb03\">9.0.0.pb03</option>
                            <option value=\"9.0.0.pb02\">9.0.0.pb02</option>
                            <option value=\"9.0.0.pb01\">9.0.0.pb01</option>
                            <option value=\"9.0.0.pb00\">9.0.0.pb00</option>
                            <option value=\"8.2.0.pb01\">8.2.0.pb01</option>
                            <option value=\"8.2.0.pb02\">8.2.0.pb02</option>
                            <option value=\"8.2.0.pb03\">8.2.0.pb03</option>
                        </select>
                    </div>
                    <div style=\"margin-bottom:15px;\">
                        <label style=\"display:block; font-weight:600; margin-bottom:8px; color:#0f766e;\">New HF Number</label>
                        <input type=\"text\" name=\"hf_number\" required placeholder=\"Enter new HF number (e.g., 13)\" pattern=\"[0-9]{2}\" title=\"Please enter a 2-digit HF number\" style=\"width:100%; padding:10px 12px; border:1px solid #0d9488; border-radius:8px; background:#fff; font-size:14px;\" />
                        <small style=\"color:#115e59; display:block; margin-top:4px;\">The previous HF row will be found and a new row added below it.</small>
                    </div>
                    <div style=\"margin-top:15px;\">
                        <button type=\"submit\" style=\"background:linear-gradient(90deg, #0d9488 0%, #0f766e 100%); color:white; border:none; padding:12px 24px; border-radius:8px; font-weight:600; cursor:pointer; font-size:14px;\">Update Javadoc.html</button>
                    </div>
                </form>
            </div>
            
            <div style=\"background:#fff; border:1px solid #5eead4; border-radius:8px; padding:15px; margin-top:15px;\">
                <h4 style=\"color:#0f766e; margin-top:0;\">ℹ️ What this does:</h4>
                <ul style=\"color:#115e59; margin:10px 0; padding-left:20px;\">
                    <li>Opens the Javadoc file at <code style=\"background:#ccfbf1; padding:2px 6px; border-radius:4px;\">\\\\usstlfounds01\\ascf\\userguides\\VersionX\\API\\X_Javadoc.html</code> (Version8, Version9, Version10, Version10_1, or Version{major})</li>
                    <li>Finds the previous HF entry (e.g., 9.22.0.pb01_hf12)</li>
                    <li>Adds a new &lt;tr&gt; entry below it (e.g., 9.22.0.pb01_hf13) with the same structure</li>
                </ul>
            </div>
            """
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html_page("Update Javadoc.html", body))
            return
        
        if path == "/foundations/update-api":
            body = """
            <h2>Update API Manually</h2>
            <p><a href=\"/foundations\">← Back to Foundations</a></p>
            
            <div style=\"background:#f5f3ff; border:1px solid #8b5cf6; border-radius:8px; padding:20px; margin:20px 0;\">
                <h3 style=\"color:#7c3aed; margin-top:0;\">🔧 Manual API Update Instructions</h3>
                <p style=\"color:#6b21a8;\">Follow these steps to manually update the API:</p>
                
                <ol style=\"color:#6b21a8; margin:15px 0; padding-left:25px;\">
                    <li style=\"margin-bottom:10px;\">Open the Foundation Admin site:<br>
                        <a href=\"http://foundations/asplib/backOffice.asp\" target=\"_blank\" style=\"background:#ede9fe; padding:4px 8px; border-radius:4px; display:inline-block; margin-top:4px;\">http://foundations/asplib/backOffice.asp</a>
                    </li>
                    <li style=\"margin-bottom:10px;\">Click on <strong>Administrative Tools</strong></li>
                    <li style=\"margin-bottom:10px;\">Select the <strong>ASCF Product Version</strong> from the dropdown</li>
                    <li style=\"margin-bottom:10px;\">Click <strong>Add New Release</strong></li>
                    <li style=\"margin-bottom:10px;\">Fill in the required fields:
                        <ul style=\"margin-top:8px;\">
                            <li>New Hotfix Version (e.g., 10.24.0.pb01_hf09)</li>
                            <li>Release Date (e.g., 2026-02-17)</li>
                            <li>Download URL (e.g., 10240pb01hf09_download.html)</li>
                            <li>Release Notes URL (e.g., ReleaseNotes/relnotes10_24_0_pb01_hf09.pdf)</li>
                        </ul>
                    </li>
                    <li style=\"margin-bottom:10px;\">Click <strong>Add Hotfix</strong> to save</li>
                </ol>
                
                <div style=\"background:#fff; border:1px solid #c4b5fd; border-radius:8px; padding:15px; margin-top:15px;\">
                    <h4 style=\"color:#7c3aed; margin-top:0;\">💡 Quick Links:</h4>
                    <ul style=\"margin:10px 0; padding-left:20px;\">
                        <li><a href=\"http://foundations\" target=\"_blank\">Foundation Home</a></li>
                        <li><a href=\"http://foundations/asplib/backOffice.asp\" target=\"_blank\">Administrative Tools</a></li>
                        <li><a href=\"http://foundations/asplib/addVersion.asp?Product=ASCF\" target=\"_blank\">Add Version Page (ASCF)</a></li>
                    </ul>
                </div>
            </div>
            
            <div style=\"margin-top:20px; display:flex; gap:10px; flex-wrap:wrap;\">
                <a href=\"http://foundations/asplib/backOffice.asp\" target=\"_blank\" style=\"background:linear-gradient(90deg, #8b5cf6 0%, #7c3aed 100%); color:white; border:none; padding:12px 24px; border-radius:8px; font-weight:600; cursor:pointer; font-size:14px; text-decoration:none; display:inline-block;\">Open Admin Tools</a>
                <a href=\"http://foundations/asplib/addVersion.asp?Product=ASCF\" target=\"_blank\" style=\"background:linear-gradient(90deg, #06b6d4 0%, #0891b2 100%); color:white; border:none; padding:12px 24px; border-radius:8px; font-weight:600; cursor:pointer; font-size:14px; text-decoration:none; display:inline-block;\">Open Add Version Page</a>
            </div>
            """
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html_page("Update API Manually", body))
            return
        
        if path == "/admin-tool":
            body = """
            <h2>Administrative Tool</h2>
            <p><a href=\"/\">← Back</a></p>
            
            <div style=\"background:#f5f3ff; border:1px solid #7c3aed; border-radius:8px; padding:20px; margin:20px 0;\">
                <h3 style=\"color:#7c3aed; margin-top:0;\">🔧 Add New Hotfix Release</h3>
                <p style=\"color:#6b7280; margin-bottom:15px;\">This tool will automatically navigate to the Foundation site and add a new hotfix release.</p>
                
                <form method=\"post\" action=\"/admin-tool/submit\">
                    <div style=\"margin-bottom:15px;\">
                        <label style=\"display:block; font-weight:600; margin-bottom:8px; color:#5b21b6;\">ASCF Product Version</label>
                        <select name=\"ascf_version\" id=\"admin_ascf_version\" required style=\"width:100%; padding:10px 12px; border:1px solid #7c3aed; border-radius:8px; background:#fff; font-size:14px; cursor:pointer;\">
                            <option value=\"\">-- Select ASCF Product Version --</option>
                            <option value=\"10.25.0.pb00\">10.25.0.pb00</option>
                            <option value=\"10.24.0.pb01\">10.24.0.pb01</option>
                            <option value=\"10.24.0.pb00\">10.24.0.pb00</option>
                            <option value=\"10.1.0.pb01\">10.1.0.pb01</option>
                            <option value=\"10.1.0.pb00\">10.1.0.pb00</option>
                            <option value=\"9.22.0.pb01\">9.22.0.pb01</option>
                            <option value=\"9.22.0.pb00\">9.22.0.pb00</option>
                            <option value=\"9.3.0.pb01\">9.3.0.pb01</option>
                            <option value=\"9.0.0.pb05\">9.0.0.pb05</option>
                            <option value=\"9.0.0.pb04\">9.0.0.pb04</option>
                            <option value=\"9.0.0.pb03\">9.0.0.pb03</option>
                            <option value=\"9.0.0.pb02\">9.0.0.pb02</option>
                            <option value=\"9.0.0.pb01\">9.0.0.pb01</option>
                            <option value=\"9.0.0.pb00\">9.0.0.pb00</option>
                            <option value=\"8.2.0.pb01\">8.2.0.pb01</option>
                            <option value=\"8.2.0.pb02\">8.2.0.pb02</option>
                            <option value=\"8.2.0.pb03\">8.2.0.pb03</option>
                        </select>
                    </div>
                    
                    <div style=\"margin-bottom:15px;\">
                        <label style=\"display:block; font-weight:600; margin-bottom:8px; color:#5b21b6;\">New Hotfix Version</label>
                        <input type=\"text\" name=\"hotfix_version\" required placeholder=\"Enter hotfix version (e.g., 10.24.0.pb01_hf09)\" style=\"width:100%; padding:10px 12px; border:1px solid #7c3aed; border-radius:8px; background:#fff; font-size:14px;\" />
                        <small style=\"color:#6b7280; display:block; margin-top:4px;\">Example: 10.24.0.pb01_hf09</small>
                    </div>
                    
                    <div style=\"margin-bottom:15px;\">
                        <label style=\"display:block; font-weight:600; margin-bottom:8px; color:#5b21b6;\">Release Date</label>
                        <input type=\"text\" name=\"release_date\" required placeholder=\"Enter release date (e.g., 2026-02-17)\" style=\"width:100%; padding:10px 12px; border:1px solid #7c3aed; border-radius:8px; background:#fff; font-size:14px;\" />
                        <small style=\"color:#6b7280; display:block; margin-top:4px;\">Example: 2026-02-17</small>
                    </div>
                    
                    <div style=\"margin-bottom:15px;\">
                        <label style=\"display:block; font-weight:600; margin-bottom:8px; color:#5b21b6;\">Download URL</label>
                        <input type=\"text\" name=\"download_url\" required placeholder=\"Enter download URL (e.g., 10240pb01hf09_download.html)\" style=\"width:100%; padding:10px 12px; border:1px solid #7c3aed; border-radius:8px; background:#fff; font-size:14px;\" />
                        <small style=\"color:#6b7280; display:block; margin-top:4px;\">Example: 10240pb01hf09_download.html</small>
                    </div>
                    
                    <div style=\"margin-bottom:15px;\">
                        <label style=\"display:block; font-weight:600; margin-bottom:8px; color:#5b21b6;\">Release Notes URL</label>
                        <input type=\"text\" name=\"release_notes_url\" required placeholder=\"Enter release notes URL (e.g., ReleaseNotes/relnotes10_24_0_pb01_hf09.pdf)\" style=\"width:100%; padding:10px 12px; border:1px solid #7c3aed; border-radius:8px; background:#fff; font-size:14px;\" />
                        <small style=\"color:#6b7280; display:block; margin-top:4px;\">Example: ReleaseNotes/relnotes10_24_0_pb01_hf09.pdf</small>
                    </div>
                    
                    <div style=\"margin-top:20px;\">
                        <button type=\"submit\" style=\"background:linear-gradient(90deg, #7c3aed 0%, #6d28d9 100%); color:white; border:none; padding:12px 24px; border-radius:8px; font-weight:600; cursor:pointer; font-size:14px;\">Submit & Launch Automation</button>
                    </div>
                </form>
            </div>
            
            <div style=\"background:#fefce8; border:1px solid #f59e0b; border-radius:8px; padding:15px; margin:20px 0;\">
                <h4 style=\"color:#d97706; margin-top:0;\">⚠️ Important Notes</h4>
                <ul style=\"color:#92400e; margin:0; padding-left:20px;\">
                    <li>This tool will automatically navigate to the Foundation site and fill in the form</li>
                    <li>Browser automation uses Selenium WebDriver with Chrome</li>
                    <li>The browser window will remain open for you to verify the data</li>
                </ul>
            </div>
            """
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html_page("Administrative Tool", body))
            return

        if path == "/view":
            qs = parse_qs(parsed.query)
            name = qs.get("file", [""])[0]
            name = os.path.basename(unquote(name))
            machine = resolve_client_machine_hostname(
                (self.client_address[0] or ""),
                {k: v for k, v in self.headers.items()},
                qs.get("report_user", [""])[0],
            )
            candidate = resolve_report_path(name, machine, PROJECT_ROOT)
            if not candidate or not candidate.is_file():
                self.send_error(404)
                return
            if not is_allowed_report_name(name):
                self.send_error(404)
                return
            try:
                data = candidate.read_bytes()
                print(f"[DEBUG] Serving file: {name}, size: {len(data)} bytes, type: {'HTML' if name.lower().endswith('.html') else 'TEXT'}")
            except Exception as e:
                print(f"[DEBUG] Error reading file {name}: {e}")
                self.send_error(500)
                return
            self.send_response(200)
            ctype = "text/html; charset=utf-8" if name.lower().endswith(".html") else "text/plain; charset=utf-8"
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            if name.lower().endswith(".html"):
                self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
                self.send_header("Pragma", "no-cache")
            self.end_headers()
            self.wfile.write(data)
            return

        if path == "/download":
            qs = parse_qs(parsed.query)
            name = qs.get("file", [""])[0]
            name = os.path.basename(unquote(name))
            machine = resolve_client_machine_hostname(
                (self.client_address[0] or ""),
                {k: v for k, v in self.headers.items()},
                qs.get("report_user", [""])[0],
            )
            candidate = resolve_report_path(name, machine, PROJECT_ROOT)
            if not candidate or not candidate.is_file():
                self.send_error(404)
                return
            if not is_allowed_report_name(name):
                self.send_error(404)
                return
            try:
                data = candidate.read_bytes()
            except Exception:
                self.send_error(500)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Disposition", f"attachment; filename={name}")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        if path == "/run-crm-comparison":
            # Pre-configured CRM SDK JAR comparison
            body_content = """
            <h2>Running CRM SDK JAR Comparison</h2>
            <p><a href="/">← Back to home</a></p>
            <p>Comparing HFB22 vs HFB21 with normalization enabled...</p>
            <div style="background:#f7f7f9; padding:16px; border:1px solid #eee; border-radius:10px; margin:16px 0;">
            """
            
            try:
                python_exe = sys.executable
                script_path = PROJECT_ROOT / "compare_crm_jars.py"
                
                if not script_path.exists():
                    body_content += "<p style='color:#ef4444'>Error: compare_crm_jars.py not found</p>"
                else:
                    result = subprocess.run(
                        [python_exe, str(script_path)],
                        cwd=str(PROJECT_ROOT),
                        capture_output=True,
                        text=True,
                        timeout=60
                    )
                    
                    output = result.stdout if result.stdout else result.stderr
                    body_content += f"<pre style='white-space: pre-wrap; word-wrap: break-word;'>{html.escape(output)}</pre>"
                    
                    if result.returncode == 0:
                        body_content += "<p style='color:#10b981; font-weight:bold;'>✓ Comparison completed successfully!</p>"
                        # Link to the report
                        body_content += "<p><a class='button' href='/reports'>View Reports</a></p>"
                    else:
                        body_content += f"<p style='color:#ef4444'>Warning: Process exited with code {result.returncode}</p>"
                        
            except subprocess.TimeoutExpired:
                body_content += "<p style='color:#ef4444'>Error: Comparison timed out after 60 seconds</p>"
            except Exception as e:
                body_content += f"<p style='color:#ef4444'>Error: {html.escape(str(e))}</p>"
            
            body_content += "</div>"
            
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html_page("CRM Comparison", body_content))
            return

        if path == "/run-demo-tests":
            # Run normalization tests
            body_content = """
            <h2>Running Normalization Tests</h2>
            <p><a href="/">← Back to home</a></p>
            <div style="background:#f7f7f9; padding:16px; border:1px solid #eee; border-radius:10px; margin:16px 0;">
            """
            
            demos = [
                ("test_normalization.py", "Normalization Tests"),
                ("demo_nested_normalization.py", "Nested Path Demo"),
            ]
            
            python_exe = sys.executable
            
            for script, name in demos:
                body_content += f"<h3 style='margin:16px 0 8px 0; color:#0d6efd;'>{html.escape(name)}</h3>"
                script_path = PROJECT_ROOT / script
                
                if not script_path.exists():
                    body_content += f"<p style='color:#ef4444'>Script not found: {script}</p>"
                    continue
                
                try:
                    result = subprocess.run(
                        [python_exe, str(script_path)],
                        cwd=str(PROJECT_ROOT),
                        capture_output=True,
                        text=True,
                        timeout=30
                    )
                    
                    output = result.stdout if result.stdout else result.stderr
                    body_content += f"<pre style='white-space: pre-wrap; word-wrap: break-word; font-size:12px;'>{html.escape(output)}</pre>"
                    
                    if result.returncode == 0:
                        body_content += "<p style='color:#10b981; font-weight:bold;'>✓ Test passed</p>"
                    else:
                        body_content += f"<p style='color:#ef4444'>Test exited with code {result.returncode}</p>"
                        
                except subprocess.TimeoutExpired:
                    body_content += "<p style='color:#ef4444'>Test timed out after 30 seconds</p>"
                except Exception as e:
                    body_content += f"<p style='color:#ef4444'>Error: {html.escape(str(e))}</p>"
            
            body_content += "</div>"
            
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html_page("Demo Tests", body_content))
            return

        if path == "/run":
            global last_folder1_path, last_folder2_path
            qs = parse_qs(parsed.query)
            folder1 = normalize_build_path_for_windows(qs.get("folder1_path", [""])[0].strip())
            folder2 = normalize_build_path_for_windows(qs.get("folder2_path", [""])[0].strip())
            if folder1:
                last_folder1_path = folder1
            if folder2:
                last_folder2_path = folder2
            mode = qs.get("mode", ["full"])[0]
            if mode == "naming" or "naming_convention" in qs:
                mode = "naming"
            
            # Read application selections
            app_ascf = "app_ascf" in qs
            app_crm = "app_crm" in qs
            app_uxf = "app_uxf" in qs
            app_dex = "app_dex" in qs
            app_wef = "app_wef" in qs
            selected_apps = []
            if app_ascf:
                selected_apps.append("ASCF")
            if app_crm:
                selected_apps.append("CRM")
            if app_uxf:
                selected_apps.append("UXF/OMNI")
            if app_dex:
                selected_apps.append("DEX")
            if app_wef:
                selected_apps.append("WEF")
            
            # These options are always enabled (removed from UI but always active)
            show_all_differences = True
            ignore_timestamp_only = True
            suppress_warnings = True
            jar_name = qs.get("jar_name", [""])[0].strip()
            fast_crm_full = "fast_crm_full" in qs
            crm_class_diff_jar = qs.get("crm_class_diff_jar", [""])[0].strip()
            crm_manifest_diff_jar = qs.get("crm_manifest_diff_jar", [""])[0].strip()
            crm_config_files_filter = qs.get("crm_config_files_filter", [""])[0].strip()
            crm_explicit_size_paths = qs.get("crm_explicit_size_paths", [""])[0].strip()
            _clfy_vals = qs.get("compare_clfy_agent_zip", ["0"])
            compare_clfy_agent_zip = str(_clfy_vals[-1]).strip().lower() in ("1", "on", "true", "yes")
            # ASCF compare options (from UI when ASCF is selected)
            use_local_copy = "use_local_copy" in qs
            keep_temp_folders = "keep_temp_folders" in qs
            exclude_missing_folder2 = "exclude_missing_folder2" in qs
            rename_folder2_only = "rename_folder2_only" in qs
            rename_suffix = (qs.get("rename_suffix", ["_renamed"])[0] or "_renamed").strip() or "_renamed"
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
            crm_playbooks_nexus_previous = qs.get("crm_playbooks_nexus_previous", [""])[0].strip()
            crm_playbooks_nexus_latest = qs.get("crm_playbooks_nexus_latest", [""])[0].strip()
            crm_playbooks_nexus_pair = bool(crm_playbooks_nexus_previous and crm_playbooks_nexus_latest)
            crm_playbooks_nexus_mode = app_crm and crm_playbooks_nexus_pair

            if (
                not dex_nexus_mode
                and not crm_playbooks_nexus_mode
                and not app_wef
                and (not folder1 or not folder2)
            ):
                body = (
                    "<p style=\"color:#900\">Folder 1 and 2 are required (or select DEX Nexus versions, "
                    "CRM crm-playbooks Nexus previous/latest versions, or WEF SSH paths).</p>"
                    + index_html().decode("utf-8")
                )
                self.send_response(400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html_page("Invalid Input", body))
                return

            # Validate paths exist before starting comparison
            # Skip validation for network/UNC paths (\\server\share) as they may need special handling
            from pathlib import Path as PathCheck
            p1_check = PathCheck(folder1)
            p2_check = PathCheck(folder2)
            
            # Helper to check if path is a network/UNC path
            def is_network_path(p):
                path_str = str(p)
                return path_str.startswith('\\\\') or path_str.startswith('//')
            
            path_errors = []
            # Only validate local paths - network paths are handled by the comparison code
            if not dex_nexus_mode and not crm_playbooks_nexus_mode and not app_wef:
                if not is_network_path(p1_check) and not _path_exists(p1_check):
                    path_errors.append(f"<li>Old Build Path does not exist or is not accessible: <code>{html.escape(folder1)}</code></li>")
                if not is_network_path(p2_check) and not _path_exists(p2_check):
                    path_errors.append(f"<li>New Build Path does not exist or is not accessible: <code>{html.escape(folder2)}</code></li>")
            
            if path_errors:
                error_html = f"""
                <div style="background:#fee2e2;border:2px solid #fca5a5;border-radius:12px;padding:20px;margin:20px 0;">
                    <h2 style="color:#dc2626;margin:0 0 15px 0;">❌ Path Validation Failed</h2>
                    <ul style="color:#991b1b;margin:0;padding-left:20px;">
                        {''.join(path_errors)}
                    </ul>
                </div>
                <div style="background:#fef3c7;border:2px solid #fcd34d;border-radius:12px;padding:20px;margin:20px 0;">
                    <h3 style="color:#92400e;margin:0 0 10px 0;">💡 Troubleshooting Tips</h3>
                    <ul style="color:#92400e;margin:0;padding-left:20px;">
                        <li>Verify the paths are correctly typed</li>
                        <li>For network paths, use the format: <code>\\\\server\\share\\folder</code></li>
                        <li>Check if you have read access to the folders</li>
                        <li>Ensure network drives are connected or VPN is active</li>
                    </ul>
                </div>
                <p><a href="/" style="display:inline-block;padding:10px 20px;background:#3b82f6;color:white;text-decoration:none;border-radius:8px;">← Back to Main Page</a></p>
                """
                body = error_html
                self.send_response(400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html_page("Path Validation Error", body))
                return

            ctx = parse_run_comparison_qs(qs)
            if not ctx.selected_apps:
                body = """<div style="background:#fee2e2;border:2px solid #fca5a5;border-radius:12px;padding:20px;margin:20px 0;">
                    <h2 style="color:#dc2626;margin:0 0 15px 0;">❌ No Application Selected</h2>
                    <p style="color:#991b1b;">Please select at least one application (ASCF, CRM/AMSS, UXF/OMNI, DEX, or WEF) to compare.</p>
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

            session_id, session_progress = _prepare_comparison_session(self, qs)
            if session_progress.get("running"):
                self._send_comparison_wait_page(session_id)
                return
            with sessions_lock:
                session_progress["worker_started"] = True
                session_progress["result_url"] = None
                session_progress.pop("error_html", None)
                session_progress["error"] = None
                session_progress["running"] = True
                qs_snap = {k: list(v) for k, v in qs.items()}
                qs_snap["session_id"] = [session_id]
                if session_progress.get("client_identity"):
                    qs_snap["report_user"] = [session_progress["client_identity"]]
                handler_impl = AppHandler.__new__(AppHandler)
                threading.Thread(
                    target=AppHandler._run_comparison_thread,
                    args=(handler_impl, qs_snap, session_id),
                    daemon=True,
                    name=f"comparison-{session_id}",
                ).start()
            self._send_comparison_wait_page(session_id)
            return


        # Not found
        self.send_error(404)

    def do_POST(self):  # noqa: N802
        parsed = urlparse(self.path)
        if not self._enforce_security("POST", parsed):
            return

        if parsed.path == "/api/windows-folder-picker":
            import os

            addr = (self.client_address[0] or "").lower()
            if addr not in ("127.0.0.1", "::1") and not addr.endswith("127.0.0.1"):
                self.send_error(403)
                return
            if os.name != "nt":
                msg = json.dumps({"ok": False, "error": "Folder picker requires Windows."}).encode("utf-8")
                self.send_response(501)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(msg)))
                self.end_headers()
                self.wfile.write(msg)
                return
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8") if length else "{}"
            try:
                data = json.loads(raw) if raw.strip() else {}
            except json.JSONDecodeError:
                data = {}
            initial_dir = normalize_build_path_for_windows((data.get("initial_dir") or "").strip())
            try:
                from windows_folder_picker import pick_folder

                path = pick_folder(initial_dir if initial_dir else None)
            except RuntimeError as e:
                err = json.dumps({"ok": False, "error": str(e)}).encode("utf-8")
                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(err)))
                self.end_headers()
                self.wfile.write(err)
                return
            if path is None:
                resp = json.dumps({"ok": False, "cancelled": True}).encode("utf-8")
            else:
                resp = json.dumps({"ok": True, "path": path}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)
            return

        if parsed.path == "/api/crm-find-jar-for-class":
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length).decode("utf-8") if length else "{}"
            try:
                data = json.loads(raw) if raw.strip() else {}
            except json.JSONDecodeError:
                data = {}
            class_name = str(
                data.get("class_name") or data.get("class") or ""
            ).strip()
            try:
                ssh_port = int(data.get("port") or data.get("ssh_port") or 22)
            except (ValueError, TypeError):
                ssh_port = 22
            try:
                status, payload = _crm_find_jar_for_class_api(
                    class_name,
                    ssh_host=str(data.get("host") or data.get("ssh_host") or "").strip(),
                    ssh_username=str(
                        data.get("username") or data.get("ssh_username") or ""
                    ).strip(),
                    ssh_password=str(
                        data.get("password") or data.get("ssh_password") or ""
                    ),
                    ssh_port=ssh_port,
                    ssh_remote_path=str(
                        data.get("path")
                        or data.get("remote_path")
                        or data.get("ssh_remote_path")
                        or ""
                    ).strip(),
                    folder2=str(
                        data.get("folder2_path") or data.get("build_path") or ""
                    ).strip(),
                )
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                err = json.dumps(
                    {"ok": False, "error": str(e), "jars": []}
                ).encode("utf-8")
                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(err)))
                self.end_headers()
                self.wfile.write(err)
            return

        if parsed.path == "/api/validate-environment-jars":
            from datetime import datetime
            from environment_jar_validate import (
                render_environment_validation_html,
                run_validation_from_request,
            )

            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8") if length else "{}"
            try:
                data = json.loads(raw) if raw.strip() else {}
            except json.JSONDecodeError:
                data = {}
            try:
                result = run_validation_from_request(data)
                env_client = resolve_client_identity(
                    (self.client_address[0] or ""),
                    {k: v for k, v in self.headers.items()},
                    str(data.get("report_user") or ""),
                )
                env_machine = resolve_client_machine_hostname(
                    (self.client_address[0] or ""),
                    {k: v for k, v in self.headers.items()},
                    str(data.get("report_user") or ""),
                )
                env_report_dir = resolve_report_save_dir(env_machine, PROJECT_ROOT)
                report_name = allocate_report_filename(
                    "environment_jar_validation",
                    env_client,
                    env_report_dir,
                    suffix=".html",
                )
                report_path = env_report_dir / report_name
                report_path.write_text(
                    render_environment_validation_html(result), encoding="utf-8"
                )
                result["report_url"] = f"/view?file={quote(report_name)}"
                body = json.dumps(result).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            except ValueError as e:
                err = json.dumps({"ok": False, "error": str(e)}).encode("utf-8")
                self.send_response(400)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(err)))
                self.end_headers()
                self.wfile.write(err)
            except Exception as e:
                err = json.dumps({"ok": False, "error": str(e)}).encode("utf-8")
                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(err)))
                self.end_headers()
                self.wfile.write(err)
            return

        if parsed.path == "/api/open-path-in-explorer":
            import os

            addr = (self.client_address[0] or "").lower()
            if addr not in ("127.0.0.1", "::1") and not addr.endswith("127.0.0.1"):
                self.send_error(403)
                return
            if os.name != "nt":
                msg = json.dumps({"ok": False, "error": "Windows only.", "fallback": "copy"}).encode("utf-8")
                self.send_response(501)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(msg)))
                self.end_headers()
                self.wfile.write(msg)
                return
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8") if length else "{}"
            try:
                data = json.loads(raw) if raw.strip() else {}
            except json.JSONDecodeError:
                data = {}
            path = (data.get("path") or "").strip()
            try:
                from windows_folder_picker import open_folder_in_explorer

                open_folder_in_explorer(path)
            except ValueError as e:
                err = json.dumps({"ok": False, "error": str(e), "fallback": "copy"}).encode("utf-8")
                self.send_response(400)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(err)))
                self.end_headers()
                self.wfile.write(err)
                return
            except RuntimeError as e:
                err = json.dumps({"ok": False, "error": str(e), "fallback": "copy"}).encode("utf-8")
                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(err)))
                self.end_headers()
                self.wfile.write(err)
                return
            ok = json.dumps({"ok": True}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(ok)))
            self.end_headers()
            self.wfile.write(ok)
            return

        # Global declaration for copy logs (needed by both /copy_async and /copy handlers)
        global last_copy_logs, last_copy_session_id


        if parsed.path == "/foundations/prepare":
            import shutil
            
            length = int(self.headers.get('Content-Length', '0'))
            body = self.rfile.read(length).decode('utf-8') if length > 0 else ''
            pairs = [p for p in body.split('&') if p]
            form = {}
            for p in pairs:
                if '=' in p:
                    k, v = p.split('=', 1)
                    form[unquote(k)] = unquote(v.replace('+', ' '))
            
            ascf_version = form.get('ascf_version', '').strip()
            hf_number = form.get('hf_number', '').strip()
            
            # Validate inputs
            if not ascf_version or not hf_number:
                body_content = """
                <h2>Foundations Link - Error</h2>
                <p><a href=\"/foundations\">← Back</a></p>
                <div style=\"background:#fef2f2; border:1px solid #dc2626; border-radius:8px; padding:20px; margin:20px 0;\">
                    <h3 style=\"color:#dc2626; margin-top:0;\">❌ Missing Input</h3>
                    <p>Please provide both ASCF Product Version and HF Number.</p>
                </div>
                """
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html_page("Foundations Link - Error", body_content))
                return
            
            # Convert version format: 10.24.0.pb01 -> 10240pb01
            version_code = ascf_version.replace('.', '')
            
            # Calculate previous HF number
            try:
                hf_num_int = int(hf_number)
                if hf_num_int < 1:
                    raise ValueError("HF number must be at least 01")
                prev_hf_num = f"{hf_num_int - 1:02d}"
            except ValueError as e:
                body_content = f"""
                <h2>Foundations Link - Error</h2>
                <p><a href=\"/foundations\">← Back</a></p>
                <div style=\"background:#fef2f2; border:1px solid #dc2626; border-radius:8px; padding:20px; margin:20px 0;\">
                    <h3 style=\"color:#dc2626; margin-top:0;\">❌ Invalid HF Number</h3>
                    <p>Please enter a valid 2-digit HF number (e.g., 01, 09, 12).</p>
                    <p>Error: {html.escape(str(e))}</p>
                </div>
                """
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html_page("Foundations Link - Error", body_content))
                return
            
            # Build file paths
            base_path = r"\\usstlfounds01\ascf\downloads"
            source_filename = f"{version_code}hf{prev_hf_num}_download.html"
            dest_filename = f"{version_code}hf{hf_number}_download.html"
            source_path = os.path.join(base_path, source_filename)
            dest_path = os.path.join(base_path, dest_filename)
            
            print(f"[Foundations] Preparing foundation link:")
            print(f"[Foundations] Version: {ascf_version} -> {version_code}")
            print(f"[Foundations] HF Number: {hf_number} (Previous: {prev_hf_num})")
            print(f"[Foundations] Source: {source_path}")
            print(f"[Foundations] Destination: {dest_path}")
            
            try:
                # Check if source file exists
                if not os.path.exists(source_path):
                    body_content = f"""
                    <h2>Foundations Link - Error</h2>
                    <p><a href=\"/foundations\">← Back</a></p>
                    <div style=\"background:#fef2f2; border:1px solid #dc2626; border-radius:8px; padding:20px; margin:20px 0;\">
                        <h3 style=\"color:#dc2626; margin-top:0;\">❌ Source File Not Found</h3>
                        <p>Could not find the previous HF download file:</p>
                        <p style=\"font-family:monospace; background:#fee2e2; padding:10px; border-radius:4px; word-break:break-all;\">{html.escape(source_path)}</p>
                        <p>Please verify that the previous HF (hf{prev_hf_num}) exists for this version.</p>
                    </div>
                    """
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(html_page("Foundations Link - Error", body_content))
                    return
                
                # Check if destination already exists
                if os.path.exists(dest_path):
                    body_content = f"""
                    <h2>Foundations Link - Warning</h2>
                    <p><a href=\"/foundations\">← Back</a></p>
                    <div style=\"background:#fefce8; border:1px solid #f59e0b; border-radius:8px; padding:20px; margin:20px 0;\">
                        <h3 style=\"color:#d97706; margin-top:0;\">⚠️ File Already Exists</h3>
                        <p>The destination file already exists:</p>
                        <p style=\"font-family:monospace; background:#fef3c7; padding:10px; border-radius:4px; word-break:break-all;\">{html.escape(dest_path)}</p>
                        <p>No action was taken. Delete the existing file first if you want to recreate it.</p>
                    </div>
                    """
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(html_page("Foundations Link - Warning", body_content))
                    return
                
                # Copy the file
                shutil.copy2(source_path, dest_path)
                print(f"[Foundations] Successfully copied file to: {dest_path}")
                
                body_content = f"""
                <h2>Foundations Link - Success</h2>
                <p><a href=\"/foundations\">← Back</a></p>
                <div style=\"background:#f0fdf4; border:1px solid #059669; border-radius:8px; padding:20px; margin:20px 0;\">
                    <h3 style=\"color:#059669; margin-top:0;\">✅ Foundation Link Prepared Successfully</h3>
                    <table style=\"width:100%; border-collapse:collapse; margin:15px 0;\">
                        <tr style=\"border-bottom:1px solid #d1fae5;\">
                            <td style=\"padding:8px 0; font-weight:600; color:#065f46;\">ASCF Version:</td>
                            <td style=\"padding:8px 0;\">{html.escape(ascf_version)}</td>
                        </tr>
                        <tr style=\"border-bottom:1px solid #d1fae5;\">
                            <td style=\"padding:8px 0; font-weight:600; color:#065f46;\">HF Number:</td>
                            <td style=\"padding:8px 0;\">{html.escape(hf_number)}</td>
                        </tr>
                        <tr style=\"border-bottom:1px solid #d1fae5;\">
                            <td style=\"padding:8px 0; font-weight:600; color:#065f46;\">Source File:</td>
                            <td style=\"padding:8px 0; font-family:monospace; font-size:12px; word-break:break-all;\">{html.escape(source_filename)}</td>
                        </tr>
                        <tr>
                            <td style=\"padding:8px 0; font-weight:600; color:#065f46;\">Created File:</td>
                            <td style=\"padding:8px 0; font-family:monospace; font-size:12px; word-break:break-all;\">{html.escape(dest_filename)}</td>
                        </tr>
                    </table>
                    <div style=\"background:#dcfce7; padding:12px; border-radius:6px; margin-top:15px;\">
                        <p style=\"margin:0; font-size:13px;\"><strong>Full Path:</strong></p>
                        <p style=\"margin:5px 0 0 0; font-family:monospace; font-size:12px; word-break:break-all;\">{html.escape(dest_path)}</p>
                    </div>
                </div>
                """
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html_page("Foundations Link - Success", body_content))
                return
                
            except PermissionError as e:
                print(f"[Foundations] Permission error: {e}")
                body_content = f"""
                <h2>Foundations Link - Error</h2>
                <p><a href=\"/foundations\">← Back</a></p>
                <div style=\"background:#fef2f2; border:1px solid #dc2626; border-radius:8px; padding:20px; margin:20px 0;\">
                    <h3 style=\"color:#dc2626; margin-top:0;\">❌ Permission Denied</h3>
                    <p>Could not create the file due to permission restrictions:</p>
                    <p style=\"font-family:monospace; background:#fee2e2; padding:10px; border-radius:4px;\">{html.escape(str(e))}</p>
                    <p>Please check that you have write access to the destination folder.</p>
                </div>
                """
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html_page("Foundations Link - Error", body_content))
                return
                
            except Exception as e:
                print(f"[Foundations] Error: {e}")
                body_content = f"""
                <h2>Foundations Link - Error</h2>
                <p><a href=\"/foundations\">← Back</a></p>
                <div style=\"background:#fef2f2; border:1px solid #dc2626; border-radius:8px; padding:20px; margin:20px 0;\">
                    <h3 style=\"color:#dc2626; margin-top:0;\">❌ Error Creating File</h3>
                    <p>An error occurred while creating the foundation link file:</p>
                    <p style=\"font-family:monospace; background:#fee2e2; padding:10px; border-radius:4px;\">{html.escape(str(e))}</p>
                </div>
                """
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html_page("Foundations Link - Error", body_content))
                return
        
        # Handle Update Index.html automation
        if parsed.path == "/foundations/update-index" and self.command == "POST":
            import re
            
            length = int(self.headers.get('Content-Length', '0'))
            body = self.rfile.read(length).decode('utf-8') if length > 0 else ''
            pairs = [p for p in body.split('&') if p]
            form = {}
            for p in pairs:
                if '=' in p:
                    k, v = p.split('=', 1)
                    form[unquote(k)] = unquote(v.replace('+', ' '))
            
            ascf_version = form.get('ascf_version', '').strip()
            hf_number = form.get('hf_number', '').strip()
            month = form.get('month', '').strip()
            year = form.get('year', '').strip()
            
            # Validate inputs
            if not ascf_version or not hf_number or not month or not year:
                body_content = """
                <h2>Update Index.html - Error</h2>
                <p><a href=\"/foundations/update-index\">← Back</a></p>
                <div style=\"background:#fef2f2; border:1px solid #dc2626; border-radius:8px; padding:20px; margin:20px 0;\">
                    <h3 style=\"color:#dc2626; margin-top:0;\">❌ Missing Input</h3>
                    <p>Please provide all required fields: ASCF Product Version, HF Number, Month, and Year.</p>
                </div>
                """
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html_page("Update Index.html - Error", body_content))
                return
            
            # Calculate previous HF number
            try:
                hf_num_int = int(hf_number)
                if hf_num_int < 1:
                    raise ValueError("HF number must be at least 01")
                prev_hf_num = f"{hf_num_int - 1:02d}"
            except ValueError as e:
                body_content = f"""
                <h2>Update Index.html - Error</h2>
                <p><a href=\"/foundations/update-index\">← Back</a></p>
                <div style=\"background:#fef2f2; border:1px solid #dc2626; border-radius:8px; padding:20px; margin:20px 0;\">
                    <h3 style=\"color:#dc2626; margin-top:0;\">❌ Invalid HF Number</h3>
                    <p>Please enter a valid 2-digit HF number (e.g., 01, 09, 12).</p>
                    <p>Error: {html.escape(str(e))}</p>
                </div>
                """
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html_page("Update Index.html - Error", body_content))
                return
            
            # Build file path - ReleaseNotes index.html
            base_path = r"\\usstlfounds01\ascf\userguides\ReleaseNotes"
            index_file = os.path.join(base_path, "index.html")
            
            # Convert version format for file naming: 10.24.0.pb01 -> 10240pb01
            version_code = ascf_version.replace('.', '')
            
            # Previous and new version strings
            prev_version_str = f"{ascf_version}_hf{prev_hf_num}"
            new_version_str = f"{ascf_version}_hf{hf_number}"
            
            # Previous and new download file names
            prev_download = f"relnotes{version_code.replace('pb', '_pb')}_hf{prev_hf_num}.pdf"
            new_download = f"relnotes{version_code.replace('pb', '_pb')}_hf{hf_number}.pdf"
            
            print(f"[Update Index] Updating index.html:")
            print(f"[Update Index] Version: {ascf_version}")
            print(f"[Update Index] Previous HF: {prev_hf_num} -> New HF: {hf_number}")
            print(f"[Update Index] Month/Year: {month} {year}")
            print(f"[Update Index] File: {index_file}")
            print(f"[Update Index] Looking for: {prev_version_str}")
            print(f"[Update Index] Adding: {new_version_str}")
            
            try:
                # Check if file exists
                if not os.path.exists(index_file):
                    body_content = f"""
                    <h2>Update Index.html - Error</h2>
                    <p><a href=\"/foundations/update-index\">← Back</a></p>
                    <div style=\"background:#fef2f2; border:1px solid #dc2626; border-radius:8px; padding:20px; margin:20px 0;\">
                        <h3 style=\"color:#dc2626; margin-top:0;\">❌ File Not Found</h3>
                        <p>Could not find the index.html file:</p>
                        <p style=\"font-family:monospace; background:#fee2e2; padding:10px; border-radius:4px; word-break:break-all;\">{html.escape(index_file)}</p>
                    </div>
                    """
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(html_page("Update Index.html - Error", body_content))
                    return
                
                # Read the file content
                with open(index_file, 'r', encoding='utf-8') as f:
                    content = f.read()
                
                original_content = content
                
                # Check if new entry already exists
                if new_version_str in content:
                    body_content = f"""
                    <h2>Update Index.html - Info</h2>
                    <p><a href=\"/foundations/update-index\">← Back</a></p>
                    <div style=\"background:#eff6ff; border:1px solid #3b82f6; border-radius:8px; padding:20px; margin:20px 0;\">
                        <h3 style=\"color:#1d4ed8; margin-top:0;\">ℹ️ Entry Already Exists</h3>
                        <p>The entry for <strong>{html.escape(new_version_str)}</strong> already exists in the index.html file.</p>
                        <p>No changes were made.</p>
                    </div>
                    """
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(html_page("Update Index.html - Info", body_content))
                    return
                
                # Find the previous HF entry and add new entry after it
                # Look for a table row containing the previous version
                # Pattern: <tr>..._hfXX...</tr> where XX is prev_hf_num
                
                # Try to find the previous entry in various formats
                prev_patterns = [
                    prev_version_str,
                    f"{ascf_version}_hf{prev_hf_num}",
                    f"{version_code}hf{prev_hf_num}",
                    f"hf{prev_hf_num}",
                ]
                
                found_prev = False
                for prev_pattern in prev_patterns:
                    if prev_pattern in content:
                        found_prev = True
                        print(f"[Update Index] Found previous entry pattern: {prev_pattern}")
                        break
                
                if not found_prev:
                    body_content = f"""
                    <h2>Update Index.html - Error</h2>
                    <p><a href=\"/foundations/update-index\">← Back</a></p>
                    <div style=\"background:#fef2f2; border:1px solid #dc2626; border-radius:8px; padding:20px; margin:20px 0;\">
                        <h3 style=\"color:#dc2626; margin-top:0;\">❌ Previous Entry Not Found</h3>
                        <p>Could not find the previous HF entry in the index.html file:</p>
                        <p style=\"font-family:monospace; background:#fee2e2; padding:10px; border-radius:4px;\">{html.escape(prev_version_str)}</p>
                        <p>Please check if the previous hotfix ({prev_hf_num}) entry exists in the file.</p>
                    </div>
                    """
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(html_page("Update Index.html - Error", body_content))
                    return
                
                # Build new <li> entry from user inputs (month, year, new HF, ASCF version)
                # Format: <li><a href="relnotes9_22_0_pb01_hf13.pdf">ASCF hotfix 9.22.0.pb01_hf13 Release Notes</a><br><em class="cDate">(Mar 2026)</em><br /></li>
                relnotes_version = ascf_version.replace('.', '_')
                prev_relnotes_ref = f"relnotes{relnotes_version}_hf{prev_hf_num}.pdf"
                new_li = (
                    f'<li><a href="relnotes{relnotes_version}_hf{hf_number}.pdf">'
                    f'ASCF hotfix {ascf_version}_hf{hf_number} Release Notes</a><br>'
                    f'<em class="cDate">({month} {year})</em><br /></li>'
                )
                
                # 1) Prefer <li> list format: find previous <li>...</li> and add single new entry below it
                pos = content.find(prev_relnotes_ref)
                if pos == -1:
                    pos = content.find(prev_version_str)
                if pos != -1:
                    start_li = content.rfind("<li>", 0, pos)
                    end_li = content.find("</li>", pos)
                    if start_li != -1 and end_li != -1:
                        end_li += 5  # include '</li>'
                        content = content[:end_li] + "\n" + new_li + content[end_li:]
                        print(f"[Update Index] Added new <li> entry after previous HF list item")
                
                # 2) Fallback: find table row (<tr>...</tr>) or line-by-line only when <li> did not apply
                if content == original_content:
                    tr_pattern = re.compile(
                        r'(<tr[^>]*>.*?' + re.escape(prev_version_str) + r'.*?</tr>)',
                        re.IGNORECASE | re.DOTALL
                    )
                    match = tr_pattern.search(content)
                    if match:
                        old_row = match.group(1)
                        new_row = old_row.replace(prev_version_str, new_version_str)
                        new_row = new_row.replace(f"_hf{prev_hf_num}", f"_hf{hf_number}")
                        new_row = new_row.replace(f"hf{prev_hf_num}", f"hf{hf_number}")
                        month_year_pattern = re.compile(r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{4}')
                        new_row = month_year_pattern.sub(f"{month} {year}", new_row)
                        content = content.replace(old_row, old_row + "\n" + new_row)
                        print(f"[Update Index] Added new row after existing row")
                    else:
                        # Fallback: try line-by-line (single entry only)
                        lines = content.split('\n')
                        new_lines = []
                        inserted = False
                        for line in lines:
                            new_lines.append(line)
                            if prev_version_str in line and not inserted:
                                new_line = line.replace(prev_version_str, new_version_str)
                                new_line = new_line.replace(f"_hf{prev_hf_num}", f"_hf{hf_number}")
                                new_line = new_line.replace(f"hf{prev_hf_num}", f"hf{hf_number}")
                                month_year_pattern = re.compile(r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{4}')
                                new_line = month_year_pattern.sub(f"{month} {year}", new_line)
                                new_lines.append(new_line)
                                inserted = True
                                print(f"[Update Index] Added new line after: {line[:50]}...")
                        if inserted:
                            content = '\n'.join(new_lines)
                
                # Check if any changes were made
                if content == original_content:
                    body_content = f"""
                    <h2>Update Index.html - Warning</h2>
                    <p><a href=\"/foundations/update-index\">← Back</a></p>
                    <div style=\"background:#fefce8; border:1px solid #f59e0b; border-radius:8px; padding:20px; margin:20px 0;\">
                        <h3 style=\"color:#d97706; margin-top:0;\">⚠️ No Changes Made</h3>
                        <p>Could not automatically add the new entry. The file structure may be different than expected.</p>
                        <p>Please update the index.html file manually.</p>
                    </div>
                    """
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(html_page("Update Index.html - Warning", body_content))
                    return
                
                # Write updated content back to file
                with open(index_file, 'w', encoding='utf-8') as f:
                    f.write(content)
                
                print(f"[Update Index] Successfully updated index.html")
                
                body_content = f"""
                <h2>Update Index.html - Success</h2>
                <p><a href=\"/foundations/update-index\">← Back</a></p>
                <div style=\"background:#f0fdf4; border:1px solid #059669; border-radius:8px; padding:20px; margin:20px 0;\">
                    <h3 style=\"color:#059669; margin-top:0;\">✅ Index.html Updated Successfully</h3>
                    <p>Added new entry for <strong>{html.escape(new_version_str)}</strong> to the index.html file.</p>
                    <table style=\"width:100%; border-collapse:collapse; margin:15px 0;\">
                        <tr style=\"border-bottom:1px solid #d1fae5;\">
                            <td style=\"padding:8px 0; font-weight:600; color:#065f46; width:180px;\">ASCF Version:</td>
                            <td style=\"padding:8px 0;\">{html.escape(ascf_version)}</td>
                        </tr>
                        <tr style=\"border-bottom:1px solid #d1fae5;\">
                            <td style=\"padding:8px 0; font-weight:600; color:#065f46;\">New HF:</td>
                            <td style=\"padding:8px 0;\">hf{html.escape(hf_number)}</td>
                        </tr>
                        <tr style=\"border-bottom:1px solid #d1fae5;\">
                            <td style=\"padding:8px 0; font-weight:600; color:#065f46;\">Previous HF:</td>
                            <td style=\"padding:8px 0;\">hf{html.escape(prev_hf_num)}</td>
                        </tr>
                        <tr style=\"border-bottom:1px solid #d1fae5;\">
                            <td style=\"padding:8px 0; font-weight:600; color:#065f46;\">Month/Year:</td>
                            <td style=\"padding:8px 0;\">{html.escape(month)} {html.escape(year)}</td>
                        </tr>
                        <tr>
                            <td style=\"padding:8px 0; font-weight:600; color:#065f46;\">File:</td>
                            <td style=\"padding:8px 0; font-family:monospace; font-size:12px; word-break:break-all;\">{html.escape(index_file)}</td>
                        </tr>
                    </table>
                </div>
                <p><a href=\"/foundations\">← Back to Foundations Link</a></p>
                """
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html_page("Update Index.html - Success", body_content))
                return
                
            except PermissionError:
                body_content = f"""
                <h2>Update Index.html - Error</h2>
                <p><a href=\"/foundations/update-index\">← Back</a></p>
                <div style=\"background:#fef2f2; border:1px solid #dc2626; border-radius:8px; padding:20px; margin:20px 0;\">
                    <h3 style=\"color:#dc2626; margin-top:0;\">❌ Permission Denied</h3>
                    <p>Cannot write to the index.html file. Please check your permissions.</p>
                    <p style=\"font-family:monospace; background:#fee2e2; padding:10px; border-radius:4px; word-break:break-all;\">{html.escape(index_file)}</p>
                </div>
                """
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html_page("Update Index.html - Error", body_content))
                return
                
            except Exception as e:
                print(f"[Update Index] Error: {e}")
                body_content = f"""
                <h2>Update Index.html - Error</h2>
                <p><a href=\"/foundations/update-index\">← Back</a></p>
                <div style=\"background:#fef2f2; border:1px solid #dc2626; border-radius:8px; padding:20px; margin:20px 0;\">
                    <h3 style=\"color:#dc2626; margin-top:0;\">❌ Error</h3>
                    <p>An error occurred while updating the index.html file:</p>
                    <p style=\"font-family:monospace; background:#fee2e2; padding:10px; border-radius:4px;\">{html.escape(str(e))}</p>
                </div>
                """
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html_page("Update Index.html - Error", body_content))
                return
        
        # Handle Update Javadoc.html POST
        if parsed.path == "/foundations/update-javadoc" and self.command == "POST":
            import re
            length = int(self.headers.get('Content-Length', '0'))
            body = self.rfile.read(length).decode('utf-8') if length > 0 else ''
            pairs = [p for p in body.split('&') if p]
            form = {}
            for p in pairs:
                if '=' in p:
                    k, v = p.split('=', 1)
                    form[unquote(k)] = unquote(v.replace('+', ' '))
            ascf_version = form.get('ascf_version', '').strip()
            hf_number = form.get('hf_number', '').strip()
            if not ascf_version or not hf_number:
                body_content = """
                <h2>Update Javadoc.html - Error</h2>
                <p><a href=\"/foundations/update-javadoc\">← Back</a></p>
                <div style=\"background:#fef2f2; border:1px solid #dc2626; border-radius:8px; padding:20px; margin:20px 0;\">
                    <h3 style=\"color:#dc2626; margin-top:0;\">❌ Missing Input</h3>
                    <p>Please provide ASCF Product Version and HF Number.</p>
                </div>
                """
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html_page("Update Javadoc.html - Error", body_content))
                return
            try:
                hf_num_int = int(hf_number)
                if hf_num_int < 1:
                    raise ValueError("HF number must be at least 01")
                prev_hf_num = f"{hf_num_int - 1:02d}"
            except ValueError as e:
                body_content = f"""
                <h2>Update Javadoc.html - Error</h2>
                <p><a href=\"/foundations/update-javadoc\">← Back</a></p>
                <div style=\"background:#fef2f2; border:1px solid #dc2626; border-radius:8px; padding:20px; margin:20px 0;\">
                    <h3 style=\"color:#dc2626; margin-top:0;\">❌ Invalid HF Number</h3>
                    <p>{html.escape(str(e))}</p>
                </div>
                """
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html_page("Update Javadoc.html - Error", body_content))
                return
            # Map ASCF version to userguides folder: Version9, Version10, Version10_24, Version10_1, Version8, or Version{major}
            base_path = r"\\usstlfounds01\ascf\userguides"
            version_mapping = [
                ("10.24.", "Version10_24", "1024"),
                ("10.1.", "Version10_1", "10_1"),
                ("10.", "Version10", "10"),
                ("9.", "Version9", "9"),
                ("8.", "Version8", "8"),
            ]
            version_folder, javadoc_prefix = None, None
            for prefix_ver, folder_name, file_prefix in version_mapping:
                if ascf_version.startswith(prefix_ver):
                    version_folder, javadoc_prefix = folder_name, file_prefix
                    break
            if version_folder is None:
                major = ascf_version.split(".")[0]
                version_folder, javadoc_prefix = f"Version{major}", major
            javadoc_dir = os.path.join(base_path, version_folder, "API")
            javadoc_file = os.path.join(javadoc_dir, f"{javadoc_prefix}_Javadoc.html")
            version_code = ascf_version.replace(".", "")
            prev_version_str = f"{ascf_version}_hf{prev_hf_num}"
            new_version_str = f"{ascf_version}_hf{hf_number}"
            try:
                if not os.path.exists(javadoc_file):
                    body_content = f"""
                    <h2>Update Javadoc.html - Error</h2>
                    <p><a href=\"/foundations/update-javadoc\">← Back</a></p>
                    <div style=\"background:#fef2f2; border:1px solid #dc2626; border-radius:8px; padding:20px; margin:20px 0;\">
                        <h3 style=\"color:#dc2626; margin-top:0;\">❌ File Not Found</h3>
                        <p>Could not find: {html.escape(javadoc_file)}</p>
                    </div>
                    """
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(html_page("Update Javadoc.html - Error", body_content))
                    return
                with open(javadoc_file, 'r', encoding='utf-8') as f:
                    content = f.read()
                original_content = content
                if new_version_str in content:
                    body_content = f"""
                    <h2>Update Javadoc.html - Info</h2>
                    <p><a href=\"/foundations/update-javadoc\">← Back</a></p>
                    <div style=\"background:#eff6ff; border:1px solid #3b82f6; border-radius:8px; padding:20px; margin:20px 0;\">
                        <h3 style=\"color:#1d4ed8; margin-top:0;\">ℹ️ Entry Already Exists</h3>
                        <p>The entry for <strong>{html.escape(new_version_str)}</strong> already exists. No changes were made.</p>
                    </div>
                    """
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(html_page("Update Javadoc.html - Info", body_content))
                    return
                if prev_version_str not in content:
                    body_content = f"""
                    <h2>Update Javadoc.html - Error</h2>
                    <p><a href=\"/foundations/update-javadoc\">← Back</a></p>
                    <div style=\"background:#fef2f2; border:1px solid #dc2626; border-radius:8px; padding:20px; margin:20px 0;\">
                        <h3 style=\"color:#dc2626; margin-top:0;\">❌ Previous Entry Not Found</h3>
                        <p>Could not find <strong>{html.escape(prev_version_str)}</strong> in the Javadoc file.</p>
                    </div>
                    """
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(html_page("Update Javadoc.html - Error", body_content))
                    return
                tr_pattern = re.compile(
                    r'(<tr[^>]*>.*?' + re.escape(prev_version_str) + r'.*?</tr>)',
                    re.IGNORECASE | re.DOTALL
                )
                match = tr_pattern.search(content)
                if match:
                    old_row = match.group(1)
                    patches_match = re.search(r'patches_([^/"]+)/', old_row)
                    patches_part = patches_match.group(1).strip() if patches_match else None
                    if not patches_part or patches_part.upper() == 'XXX':
                        parts = ascf_version.split('.')
                        if len(parts) >= 3:
                            try:
                                patch_num = int(parts[2]) + 1
                                patches_part = f'{parts[0]}{parts[1]}{patch_num}'
                            except ValueError:
                                patches_part = version_code
                        else:
                            patches_part = version_code
                    new_row = (
                        '<tr align="left">\n'
                        '    <td><a name="wp998526"> </a><div class="pCellBody"></div></td>\n'
                        '    <td valign="top"><ul><ul><ul class="pSmartList3"><div class="pCellHeadingLite">\n'
                        '\t<li><a href="file://corp.amdocs.com/shares/St%20Louis/STLFAS2A%20Shares/nt2/AUIF_Website/patches_'
                        f'{patches_part}/ASCF_HF_{version_code}hf{hf_number}/Javadoc/index.html" target="_blank">'
                        f'{ascf_version}_hf{hf_number} </li>\n'
                        '</tr>'
                    )
                    content = content.replace(old_row, old_row + "\n" + new_row)
                    with open(javadoc_file, 'w', encoding='utf-8') as f:
                        f.write(content)
                    body_content = f"""
                    <h2>Update Javadoc.html - Success</h2>
                    <p><a href=\"/foundations/update-javadoc\">← Back</a></p>
                    <div style=\"background:#f0fdfa; border:1px solid #0d9488; border-radius:8px; padding:20px; margin:20px 0;\">
                        <h3 style=\"color:#0f766e; margin-top:0;\">✅ Javadoc.html Updated Successfully</h3>
                        <p>Added new entry for <strong>{html.escape(new_version_str)}</strong>.</p>
                        <table style=\"width:100%; border-collapse:collapse; margin:15px 0;\">
                            <tr style=\"border-bottom:1px solid #99f6e4;\"><td style=\"padding:8px 0; font-weight:600; color:#065f46; width:180px;\">ASCF Version:</td><td>{html.escape(ascf_version)}</td></tr>
                            <tr style=\"border-bottom:1px solid #99f6e4;\"><td style=\"padding:8px 0; font-weight:600; color:#065f46;\">New HF:</td><td>hf{html.escape(hf_number)}</td></tr>
                            <tr style=\"border-bottom:1px solid #99f6e4;\"><td style=\"padding:8px 0; font-weight:600; color:#065f46;\">Previous HF:</td><td>hf{html.escape(prev_hf_num)}</td></tr>
                            <tr><td style=\"padding:8px 0; font-weight:600; color:#065f46;\">File:</td><td style=\"font-family:monospace; font-size:12px; word-break:break-all;\">{html.escape(javadoc_file)}</td></tr>
                        </table>
                    </div>
                    <p><a href=\"/foundations\">← Back to Foundations Link</a></p>
                    """
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(html_page("Update Javadoc.html - Success", body_content))
                    return
                body_content = f"""
                <h2>Update Javadoc.html - Error</h2>
                <p><a href=\"/foundations/update-javadoc\">← Back</a></p>
                <div style=\"background:#fef2f2; border:1px solid #dc2626; border-radius:8px; padding:20px; margin:20px 0;\">
                    <h3 style=\"color:#dc2626; margin-top:0;\">❌ Row Not Found</h3>
                    <p>Could not find a table row containing <strong>{html.escape(prev_version_str)}</strong>.</p>
                </div>
                """
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html_page("Update Javadoc.html - Error", body_content))
            except PermissionError:
                body_content = f"""
                <h2>Update Javadoc.html - Error</h2>
                <p><a href=\"/foundations/update-javadoc\">← Back</a></p>
                <div style=\"background:#fef2f2; border:1px solid #dc2626; border-radius:8px; padding:20px; margin:20px 0;\">
                    <h3 style=\"color:#dc2626; margin-top:0;\">❌ Permission Denied</h3>
                    <p>Cannot write to the Javadoc file.</p>
                </div>
                """
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html_page("Update Javadoc.html - Error", body_content))
            except Exception as e:
                body_content = f"""
                <h2>Update Javadoc.html - Error</h2>
                <p><a href=\"/foundations/update-javadoc\">← Back</a></p>
                <div style=\"background:#fef2f2; border:1px solid #dc2626; border-radius:8px; padding:20px; margin:20px 0;\">
                    <h3 style=\"color:#dc2626; margin-top:0;\">❌ Error</h3>
                    <p>{html.escape(str(e))}</p>
                </div>
                """
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html_page("Update Javadoc.html - Error", body_content))
            return
        
        # Handle Foundations Link update (modify content inside the file)
        if parsed.path == "/foundations/update":
            import re
            
            length = int(self.headers.get('Content-Length', '0'))
            body = self.rfile.read(length).decode('utf-8') if length > 0 else ''
            pairs = [p for p in body.split('&') if p]
            form = {}
            for p in pairs:
                if '=' in p:
                    k, v = p.split('=', 1)
                    form[unquote(k)] = unquote(v.replace('+', ' '))
            
            ascf_version = form.get('ascf_version', '').strip()
            hf_number = form.get('hf_number', '').strip()
            month = form.get('month', '').strip()
            year = form.get('year', '').strip()
            creation_date = form.get('creation_date', '').strip()
            
            # Validate inputs
            if not ascf_version or not hf_number or not month or not year or not creation_date:
                body_content = """
                <h2>Foundations Link - Error</h2>
                <p><a href=\"/foundations\">← Back</a></p>
                <div style=\"background:#fef2f2; border:1px solid #dc2626; border-radius:8px; padding:20px; margin:20px 0;\">
                    <h3 style=\"color:#dc2626; margin-top:0;\">❌ Missing Input</h3>
                    <p>Please provide all required fields: ASCF Product Version, HF Number, Month, Year, and Creation Date.</p>
                </div>
                """
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html_page("Foundations Link - Error", body_content))
                return
            
            # Convert version format: 10.24.0.pb01 -> 10240pb01
            version_code = ascf_version.replace('.', '')
            
            # Calculate previous HF number
            try:
                hf_num_int = int(hf_number)
                if hf_num_int < 1:
                    raise ValueError("HF number must be at least 01")
                prev_hf_num = f"{hf_num_int - 1:02d}"
            except ValueError as e:
                body_content = f"""
                <h2>Foundations Link - Error</h2>
                <p><a href=\"/foundations\">← Back</a></p>
                <div style=\"background:#fef2f2; border:1px solid #dc2626; border-radius:8px; padding:20px; margin:20px 0;\">
                    <h3 style=\"color:#dc2626; margin-top:0;\">❌ Invalid HF Number</h3>
                    <p>Please enter a valid 2-digit HF number (e.g., 01, 09, 12).</p>
                    <p>Error: {html.escape(str(e))}</p>
                </div>
                """
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html_page("Foundations Link - Error", body_content))
                return
            
            # Build file path
            base_path = r"\\usstlfounds01\ascf\downloads"
            filename = f"{version_code}hf{hf_number}_download.html"
            file_path = os.path.join(base_path, filename)
            
            print(f"[Foundations Update] Updating foundation link:")
            print(f"[Foundations Update] Version: {ascf_version} -> {version_code}")
            print(f"[Foundations Update] HF Number: {hf_number} (Previous: {prev_hf_num})")
            print(f"[Foundations Update] Month/Year: {month} {year}")
            print(f"[Foundations Update] Creation Date: {creation_date}")
            print(f"[Foundations Update] File: {file_path}")
            
            try:
                # Check if file exists
                if not os.path.exists(file_path):
                    body_content = f"""
                    <h2>Foundations Link - Error</h2>
                    <p><a href=\"/foundations\">← Back</a></p>
                    <div style=\"background:#fef2f2; border:1px solid #dc2626; border-radius:8px; padding:20px; margin:20px 0;\">
                        <h3 style=\"color:#dc2626; margin-top:0;\">❌ File Not Found</h3>
                        <p>Could not find the download file to update:</p>
                        <p style=\"font-family:monospace; background:#fee2e2; padding:10px; border-radius:4px; word-break:break-all;\">{html.escape(file_path)}</p>
                        <p>Please run \"Prepare Foundation Link\" first to create the file.</p>
                    </div>
                    """
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(html_page("Foundations Link - Error", body_content))
                    return
                
                # Read the file content
                with open(file_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                
                original_content = content
                updates_made = []
                
                # 1. Update HF number in paths - multiple patterns
                # Pattern 1: ASCF_HF_10240pb01hfXX -> ASCF_HF_10240pb01hf{hf_number}
                new_hf_pattern = f"ASCF_HF_{version_code}hf{hf_number}"
                hf_path_regex = re.compile(r"ASCF_HF_" + re.escape(version_code) + r"hf\d{2}")
                hf_path_matches = hf_path_regex.findall(content)
                if hf_path_matches:
                    for old_match in set(hf_path_matches):
                        if old_match != new_hf_pattern:
                            content = content.replace(old_match, new_hf_pattern)
                            updates_made.append(f"HF path: {old_match} → {new_hf_pattern}")
                
                # Pattern 2: 10240pb01hfXX (without ASCF_HF_ prefix) -> 10240pb01hf{hf_number}
                # But NOT when preceded by ASCF_HF_ (already handled above)
                new_simple_hf = f"{version_code}hf{hf_number}"
                simple_hf_regex = re.compile(r"(?<!ASCF_HF_)" + re.escape(version_code) + r"hf\d{2}")
                simple_hf_matches = simple_hf_regex.findall(content)
                if simple_hf_matches:
                    for old_match in set(simple_hf_matches):
                        if old_match != new_simple_hf:
                            content = content.replace(old_match, new_simple_hf)
                            updates_made.append(f"Simple HF: {old_match} → {new_simple_hf}")
                
                # Pattern 3: 10.24.0.pb01_hfXX or 10.24.0.pb01_hf_XX (with dots and underscores)
                new_dotted_hf = f"{ascf_version}_hf{hf_number}"
                dotted_hf_regex = re.compile(re.escape(ascf_version) + r"_hf_?\d{2}")
                dotted_hf_matches = dotted_hf_regex.findall(content)
                if dotted_hf_matches:
                    for old_match in set(dotted_hf_matches):
                        if old_match != new_dotted_hf:
                            content = content.replace(old_match, new_dotted_hf)
                            updates_made.append(f"Dotted HF: {old_match} → {new_dotted_hf}")
                
                # Pattern 4: _hfXX standalone (at word boundary)
                new_hf_suffix = f"_hf{hf_number}"
                hf_suffix_regex = re.compile(r"_hf\d{2}(?=_|\.|/|\\|\"|\'|\s|$)")
                hf_suffix_matches = hf_suffix_regex.findall(content)
                if hf_suffix_matches:
                    for old_match in set(hf_suffix_matches):
                        if old_match != new_hf_suffix:
                            # Use word boundary replacement to avoid partial matches
                            content = re.sub(re.escape(old_match) + r"(?=_|\.|/|\\|\"|\'|\s|$)", new_hf_suffix, content)
                            updates_made.append(f"HF suffix: {old_match} → {new_hf_suffix}")
                
                # 2. Update month/year in pCellHeadingSpec: (Feb 2026) -> (Mar 2026)
                # Pattern to match: (Mon YYYY) format - handles various spacing and formats
                new_month_str = f'<div class="pCellHeadingSpec">({month} {year})'
                # Match pattern with optional whitespace
                month_regex = re.compile(r'<div class="pCellHeadingSpec">\s*\(([A-Za-z]{3,9}\s+\d{4})\)')
                month_matches = month_regex.findall(content)
                if month_matches:
                    for old_month in set(month_matches):
                        if old_month.strip() != f"{month} {year}":
                            # Replace with regex to handle whitespace variations
                            old_pattern = re.compile(r'<div class="pCellHeadingSpec">\s*\(' + re.escape(old_month) + r'\)')
                            if old_pattern.search(content):
                                content = old_pattern.sub(new_month_str, content)
                                updates_made.append(f"Month: ({old_month}) → ({month} {year})")
                
                # 3. Update ASCFDesignerUpdateSite creation date (with or without .zip extension)
                # Pattern: ASCFDesignerUpdateSite_X.XX.X.pbXX_hfXX_YYYYMMDD or pbXXhfXX (no underscore) and .zip
                # Use flexible regex to match pb01_hf12 or pb01hf12, any date
                new_designer_base = f"ASCFDesignerUpdateSite_{ascf_version}_hf{hf_number}_{creation_date}"
                
                # Match ASCFDesignerUpdateSite with any version, optional _ between pbXX and hfXX, any date
                designer_regex = re.compile(r"ASCFDesignerUpdateSite_[\d.]+\.pb\d{2}_?hf\d{2}_\d{8}(?=\.zip|[^0-9]|$)")
                designer_matches = designer_regex.findall(content)
                if designer_matches:
                    for old_match in set(designer_matches):
                        if old_match != new_designer_base:
                            content = content.replace(old_match, new_designer_base)
                            updates_made.append(f"Designer: {old_match} → {new_designer_base}")
                
                # Also handle the .zip version explicitly (match both pb01_hf12 and pb01hf12)
                designer_zip_regex = re.compile(r"ASCFDesignerUpdateSite_[\d.]+\.pb\d{2}_?hf\d{2}_\d{8}\.zip")
                designer_zip_matches = designer_zip_regex.findall(content)
                new_designer_zip = f"{new_designer_base}.zip"
                if designer_zip_matches:
                    for old_match in set(designer_zip_matches):
                        if old_match != new_designer_zip:
                            content = content.replace(old_match, new_designer_zip)
                            updates_made.append(f"Designer zip: {old_match} → {new_designer_zip}")
                
                # Check if any updates were made
                if content == original_content:
                    body_content = f"""
                    <h2>Foundations Link - Info</h2>
                    <p><a href=\"/foundations\">← Back</a></p>
                    <div style=\"background:#eff6ff; border:1px solid #3b82f6; border-radius:8px; padding:20px; margin:20px 0;\">
                        <h3 style=\"color:#1d4ed8; margin-top:0;\">ℹ️ No Changes Required</h3>
                        <p>The file already contains the values you specified, or no matching patterns were found:</p>
                        <table style=\"width:100%; border-collapse:collapse; margin:15px 0;\">
                            <tr style=\"border-bottom:1px solid #bfdbfe;\">
                                <td style=\"padding:8px 0; font-weight:600; color:#1e40af; width:180px;\">HF Path:</td>
                                <td style=\"padding:8px 0; font-family:monospace; font-size:12px;\">ASCF_HF_{version_code}hf{hf_number}</td>
                            </tr>
                            <tr style=\"border-bottom:1px solid #bfdbfe;\">
                                <td style=\"padding:8px 0; font-weight:600; color:#1e40af;\">Month/Year:</td>
                                <td style=\"padding:8px 0;\">({month} {year})</td>
                            </tr>
                            <tr style=\"border-bottom:1px solid #bfdbfe;\">
                                <td style=\"padding:8px 0; font-weight:600; color:#1e40af;\">Designer Date:</td>
                                <td style=\"padding:8px 0; font-family:monospace; font-size:12px;\">ASCFDesignerUpdateSite_{ascf_version}_hf{hf_number}_{creation_date}</td>
                            </tr>
                            <tr>
                                <td style=\"padding:8px 0; font-weight:600; color:#1e40af;\">Designer Zip:</td>
                                <td style=\"padding:8px 0; font-family:monospace; font-size:12px;\">ASCFDesignerUpdateSite_{ascf_version}_hf{hf_number}_{creation_date}.zip</td>
                            </tr>
                        </table>
                        <p><strong>File:</strong> <span style=\"font-family:monospace;\">{html.escape(filename)}</span></p>
                        <p style=\"color:#6b7280; margin-top:15px;\">If you want to update to different values, enter the new values in the form and submit again.</p>
                        <p style=\"color:#6b7280; font-size:12px;\"><strong>Note:</strong> The system searches for patterns like:<br>
                        - <code>ASCF_HF_XXXXpbXXhfNN</code><br>
                        - <code>&lt;div class=\"pCellHeadingSpec\"&gt;(Mon YYYY)</code><br>
                        - <code>ASCFDesignerUpdateSite_X.XX.X.pbXX_hfNN_YYYYMMDD</code> or <code>.zip</code></p>
                    </div>
                    """
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(html_page("Foundations Link - Info", body_content))
                    return
                
                # Write updated content back to file
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(content)
                
                print(f"[Foundations Update] Successfully updated file with {len(updates_made)} changes")
                
                updates_html = "".join(f"<li style=\"margin:5px 0; font-family:monospace; font-size:12px;\">{html.escape(u)}</li>" for u in updates_made)
                
                body_content = f"""
                <h2>Foundations Link - Update Success</h2>
                <p><a href=\"/foundations\">← Back</a></p>
                <div style=\"background:#f0fdf4; border:1px solid #059669; border-radius:8px; padding:20px; margin:20px 0;\">
                    <h3 style=\"color:#059669; margin-top:0;\">✅ Foundation Link Updated Successfully</h3>
                    <table style=\"width:100%; border-collapse:collapse; margin:15px 0;\">
                        <tr style=\"border-bottom:1px solid #d1fae5;\">
                            <td style=\"padding:8px 0; font-weight:600; color:#065f46; width:180px;\">ASCF Version:</td>
                            <td style=\"padding:8px 0;\">{html.escape(ascf_version)}</td>
                        </tr>
                        <tr style=\"border-bottom:1px solid #d1fae5;\">
                            <td style=\"padding:8px 0; font-weight:600; color:#065f46;\">HF Number:</td>
                            <td style=\"padding:8px 0;\">{html.escape(hf_number)}</td>
                        </tr>
                        <tr style=\"border-bottom:1px solid #d1fae5;\">
                            <td style=\"padding:8px 0; font-weight:600; color:#065f46;\">Month/Year:</td>
                            <td style=\"padding:8px 0;\">{html.escape(month)} {html.escape(year)}</td>
                        </tr>
                        <tr style=\"border-bottom:1px solid #d1fae5;\">
                            <td style=\"padding:8px 0; font-weight:600; color:#065f46;\">Creation Date:</td>
                            <td style=\"padding:8px 0;\">{html.escape(creation_date)}</td>
                        </tr>
                        <tr>
                            <td style=\"padding:8px 0; font-weight:600; color:#065f46;\">Updated File:</td>
                            <td style=\"padding:8px 0; font-family:monospace; font-size:12px; word-break:break-all;\">{html.escape(filename)}</td>
                        </tr>
                    </table>
                    <div style=\"background:#dcfce7; padding:12px; border-radius:6px; margin-top:15px;\">
                        <p style=\"margin:0 0 10px 0; font-weight:600;\">Updates Made ({len(updates_made)}):</p>
                        <ul style=\"margin:0; padding-left:20px;\">{updates_html}</ul>
                    </div>
                    <div style=\"background:#d1fae5; padding:12px; border-radius:6px; margin-top:15px;\">
                        <p style=\"margin:0; font-size:13px;\"><strong>Full Path:</strong></p>
                        <p style=\"margin:5px 0 0 0; font-family:monospace; font-size:12px; word-break:break-all;\">{html.escape(file_path)}</p>
                    </div>
                </div>
                """
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html_page("Foundations Link - Update Success", body_content))
                return
                
            except PermissionError as e:
                print(f"[Foundations Update] Permission error: {e}")
                body_content = f"""
                <h2>Foundations Link - Error</h2>
                <p><a href=\"/foundations\">← Back</a></p>
                <div style=\"background:#fef2f2; border:1px solid #dc2626; border-radius:8px; padding:20px; margin:20px 0;\">
                    <h3 style=\"color:#dc2626; margin-top:0;\">❌ Permission Denied</h3>
                    <p>Could not update the file due to permission restrictions:</p>
                    <p style=\"font-family:monospace; background:#fee2e2; padding:10px; border-radius:4px;\">{html.escape(str(e))}</p>
                </div>
                """
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html_page("Foundations Link - Error", body_content))
                return
                
            except Exception as e:
                print(f"[Foundations Update] Error: {e}")
                import traceback
                traceback.print_exc()
                body_content = f"""
                <h2>Foundations Link - Error</h2>
                <p><a href=\"/foundations\">← Back</a></p>
                <div style=\"background:#fef2f2; border:1px solid #dc2626; border-radius:8px; padding:20px; margin:20px 0;\">
                    <h3 style=\"color:#dc2626; margin-top:0;\">❌ Error Updating File</h3>
                    <p>An error occurred while updating the foundation link file:</p>
                    <p style=\"font-family:monospace; background:#fee2e2; padding:10px; border-radius:4px;\">{html.escape(str(e))}</p>
                </div>
                """
                self.send_response(200)
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html_page("Foundations Link - Error", body_content))
                return
        
        # Handle Administrative Tool submission
        if parsed.path == "/admin-tool/submit":
            length = int(self.headers.get('Content-Length', '0'))
            body = self.rfile.read(length).decode('utf-8') if length > 0 else ''
            pairs = [p for p in body.split('&') if p]
            form = {}
            for p in pairs:
                if '=' in p:
                    k, v = p.split('=', 1)
                    form[unquote(k)] = unquote(v.replace('+', ' '))
            
            ascf_version = form.get('ascf_version', '').strip()
            hotfix_version = form.get('hotfix_version', '').strip()
            release_date = form.get('release_date', '').strip()
            download_url = form.get('download_url', '').strip()
            release_notes_url = form.get('release_notes_url', '').strip()
            
            # Validate inputs
            if not all([ascf_version, hotfix_version, release_date, download_url, release_notes_url]):
                body_content = """
                <h2>Administrative Tool - Error</h2>
                <p><a href=\"/admin-tool\">← Back</a></p>
                <div style=\"background:#fef2f2; border:1px solid #dc2626; border-radius:8px; padding:20px; margin:20px 0;\">
                    <h3 style=\"color:#dc2626; margin-top:0;\">❌ Missing Input</h3>
                    <p>Please provide all required fields.</p>
                </div>
                """
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html_page("Administrative Tool - Error", body_content))
                return
            
            print(f"[Admin Tool] Starting automation...")
            print(f"[Admin Tool] ASCF Version: {ascf_version}")
            print(f"[Admin Tool] Hotfix: {hotfix_version}")
            print(f"[Admin Tool] Release Date: {release_date}")
            print(f"[Admin Tool] Download URL: {download_url}")
            print(f"[Admin Tool] Release Notes URL: {release_notes_url}")
            
            automation_result = None
            automation_error = None
            
            try:
                # Try to use Selenium for browser automation
                from selenium import webdriver
                from selenium.webdriver.common.by import By
                from selenium.webdriver.support.ui import WebDriverWait, Select
                from selenium.webdriver.support import expected_conditions as EC
                from selenium.webdriver.chrome.options import Options
                from selenium.webdriver.chrome.service import Service
                import time as time_module
                
                print("[Admin Tool] Selenium found, starting browser automation...")
                
                # Setup Chrome options
                chrome_options = Options()
                chrome_options.add_argument("--start-maximized")
                
                # Obtain ChromeDriver: env override, or webdriver-manager (downloads matching driver for installed Chrome)
                chromedriver_path = os.environ.get("CHROMEDRIVER_PATH", "").strip()
                if chromedriver_path and os.path.isfile(chromedriver_path):
                    service = Service(executable_path=chromedriver_path)
                    driver = webdriver.Chrome(service=service, options=chrome_options)
                else:
                    try:
                        from webdriver_manager.chrome import ChromeDriverManager
                        service = Service(ChromeDriverManager().install())
                        driver = webdriver.Chrome(service=service, options=chrome_options)
                    except ImportError:
                        # Fallback: let Selenium use its built-in manager (Selenium 4.6+)
                        driver = webdriver.Chrome(options=chrome_options)
                
                try:
                    # Step 1: Navigate directly to Add Version page
                    print("[Admin Tool] Step 1: Navigating to http://foundations/asplib/addVersion.asp?Product=ASCF")
                    driver.get("http://foundations/asplib/addVersion.asp?Product=ASCF")
                    time_module.sleep(3)
                    
                    # Step 2: Select ASCF Product Version from dropdown
                    print(f"[Admin Tool] Step 2: Selecting ASCF Product Version: {ascf_version}")
                    version_select = None
                    
                    # Try multiple selectors for the version dropdown
                    version_selectors = [
                        (By.ID, "productVersion"),
                        (By.NAME, "productVersion"),
                        (By.ID, "version"),
                        (By.NAME, "version"),
                        (By.XPATH, "//select[contains(@name, 'version') or contains(@name, 'Version')]"),
                        (By.XPATH, "//select[contains(@id, 'version') or contains(@id, 'Version')]"),
                        (By.TAG_NAME, "select"),
                    ]
                    
                    for selector_type, selector_value in version_selectors:
                        try:
                            version_select = WebDriverWait(driver, 3).until(
                                EC.presence_of_element_located((selector_type, selector_value))
                            )
                            if version_select:
                                print(f"[Admin Tool] Found version dropdown with: {selector_value}")
                                break
                        except:
                            continue
                    
                    if version_select:
                        try:
                            Select(version_select).select_by_visible_text(ascf_version)
                            print(f"[Admin Tool] Selected version: {ascf_version}")
                        except:
                            # Try selecting by value
                            try:
                                Select(version_select).select_by_value(ascf_version)
                                print(f"[Admin Tool] Selected version by value: {ascf_version}")
                            except:
                                print(f"[Admin Tool] Could not select version, trying partial match...")
                                # Try partial match
                                for option in Select(version_select).options:
                                    if ascf_version in option.text or ascf_version in option.get_attribute('value'):
                                        option.click()
                                        print(f"[Admin Tool] Selected option: {option.text}")
                                        break
                    else:
                        print("[Admin Tool] WARNING: Could not find version dropdown")
                    
                    time_module.sleep(2)
                    
                    # Step 3: Fill in the form fields
                    print("[Admin Tool] Step 3: Filling in form fields")
                    
                    # First, list all input fields on the page for debugging
                    try:
                        all_inputs = driver.find_elements(By.TAG_NAME, "input")
                        print(f"[Admin Tool] Found {len(all_inputs)} input fields on page:")
                        for inp in all_inputs:
                            inp_name = inp.get_attribute('name') or ''
                            inp_id = inp.get_attribute('id') or ''
                            inp_type = inp.get_attribute('type') or ''
                            if inp_type != 'hidden':
                                print(f"[Admin Tool]   - name='{inp_name}', id='{inp_id}', type='{inp_type}'")
                    except Exception as debug_err:
                        print(f"[Admin Tool] Debug error: {debug_err}")
                    
                    # Helper function to find and fill input field
                    def fill_field(field_names, value, field_label):
                        for name in field_names:
                            try:
                                field = driver.find_element(By.ID, name)
                                field.clear()
                                field.send_keys(value)
                                print(f"[Admin Tool] Filled {field_label} (by ID '{name}'): {value}")
                                return field
                            except:
                                pass
                            try:
                                field = driver.find_element(By.NAME, name)
                                field.clear()
                                field.send_keys(value)
                                print(f"[Admin Tool] Filled {field_label} (by NAME '{name}'): {value}")
                                return field
                            except:
                                pass
                        return None
                    
                    # Fill other fields FIRST (before hotfix version)
                    
                    # Release date field
                    date_fields = ["releaseDate", "ReleaseDate", "release_date", "date", "Date", "txtDate", "txtReleaseDate", "RelDate", "relDate"]
                    if not fill_field(date_fields, release_date, "Release Date"):
                        try:
                            date_input = driver.find_element(By.XPATH, "//input[contains(@name, 'ate') or contains(@id, 'ate')]")
                            date_input.clear()
                            date_input.send_keys(release_date)
                            print(f"[Admin Tool] Filled Release Date via XPath: {release_date}")
                        except:
                            print("[Admin Tool] WARNING: Could not find Release Date field")
                    
                    time_module.sleep(0.3)
                    
                    # Download URL field
                    download_fields = ["downloadUrl", "DownloadUrl", "download_url", "downloadURL", "txtDownload", "txtDownloadUrl", "DownloadURL", "download", "Download"]
                    if not fill_field(download_fields, download_url, "Download URL"):
                        try:
                            download_input = driver.find_element(By.XPATH, "//input[contains(@name, 'ownload') or contains(@id, 'ownload')]")
                            download_input.clear()
                            download_input.send_keys(download_url)
                            print(f"[Admin Tool] Filled Download URL via XPath: {download_url}")
                        except:
                            print("[Admin Tool] WARNING: Could not find Download URL field")
                    
                    time_module.sleep(0.3)
                    
                    # Release Notes URL field
                    notes_fields = ["releaseNotesUrl", "ReleaseNotesUrl", "release_notes_url", "releaseNotesURL", "notesUrl", "txtNotes", "txtReleaseNotes", "ReleaseNotes", "releaseNotes", "Notes", "notes"]
                    if not fill_field(notes_fields, release_notes_url, "Release Notes URL"):
                        try:
                            notes_input = driver.find_element(By.XPATH, "//input[contains(@name, 'otes') or contains(@id, 'otes')]")
                            notes_input.clear()
                            notes_input.send_keys(release_notes_url)
                            print(f"[Admin Tool] Filled Release Notes URL via XPath: {release_notes_url}")
                        except:
                            print("[Admin Tool] WARNING: Could not find Release Notes URL field")
                    
                    time_module.sleep(0.3)
                    
                    # Fill Hotfix version field LAST (right before submit)
                    # Try many possible field names for ASP pages
                    hotfix_fields = [
                        "hotfixVersion", "HotfixVersion", "hotfix_version", 
                        "version", "Version", "txtVersion", "txtHotfix",
                        "NewVersion", "newVersion", "new_version", "txtNewVersion",
                        "HotfixVer", "hotfixVer", "Hotfix", "hotfix",
                        "VersionNumber", "versionNumber", "ver", "Ver"
                    ]
                    hotfix_element = fill_field(hotfix_fields, hotfix_version, "Hotfix Version")
                    
                    if not hotfix_element:
                        # Try by XPath - look for any text input that might be for version
                        try:
                            # Try to find input fields containing 'version' or 'hotfix' in name/id
                            hotfix_input = driver.find_element(By.XPATH, "//input[@type='text'][contains(@name, 'ersion') or contains(@id, 'ersion') or contains(@name, 'otfix') or contains(@id, 'otfix')]")
                            hotfix_input.clear()
                            hotfix_input.send_keys(hotfix_version)
                            hotfix_element = hotfix_input
                            print(f"[Admin Tool] Filled Hotfix Version via XPath: {hotfix_version}")
                        except:
                            # Try finding the first text input field that's not already filled
                            try:
                                text_inputs = driver.find_elements(By.XPATH, "//input[@type='text']")
                                for inp in text_inputs:
                                    if not inp.get_attribute('value'):
                                        inp.clear()
                                        inp.send_keys(hotfix_version)
                                        hotfix_element = inp
                                        print(f"[Admin Tool] Filled first empty text field with Hotfix Version: {hotfix_version}")
                                        break
                            except:
                                pass
                    
                    if not hotfix_element:
                        print("[Admin Tool] WARNING: Could not find Hotfix Version field")
                        print("[Admin Tool] Please enter the Hotfix Version manually")
                    
                    time_module.sleep(0.5)
                    
                    # Step 4: Click Add Hotfix / Submit button
                    print("[Admin Tool] Step 4: Clicking Add Hotfix / Submit button")
                    submit_btn = None
                    
                    submit_selectors = [
                        (By.XPATH, "//input[@value='Add Hotfix']"),
                        (By.XPATH, "//input[contains(@value, 'Add')]"),
                        (By.XPATH, "//input[@type='submit']"),
                        (By.XPATH, "//button[contains(text(), 'Add Hotfix')]"),
                        (By.XPATH, "//button[contains(text(), 'Add')]"),
                        (By.XPATH, "//button[contains(text(), 'Submit')]"),
                        (By.XPATH, "//input[@value='Submit']"),
                        (By.NAME, "submit"),
                        (By.NAME, "Submit"),
                    ]
                    
                    for selector_type, selector_value in submit_selectors:
                        try:
                            submit_btn = WebDriverWait(driver, 2).until(
                                EC.element_to_be_clickable((selector_type, selector_value))
                            )
                            if submit_btn:
                                print(f"[Admin Tool] Found submit button with: {selector_value}")
                                break
                        except:
                            continue
                    
                    if submit_btn:
                        driver.execute_script("arguments[0].scrollIntoView(true);", submit_btn)
                        time_module.sleep(0.5)
                        try:
                            submit_btn.click()
                            print("[Admin Tool] Submit button clicked successfully")
                        except:
                            driver.execute_script("arguments[0].click();", submit_btn)
                            print("[Admin Tool] Submit button clicked via JavaScript")
                    else:
                        print("[Admin Tool] WARNING: Could not find Submit button")
                        print("[Admin Tool] Please click the button manually")
                    
                    time_module.sleep(2)
                    
                    automation_result = "success"
                    print("[Admin Tool] Automation completed successfully!")
                    
                except Exception as e:
                    automation_error = str(e)
                    print(f"[Admin Tool] Automation error: {e}")
                finally:
                    # Keep browser open for user to verify
                    pass
                    
            except ImportError:
                # Selenium not available, fall back to opening browser manually
                print("[Admin Tool] Selenium not found, opening browser manually...")
                import webbrowser
                webbrowser.open("http://foundations")
                automation_result = "manual"
                
            except Exception as e:
                automation_error = str(e)
                print(f"[Admin Tool] Error: {e}")
            
            # Build response
            if automation_result == "success":
                body_content = f"""
                <h2>Administrative Tool - Success</h2>
                <p><a href=\"/admin-tool\">← Back</a></p>
                <div style=\"background:#f0fdf4; border:1px solid #059669; border-radius:8px; padding:20px; margin:20px 0;\">
                    <h3 style=\"color:#059669; margin-top:0;\">✅ Automation Completed Successfully</h3>
                    <p>The hotfix release has been added to the Foundation site.</p>
                    <table style=\"width:100%; border-collapse:collapse; margin:15px 0;\">
                        <tr style=\"border-bottom:1px solid #d1fae5;\">
                            <td style=\"padding:8px 0; font-weight:600; color:#065f46; width:180px;\">ASCF Version:</td>
                            <td style=\"padding:8px 0;\">{html.escape(ascf_version)}</td>
                        </tr>
                        <tr style=\"border-bottom:1px solid #d1fae5;\">
                            <td style=\"padding:8px 0; font-weight:600; color:#065f46;\">Hotfix Version:</td>
                            <td style=\"padding:8px 0;\">{html.escape(hotfix_version)}</td>
                        </tr>
                        <tr style=\"border-bottom:1px solid #d1fae5;\">
                            <td style=\"padding:8px 0; font-weight:600; color:#065f46;\">Release Date:</td>
                            <td style=\"padding:8px 0;\">{html.escape(release_date)}</td>
                        </tr>
                        <tr style=\"border-bottom:1px solid #d1fae5;\">
                            <td style=\"padding:8px 0; font-weight:600; color:#065f46;\">Download URL:</td>
                            <td style=\"padding:8px 0; word-break:break-all;\">{html.escape(download_url)}</td>
                        </tr>
                        <tr>
                            <td style=\"padding:8px 0; font-weight:600; color:#065f46;\">Release Notes URL:</td>
                            <td style=\"padding:8px 0; word-break:break-all;\">{html.escape(release_notes_url)}</td>
                        </tr>
                    </table>
                </div>
                """
            elif automation_result == "manual":
                body_content = f"""
                <h2>Administrative Tool - Manual Mode</h2>
                <p><a href=\"/admin-tool\">← Back</a></p>
                <div style=\"background:#fefce8; border:1px solid #f59e0b; border-radius:8px; padding:20px; margin:20px 0;\">
                    <h3 style=\"color:#d97706; margin-top:0;\">⚠️ Selenium Not Available - Manual Mode</h3>
                    <p>The Foundation site has been opened in your browser. Please complete the following steps manually:</p>
                    <ol style=\"margin:15px 0; padding-left:25px;\">
                        <li>Click on <strong>Administrative Tools</strong> link</li>
                        <li>Select <strong>{html.escape(ascf_version)}</strong> from the Product Version dropdown</li>
                        <li>Click <strong>Add New Release</strong> button</li>
                        <li>Fill in the following details:</li>
                    </ol>
                    <table style=\"width:100%; border-collapse:collapse; margin:15px 0; background:#fff; border-radius:8px;\">
                        <tr style=\"border-bottom:1px solid #fef3c7;\">
                            <td style=\"padding:10px; font-weight:600; color:#92400e; width:180px;\">Hotfix Version:</td>
                            <td style=\"padding:10px; font-family:monospace; background:#fffbeb;\">{html.escape(hotfix_version)}</td>
                            <td style=\"padding:10px;\"><button onclick=\"navigator.clipboard.writeText('{html.escape(hotfix_version)}')\" style=\"padding:4px 8px; cursor:pointer;\">📋 Copy</button></td>
                        </tr>
                        <tr style=\"border-bottom:1px solid #fef3c7;\">
                            <td style=\"padding:10px; font-weight:600; color:#92400e;\">Release Date:</td>
                            <td style=\"padding:10px; font-family:monospace; background:#fffbeb;\">{html.escape(release_date)}</td>
                            <td style=\"padding:10px;\"><button onclick=\"navigator.clipboard.writeText('{html.escape(release_date)}')\" style=\"padding:4px 8px; cursor:pointer;\">📋 Copy</button></td>
                        </tr>
                        <tr style=\"border-bottom:1px solid #fef3c7;\">
                            <td style=\"padding:10px; font-weight:600; color:#92400e;\">Download URL:</td>
                            <td style=\"padding:10px; font-family:monospace; background:#fffbeb; word-break:break-all; font-size:12px;\">{html.escape(download_url)}</td>
                            <td style=\"padding:10px;\"><button onclick=\"navigator.clipboard.writeText('{html.escape(download_url)}')\" style=\"padding:4px 8px; cursor:pointer;\">📋 Copy</button></td>
                        </tr>
                        <tr>
                            <td style=\"padding:10px; font-weight:600; color:#92400e;\">Release Notes URL:</td>
                            <td style=\"padding:10px; font-family:monospace; background:#fffbeb; word-break:break-all; font-size:12px;\">{html.escape(release_notes_url)}</td>
                            <td style=\"padding:10px;\"><button onclick=\"navigator.clipboard.writeText('{html.escape(release_notes_url)}')\" style=\"padding:4px 8px; cursor:pointer;\">📋 Copy</button></td>
                        </tr>
                    </table>
                    <p style=\"margin-top:15px;\">5. Click <strong>Add Hotfix</strong> button to save.</p>
                </div>
                <div style=\"background:#eff6ff; border:1px solid #3b82f6; border-radius:8px; padding:15px; margin:20px 0;\">
                    <p style=\"margin:0; color:#1e40af;\"><strong>💡 Tip:</strong> To enable automatic browser automation, install Selenium WebDriver:<br>
                    <code style=\"background:#dbeafe; padding:4px 8px; border-radius:4px; display:inline-block; margin-top:8px;\">pip install selenium</code></p>
                </div>
                """
            else:
                body_content = f"""
                <h2>Administrative Tool - Error</h2>
                <p><a href=\"/admin-tool\">← Back</a></p>
                <div style=\"background:#fef2f2; border:1px solid #dc2626; border-radius:8px; padding:20px; margin:20px 0;\">
                    <h3 style=\"color:#dc2626; margin-top:0;\">❌ Automation Error</h3>
                    <p>An error occurred during browser automation:</p>
                    <p style=\"font-family:monospace; background:#fee2e2; padding:10px; border-radius:4px;\">{html.escape(automation_error or 'Unknown error')}</p>
                </div>
                <div style=\"background:#fefce8; border:1px solid #f59e0b; border-radius:8px; padding:15px; margin:20px 0;\">
                    <h4 style=\"color:#d97706; margin-top:0;\">Manual Data Reference</h4>
                    <p>You can manually enter this data on the Foundation site:</p>
                    <table style=\"width:100%; border-collapse:collapse; margin:10px 0;\">
                        <tr style=\"border-bottom:1px solid #fef3c7;\">
                            <td style=\"padding:8px; font-weight:600;\">ASCF Version:</td>
                            <td style=\"padding:8px;\">{html.escape(ascf_version)}</td>
                        </tr>
                        <tr style=\"border-bottom:1px solid #fef3c7;\">
                            <td style=\"padding:8px; font-weight:600;\">Hotfix:</td>
                            <td style=\"padding:8px;\">{html.escape(hotfix_version)}</td>
                        </tr>
                        <tr style=\"border-bottom:1px solid #fef3c7;\">
                            <td style=\"padding:8px; font-weight:600;\">Release Date:</td>
                            <td style=\"padding:8px;\">{html.escape(release_date)}</td>
                        </tr>
                        <tr style=\"border-bottom:1px solid #fef3c7;\">
                            <td style=\"padding:8px; font-weight:600;\">Download URL:</td>
                            <td style=\"padding:8px; word-break:break-all;\">{html.escape(download_url)}</td>
                        </tr>
                        <tr>
                            <td style=\"padding:8px; font-weight:600;\">Release Notes URL:</td>
                            <td style=\"padding:8px; word-break:break-all;\">{html.escape(release_notes_url)}</td>
                        </tr>
                    </table>
                </div>
                """
            
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html_page("Administrative Tool", body_content))
            return

        if parsed.path == "/copy_async":
            # Async copy with progress tracking AND console logging
            import threading
            import json as json_module
            # Note: time is imported at module level
            
            length = int(self.headers.get('Content-Length', '0'))
            body = self.rfile.read(length).decode('utf-8') if length > 0 else ''
            pairs = [p for p in body.split('&') if p]
            form = {}
            for p in pairs:
                if '=' in p:
                    k, v = p.split('=', 1)
                    form[unquote(k)] = unquote(v.replace('+', ' '))
            
            src_choice = form.get('copy_source', 'folder1')
            folder1_path = normalize_build_path_for_windows(form.get('folder1_path', '').strip())
            folder2_path = normalize_build_path_for_windows(form.get('folder2_path', '').strip())
            dest_path = normalize_build_path_for_windows(form.get('dest_path', '').strip())
            overwrite = 'overwrite' in form
            
            # Initialize copy logging for console output
            copy_session_id = str(uuid.uuid4())[:8]
            last_copy_logs = []
            last_copy_session_id = copy_session_id
            
            # Helper function to add copy log (defined before copy_with_progress so it's in scope)
            def add_copy_log(message, level="INFO"):
                timestamp = datetime.now().strftime("%H:%M:%S")
                log_entry = f"[{timestamp}] [{level}] {message}"
                last_copy_logs.append(log_entry)
                print(log_entry)  # Also print to server console
            
            # Log initial info
            add_copy_log("=" * 60)
            add_copy_log("=== ASYNC COPY OPERATION STARTED ===")
            add_copy_log("=" * 60)
            add_copy_log(f"Session ID: {copy_session_id}")
            add_copy_log(f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            add_copy_log(f"Source Selection: {src_choice}")
            add_copy_log(f"Folder 1 Path: {folder1_path}")
            add_copy_log(f"Folder 2 Path: {folder2_path}")
            add_copy_log(f"Destination Path: {dest_path}")
            add_copy_log(f"Overwrite Enabled: {overwrite}")
            add_copy_log("-" * 60)
            
            def copy_with_progress():
                import shutil
                global copy_progress
                
                copy_progress["running"] = True
                copy_progress["error"] = None
                copy_progress["message"] = ""
                copy_progress["progress"] = 0
                copy_progress["total"] = 0
                copy_progress["current_file"] = ""
                copy_progress["start_time"] = time.time()
                copy_progress["bytes_copied"] = 0
                copy_progress["total_bytes"] = 0
                copy_progress["failed_files"] = []
                copy_progress["skipped_count"] = 0
                
                try:
                    if src_choice not in ('folder1', 'folder2'):
                        add_copy_log("ERROR: Invalid source selection", "ERROR")
                        raise ValueError('Invalid source selection')
                    src = folder1_path if src_choice == 'folder1' else folder2_path
                    add_copy_log(f"Selected source path: {src}")
                    
                    if not src or not dest_path:
                        add_copy_log("ERROR: Source and destination paths are required", "ERROR")
                        raise ValueError('Source and destination paths are required')
                    
                    src_path = Path(src)
                    dest_base = Path(dest_path)
                    
                    add_copy_log(f"Checking if source exists: {src_path}")
                    if not src_path.exists():
                        add_copy_log(f"ERROR: Source does not exist: {src}", "ERROR")
                        raise FileNotFoundError(f'Source does not exist: {src}')
                    add_copy_log("Source path verified - exists")
                    
                    # Check if source is file or directory
                    if src_path.is_dir():
                        add_copy_log("Source is a DIRECTORY")
                        try:
                            file_count = sum(1 for _ in src_path.rglob('*') if _.is_file())
                            dir_count = sum(1 for _ in src_path.rglob('*') if _.is_dir())
                            add_copy_log(f"Source contains: {file_count} files, {dir_count} subdirectories")
                        except Exception as count_err:
                            add_copy_log(f"Could not count source contents: {count_err}", "WARN")
                    else:
                        add_copy_log("Source is a FILE")
                        try:
                            file_size = src_path.stat().st_size
                            add_copy_log(f"File size: {file_size:,} bytes ({file_size/1024/1024:.2f} MB)")
                        except Exception as size_err:
                            add_copy_log(f"Could not get file size: {size_err}", "WARN")
                    
                    final_dest = dest_base / src_path.name
                    add_copy_log(f"Final destination path: {final_dest}")
                    
                    if final_dest.exists():
                        add_copy_log("Destination already exists", "WARN")
                        if overwrite:
                            add_copy_log("Overwrite enabled - removing existing destination")
                            copy_progress["current_file"] = "Removing existing destination..."
                            if final_dest.is_dir():
                                add_copy_log("Removing existing directory...")
                                shutil.rmtree(final_dest)
                                add_copy_log("Existing directory removed")
                            else:
                                add_copy_log("Removing existing file...")
                                final_dest.unlink()
                                add_copy_log("Existing file removed")
                        else:
                            add_copy_log("ERROR: Destination exists and overwrite is disabled", "ERROR")
                            raise FileExistsError('Destination exists. Enable overwrite to replace')
                    
                    # Ensure base exists
                    add_copy_log(f"Ensuring destination directory exists: {final_dest.parent}")
                    final_dest.parent.mkdir(parents=True, exist_ok=True)
                    add_copy_log("Destination directory ready")
                    
                    add_copy_log("-" * 60)
                    add_copy_log("STARTING COPY OPERATION...")
                    copy_start_time = time.time()
                    
                    if src_path.is_dir():
                        add_copy_log(f"Copying directory tree: {src_path} -> {final_dest}")
                        # Count all files first
                        all_files = []
                        total_size = 0
                        skipped_during_scan = []
                        copy_progress["current_file"] = "Scanning files..."
                        add_copy_log("Scanning source directory...")
                        for root, dirs, files in os.walk(src_path):
                            for f in files:
                                fp = Path(root) / f
                                try:
                                    file_size = fp.stat().st_size
                                    all_files.append((fp, file_size))
                                    total_size += file_size
                                except PermissionError as e:
                                    skipped_during_scan.append((str(fp), f"Access denied during scan: {e}"))
                                    add_copy_log(f"Skipped during scan (access denied): {fp}", "WARN")
                                except Exception as e:
                                    skipped_during_scan.append((str(fp), f"Error during scan: {e}"))
                                    add_copy_log(f"Skipped during scan (error): {fp} - {e}", "WARN")
                        
                        add_copy_log(f"Scan complete: {len(all_files)} files, {total_size/1024/1024:.2f} MB total")
                        if skipped_during_scan:
                            add_copy_log(f"Files skipped during scan: {len(skipped_during_scan)}", "WARN")
                        
                        copy_progress["total"] = len(all_files)
                        copy_progress["total_bytes"] = total_size
                        
                        # Track copy results
                        copied_count = 0
                        failed_files = list(skipped_during_scan)  # Start with files skipped during scan
                        
                        # Copy files with progress
                        add_copy_log("Copying files...")
                        for i, (src_file, file_size) in enumerate(all_files):
                            rel_path = src_file.relative_to(src_path)
                            dest_file = final_dest / rel_path
                            
                            copy_progress["current_file"] = str(rel_path)
                            copy_progress["progress"] = i + 1
                            
                            try:
                                dest_file.parent.mkdir(parents=True, exist_ok=True)
                                shutil.copy2(src_file, dest_file)
                                copy_progress["bytes_copied"] += file_size
                                copied_count += 1
                                # Log progress every 100 files
                                if (i + 1) % 100 == 0:
                                    add_copy_log(f"  Progress: {i + 1}/{len(all_files)} files copied ({copy_progress['bytes_copied']/1024/1024:.2f} MB)")
                            except PermissionError as e:
                                error_msg = f"Access denied: {e}"
                                failed_files.append((str(rel_path), error_msg))
                                copy_progress["skipped_count"] += 1
                                add_copy_log(f"Failed to copy {rel_path}: {error_msg}", "WARN")
                            except OSError as e:
                                # Handle network errors, file locks, etc.
                                error_msg = f"OS Error: {e}"
                                failed_files.append((str(rel_path), error_msg))
                                copy_progress["skipped_count"] += 1
                                add_copy_log(f"Failed to copy {rel_path}: {error_msg}", "WARN")
                            except Exception as e:
                                error_msg = str(e)
                                failed_files.append((str(rel_path), error_msg))
                                copy_progress["skipped_count"] += 1
                                add_copy_log(f"Failed to copy {rel_path}: {error_msg}", "WARN")
                        
                        copy_progress["progress"] = len(all_files)
                        copy_progress["failed_files"] = failed_files[:50]  # Keep first 50 failures for UI
                        
                        copy_end_time = time.time()
                        copy_duration = copy_end_time - copy_start_time
                        
                        # Build summary message
                        add_copy_log("-" * 60)
                        if failed_files:
                            copy_progress["message"] = f'Copied {copied_count}/{len(all_files)} files to {str(final_dest)}. {len(failed_files)} file(s) skipped due to access errors.'
                            add_copy_log(f"=== COPY COMPLETED WITH WARNINGS ===", "WARN")
                            add_copy_log(f"Files copied: {copied_count}/{len(all_files)}")
                            add_copy_log(f"Files skipped: {len(failed_files)}", "WARN")
                            if len(failed_files) <= 3:
                                # Show specific files if only a few failed
                                copy_progress["error"] = "Some files could not be copied: " + "; ".join([f[0] for f in failed_files[:3]])
                        else:
                            copy_progress["message"] = f'Successfully copied {copied_count} files to {str(final_dest)}'
                            add_copy_log("=== COPY OPERATION COMPLETED SUCCESSFULLY ===")
                            add_copy_log(f"Files copied: {copied_count}")
                        
                        add_copy_log(f"Total size: {copy_progress['bytes_copied']/1024/1024:.2f} MB")
                        add_copy_log(f"Duration: {copy_duration:.2f} seconds")
                        add_copy_log(f"Destination: {final_dest}")
                        add_copy_log("=" * 60)
                    else:
                        # Single file copy
                        add_copy_log(f"Copying single file: {src_path} -> {final_dest}")
                        copy_progress["total"] = 1
                        copy_progress["total_bytes"] = src_path.stat().st_size
                        copy_progress["current_file"] = src_path.name
                        shutil.copy2(src_path, final_dest)
                        copy_progress["progress"] = 1
                        copy_progress["bytes_copied"] = src_path.stat().st_size
                        copy_progress["message"] = f'Successfully copied to {str(final_dest)}'
                        
                        copy_end_time = time.time()
                        copy_duration = copy_end_time - copy_start_time
                        
                        add_copy_log("-" * 60)
                        add_copy_log("=== COPY OPERATION COMPLETED SUCCESSFULLY ===")
                        add_copy_log(f"File size: {copy_progress['bytes_copied']/1024/1024:.2f} MB")
                        add_copy_log(f"Duration: {copy_duration:.2f} seconds")
                        add_copy_log(f"Destination: {final_dest}")
                        add_copy_log("=" * 60)
                    
                except PermissionError as e:
                    copy_progress["error"] = f'Access denied: {e}. Check file permissions or if files are in use.'
                    add_copy_log("-" * 60, "ERROR")
                    add_copy_log("=== COPY OPERATION FAILED ===", "ERROR")
                    add_copy_log(f"Error: Access denied - {e}", "ERROR")
                    add_copy_log("=" * 60, "ERROR")
                except OSError as e:
                    if 'WinError 5' in str(e) or 'Access is denied' in str(e):
                        copy_progress["error"] = f'Access denied: {e}. The file may be locked or you lack permission.'
                        add_copy_log("-" * 60, "ERROR")
                        add_copy_log("=== COPY OPERATION FAILED ===", "ERROR")
                        add_copy_log(f"Error: Access denied - {e}", "ERROR")
                        add_copy_log("=" * 60, "ERROR")
                    else:
                        copy_progress["error"] = f'OS Error: {e}'
                        add_copy_log("-" * 60, "ERROR")
                        add_copy_log("=== COPY OPERATION FAILED ===", "ERROR")
                        add_copy_log(f"Error: OS Error - {e}", "ERROR")
                        add_copy_log("=" * 60, "ERROR")
                except Exception as e:
                    copy_progress["error"] = str(e)
                    add_copy_log("-" * 60, "ERROR")
                    add_copy_log("=== COPY OPERATION FAILED ===", "ERROR")
                    add_copy_log(f"Error: {e}", "ERROR")
                    import traceback
                    for line in traceback.format_exc().split('\n'):
                        if line.strip():
                            add_copy_log(line, "ERROR")
                    add_copy_log("=" * 60, "ERROR")
                finally:
                    copy_progress["running"] = False
            
            # Start copy in background thread
            thread = threading.Thread(target=copy_with_progress)
            thread.daemon = True
            thread.start()
            
            # Return immediately with session ID so UI can fetch logs
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            response = json_module.dumps({"status": "started", "session_id": copy_session_id}).encode('utf-8')
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)
            return
        
        if parsed.path == "/copy":
            length = int(self.headers.get('Content-Length', '0'))
            body = self.rfile.read(length).decode('utf-8') if length > 0 else ''
            # Parse form-encoded body
            pairs = [p for p in body.split('&') if p]
            form = {}
            for p in pairs:
                if '=' in p:
                    k, v = p.split('=', 1)
                    form[unquote(k)] = unquote(v.replace('+', ' '))
            src_choice = form.get('copy_source', 'folder1')
            folder1_path = normalize_build_path_for_windows(form.get('folder1_path', '').strip())
            folder2_path = normalize_build_path_for_windows(form.get('folder2_path', '').strip())
            dest_path = normalize_build_path_for_windows(form.get('dest_path', '').strip())
            overwrite = 'overwrite' in form

            import shutil
            msg = ''
            ok = True
            
            # Create a session for copy operation logging
            copy_session_id = str(uuid.uuid4())[:8]
            print(f"[DEBUG COPY] Created copy session ID: {copy_session_id}")
            
            # Use dedicated copy logs storage (simple and reliable)
            last_copy_logs = []  # Clear previous copy logs
            last_copy_session_id = copy_session_id
            
            # Helper function to add copy log
            def add_copy_log(message, level="INFO"):
                timestamp = datetime.now().strftime("%H:%M:%S")
                log_entry = f"[{timestamp}] [{level}] {message}"
                last_copy_logs.append(log_entry)
                print(log_entry)  # Also print to server console
            
            # Log copy operation start
            add_copy_log("=" * 60)
            add_copy_log("=== COPY OPERATION STARTED ===")
            add_copy_log("=" * 60)
            add_copy_log(f"Session ID: {copy_session_id}")
            add_copy_log(f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            add_copy_log(f"Source Selection: {src_choice}")
            add_copy_log(f"Folder 1 Path: {folder1_path}")
            add_copy_log(f"Folder 2 Path: {folder2_path}")
            add_copy_log(f"Destination Path: {dest_path}")
            add_copy_log(f"Overwrite Enabled: {overwrite}")
            add_copy_log("-" * 60)
            
            print(f"[DEBUG COPY] After initial logs, last_copy_logs has {len(last_copy_logs)} entries")
            
            try:
                if src_choice not in ('folder1', 'folder2'):
                    add_copy_log("ERROR: Invalid source selection", "ERROR")
                    raise ValueError('Invalid source selection')
                src = folder1_path if src_choice == 'folder1' else folder2_path
                add_copy_log(f"Selected source path: {src}")
                
                if not src or not dest_path:
                    add_copy_log("ERROR: Source and destination paths are required", "ERROR")
                    raise ValueError('Source and destination paths are required')
                
                src_path = Path(src)
                dest_base = Path(dest_path)
                
                add_copy_log(f"Checking if source exists: {src_path}")
                if not src_path.exists():
                    add_copy_log(f"ERROR: Source does not exist: {src}", "ERROR")
                    raise FileNotFoundError(f'Source does not exist: {src}')
                add_copy_log("Source path verified - exists")
                
                # Check if source is file or directory
                if src_path.is_dir():
                    add_copy_log("Source is a DIRECTORY")
                    # Count files in source directory
                    try:
                        file_count = sum(1 for _ in src_path.rglob('*') if _.is_file())
                        dir_count = sum(1 for _ in src_path.rglob('*') if _.is_dir())
                        add_copy_log(f"Source contains: {file_count} files, {dir_count} subdirectories")
                    except Exception as count_err:
                        add_copy_log(f"Could not count source contents: {count_err}", "WARN")
                else:
                    add_copy_log("Source is a FILE")
                    try:
                        file_size = src_path.stat().st_size
                        add_copy_log(f"File size: {file_size:,} bytes ({file_size/1024/1024:.2f} MB)")
                    except Exception as size_err:
                        add_copy_log(f"Could not get file size: {size_err}", "WARN")
                
                # Final destination inside the destination base
                final_dest = dest_base / src_path.name
                add_copy_log(f"Final destination path: {final_dest}")
                
                if final_dest.exists():
                    add_copy_log("Destination already exists", "WARN")
                    if overwrite:
                        add_copy_log("Overwrite enabled - removing existing destination")
                        if final_dest.is_dir():
                            add_copy_log("Removing existing directory...")
                            shutil.rmtree(final_dest)
                            add_copy_log("Existing directory removed")
                        else:
                            add_copy_log("Removing existing file...")
                            final_dest.unlink()
                            add_copy_log("Existing file removed")
                    else:
                        add_copy_log("ERROR: Destination exists and overwrite is disabled", "ERROR")
                        raise FileExistsError('Destination exists. Enable overwrite to replace')
                
                # Ensure base exists
                add_copy_log(f"Ensuring destination directory exists: {final_dest.parent}")
                final_dest.parent.mkdir(parents=True, exist_ok=True)
                add_copy_log("Destination directory ready")
                
                # Perform the copy
                add_copy_log("-" * 60)
                add_copy_log("STARTING COPY OPERATION...")
                copy_start_time = time.time()
                
                if src_path.is_dir():
                    add_copy_log(f"Copying directory tree: {src_path} -> {final_dest}")
                    
                    # Custom copytree with progress logging
                    def copy_with_progress(src, dst):
                        files_copied = 0
                        total_size = 0
                        
                        def copy_function(src_file, dst_file):
                            nonlocal files_copied, total_size
                            shutil.copy2(src_file, dst_file)
                            files_copied += 1
                            try:
                                total_size += Path(src_file).stat().st_size
                            except:
                                pass
                            if files_copied % 100 == 0:
                                add_copy_log(f"  Progress: {files_copied} files copied ({total_size/1024/1024:.2f} MB)")
                        
                        shutil.copytree(src, dst, copy_function=copy_function)
                        return files_copied, total_size
                    
                    files_copied, total_size = copy_with_progress(src_path, final_dest)
                    add_copy_log(f"Directory copy completed: {files_copied} files ({total_size/1024/1024:.2f} MB)")
                else:
                    add_copy_log(f"Copying file: {src_path} -> {final_dest}")
                    shutil.copy2(src_path, final_dest)
                    add_copy_log("File copy completed")
                
                copy_end_time = time.time()
                copy_duration = copy_end_time - copy_start_time
                
                add_copy_log("-" * 60)
                add_copy_log("=== COPY OPERATION COMPLETED SUCCESSFULLY ===")
                add_copy_log(f"Duration: {copy_duration:.2f} seconds")
                add_copy_log(f"Source: {src}")
                add_copy_log(f"Destination: {final_dest}")
                add_copy_log("=" * 60)
                
                msg = f'Copied {src} -> {str(final_dest)}'
                
            except Exception as e:
                ok = False
                msg = f'Copy failed: {e}'
                add_copy_log("-" * 60, "ERROR")
                add_copy_log("=== COPY OPERATION FAILED ===", "ERROR")
                add_copy_log(f"Error: {str(e)}", "ERROR")
                import traceback
                error_details = traceback.format_exc()
                for line in error_details.split('\n'):
                    if line.strip():
                        add_copy_log(line, "ERROR")
                add_copy_log("=" * 60, "ERROR")

            # Print summary to server console as well
            print(f"[COPY] {msg}")

            # Redirect back to index with a simple message banner
            # Use dedicated /copy_logs endpoint which always returns the most recent copy logs
            banner = f"""<div class=\"flash\" style=\"padding:8px; margin:12px 0; border:1px solid {'#9c9' if ok else '#f99'}; background:{'#efe' if ok else '#fee'}\">
                {html.escape(msg)}<br/>
                <small>Session ID: <strong>{copy_session_id}</strong> - <a href=\"#\" onclick=\"showCopyConsoleLogs(); return false;\" style=\"color:#3b82f6; font-weight:bold;\">📋 Click here to view Console Output</a></small>
            </div>
            <script>
            function showCopyConsoleLogs() {{
                console.log('Fetching copy logs from /copy_logs endpoint');
                
                // Open the modal
                document.getElementById('console-modal').style.display = 'block';
                document.body.style.overflow = 'hidden';
                
                // Fetch copy logs from dedicated endpoint
                fetch('/copy_logs')
                    .then(function(response) {{ return response.json(); }})
                    .then(function(data) {{
                        console.log('Copy logs received:', data);
                        var outputDiv = document.getElementById('console-output');
                        
                        // Update session display
                        document.getElementById('console-session-id').textContent = 'Copy Session: ' + data.session_id;
                        document.getElementById('console-line-count').textContent = data.total_lines + ' lines';
                        document.getElementById('console-timestamp').textContent = 'Last updated: ' + data.timestamp;
                        
                        if (data.logs && data.logs.length > 0) {{
                            var html = '<div style=\"background:#1a365d; color:#90cdf4; padding:10px; margin-bottom:10px; border-radius:6px; font-weight:bold;\">📋 COPY OPERATION LOGS</div>';
                            data.logs.forEach(function(line) {{
                                var color = '#d4d4d4';
                                if (line.indexOf('[ERROR]') >= 0 || line.indexOf('ERROR') >= 0) color = '#ef4444';
                                else if (line.indexOf('[WARN]') >= 0 || line.indexOf('WARNING') >= 0) color = '#f59e0b';
                                else if (line.indexOf('[INFO]') >= 0) color = '#22c55e';
                                html += '<div style=\"color:' + color + ';\">' + line.replace(/</g,'&lt;').replace(/>/g,'&gt;') + '</div>';
                            }});
                            outputDiv.innerHTML = html;
                            outputDiv.scrollTop = outputDiv.scrollHeight;
                        }} else {{
                            outputDiv.innerHTML = '<div style=\"color:#f59e0b; padding:20px;\">No copy logs available. The copy operation may not have generated any logs.</div>';
                        }}
                    }})
                    .catch(function(err) {{
                        document.getElementById('console-output').innerHTML = '<div style=\"color:#ef4444; padding:20px;\">Error fetching copy logs: ' + err.message + '</div>';
                    }});
            }}
            </script>"""
            
            # No additional script needed - everything is in the banner
            session_update_script = ""
            
            content = index_html().decode('utf-8')
            content = content.replace('<!-- builtin-post-banner -->', banner)
            page = html_page('CRM build comparison', content)
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(page)))
            self.end_headers()
            self.wfile.write(page)
            return

        if parsed.path == "/stop":
            # Stop only the requesting user's comparison session
            qs = parse_qs(parsed.query)
            session_id = qs.get("session_id", [""])[0]
            client_id = request_client_identity(self, qs.get("report_user", [""])[0])
            client_host = (self.client_address[0] or "")

            if not session_id:
                self.send_error(400, "session_id is required to stop a comparison")
                return

            session = get_session(session_id)
            if session and not session_owned_by_client(session, client_id, client_host):
                self.send_error(403, "Access denied for this comparison session")
                return

            if session:
                session["stop_requested"] = True
                session["running"] = False
                if session.get("comparer") and hasattr(session["comparer"], 'stop_requested'):
                    session["comparer"].stop_requested = True
                print(f"[INFO] Stop requested for session: {session_id} (client: {client_id})")
                if comparison_progress.get("_session_id") == session_id:
                    comparison_progress["running"] = False
                    if session.get("comparer") and comparison_progress.get("comparer") is session.get("comparer"):
                        comparison_progress["comparer"] = None
            
            copy_sess = get_copy_session(session_id)
            if copy_sess:
                copy_sess["stop_requested"] = True
                copy_sess["running"] = False
            
            cleanup_old_sessions()
            
            self.send_response(204)
            self.send_header('Content-Length', '0')
            self.end_headers()
            return

        self.send_error(404)


def run_startup_demos():
    """Run all normalization tests and demos on server startup."""
    import time
    
    print("=" * 80)
    print("RUNNING STARTUP DEMOS AND TESTS")
    print("=" * 80)
    print()
    
    # Get Python executable path (same as current process)
    python_exe = sys.executable
    
    demos = [
        ("Normalization Tests", "test_normalization.py"),
        ("Nested Path Normalization Demo", "demo_nested_normalization.py"),
    ]
    
    for name, script in demos:
        script_path = PROJECT_ROOT / script
        if not script_path.exists():
            print(f"[SKIP] {name}: {script} not found")
            continue
        
        print(f"[RUN] {name}")
        print("-" * 80)
        try:
            result = subprocess.run(
                [python_exe, str(script_path)],
                cwd=str(PROJECT_ROOT),
                capture_output=True,
                text=True,
                timeout=30
            )
            
            # Print output
            if result.stdout:
                print(result.stdout)
            if result.stderr:
                print(result.stderr)
            
            if result.returncode == 0:
                print(f"[OK] {name} completed successfully")
            else:
                print(f"[WARN] {name} exited with code {result.returncode}")
            
        except subprocess.TimeoutExpired:
            print(f"[WARN] {name} timed out after 30 seconds")
        except Exception as e:
            print(f"[ERROR] {name} failed: {e}")
        
        print()
        time.sleep(0.5)  # Brief pause between tests
    
    print("=" * 80)
    print("STARTUP DEMOS COMPLETE")
    print("=" * 80)
    print()


def cleanup_existing_servers():
    """Kill any existing server processes on this port"""
    try:
        import psutil
    except ImportError:
        # psutil not available - skip cleanup silently
        print("  -> Auto-cleanup disabled (to enable: pip install psutil)")
        return
    
    try:
        import time
        current_pid = os.getpid()
        killed_any = False
        
        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                # Skip current process
                if proc.info['pid'] == current_pid:
                    continue
                
                # Check if it's a Python process running builtin_server.py
                cmdline = proc.info.get('cmdline', [])
                if cmdline and any('builtin_server.py' in str(arg) for arg in cmdline):
                    print(f"  -> Stopping existing server (PID: {proc.info['pid']})")
                    proc.terminate()
                    killed_any = True
                    
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                pass
        
        if killed_any:
            print("  -> Waiting for processes to stop...")
            time.sleep(2)
            print("  -> Cleanup complete")
        else:
            print("  -> No existing servers found")
    except Exception as e:
        print(f"  -> Cleanup skipped: {e}")


def _local_ipv4_addresses() -> list[str]:
    """Non-loopback IPv4 addresses reachable from other machines on the LAN."""
    addrs: set[str] = set()
    public = (os.environ.get("PUBLIC_HOST") or "").strip()
    if public and not public.startswith("127."):
        addrs.add(public)
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET, socket.SOCK_STREAM):
            ip = str(info[4][0])
            if ip and not ip.startswith("127."):
                addrs.add(ip)
    except OSError:
        pass
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("8.8.8.8", 80))
        ip = str(probe.getsockname()[0])
        probe.close()
        if ip and not ip.startswith("127."):
            addrs.add(ip)
    except OSError:
        pass
    return sorted(addrs)


def _ensure_windows_firewall_rules(ports: list[int]) -> None:
    """Optionally allow inbound TCP when CRM_OPEN_FIREWALL=1."""
    from enterprise_security import firewall_remoteip_spec, should_open_firewall

    if not should_open_firewall():
        print("[SECURITY] Skipping automatic firewall rules (set CRM_OPEN_FIREWALL=1 to enable).")
        return
    if os.name != "nt":
        return
    remoteip = firewall_remoteip_spec()
    unique_ports = sorted({int(p) for p in ports if p})
    for port in unique_ports:
        rule_name = f"CRM Compare Tool TCP {port}"
        try:
            subprocess.run(
                ["netsh", "advfirewall", "firewall", "delete", "rule", f"name={rule_name}"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
            )
            add_args = [
                "netsh", "advfirewall", "firewall", "add", "rule",
                f"name={rule_name}",
                "dir=in",
                "action=allow",
                "protocol=TCP",
                f"localport={port}",
                "profile=domain,private,public",
            ]
            if remoteip:
                add_args.append(f"remoteip={remoteip}")
            add = subprocess.run(
                add_args,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
            )
            if add.returncode == 0:
                if remoteip:
                    print(
                        f"[OK] Windows Firewall: inbound TCP {port} allowed from corporate network only "
                        f"({remoteip})"
                    )
                else:
                    print(f"[OK] Windows Firewall: inbound TCP {port} allowed for LAN access")
            else:
                detail = (add.stderr or add.stdout or "").strip()
                print(f"[WARN] Could not add Windows Firewall rule for TCP {port}.")
                if detail:
                    print(f"       {detail}")
                remote_hint = f" remoteip={remoteip}" if remoteip else ""
                print(
                    f"       Run as Administrator: netsh advfirewall firewall add rule "
                    f'name="{rule_name}" dir=in action=allow protocol=TCP localport={port}{remote_hint}'
                )
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            print(f"[WARN] Firewall setup skipped for TCP {port}: {exc}")


def _build_ssl_context(cert_path: Path, key_path: Path) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    try:
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    except AttributeError:
        ctx.options |= ssl.OP_NO_SSLv3 | ssl.OP_NO_TLSv1 | ssl.OP_NO_TLSv1_1
    ctx.load_cert_chain(str(cert_path), str(key_path))
    return ctx


def _apply_https_socket(httpd: ThreadingHTTPServer, ssl_certfile: Optional[str], ssl_keyfile: Optional[str]) -> str:
    """Wrap the server socket for TLS. Returns ``https`` or ``http``."""
    if ssl_certfile and ssl_keyfile:
        cert_path = Path(ssl_certfile).resolve()
        key_path = Path(ssl_keyfile).resolve()
        if not cert_path.is_file() or not key_path.is_file():
            raise FileNotFoundError(f"SSL cert or key not found: {cert_path} / {key_path}")
        ctx = _build_ssl_context(cert_path, key_path)
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        return "https"
    if ssl_certfile or ssl_keyfile:
        raise ValueError("HTTPS requires both certificate and key (--ssl-cert and --ssl-key, or SSL_CERTFILE and SSL_KEYFILE).")
    return "http"


def _print_listen_urls(scheme: str, host: str, port: int) -> None:
    bind = (host or "127.0.0.1").strip().lower()
    if bind in ("0.0.0.0", "::", ""):
        print(f"[OK] {scheme.upper()} listening on all interfaces (0.0.0.0:{port}) — {scheme}://127.0.0.1:{port}/crm")
        lan_ips = _local_ipv4_addresses()
        if lan_ips:
            print(f"     Other machines on your network:")
            for ip in lan_ips:
                print(f"       {scheme}://{ip}:{port}/crm")
        else:
            print(f"     Other machines: {scheme}://<this-machine-ip>:{port}/crm")
    else:
        print(f"[OK] {scheme.upper()} listening on localhost ({bind}:{port}) — {scheme}://127.0.0.1:{port}/crm")
        print(f"     Local access only — other PCs cannot connect.")
    print()


def _default_ssl_paths() -> Tuple[Optional[str], Optional[str]]:
    c = PROJECT_ROOT / ".ssl" / "server.crt"
    k = PROJECT_ROOT / ".ssl" / "server.key"
    if c.is_file() and k.is_file():
        return str(c), str(k)
    return None, None


def run(
    host: str = "127.0.0.1",
    port: int = 5000,
    skip_demos: bool = False,
    ssl_certfile: Optional[str] = None,
    ssl_keyfile: Optional[str] = None,
    https_port: Optional[int] = None,
    use_default_ssl_if_present: bool = False,
):
    # Clean up any existing server instances (optional feature)
    try:
        print("Checking for existing server instances...")
        cleanup_existing_servers()
        print()
    except Exception as e:
        # Cleanup failed or psutil not available - continue anyway
        print(f"  -> Skipping cleanup: {e}")
        print()
    
    # Run startup demos unless skipped
    if not skip_demos:
        run_startup_demos()
    
    if use_default_ssl_if_present and not (ssl_certfile and ssl_keyfile):
        dc, dk = _default_ssl_paths()
        if dc and dk:
            ssl_certfile, ssl_keyfile = dc, dk

    use_tls = bool(ssl_certfile and ssl_keyfile)
    hp = int(https_port if https_port is not None else os.environ.get("HTTPS_PORT", "5443"))
    if use_tls and hp == port:
        print(f"ERROR: HTTPS port ({hp}) must differ from HTTP port ({port}).")
        return

    bind_host = (host or "127.0.0.1").strip()
    from enterprise_security import validate_startup_security, get_api_token

    validate_startup_security(bind_host)
    if get_api_token():
        print("[SECURITY] CRM_API_TOKEN is configured — remote users need ?access_token= on first visit.")
        print()

    if bind_host in ("127.0.0.1", "localhost", "::1"):
        print("[INFO] Localhost-only binding — other machines cannot connect.")
        print("       Use --host 0.0.0.0 with CRM_API_TOKEN for secured LAN access.")
        print()
    elif bind_host in ("0.0.0.0", "::", ""):
        from enterprise_security import network_restriction_enabled, get_network_allowlist_cidrs

        if network_restriction_enabled():
            cidrs = ", ".join(get_network_allowlist_cidrs())
            print(f"[INFO] Network-wide binding — remote access limited to corporate network: {cidrs}")
        else:
            print("[INFO] Network-wide binding — remote clients from any network are allowed.")
        print("[INFO] Access token required for remote clients when CRM_API_TOKEN is set.")
        print()

    firewall_ports = [port]
    if use_tls:
        firewall_ports.append(hp)
    print("Checking Windows Firewall (CRM_OPEN_FIREWALL=1 to auto-add rules)...")
    _ensure_windows_firewall_rules(firewall_ports)
    print()

    httpd_http: Optional[ThreadingHTTPServer] = None
    httpd_https: Optional[ThreadingHTTPServer] = None
    http_thread: Optional[threading.Thread] = None
    try:
        print(f"Starting HTTP server on {bind_host}:{port}...")
        httpd_http = ThreadingHTTPServer((bind_host, port), AppHandler)
        _print_listen_urls("http", bind_host, port)

        if use_tls:
            print(f"Starting HTTPS server on {bind_host}:{hp}...")
            httpd_https = ThreadingHTTPServer((bind_host, hp), AppHandler)
            _apply_https_socket(httpd_https, ssl_certfile, ssl_keyfile)
            _print_listen_urls("https", bind_host, hp)

            def _serve_http() -> None:
                httpd_http.serve_forever()

            http_thread = threading.Thread(target=_serve_http, name="builtin-http", daemon=True)
            http_thread.start()
            print("HTTP and HTTPS are both active (same app). Use HTTPS URL for encrypted access.")
            print("Press Ctrl+C to stop both servers")
            print()
            httpd_https.serve_forever()
        else:
            print("Press Ctrl+C to stop the server")
            print()
            httpd_http.serve_forever()
    except (OSError, ValueError, FileNotFoundError) as e:
        if isinstance(e, OSError):
            print(f"ERROR: Could not start server: {e}")
            print()
            print("Possible solutions:")
            print("  1. Check if another process is using this port (or HTTPS_PORT)")
            print("  2. Try: py builtin_server.py --port 8081 --https-port 5443")
            print("  3. Check firewall settings")
        else:
            print(f"ERROR: {e}")
        return
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        if httpd_http is not None:
            httpd_http.shutdown()
            httpd_http.server_close()
        if httpd_https is not None:
            httpd_https.shutdown()
            httpd_https.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Built-in ASCF comparison server with auto-cleanup")
    parser.add_argument(
        "--host",
        default=os.environ.get("HOST", "127.0.0.1"),
        help="Host/IP to bind. Default 127.0.0.1 (local only). Use 0.0.0.0 for LAN with CRM_API_TOKEN.",
    )
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "5000")), help="Port to bind (default: 5000)")
    parser.add_argument(
        "--ssl-cert",
        default=os.environ.get("SSL_CERTFILE"),
        metavar="FILE",
        help="PEM certificate file for HTTPS (pair with --ssl-key)",
    )
    parser.add_argument(
        "--ssl-key",
        default=os.environ.get("SSL_KEYFILE"),
        metavar="FILE",
        help="PEM private key for HTTPS (pair with --ssl-cert)",
    )
    parser.add_argument(
        "--https",
        action="store_true",
        help="Enable HTTPS using .ssl/server.crt and .ssl/server.key if --ssl-cert/--ssl-key are not set. HTTP stays on --port; HTTPS uses --https-port.",
    )
    parser.add_argument(
        "--https-port",
        type=int,
        default=None,
        metavar="PORT",
        help="HTTPS listen port when TLS is enabled (default: env HTTPS_PORT or 5443). Must differ from --port.",
    )
    parser.add_argument("--skip-demos", action="store_true", help="Skip running startup demos and tests")
    args = parser.parse_args()
    
    print("=" * 70)
    print("ASCF Comparison Server")
    print("=" * 70)
    print()
    
    run(
        args.host,
        args.port,
        args.skip_demos,
        args.ssl_cert,
        args.ssl_key,
        https_port=args.https_port,
        use_default_ssl_if_present=args.https,
    )



