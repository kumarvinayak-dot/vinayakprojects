#!/usr/bin/env python3
import os
import html
import ssl
import threading
import io
import zipfile
import ai_testgen
from pathlib import Path
from datetime import datetime
from typing import Optional, Tuple
from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    send_from_directory,
    send_file,
    abort,
    flash,
    make_response,
    jsonify,
)

# Ensure src is importable
PROJECT_ROOT = Path(__file__).parent
SRC_DIR = PROJECT_ROOT / "src"
import sys
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from enterprise_security import (  # noqa: E402
    check_request_access,
    is_https_flask_request,
    load_dotenv,
    validate_startup_security,
)

load_dotenv(str(PROJECT_ROOT))
os.environ.setdefault("CRM_PROJECT_ROOT", str(PROJECT_ROOT))

from folder_compare import (
    FolderComparer,
    NamingConventionComparer,
    ArchiveContentsComparer,
    JarManifestComparer,
    _path_exists,
    _path_is_file,
)
from crm_build_paths import get_crm_build_base_paths, normalize_build_path_for_windows
from crm_jar_download import (
    BadSignature,
    SignatureExpired,
    get_jar_download_secret,
    is_path_allowed_for_jar_download,
    load_jar_download_payload,
)
from report_html_sections import inject_report_section_controls


app = Flask(__name__)
# Flash sessions and signed JAR download tokens use the same secret.
app.secret_key = get_jar_download_secret()
last_folder1_path = ""
last_folder2_path = ""


@app.before_request
def _enterprise_security_gate():
    allowed, message, status = check_request_access(
        request.remote_addr or "",
        request.path,
        request.query_string.decode("utf-8") if request.query_string else "",
        request.method,
        dict(request.headers),
        is_https=is_https_flask_request(request),
    )
    if not allowed:
        if request.path.startswith("/api/") or request.accept_mimetypes.accept_json:
            return jsonify({"ok": False, "error": message}), status
        return message, status


@app.after_request
def _enterprise_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    return response


@app.context_processor
def _inject_security_bootstrap():
    from enterprise_security import client_access_token_bootstrap_js

    return {"crm_security_bootstrap_js": client_access_token_bootstrap_js()}


from report_naming import allocate_report_filename, resolve_client_identity  # noqa: E402
from client_temp_paths import resolve_client_machine_hostname  # noqa: E402
from report_storage import (  # noqa: E402
    MAX_BROWSE_REPORTS,
    is_allowed_report_name,
    list_report_files as _list_report_files,
    resolve_report_path,
    resolve_report_save_dir,
)


def _request_report_dirs():
    machine = resolve_client_machine_hostname(
        request.remote_addr or "",
        dict(request.headers),
        (request.form.get("report_user") or request.args.get("report_user") or "").strip(),
    )
    report_dir = resolve_report_save_dir(machine, PROJECT_ROOT)
    dirs = [report_dir]
    if report_dir.resolve() != PROJECT_ROOT.resolve():
        dirs.append(PROJECT_ROOT)
    return machine, report_dir, dirs


def list_report_files(limit=MAX_BROWSE_REPORTS, machine_host: str = ""):
    """Latest comparison reports on client PC (and legacy server copies)."""
    if machine_host:
        report_dir = resolve_report_save_dir(machine_host, PROJECT_ROOT)
        dirs = [report_dir]
        if report_dir.resolve() != PROJECT_ROOT.resolve():
            dirs.append(PROJECT_ROOT)
    else:
        _, _, dirs = _request_report_dirs()
    return _list_report_files(dirs, limit=limit)


def cleanup_old_reports(max_reports=10):
    """Reports persist on the user's PC until manually deleted."""
    return 0


def redirect_open_report_new_tab(view_url: str):
    """Open HTML report in a new tab, then return to home (popup fallback: same tab)."""
    import json

    safe_url = json.dumps(view_url)
    home = json.dumps(url_for("index"))
    body = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Opening report</title></head>
<body><p>Opening report in a new tab&hellip;</p>
<script>
(function() {{
  var u = {safe_url};
  var w = window.open(u, '_blank', 'noopener,noreferrer');
  if (w) {{ try {{ w.opener = null; }} catch (e) {{}} window.location.replace({home}); }}
  else {{ window.location.href = u; }}
}})();
</script></body></html>"""
    resp = make_response(body)
    resp.headers["Content-Type"] = "text/html; charset=utf-8"
    return resp


@app.route("/api/crm-build-base-paths", methods=["GET"])
def api_crm_build_base_paths():
    """Return CRM build base paths for the home page (copy / Old / New helpers)."""
    return jsonify({"bases": get_crm_build_base_paths()})


@app.route("/api/open-path-in-explorer", methods=["POST"])
def api_open_path_in_explorer():
    """
    Open a UNC or local folder in Windows Explorer (localhost only).
    Remote browsers get 403 — use copy-to-clipboard fallback in the UI.
    """
    import os

    from flask import abort, request

    addr = (request.remote_addr or "").lower()
    if addr not in ("127.0.0.1", "::1") and not addr.endswith("127.0.0.1"):
        abort(403)
    if os.name != "nt":
        return jsonify({"ok": False, "error": "Windows only.", "fallback": "copy"}), 501

    data = request.get_json(silent=True) or {}
    path = (data.get("path") or "").strip()

    try:
        from windows_folder_picker import open_folder_in_explorer

        open_folder_in_explorer(path)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e), "fallback": "copy"}), 400
    except RuntimeError as e:
        return jsonify({"ok": False, "error": str(e), "fallback": "copy"}), 500

    return jsonify({"ok": True})


@app.route("/api/validate-environment-jars", methods=["POST"])
def api_validate_environment_jars():
    """Scan environment path for FOSS/Foundation JAR Implementation-Version values."""
    from datetime import datetime
    from environment_jar_validate import (
        render_environment_validation_html,
        run_validation_from_request,
    )

    data = request.get_json(silent=True) or {}
    try:
        result = run_validation_from_request(data)
        env_client = resolve_client_identity(
            request.remote_addr or "",
            dict(request.headers),
            str(data.get("report_user") or ""),
        )
        _machine, env_report_dir, _ = _request_report_dirs()
        report_name = allocate_report_filename(
            "environment_jar_validation",
            env_client,
            env_report_dir,
            suffix=".html",
        )
        report_path = env_report_dir / report_name
        report_path.write_text(render_environment_validation_html(result), encoding="utf-8")
        result["report_url"] = url_for("view_report", filename=report_name)
        return jsonify(result)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/windows-folder-picker", methods=["POST"])
def api_windows_folder_picker():
    """
    Open the native Windows folder dialog (localhost only).
    JSON body: optional {"initial_dir": "file://server/share/..."} or UNC; converted for the OS picker.
    """
    import os

    from flask import abort, request

    addr = (request.remote_addr or "").lower()
    if addr not in ("127.0.0.1", "::1") and not addr.endswith("127.0.0.1"):
        abort(403)
    if os.name != "nt":
        return jsonify({"ok": False, "error": "Folder picker requires Windows."}), 501

    data = request.get_json(silent=True) or {}
    initial_dir = normalize_build_path_for_windows((data.get("initial_dir") or "").strip())

    try:
        from windows_folder_picker import pick_folder

        path = pick_folder(initial_dir if initial_dir else None)
    except RuntimeError as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    if path is None:
        return jsonify({"ok": False, "cancelled": True})
    return jsonify({"ok": True, "path": path})


@app.route("/")
@app.route("/crm")
@app.route("/ascf")
@app.route("/uxf")
@app.route("/dex")
def index():
    default_folder1 = last_folder1_path
    default_folder2 = last_folder2_path

    # Build recent reports list with HTML filenames for viewing (latest 10 by save time)
    recent_reports = []
    for p in list_report_files(limit=MAX_BROWSE_REPORTS):
        html_path = p.with_suffix('.html')
        open_name = html_path.name if html_path.exists() else p.name
        try:
            ts = datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            ts = ""
        recent_reports.append({"name": p.name, "open_name": open_name, "mtime": ts})
    
    return render_template(
        "index.html",
        default_folder1=default_folder1,
        default_folder2=default_folder2,
        recent_reports=recent_reports,
    )


@app.route("/run", methods=["POST"]) 
def run_compare():
    global last_folder1_path, last_folder2_path
    mode_naming = bool(request.form.get("naming_convention"))
    mode = request.form.get("mode", "full")
    jar_name = request.form.get("jar_name", "").strip()

    # Read common fields (file:// URLs from Chrome → UNC for comparers)
    folder1_path = normalize_build_path_for_windows(request.form.get("folder1_path", "").strip())
    folder2_path = normalize_build_path_for_windows(request.form.get("folder2_path", "").strip())
    if folder1_path:
        last_folder1_path = folder1_path
    if folder2_path:
        last_folder2_path = folder2_path

    # Read application selections
    app_ascf = bool(request.form.get("app_ascf"))
    app_crm = bool(request.form.get("app_crm"))
    app_uxf = bool(request.form.get("app_uxf"))
    app_dex = bool(request.form.get("app_dex"))
    app_wef = bool(request.form.get("app_wef"))
    selected_apps = []
    if app_ascf:
        selected_apps.append("ASCF")
    if app_crm:
        selected_apps.append("CRM/AMSS")
    if app_uxf:
        selected_apps.append("UXF/OMNI")
    if app_dex:
        selected_apps.append("DEX")
    if app_wef:
        selected_apps.append("WEF")

    # Options
    # These options are always enabled (removed from UI but always active)
    show_all_differences = True
    ignore_timestamp_only = True
    suppress_warnings = True
    
    # Advanced compare options removed from UI — fixed defaults (temp placement uses \\\\inpnqbdnc04\\software\\crm_compare_temp when local disk is full).
    use_local_copy = False
    keep_temp_folders = False
    exclude_missing_folder2 = False
    rename_folder2_only = False
    rename_suffix = "_renamed"
    max_retries = 3
    retry_delay = 1.0

    dex_nexus_previous = request.form.get("dex_nexus_previous_version", "").strip()
    dex_nexus_latest = request.form.get("dex_nexus_latest_version", "").strip()
    dex_webapp_ces_previous = request.form.get("dex_webapp_ces_previous_version", "").strip()
    dex_webapp_ces_latest = request.form.get("dex_webapp_ces_latest_version", "").strip()
    dex_playbook_pair = bool(dex_nexus_previous and dex_nexus_latest)
    dex_webapp_pair = bool(dex_webapp_ces_previous and dex_webapp_ces_latest)
    dex_nexus_mode = app_dex and (dex_playbook_pair or dex_webapp_pair)

    # Validate inputs
    if not dex_nexus_mode and not app_wef and (not folder1_path or not folder2_path):
        flash(
            "Old Build Path and New Build Path are required (or enter DEX Nexus versions, or select WEF).",
            "error",
        )
        return redirect(url_for("index"))

    # Validate application selection
    if not selected_apps:
        flash("Please select at least one application (ASCF, CRM/AMSS, UXF/OMNI, DEX, or WEF).", "error")
        return redirect(url_for("index"))

    # Execute comparison
    report_text = None
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    _machine, report_dir, _ = _request_report_dirs()

    # Route based on selected application
    # CRM comparison - separate implementation
    if app_crm and not app_ascf and not app_uxf and not app_dex and not app_wef:
        try:
            from crm_compare import CRMComparer, run_crm_comparison
            
            mode = request.form.get("mode", "full")
            crm_class_diff_jar = request.form.get("crm_class_diff_jar", "").strip()
            crm_manifest_diff_jar = request.form.get("crm_manifest_diff_jar", "").strip()
            crm_config_files_filter = request.form.get("crm_config_files_filter", "").strip()
            crm_explicit_size_paths = request.form.get("crm_explicit_size_paths", "").strip()
            run_full_tree_compare = mode == "full" and request.form.get("fast_crm_full") == "1"
            crm_fast_enabled = run_full_tree_compare
            _clfy_vals = request.form.getlist("compare_clfy_agent_zip")
            compare_clfy_agent_zip = (
                str(_clfy_vals[-1]).strip().lower() in ("1", "on", "true", "yes")
                if _clfy_vals
                else False
            )
            
            comparer, results, output_filename = run_crm_comparison(
                folder1_path, folder2_path,
                mode=mode,
                jar_name=jar_name,
                suppress_warnings=suppress_warnings,
                show_missing_only=not show_all_differences,
                ignore_timestamp_only=ignore_timestamp_only,
                fast_mode=crm_fast_enabled,
                run_full_tree_compare=run_full_tree_compare,
                compare_archive_contents=not crm_fast_enabled if mode == "full" else True,
                deep_nested_archives=not crm_fast_enabled if mode == "full" else True,
                use_local_copy=not crm_fast_enabled,
                keep_temp_folders=keep_temp_folders,
                max_retries=max_retries,
                retry_delay=retry_delay,
                class_diff_jar_name=crm_class_diff_jar if mode == "full" else "",
                manifest_diff_jar_name=crm_manifest_diff_jar if mode == "full" else "",
                crm_config_files_filter=crm_config_files_filter if mode == "full" else "",
                crm_explicit_size_paths=crm_explicit_size_paths if mode == "full" else "",
                compare_clfy_agent_zip=compare_clfy_agent_zip if mode == "full" else False,
                report_dir=str(report_dir),
                report_user=request.form.get("report_user", "").strip() or None,
            )
            
            output_filepath = str(report_dir / output_filename)
            comparer.save_report(output_filepath)
            cleanup_old_reports(10)
            
            flash("CRM comparison completed.", "success")
        except Exception as e:
            flash(f"CRM comparison failed: {str(e)}", "error")
            print(f"CRM comparison error: {e}")
            import traceback
            traceback.print_exc()
            output_filename = f"crm_patch_comparison_{timestamp}.html"
        
        # Redirect to HTML report
        try:
            html_name = Path(output_filename).name
            if not html_name.lower().endswith(".html"):
                html_name = Path(output_filename).with_suffix(".html").name
            html_path = report_dir / html_name
            if _path_exists(html_path):
                return redirect_open_report_new_tab(url_for("view_report", filename=html_name))
        except Exception:
            pass
        return redirect(url_for("index"))

    # UXF comparison - separate implementation
    if app_uxf and not app_ascf and not app_crm and not app_dex and not app_wef:
        try:
            from uxf_compare import UXFComparer, run_uxf_comparison
            
            print(f"[UXF] Starting UXF comparison...")
            print(f"[UXF] Old Build: {folder1_path}")
            print(f"[UXF] New Build: {folder2_path}")
            
            comparer, results, output_filename = run_uxf_comparison(
                folder1_path, folder2_path,
                suppress_warnings=suppress_warnings,
                show_missing_only=not show_all_differences,
                ignore_timestamp_only=ignore_timestamp_only,
                report_dir=str(report_dir),
            )
            
            output_filepath = str(report_dir / output_filename)
            comparer.save_report(output_filepath)
            cleanup_old_reports(10)
            
            # Build summary message
            total_diff = len(comparer.only_in_1) + len(comparer.only_in_2) + len(comparer.version_changes) + len(comparer.size_changes)
            flash(f"UXF comparison completed. Found {total_diff} differences.", "success")
        except Exception as e:
            flash(f"UXF comparison failed: {str(e)}", "error")
            print(f"UXF comparison error: {e}")
            import traceback
            traceback.print_exc()
            output_filename = f"uxf_patch_comparison_{timestamp}.txt"
        
        # Redirect to HTML report
        try:
            html_name = Path(output_filename).with_suffix(".html").name
            html_path = report_dir / html_name
            if _path_exists(html_path):
                return redirect_open_report_new_tab(url_for("view_report", filename=html_name))
        except Exception:
            pass
        return redirect(url_for("index"))

    # DEX comparison - separate implementation
    if app_dex and not app_ascf and not app_crm and not app_uxf and not app_wef:
        if not dex_nexus_mode:
            flash(
                "Enter previous and latest Nexus versions for digital-playbook and/or digital-webapp-ces. "
                "Artifacts are downloaded from Nexus and compared automatically.",
                "error",
            )
            return redirect(url_for("index"))
        try:
            from dex_compare import run_dex_comparison

            print(f"[DEX] Starting DEX comparison...")
            if dex_playbook_pair:
                print(f"[DEX] digital-playbook: {dex_nexus_previous} -> {dex_nexus_latest}")
            if dex_webapp_pair:
                print(f"[DEX] digital-webapp-ces: {dex_webapp_ces_previous} -> {dex_webapp_ces_latest}")
            else:
                print(f"[DEX] Old Build: {folder1_path}")
                print(f"[DEX] New Build: {folder2_path}")

            comparer, results, output_filename = run_dex_comparison(
                folder1_path,
                folder2_path,
                suppress_warnings=suppress_warnings,
                show_missing_only=not show_all_differences,
                ignore_timestamp_only=ignore_timestamp_only,
                previous_nexus_version=dex_nexus_previous,
                latest_nexus_version=dex_nexus_latest,
                webapp_ces_previous_version=dex_webapp_ces_previous,
                webapp_ces_latest_version=dex_webapp_ces_latest,
                report_dir=str(report_dir),
            )

            output_filepath = str(report_dir / output_filename)
            comparer.save_report(output_filepath)
            cleanup_old_reports(10)

            total_diff = len(comparer.only_in_1) + len(comparer.only_in_2) + len(comparer.version_changes) + len(comparer.size_changes)
            flash(f"DEX comparison completed. Found {total_diff} differences.", "success")
        except Exception as e:
            flash(f"DEX comparison failed: {str(e)}", "error")
            print(f"DEX comparison error: {e}")
            import traceback
            traceback.print_exc()
            output_filename = f"dex_patch_comparison_{timestamp}.txt"

        try:
            html_name = Path(output_filename).with_suffix(".html").name
            html_path = report_dir / html_name
            if _path_exists(html_path):
                return redirect_open_report_new_tab(url_for("view_report", filename=html_name))
        except Exception:
            pass
        return redirect(url_for("index"))

    # WEF comparison — remote SSH on Unix/Linux
    if app_wef and not app_ascf and not app_crm and not app_uxf and not app_dex:
        wef_ssh_host = request.form.get("wef_ssh_host", "").strip()
        wef_ssh_username = request.form.get("wef_ssh_username", "").strip()
        wef_ssh_password = request.form.get("wef_ssh_password", "")
        wef_old_path = request.form.get("wef_old_path", "").strip()
        wef_new_path = request.form.get("wef_new_path", "").strip()
        try:
            wef_ssh_port = int(request.form.get("wef_ssh_port", "22") or 22)
        except ValueError:
            wef_ssh_port = 22
        missing = []
        if not wef_ssh_host:
            missing.append("SSH host")
        if not wef_ssh_username:
            missing.append("SSH username")
        if not wef_ssh_password:
            missing.append("SSH password")
        if not wef_old_path:
            missing.append("old build path")
        if not wef_new_path:
            missing.append("new build path")
        if missing:
            flash(f"WEF requires: {', '.join(missing)}.", "error")
            return redirect(url_for("index"))
        try:
            from wef_compare import run_wef_comparison

            print(f"[WEF] SSH {wef_ssh_username}@{wef_ssh_host}:{wef_ssh_port}")
            print(f"[WEF] Old: {wef_old_path}  New: {wef_new_path}")
            comparer, results, output_filename = run_wef_comparison(
                wef_ssh_host,
                wef_ssh_username,
                wef_ssh_password,
                wef_old_path,
                wef_new_path,
                ssh_port=wef_ssh_port,
                show_missing_only=not show_all_differences,
                report_dir=str(report_dir),
            )
            if comparer.comparison_error:
                raise RuntimeError(comparer.comparison_error)
            output_filepath = str(report_dir / output_filename)
            comparer.save_report(output_filepath)
            cleanup_old_reports(10)
            added = sum(1 for r in results if r.status == "added")
            removed = sum(1 for r in results if r.status == "removed")
            modified = sum(1 for r in results if r.status == "modified")
            flash(
                f"WEF comparison completed. Added={added}, removed={removed}, modified={modified}.",
                "success",
            )
        except Exception as e:
            flash(f"WEF comparison failed: {str(e)}", "error")
            print(f"WEF comparison error: {e}")
            import traceback
            traceback.print_exc()
            output_filename = f"wef_remote_comparison_{timestamp}.txt"

        try:
            html_name = Path(output_filename).with_suffix(".html").name
            html_path = report_dir / html_name
            if _path_exists(html_path):
                return redirect_open_report_new_tab(url_for("view_report", filename=html_name))
        except Exception:
            pass
        return redirect(url_for("index"))

    # Multiple applications selected - show error
    if len(selected_apps) > 1:
        flash("Please select only one application at a time. ASCF, CRM/AMSS, UXF/OMNI, DEX, and WEF have different comparison logic.", "error")
        return redirect(url_for("index"))

    # ASCF-only: user-configurable compare options from the form
    if app_ascf and not app_crm and not app_uxf and not app_dex and not app_wef:
        use_local_copy = bool(request.form.get("use_local_copy"))
        keep_temp_folders = bool(request.form.get("keep_temp_folders"))
        exclude_missing_folder2 = bool(request.form.get("exclude_missing_folder2"))
        rename_folder2_only = bool(request.form.get("rename_folder2_only"))
        rename_suffix = request.form.get("rename_suffix", "_renamed").strip() or "_renamed"
        try:
            max_retries = int(request.form.get("max_retries", 3))
        except ValueError:
            max_retries = 3
        try:
            retry_delay = float(request.form.get("retry_delay", 1.0))
        except ValueError:
            retry_delay = 1.0

    # ASCF comparison - current working implementation (default behavior)
    # This is the original code that was working before
    if mode == "archives":
        try:
            comparer = ArchiveContentsComparer(folder1_path, folder2_path)
            comparer.suppress_warnings = suppress_warnings
            comparer.use_local_copy = use_local_copy
            comparer.max_retries = max_retries
            comparer.retry_delay = retry_delay
            comparer.should_cleanup_temp_folders = not keep_temp_folders
            
            output_filename = f"ascf_archives_report_{timestamp}.txt"
            output_filepath = str(report_dir / output_filename)
            
            # Run comparison with error handling
            results = comparer.compare_archives()
            
            # Save reports to PROJECT_ROOT (this will handle empty results gracefully)
            comparer.save_archive_report(output_filepath)
            
            if results:
                flash(f"Archive comparison completed: {len(results)} archives analyzed", "success")
            else:
                flash("Archive comparison completed with no results. Check server logs for details.", "warning")
        except Exception as e:
            flash(f"Archive comparison failed: {str(e)}", "error")
            print(f"Archive comparison error: {e}")
            import traceback
            traceback.print_exc()
            output_filename = f"ascf_archives_report_{timestamp}.txt"
    elif mode_naming:
        from pathlib import Path as P
        # Check if inputs are zip files - use fast method if so
        p1 = P(folder1_path)
        p2 = P(folder2_path)
        
        if (_path_exists(p1) and _path_is_file(p1) and p1.suffix.lower() in ['.zip', '.jar', '.war', '.ear'] and
            _path_exists(p2) and _path_is_file(p2) and p2.suffix.lower() in ['.zip', '.jar', '.war', '.ear']):
            # Fast comparison for zip files - no extraction needed
            flash("Using fast comparison mode for zip archives...", "info")
            import zipfile
            from collections import defaultdict
            
            # Quick archive listing
            def list_archives_in_zip(zip_path):
                archives = {}
                try:
                    with zipfile.ZipFile(zip_path, 'r') as zf:
                        for member in zf.namelist():
                            if any(member.lower().endswith(ext) for ext in ['.jar', '.war', '.ear', '.zip']):
                                filename = P(member).name
                                if filename and not member.endswith('/'):
                                    archives[filename] = member
                except Exception as e:
                    print(f"Error reading {zip_path}: {e}")
                return archives
            
            archives1 = list_archives_in_zip(p1)
            archives2 = list_archives_in_zip(p2)
            
            # Generate simple comparison report
            output_filename = f"ascf_naming_convention_report_{timestamp}.txt"
            set1 = set(archives1.keys())
            set2 = set(archives2.keys())
            only_in_1 = sorted(set1 - set2)
            only_in_2 = sorted(set2 - set1)
            in_both = sorted(set1 & set2)
            
            report_lines = []
            report_lines.append("=" * 80)
            report_lines.append("FAST ARCHIVE NAMING CONVENTION COMPARISON")
            report_lines.append("=" * 80)
            report_lines.append(f"Old Build Path: {folder1_path}")
            report_lines.append(f"New Build Path: {folder2_path}")
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
            with open(report_dir / output_filename, 'w', encoding='utf-8') as f:
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
        .status-badge.missing-left {{ background: #fee2e2; color: #991b1b; }}
        .status-badge.missing-right {{ background: #dcfce7; color: #166534; }}
        .status-badge.additional {{ background: #fef3c7; color: #92400e; }}
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
                <div class="path-label">📁 Old Build Path</div>
                <div class="path-value">{html.escape(folder1_path)}</div>
                <div class="path-label">📁 New Build Path</div>
                <div class="path-value">{html.escape(folder2_path)}</div>
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
                        file_info['status_text'] = '🆕 Additional in Left'
                        file_info['row_class'] = 'new-file'
                    else:
                        file_info['status'] = 'missing-right'
                        file_info['status_text'] = '← Missing in Right'
                        file_info['row_class'] = 'only-left'
                else:  # in_2 but not in_1
                    # Check if it's truly new or just a version change
                    is_truly_new = name in truly_new_in_2
                    if is_truly_new:
                        file_info['status'] = 'additional-right'
                        file_info['status_text'] = '🆕 Additional in Right'
                        file_info['row_class'] = 'new-file'
                    else:
                        file_info['status'] = 'missing-left'
                        file_info['status_text'] = '→ Missing in Left'
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
                                <th>Old Build (Left)</th>
                                <th>New Build (Right)</th>
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
                    <h4 style="color: #2d3748; margin: 15px 0 10px 0;">✅ Additional in New Build ({len(truly_new_in_2)} files)</h4>"""
                    for name in truly_new_in_2:
                        path = archives2[name]
                        html_content += f"""
                    <div class="archive-item only-in-2">
                        <div class="archive-name"><span class="badge added">NEW</span> {html.escape(name)} <span class="highlight-new">ADDITIONAL</span></div>
                        <div class="archive-path">📍 {html.escape(path)}</div>
                    </div>"""
                
                if truly_new_in_1:
                    html_content += f"""
                    <h4 style="color: #2d3748; margin: 15px 0 10px 0;">❌ Additional in Old Build ({len(truly_new_in_1)} files)</h4>"""
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
            
            # Removed in New Build section
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
            
            # Added in New Build section
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
            
            html_filename = P(output_filename).with_suffix('.html')
            with open(report_dir / html_filename, 'w', encoding='utf-8') as f:
                f.write(inject_report_section_controls(html_content))
            
            results = True  # Mark as successful
        else:
            # Standard comparison for folders
            try:
                comparer = NamingConventionComparer(folder1_path, folder2_path)
                comparer.similarity_threshold = 0.6
                comparer.use_local_copy = use_local_copy
                comparer.max_retries = max_retries
                comparer.retry_delay = retry_delay
                comparer.should_cleanup_temp_folders = not keep_temp_folders
                
                output_filename = f"ascf_naming_convention_report_{timestamp}.txt"
                output_filepath = str(report_dir / output_filename)
                
                # Run comparison with error handling
                results = comparer.compare_naming_conventions()
                
                # Save reports to PROJECT_ROOT (this will handle empty results gracefully)
                comparer.save_naming_convention_report(output_filepath)
                
                if results:
                    flash(f"Naming convention comparison completed: {len(results)} files analyzed", "success")
                else:
                    flash("Naming convention comparison completed with no results. Check server logs for details.", "warning")
            except Exception as e:
                flash(f"Naming convention comparison failed: {str(e)}", "error")
                print(f"Naming convention comparison error: {e}")
                import traceback
                traceback.print_exc()
                output_filename = f"ascf_naming_convention_report_{timestamp}.txt"
    elif mode == "foss":
        if not jar_name:
            flash("Please enter a JAR name for FOSS/Foundation mode.", "error")
            return redirect(url_for("index"))
        try:
            comparer = JarManifestComparer(folder1_path, folder2_path, jar_name)
            
            output_filename = f"ascf_foss_foundation_report_{timestamp}.txt"
            output_filepath = str(report_dir / output_filename)
            
            # Run comparison with error handling
            result = comparer.compare_versions()
            
            # Save report to PROJECT_ROOT
            comparer.save_report(output_filepath)
            
            if result:
                flash(f"JAR manifest comparison completed for: {jar_name}", "success")
            else:
                flash("JAR manifest comparison completed with no results. Check server logs for details.", "warning")
        except Exception as e:
            flash(f"JAR manifest comparison failed: {str(e)}", "error")
            print(f"JAR manifest comparison error: {e}")
            import traceback
            traceback.print_exc()
            output_filename = f"ascf_foss_foundation_report_{timestamp}.txt"
    else:
        try:
            from crm_compare import CRMFolderComparer

            ascf_class_diff_jar = request.form.get("ascf_class_diff_jar", "").strip()
            ascf_manifest_diff_jar = request.form.get("ascf_manifest_diff_jar", "").strip()
            ascf_config_files_filter = request.form.get("ascf_config_files_filter", "").strip()
            ascf_explicit_size_paths = request.form.get("ascf_explicit_size_paths", "").strip()

            comparer = CRMFolderComparer(folder1_path, folder2_path)
            comparer.use_local_copy = use_local_copy
            comparer.max_retries = max_retries
            comparer.retry_delay = retry_delay
            comparer.should_cleanup_temp_folders = not keep_temp_folders
            comparer.ignore_timestamp_only_differences = ignore_timestamp_only
            comparer.show_missing_only = not show_all_differences
            comparer.exclude_missing_from_folder2 = exclude_missing_folder2
            comparer.rename_folder2_only_files = rename_folder2_only
            comparer.folder2_rename_suffix = rename_suffix
            comparer.suppress_warnings = suppress_warnings
            comparer.fast_mode = True
            comparer.compare_archive_contents = False
            comparer.deep_nested_archives = False
            comparer.compare_clfy_agent_zip = False
            comparer.run_full_tree_compare = True
            comparer.class_diff_jar_name = ascf_class_diff_jar
            comparer.manifest_diff_jar_name = ascf_manifest_diff_jar
            comparer.config_files_filter_query = ascf_config_files_filter
            comparer.explicit_size_compare_query = ascf_explicit_size_paths

            output_filename = f"ascf_patch_comparison_{timestamp}.txt"
            output_filepath = str(report_dir / output_filename)
            
            # Run comparison with error handling
            results = comparer.compare_folders()
            
            # Save reports to PROJECT_ROOT (text + HTML via CRMFolderComparer)
            comparer.save_report(output_filepath)
            
            if results:
                flash(f"Full comparison completed: {len(results)} files analyzed", "success")
            else:
                flash("Full comparison completed with no results. Check server logs for details.", "warning")
        except Exception as e:
            flash(f"Full comparison failed: {str(e)}", "error")
            print(f"Full comparison error: {e}")
            import traceback
            traceback.print_exc()
            output_filename = f"ascf_patch_comparison_{timestamp}.txt"

    # Saved via save_* methods above
    cleanup_old_reports(10)
    # Redirect to HTML report view by default
    try:
        html_name = Path(output_filename).with_suffix(".html").name
        html_path = report_dir / html_name
        
        # Debug: Log the redirect target
        print(f"[DEBUG] Comparison complete. Mode: {mode}")
        print(f"[DEBUG] output_filename: {output_filename}")
        print(f"[DEBUG] html_name: {html_name}")
        print(f"[DEBUG] html_path: {html_path}")
        print(f"[DEBUG] html_path exists: {_path_exists(html_path)}")
    except Exception as e:
        print(f"[ERROR] Error preparing redirect: {e}")
        import traceback
        traceback.print_exc()
        flash(f"Error preparing report view: {e}", "error")
        return redirect(url_for("index"))
    
    # Check if HTML file exists before redirecting
    if not _path_exists(html_path):
        # HTML file wasn't created - create a basic error report
        try:
            error_html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Comparison Error</title>
    <style>
        body {{font-family: system-ui, Arial, sans-serif; padding: 20px; max-width: 800px; margin: 0 auto;}}
        .error {{background: #fee; border: 1px solid #fca5a5; border-radius: 8px; padding: 20px; margin: 20px 0;}}
        h1 {{color: #dc2626;}}
        .back-link {{display: inline-block; margin-top: 20px; padding: 10px 20px; background: #3b82f6; color: white; text-decoration: none; border-radius: 6px;}}
    </style>
</head>
<body>
    <h1>Comparison Report Error</h1>
    <div class="error">
        <h2>The comparison did not complete successfully</h2>
        <p>The HTML report file was not generated. This could be due to:</p>
        <ul>
            <li>Network timeout or connection issues</li>
            <li>Large folder size causing timeout</li>
            <li>Permission denied accessing folders</li>
            <li>Folder paths do not exist or are inaccessible</li>
        </ul>
        <p><strong>Suggestions:</strong></p>
        <ul>
            <li>Check the server console logs for detailed error messages</li>
            <li>Try enabling "Use Local Copy" option for network paths</li>
            <li>Ensure both folder paths are accessible</li>
            <li>For very large folders, the comparison may take several minutes</li>
        </ul>
        <p><strong>Old Build Path:</strong> {folder1_path}</p>
        <p><strong>New Build Path:</strong> {folder2_path}</p>
        <p><strong>Mode:</strong> {mode if mode != "full" else "naming_convention" if mode_naming else "full"}</p>
    </div>
    <a href="/" class="back-link">← Back to Home</a>
</body>
</html>"""
            with open(html_path, 'w', encoding='utf-8') as f:
                f.write(error_html)
            print(f"Created fallback error HTML report: {html_path}")
        except Exception as e:
            print(f"Failed to create fallback HTML report: {e}")
    
    redirect_url = url_for("view_report", filename=html_name)
    print(f"[DEBUG] Redirecting to: {redirect_url}")
    print(f"[DEBUG] HTML file final check - exists: {_path_exists(html_path)}")
    
    # Ensure we have an HTML file to show
    if _path_exists(html_path):
        flash(f"Report ready: {html_name}", "success")

    if app_ascf and not app_crm and not app_uxf and not app_dex and not app_wef:
        cleanup_old_reports(10)
    
    return redirect_open_report_new_tab(redirect_url)


@app.route("/copy", methods=["POST"])
def copy_handler():
    import shutil
    import time
    
    src_choice = request.form.get("copy_source", "folder1")
    folder1_path = normalize_build_path_for_windows(request.form.get("folder1_path", "").strip())
    folder2_path = normalize_build_path_for_windows(request.form.get("folder2_path", "").strip())
    dest_path = normalize_build_path_for_windows(request.form.get("dest_path", "").strip())
    overwrite = bool(request.form.get("overwrite"))

    # Console logging for copy operation
    print("=" * 60)
    print("=== COPY OPERATION STARTED ===")
    print("=" * 60)
    print(f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Source Selection: {src_choice}")
    print(f"Folder 1 Path: {folder1_path}")
    print(f"Folder 2 Path: {folder2_path}")
    print(f"Destination Path: {dest_path}")
    print(f"Overwrite Enabled: {overwrite}")
    print("-" * 60)

    if src_choice not in ("folder1", "folder2"):
        print("ERROR: Invalid source selection")
        flash("Invalid source selection.", "error")
        return redirect(url_for("index"))

    src = folder1_path if src_choice == "folder1" else folder2_path
    print(f"Selected source path: {src}")
    
    if not src or not dest_path:
        print("ERROR: Source and destination paths are required")
        flash("Source and destination paths are required.", "error")
        return redirect(url_for("index"))

    try:
        src_path = Path(src)
        dest_base = Path(dest_path)
        
        print(f"Checking if source exists: {src_path}")
        if not src_path.exists():
            print(f"ERROR: Source does not exist: {src}")
            flash(f"Source does not exist: {src}", "error")
            return redirect(url_for("index"))
        print("Source path verified - exists")

        # Check if source is file or directory
        if src_path.is_dir():
            print("Source is a DIRECTORY")
            try:
                file_count = sum(1 for _ in src_path.rglob('*') if _.is_file())
                dir_count = sum(1 for _ in src_path.rglob('*') if _.is_dir())
                print(f"Source contains: {file_count} files, {dir_count} subdirectories")
            except Exception as count_err:
                print(f"Could not count source contents: {count_err}")
        else:
            print("Source is a FILE")
            try:
                file_size = src_path.stat().st_size
                print(f"File size: {file_size:,} bytes ({file_size/1024/1024:.2f} MB)")
            except Exception as size_err:
                print(f"Could not get file size: {size_err}")

        # Treat destination as a directory to contain the copied item
        # For a folder source, copy into dest_base/FolderName
        # For a file source, copy to dest_base/FileName
        final_dest = dest_base / src_path.name
        print(f"Final destination path: {final_dest}")

        if final_dest.exists():
            print("Destination already exists")
            if overwrite:
                print("Overwrite enabled - removing existing destination")
                try:
                    if final_dest.is_dir():
                        print("Removing existing directory...")
                        shutil.rmtree(final_dest)
                        print("Existing directory removed")
                    else:
                        print("Removing existing file...")
                        final_dest.unlink()
                        print("Existing file removed")
                except Exception as e:
                    print(f"ERROR: Failed to remove existing destination: {e}")
                    flash(f"Failed to remove existing destination: {e}", "error")
                    return redirect(url_for("index"))
            else:
                print("ERROR: Destination exists and overwrite is disabled")
                flash("Destination exists. Enable overwrite to replace.", "error")
                return redirect(url_for("index"))

        # Ensure destination base directory exists
        print(f"Ensuring destination directory exists: {final_dest.parent}")
        final_dest.parent.mkdir(parents=True, exist_ok=True)
        print("Destination directory ready")

        # Perform the copy
        print("-" * 60)
        print("STARTING COPY OPERATION...")
        copy_start_time = time.time()

        if src_path.is_dir():
            print(f"Copying directory tree: {src_path} -> {final_dest}")
            shutil.copytree(src_path, final_dest)
            print("Directory copy completed")
        else:
            print(f"Copying file: {src_path} -> {final_dest}")
            shutil.copy2(src_path, final_dest)
            print("File copy completed")

        copy_end_time = time.time()
        copy_duration = copy_end_time - copy_start_time

        print("-" * 60)
        print("=== COPY OPERATION COMPLETED SUCCESSFULLY ===")
        print(f"Duration: {copy_duration:.2f} seconds")
        print(f"Source: {src}")
        print(f"Destination: {final_dest}")
        print("=" * 60)

        flash(f"Copied {src} -> {str(final_dest)}", "info")
    except Exception as e:
        print("-" * 60)
        print("=== COPY OPERATION FAILED ===")
        print(f"Error: {str(e)}")
        import traceback
        traceback.print_exc()
        print("=" * 60)
        flash(f"Copy failed: {e}", "error")
    return redirect(url_for("index"))


@app.route("/reports")
def reports():
    files = list_report_files(limit=MAX_BROWSE_REPORTS)
    items = []
    for p in files:
        try:
            ts = datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            ts = ""
        html_name = p.with_suffix('.html')
        open_name = html_name.name if html_name.exists() else p.name
        items.append({"name": p.name, "mtime": ts, "open_name": open_name})
    return render_template("reports.html", files=items)
@app.route("/ai-testgen")
def ai_testgen_page():
    return render_template("ai_testgen.html")

@app.route("/ai-testgen/run", methods=["POST"])
def ai_testgen_run():
    from flask import request, jsonify
    data = request.get_json(silent=True) or {}
    query = (data.get("query") or "").strip()
    if not query:
        return jsonify({"error": "No query provided"}), 400
    result = ai_testgen.generate_test_cases(query)
    return jsonify(result)

_ES_CONTINUOUS, _ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001


def _set_awake(required):
    """Windows: ask the system not to idle-sleep while this request thread works.
    Pipeline steps run for minutes with no keyboard/mouse input, and a laptop entering
    standby mid-run drops the Groq connection and the browser's request. This is a
    per-thread request that ends with the HTTP request; no power setting is changed."""
    try:
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(
            _ES_CONTINUOUS | (_ES_SYSTEM_REQUIRED if required else 0))
    except Exception:
        pass  # not Windows, or the call is unavailable


@app.before_request
def _ai_testgen_stay_awake():
    from flask import request
    if request.method == "POST" and request.path.startswith(("/ai-testgen/", "/generate")):
        _set_awake(True)


@app.teardown_request
def _ai_testgen_allow_sleep(_exc=None):
    from flask import request
    if request.method == "POST" and request.path.startswith(("/ai-testgen/", "/generate")):
        _set_awake(False)


@app.route("/generate", methods=["POST"])
def ai_testgen_generate_selenium():
    """intent -> RAG + LangGraph test cases -> Selenium script. The script is registered
    under a script_id and then uses the same /ai-testgen/guardrails, /score, /approve,
    /execute and /repair routes as a Playwright script."""
    import re
    from flask import request, jsonify
    data = request.get_json(silent=True) or {}
    intent = (data.get("intent") or "").strip()
    if not intent:
        return jsonify({"error": "No intent provided"}), 400
    url = (data.get("url") or "").strip()
    if url and not re.match(r"^https?://", url, re.IGNORECASE):
        return jsonify({"error": "Target URL must start with http:// or https://"}), 400
    result = ai_testgen.generate_selenium_script(
        intent, url, (data.get("ui_details") or "").strip()[:20000], bool(data.get("visible")))
    return jsonify(result)

@app.route("/ai-testgen/playwright", methods=["POST"])
def ai_testgen_playwright():
    import re
    from flask import request, jsonify
    data = request.get_json(silent=True) or {}
    query = (data.get("query") or "").strip()
    test_cases = (data.get("test_cases") or "").strip()
    url = (data.get("url") or "").strip()
    if not test_cases:
        return jsonify({"error": "Generate test cases first."}), 400
    if not re.match(r"^https?://", url, re.IGNORECASE):
        return jsonify({"error": "Enter a valid target URL starting with http:// or https://"}), 400
    result = ai_testgen.generate_playwright_script(
        query, test_cases, url, bool(data.get("visible")),
        (data.get("ui_details") or "").strip()[:20000])
    return jsonify(result)

@app.route("/ai-testgen/uimap", methods=["POST"])
def ai_testgen_uimap():
    from flask import request, jsonify
    data = request.get_json(silent=True) or {}
    return jsonify(ai_testgen.fetch_ui_map((data.get("url") or "").strip()))

@app.route("/ai-testgen/guardrails", methods=["POST"])
def ai_testgen_guardrails():
    from flask import request, jsonify
    data = request.get_json(silent=True) or {}
    return jsonify(ai_testgen.run_guardrails(data.get("script_id") or ""))

@app.route("/ai-testgen/score", methods=["POST"])
def ai_testgen_score():
    from flask import request, jsonify
    data = request.get_json(silent=True) or {}
    return jsonify(ai_testgen.score_test_cases(data.get("script_id") or ""))

@app.route("/ai-testgen/approve", methods=["POST"])
def ai_testgen_approve():
    from flask import request, jsonify
    data = request.get_json(silent=True) or {}
    return jsonify(ai_testgen.record_approval(
        data.get("script_id") or "", data.get("decision") or "",
        (data.get("reviewer") or "").strip(), (data.get("note") or "").strip()))

@app.route("/ai-testgen/repair", methods=["POST"])
def ai_testgen_repair():
    from flask import request, jsonify
    data = request.get_json(silent=True) or {}
    return jsonify(ai_testgen.repair_script(data.get("script_id") or ""))

@app.route("/ai-testgen/execute", methods=["POST"])
def ai_testgen_execute():
    from flask import request, jsonify
    data = request.get_json(silent=True) or {}
    return jsonify(ai_testgen.execute_script(data.get("script_id") or ""))

_CRM_JAR_DOWNLOAD_SUFFIXES = frozenset(
    (".jar", ".war", ".ear", ".zip", ".kar", ".rar")
)


def _is_allowed_crm_jar_download_name(name: str) -> bool:
    lower = (name or "").strip().lower()
    if not lower:
        return False
    if lower.endswith(".jar.pack.gz"):
        return True
    return any(lower.endswith(s) for s in _CRM_JAR_DOWNLOAD_SUFFIXES)


def _resolve_zip_member_name(zf: zipfile.ZipFile, member_path: str) -> Optional[str]:
    want = (member_path or "").replace("\\", "/").strip().lstrip("/")
    if not want:
        return None
    names = zf.namelist()
    if want in names:
        return want
    lw = want.lower()
    for n in names:
        if n.replace("\\", "/").lstrip("/").lower() == lw:
            return n
    return None


def _read_virtual_archive_member_bytes(virtual_path: str) -> Tuple[bytes, str]:
    """
    Resolve paths like:
      C:\\...\\SDK.jar!/crm/.../A.jar
      C:\\...\\outer.ear!/inner.war!/lib/B.jar
    and return (bytes, download_name).
    """
    raw = (virtual_path or "").strip().replace("!\\", "!/")
    parts = raw.split("!/")
    if len(parts) < 2:
        raise FileNotFoundError("not a virtual archive path")
    outer = Path(parts[0])
    chain = [p.replace("\\", "/").lstrip("/") for p in parts[1:] if p.strip()]
    if not chain:
        raise FileNotFoundError("missing inner member path")
    if not _path_is_file(outer.resolve()):
        raise FileNotFoundError("outer archive missing")

    with zipfile.ZipFile(str(outer.resolve()), "r") as zf:
        blob = None
        for idx, member in enumerate(chain):
            name = _resolve_zip_member_name(zf, member)
            if not name:
                raise FileNotFoundError(f"member not found: {member}")
            blob = zf.read(name)
            if idx < len(chain) - 1:
                zf = zipfile.ZipFile(io.BytesIO(blob), "r")
        if blob is None:
            raise FileNotFoundError("empty archive chain")
    dl_name = chain[-1].split("/")[-1] or Path(parts[0]).name
    return blob, dl_name




@app.route("/foundations")
def foundations():
    """Foundations Link page."""
    return render_template("foundations.html")


@app.route("/foundations/prepare", methods=["GET", "POST"])
def foundations_prepare():
    """Foundations prepare - create new HF download.html file."""
    import shutil
    
    if request.method == "GET":
        return redirect(url_for("foundations"))
    
    ascf_version = request.form.get("ascf_version", "").strip()
    hf_number = request.form.get("hf_number", "").strip()
    
    # Validate inputs
    if not ascf_version or not hf_number:
        flash("Please provide both ASCF Product Version and HF Number.", "error")
        return redirect(url_for("foundations"))
    
    # Convert version format: 10.24.0.pb01 -> 10240pb01
    version_code = ascf_version.replace('.', '')
    
    # Calculate previous HF number
    try:
        hf_num_int = int(hf_number)
        if hf_num_int < 1:
            raise ValueError("HF number must be at least 01")
        prev_hf_num = f"{hf_num_int - 1:02d}"
    except ValueError as e:
        flash(f"Invalid HF number: {e}", "error")
        return redirect(url_for("foundations"))
    
    # Build file paths
    base_path = r"\\usstlfounds01\ascf\downloads"
    source_filename = f"{version_code}hf{prev_hf_num}_download.html"
    dest_filename = f"{version_code}hf{hf_number}_download.html"
    source_path = os.path.join(base_path, source_filename)
    dest_path = os.path.join(base_path, dest_filename)
    
    result = {
        "success": False,
        "action": "prepare",
        "ascf_version": ascf_version,
        "hf_number": hf_number,
        "prev_hf_num": prev_hf_num,
        "source_filename": source_filename,
        "dest_filename": dest_filename,
        "source_path": source_path,
        "dest_path": dest_path,
        "error": None,
        "warning": None
    }
    
    try:
        # Check if source file exists
        if not os.path.exists(source_path):
            result["error"] = f"Source file not found: {source_path}"
            return render_template("foundations_results.html", result=result)
        
        # Check if destination already exists
        if os.path.exists(dest_path):
            result["warning"] = f"File already exists: {dest_path}"
            return render_template("foundations_results.html", result=result)
        
        # Copy the file
        shutil.copy2(source_path, dest_path)
        result["success"] = True
        
    except PermissionError as e:
        result["error"] = f"Permission denied: {e}"
    except Exception as e:
        result["error"] = f"Error creating file: {e}"
    
    return render_template("foundations_results.html", result=result)


@app.route("/foundations/update-index", methods=["GET", "POST"])
def foundations_update_index():
    """Update Index.html automation page."""
    if request.method == "GET":
        return render_template("foundations_update_index.html")
    
    # POST - perform the automation
    import re
    
    ascf_version = request.form.get("ascf_version", "").strip()
    hf_number = request.form.get("hf_number", "").strip()
    month = request.form.get("month", "").strip()
    year = request.form.get("year", "").strip()
    
    result = {
        "ascf_version": ascf_version,
        "hf_number": hf_number,
        "month": month,
        "year": year,
        "success": False,
        "error": None,
        "info": None
    }
    
    if not ascf_version or not hf_number or not month or not year:
        result["error"] = "Please provide all required fields."
        return render_template("foundations_update_index_result.html", result=result)
    
    try:
        hf_num_int = int(hf_number)
        if hf_num_int < 1:
            raise ValueError("HF number must be at least 01")
        prev_hf_num = f"{hf_num_int - 1:02d}"
        result["prev_hf_num"] = prev_hf_num
    except ValueError as e:
        result["error"] = f"Invalid HF number: {e}"
        return render_template("foundations_update_index_result.html", result=result)
    
    # Build file path
    base_path = r"\\usstlfounds01\ascf\userguides\ReleaseNotes"
    index_file = os.path.join(base_path, "index.html")
    result["file_path"] = index_file
    
    # Version strings
    version_code = ascf_version.replace('.', '')
    prev_version_str = f"{ascf_version}_hf{prev_hf_num}"
    new_version_str = f"{ascf_version}_hf{hf_number}"
    result["new_version_str"] = new_version_str
    
    try:
        if not os.path.exists(index_file):
            result["error"] = f"File not found: {index_file}"
            return render_template("foundations_update_index_result.html", result=result)
        
        with open(index_file, 'r', encoding='utf-8') as f:
            content = f.read()
        
        original_content = content
        
        if new_version_str in content:
            result["info"] = f"Entry for {new_version_str} already exists."
            return render_template("foundations_update_index_result.html", result=result)
        
        if prev_version_str not in content:
            result["error"] = f"Previous entry not found: {prev_version_str}"
            return render_template("foundations_update_index_result.html", result=result)
        
        # Build relnotes link: 9.22.0.pb01 -> relnotes9_22_0_pb01_hfNN.pdf
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
        
        # 2) Fallback: table row format <tr>...</tr>
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
        
        # 3) Fallback: line-by-line (add only first matching line's duplicate, then stop)
        if content == original_content:
            result["error"] = "Could not automatically add entry. Please update manually."
            return render_template("foundations_update_index_result.html", result=result)
        
        with open(index_file, 'w', encoding='utf-8') as f:
            f.write(content)
        
        result["success"] = True
        
    except PermissionError:
        result["error"] = f"Permission denied: {index_file}"
    except Exception as e:
        result["error"] = str(e)
    
    return render_template("foundations_update_index_result.html", result=result)


def _javadoc_path_for_version(ascf_version: str) -> tuple[str, str]:
    """Map ASCF product version to (version_folder_name, javadoc_file_prefix) for userguides path.
    Returns (folder_name, prefix) e.g. ('Version10_24', '1024'), ('Version10_1', '10_1'), ('Version9', '9').
    Supports Version8, Version9, Version10, Version10_1, Version10_24 and any Version* folder (fallback: Version{major})."""
    # Order matters: more specific first (10.24, 10.1, then 10)
    version_mapping = [
        ("10.24.", "Version10_24", "1024"),
        ("10.1.", "Version10_1", "10_1"),
        ("10.", "Version10", "10"),
        ("9.", "Version9", "9"),
        ("8.", "Version8", "8"),
    ]
    for prefix_ver, folder_name, file_prefix in version_mapping:
        if ascf_version.startswith(prefix_ver):
            return (folder_name, file_prefix)
    major = ascf_version.split(".")[0]
    return (f"Version{major}", major)


@app.route("/foundations/update-javadoc", methods=["GET", "POST"])
def foundations_update_javadoc():
    """Update Javadoc.html - add new HF entry to version Javadoc file (ASCF Version + HF Number only)."""
    if request.method == "GET":
        return render_template("foundations_update_javadoc.html")

    import re
    ascf_version = request.form.get("ascf_version", "").strip()
    hf_number = request.form.get("hf_number", "").strip()

    result = {
        "ascf_version": ascf_version,
        "hf_number": hf_number,
        "success": False,
        "error": None,
        "info": None,
    }

    if not ascf_version or not hf_number:
        result["error"] = "Please provide ASCF Product Version and HF Number."
        return render_template("foundations_update_javadoc_result.html", result=result)

    try:
        hf_num_int = int(hf_number)
        if hf_num_int < 1:
            raise ValueError("HF number must be at least 01")
        prev_hf_num = f"{hf_num_int - 1:02d}"
        result["prev_hf_num"] = prev_hf_num
    except ValueError as e:
        result["error"] = f"Invalid HF number: {e}"
        return render_template("foundations_update_javadoc_result.html", result=result)

    # Path: \\usstlfounds01\ascf\userguides\Version9\API\9_Javadoc.html (or Version10, Version10_1, Version8, etc.)
    base_path = r"\\usstlfounds01\ascf\userguides"
    version_folder, javadoc_prefix = _javadoc_path_for_version(ascf_version)
    javadoc_dir = os.path.join(base_path, version_folder, "API")
    javadoc_file = os.path.join(javadoc_dir, f"{javadoc_prefix}_Javadoc.html")
    result["file_path"] = javadoc_file

    version_code = ascf_version.replace(".", "")  # 9.22.0.pb01 -> 9220pb01
    prev_version_str = f"{ascf_version}_hf{prev_hf_num}"
    new_version_str = f"{ascf_version}_hf{hf_number}"
    result["new_version_str"] = new_version_str

    try:
        if not os.path.exists(javadoc_file):
            result["error"] = f"File not found: {javadoc_file}"
            return render_template("foundations_update_javadoc_result.html", result=result)

        with open(javadoc_file, "r", encoding="utf-8") as f:
            content = f.read()

        original_content = content

        if new_version_str in content:
            result["info"] = f"Entry for {new_version_str} already exists in the Javadoc file."
            return render_template("foundations_update_javadoc_result.html", result=result)

        if prev_version_str not in content:
            result["error"] = f"Previous entry not found: {prev_version_str}. Please check the Javadoc file."
            return render_template("foundations_update_javadoc_result.html", result=result)

        # Find <tr>...</tr> containing the previous HF entry and add new <tr> below it (exact same structure)
        tr_pattern = re.compile(
            r"(<tr[^>]*>.*?" + re.escape(prev_version_str) + r".*?</tr>)",
            re.IGNORECASE | re.DOTALL,
        )
        match = tr_pattern.search(content)
        if match:
            old_row = match.group(1)
            # Extract patches_XXXX from previous row href (e.g. patches_9221); if XXX or missing, derive from version (9.22.0 -> 9221)
            patches_match = re.search(r"patches_([^/\"]+)/", old_row)
            patches_part = patches_match.group(1).strip() if patches_match else None
            if not patches_part or patches_part.upper() == "XXX":
                parts = ascf_version.split(".")
                if len(parts) >= 3:
                    try:
                        patch_num = int(parts[2]) + 1  # 9.22.0 -> 9221
                        patches_part = f"{parts[0]}{parts[1]}{patch_num}"
                    except ValueError:
                        patches_part = version_code
                else:
                    patches_part = version_code
            new_row = (
                '<tr align="left">\n'
                '    <td><a name="wp998526"> </a><div class="pCellBody"></div></td>\n'
                '    <td valign="top"><ul><ul><ul class="pSmartList3"><div class="pCellHeadingLite">\n'
                "\t<li><a href=\"file://corp.amdocs.com/shares/St%20Louis/STLFAS2A%20Shares/nt2/AUIF_Website/patches_"
                f"{patches_part}/ASCF_HF_{version_code}hf{hf_number}/Javadoc/index.html\" target=\"_blank\">"
                f"{ascf_version}_hf{hf_number} </li>\n"
                "</tr>"
            )
            content = content.replace(old_row, old_row + "\n" + new_row)
        else:
            result["error"] = f"Could not find a table row containing {prev_version_str}."
            return render_template("foundations_update_javadoc_result.html", result=result)

        if content == original_content:
            result["error"] = "No changes were made. Please check the Javadoc file structure."
            return render_template("foundations_update_javadoc_result.html", result=result)

        with open(javadoc_file, "w", encoding="utf-8") as f:
            f.write(content)

        result["success"] = True
    except PermissionError:
        result["error"] = f"Permission denied: {javadoc_file}"
    except Exception as e:
        result["error"] = str(e)

    return render_template("foundations_update_javadoc_result.html", result=result)


@app.route("/foundations/update-api")
def foundations_update_api():
    """Update API manually page."""
    return render_template("foundations_update_api.html")


@app.route("/admin-tool")
def admin_tool():
    """Administrative Tool page."""
    return render_template("admin_tool.html")


@app.route("/admin-tool/submit", methods=["POST"])
def admin_tool_submit():
    """Administrative Tool submission handler."""
    import re
    
    ascf_version = request.form.get("ascf_version", "").strip()
    hotfix_version = request.form.get("hotfix_version", "").strip()
    release_date = request.form.get("release_date", "").strip()
    download_url = request.form.get("download_url", "").strip()
    release_notes_url = request.form.get("release_notes_url", "").strip()
    
    if not all([ascf_version, hotfix_version, release_date, download_url, release_notes_url]):
        flash("Please provide all required fields.", "error")
        return redirect(url_for("admin_tool"))
    
    result = {
        "ascf_version": ascf_version,
        "hotfix_version": hotfix_version,
        "release_date": release_date,
        "download_url": download_url,
        "release_notes_url": release_notes_url,
        "success": False,
        "manual": False,
        "error": None
    }
    
    try:
        # Try to use Selenium for browser automation
        from selenium import webdriver
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait, Select
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service
        import time as time_module
        
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
            # Navigate directly to Add Version page
            driver.get("http://foundations/asplib/addVersion.asp?Product=ASCF")
            time_module.sleep(3)
            
            # Select ASCF Product Version from dropdown
            version_select = None
            version_selectors = [
                (By.ID, "productVersion"),
                (By.NAME, "productVersion"),
                (By.ID, "version"),
                (By.NAME, "version"),
                (By.XPATH, "//select[contains(@name, 'version') or contains(@name, 'Version')]"),
                (By.TAG_NAME, "select"),
            ]
            
            for selector_type, selector_value in version_selectors:
                try:
                    version_select = WebDriverWait(driver, 3).until(
                        EC.presence_of_element_located((selector_type, selector_value))
                    )
                    if version_select:
                        break
                except:
                    continue
            
            if version_select:
                try:
                    Select(version_select).select_by_visible_text(ascf_version)
                except:
                    try:
                        Select(version_select).select_by_value(ascf_version)
                    except:
                        for option in Select(version_select).options:
                            if ascf_version in option.text or ascf_version in option.get_attribute('value'):
                                option.click()
                                break
            
            time_module.sleep(2)
            
            # Helper function to find and fill input field
            def fill_field(field_names, value):
                for name in field_names:
                    try:
                        field = driver.find_element(By.ID, name)
                        field.clear()
                        field.send_keys(value)
                        return field
                    except:
                        pass
                    try:
                        field = driver.find_element(By.NAME, name)
                        field.clear()
                        field.send_keys(value)
                        return field
                    except:
                        pass
                return None
            
            # Fill other fields FIRST (before hotfix version)
            date_fields = ["releaseDate", "ReleaseDate", "release_date", "date", "Date", "txtDate", "txtReleaseDate", "RelDate", "relDate"]
            fill_field(date_fields, release_date)
            time_module.sleep(0.3)
            
            download_fields = ["downloadUrl", "DownloadUrl", "download_url", "downloadURL", "txtDownload", "txtDownloadUrl", "DownloadURL", "download", "Download"]
            fill_field(download_fields, download_url)
            time_module.sleep(0.3)
            
            notes_fields = ["releaseNotesUrl", "ReleaseNotesUrl", "release_notes_url", "releaseNotesURL", "notesUrl", "txtNotes", "txtReleaseNotes", "ReleaseNotes", "releaseNotes", "Notes", "notes"]
            fill_field(notes_fields, release_notes_url)
            time_module.sleep(0.3)
            
            # Fill Hotfix version field LAST (right before submit)
            hotfix_fields = [
                "hotfixVersion", "HotfixVersion", "hotfix_version", 
                "version", "Version", "txtVersion", "txtHotfix",
                "NewVersion", "newVersion", "new_version", "txtNewVersion",
                "HotfixVer", "hotfixVer", "Hotfix", "hotfix",
                "VersionNumber", "versionNumber", "ver", "Ver"
            ]
            hotfix_element = fill_field(hotfix_fields, hotfix_version)
            
            if not hotfix_element:
                # Try by XPath
                try:
                    hotfix_input = driver.find_element(By.XPATH, "//input[@type='text'][contains(@name, 'ersion') or contains(@id, 'ersion') or contains(@name, 'otfix') or contains(@id, 'otfix')]")
                    hotfix_input.clear()
                    hotfix_input.send_keys(hotfix_version)
                except:
                    pass
            
            time_module.sleep(0.5)
            
            # Click Submit button
            submit_btn = None
            submit_selectors = [
                (By.XPATH, "//input[@value='Add Hotfix']"),
                (By.XPATH, "//input[contains(@value, 'Add')]"),
                (By.XPATH, "//input[@type='submit']"),
                (By.XPATH, "//button[contains(text(), 'Add')]"),
                (By.XPATH, "//button[contains(text(), 'Submit')]"),
                (By.XPATH, "//input[@value='Submit']"),
            ]
            
            for selector_type, selector_value in submit_selectors:
                try:
                    submit_btn = WebDriverWait(driver, 2).until(
                        EC.element_to_be_clickable((selector_type, selector_value))
                    )
                    if submit_btn:
                        break
                except:
                    continue
            
            if submit_btn:
                driver.execute_script("arguments[0].scrollIntoView(true);", submit_btn)
                time_module.sleep(0.5)
                try:
                    submit_btn.click()
                except:
                    driver.execute_script("arguments[0].click();", submit_btn)
            
            time_module.sleep(2)
            
            result["success"] = True
            
        except Exception as e:
            result["error"] = str(e)
            
    except ImportError:
        import webbrowser
        webbrowser.open("http://foundations")
        result["manual"] = True
        
    except Exception as e:
        result["error"] = str(e)
    
    return render_template("admin_tool_result.html", result=result)


@app.route("/foundations/update", methods=["GET", "POST"])
def foundations_update():
    """Foundations update - update content inside HF download.html file."""
    import re
    
    if request.method == "GET":
        return redirect(url_for("foundations"))
    
    ascf_version = request.form.get("ascf_version", "").strip()
    hf_number = request.form.get("hf_number", "").strip()
    month = request.form.get("month", "").strip()
    year = request.form.get("year", "").strip()
    creation_date = request.form.get("creation_date", "").strip()
    
    # Validate inputs
    if not ascf_version or not hf_number or not month or not year or not creation_date:
        flash("Please provide all required fields.", "error")
        return redirect(url_for("foundations"))
    
    # Convert version format: 10.24.0.pb01 -> 10240pb01
    version_code = ascf_version.replace('.', '')
    
    # Calculate previous HF number
    try:
        hf_num_int = int(hf_number)
        if hf_num_int < 1:
            raise ValueError("HF number must be at least 01")
        prev_hf_num = f"{hf_num_int - 1:02d}"
    except ValueError as e:
        flash(f"Invalid HF number: {e}", "error")
        return redirect(url_for("foundations"))
    
    # Build file path
    base_path = r"\\usstlfounds01\ascf\downloads"
    filename = f"{version_code}hf{hf_number}_download.html"
    file_path = os.path.join(base_path, filename)
    
    result = {
        "success": False,
        "action": "update",
        "ascf_version": ascf_version,
        "hf_number": hf_number,
        "prev_hf_num": prev_hf_num,
        "month": month,
        "year": year,
        "creation_date": creation_date,
        "filename": filename,
        "file_path": file_path,
        "updates_made": [],
        "error": None,
        "warning": None
    }
    
    try:
        # Check if file exists
        if not os.path.exists(file_path):
            result["error"] = f"File not found: {file_path}. Please run 'Prepare Foundation Link' first."
            return render_template("foundations_results.html", result=result)
        
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
            result["no_changes"] = True
            result["success"] = True  # It's not an error, just no changes needed
            return render_template("foundations_results.html", result=result)
        
        # Write updated content back to file
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(content)
        
        result["success"] = True
        result["updates_made"] = updates_made
        
    except PermissionError as e:
        result["error"] = f"Permission denied: {e}"
    except Exception as e:
        result["error"] = f"Error updating file: {e}"
    
    return render_template("foundations_results.html", result=result)

@app.route("/download-crm-jar")
def download_crm_jar():
    """
    Serve a JAR (or other archive) from a signed token embedded in CRM HTML reports.
    The file is read from the server’s disk (where the comparison ran); the browser saves it.
    """
    token = (request.args.get("token") or "").strip()
    if not token:
        abort(400)
    try:
        payload = load_jar_download_payload(token)
    except (ValueError, BadSignature, SignatureExpired):
        abort(400)
    candidates: list[str] = []
    for k in ("p", "s"):
        v = (payload.get(k) or "").strip()
        if v and v not in candidates:
            candidates.append(v)
    if not candidates:
        abort(404)

    for raw in candidates:
        if "!/" in raw or "!\\" in raw:
            try:
                data, name = _read_virtual_archive_member_bytes(raw)
            except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, FileNotFoundError, KeyError):
                continue
            if not _is_allowed_crm_jar_download_name(name):
                continue
            return send_file(
                io.BytesIO(data),
                as_attachment=True,
                download_name=name,
                mimetype="application/octet-stream",
            )
        p = Path(raw)
        try:
            resolved = p.resolve()
        except (OSError, ValueError, RuntimeError):
            continue
        if not _path_is_file(resolved):
            continue
        if not _is_allowed_crm_jar_download_name(resolved.name):
            continue
        if not is_path_allowed_for_jar_download(str(resolved), PROJECT_ROOT):
            continue
        return send_file(
            str(resolved),
            as_attachment=True,
            download_name=resolved.name,
            mimetype="application/octet-stream",
        )
    abort(404)


@app.route("/download/<path:filename>")
def download_report(filename: str):
    if not is_allowed_report_name(filename):
        abort(404)
    machine, _, _ = _request_report_dirs()
    candidate = resolve_report_path(filename, machine, PROJECT_ROOT)
    if not candidate or not candidate.is_file():
        abort(404)
    return send_file(candidate, as_attachment=True, download_name=filename)


@app.route("/view/<path:filename>")
def view_report(filename: str):
    if not is_allowed_report_name(filename):
        abort(404)
    machine, _, _ = _request_report_dirs()
    candidate = resolve_report_path(filename, machine, PROJECT_ROOT)
    if not candidate or not candidate.is_file():
        abort(404)
    mimetype = "text/html" if filename.lower().endswith(".html") else "text/plain"
    resp = make_response(send_file(candidate, mimetype=mimetype, as_attachment=False))
    # Avoid stale HTML when reopening the same report from Browse / Recent (browser disk cache / bfcache).
    if filename.lower().endswith(".html"):
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
    return resp


@app.route("/stop", methods=["POST"]) 
def stop_handler():
    # No-op: keep the server running; UI will refresh
    return ("", 204)


if __name__ == "__main__":
    import argparse

    from werkzeug.serving import make_server

    parser = argparse.ArgumentParser(description="Folder comparison web server")
    parser.add_argument(
        "--host",
        default=os.environ.get("HOST", "127.0.0.1"),
        help="Bind address. Default 127.0.0.1 (local only). Use 0.0.0.0 with CRM_API_TOKEN for LAN.",
    )
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "5000")))
    parser.add_argument(
        "--ssl-cert",
        default=os.environ.get("SSL_CERTFILE"),
        metavar="FILE",
        help="PEM certificate for HTTPS (use with --ssl-key)",
    )
    parser.add_argument(
        "--ssl-key",
        default=os.environ.get("SSL_KEYFILE"),
        metavar="FILE",
        help="PEM private key for HTTPS (use with --ssl-cert)",
    )
    parser.add_argument(
        "--adhoc-ssl",
        action="store_true",
        help="HTTPS with Werkzeug self-signed certificate (dev only; browser will warn)",
    )
    parser.add_argument(
        "--https",
        action="store_true",
        help="Enable HTTPS using .ssl/server.crt and .ssl/server.key when cert flags are not set. HTTP stays on --port.",
    )
    parser.add_argument(
        "--https-port",
        type=int,
        default=None,
        metavar="PORT",
        help="HTTPS port when TLS is enabled (default: env HTTPS_PORT or 5443)",
    )
    args = parser.parse_args()

    debug = bool(os.environ.get("FLASK_DEBUG", "0") == "1")

    validate_startup_security(args.host)

    ssl_cert = args.ssl_cert
    ssl_key = args.ssl_key
    if args.https and not (ssl_cert and ssl_key):
        dc = PROJECT_ROOT / ".ssl" / "server.crt"
        dk = PROJECT_ROOT / ".ssl" / "server.key"
        if dc.is_file() and dk.is_file():
            ssl_cert, ssl_key = str(dc), str(dk)

    ssl_context = None
    if args.adhoc_ssl:
        ssl_context = "adhoc"
    elif ssl_cert and ssl_key:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        try:
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        except AttributeError:
            ctx.options |= ssl.OP_NO_SSLv3 | ssl.OP_NO_TLSv1 | ssl.OP_NO_TLSv1_1
        ctx.load_cert_chain(ssl_cert, ssl_key)
        ssl_context = ctx
    elif ssl_cert or ssl_key or args.ssl_cert or args.ssl_key:
        print("ERROR: Provide both --ssl-cert and --ssl-key, or use --adhoc-ssl for a dev certificate.")
        sys.exit(1)

    https_port = int(args.https_port if args.https_port is not None else os.environ.get("HTTPS_PORT", "5443"))

    # Increase request timeout for long-running comparisons
    # Note: Flask development server has limited timeout control
    # For production, use a proper WSGI server like gunicorn with --timeout flag
    print("=" * 80)
    print("Starting Folder Comparison Web Server")
    print("=" * 80)

    public = (os.environ.get("PUBLIC_HOST") or "").strip()
    lan_ips: list[str] = []
    if args.host in ("0.0.0.0", "::", ""):
        try:
            import socket as _sock

            lan_ips = sorted({
                ip for info in _sock.getaddrinfo(_sock.gethostname(), None, _sock.AF_INET)
                for ip in [str(info[4][0])]
                if ip and not ip.startswith("127.")
            })
            if not lan_ips:
                try:
                    probe = _sock.socket(_sock.AF_INET, _sock.SOCK_DGRAM)
                    probe.connect(("8.8.8.8", 80))
                    lan_ips = [str(probe.getsockname()[0])]
                    probe.close()
                except OSError:
                    lan_ips = []
            display_host = public or (lan_ips[0] if lan_ips else "127.0.0.1")
        except Exception:
            display_host = public or "127.0.0.1"
            lan_ips = []
    else:
        display_host = args.host

    def _print_urls(http_p, https_p=None):
        print(f"HTTP:  http://127.0.0.1:{http_p}/crm  (local)")
        if args.host in ("0.0.0.0", "::", ""):
            if public:
                print(f"HTTP:  http://{public}:{http_p}/crm  (network)")
            for ip in lan_ips:
                if ip != public:
                    print(f"HTTP:  http://{ip}:{http_p}/crm  (network)")
        else:
            print(f"HTTP:  http://{display_host}:{http_p}/crm")
        if https_p is not None:
            print(f"HTTPS: https://127.0.0.1:{https_p}/crm  (local)")
            if args.host in ("0.0.0.0", "::", ""):
                if public:
                    print(f"HTTPS: https://{public}:{https_p}/crm  (network)")
                for ip in lan_ips:
                    if ip != public:
                        print(f"HTTPS: https://{ip}:{https_p}/crm  (network)")
            else:
                print(f"HTTPS: https://{display_host}:{https_p}/crm")
        print("Note: For large folder comparisons on network paths, the comparison may take")
        print("      several minutes. The browser will redirect automatically when complete.")
        print("      If the page appears stuck, check the console logs for progress.")
        print("=" * 80)

    if ssl_context is not None:
        if https_port == args.port:
            print("ERROR: HTTPS port must differ from HTTP port (e.g. --port 5000 --https-port 5443).")
            sys.exit(1)
        http_srv = make_server(args.host, args.port, app, threaded=True)
        https_srv = make_server(args.host, https_port, app, threaded=True, ssl_context=ssl_context)

        def _run_http() -> None:
            http_srv.serve_forever()

        threading.Thread(target=_run_http, name="flask-http", daemon=True).start()
        _print_urls(args.port, https_port)
        try:
            https_srv.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            http_srv.shutdown()
            https_srv.shutdown()
    else:
        _print_urls(args.port, None)
        app.run(host=args.host, port=args.port, debug=debug, threaded=True)



