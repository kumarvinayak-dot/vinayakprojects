"""
Native Windows folder picker for the web UI (localhost only).

Uses tkinter from a subprocess so the Flask/builtin server thread is not blocked
by the modal dialog. Browsers cannot expose full UNC paths from <input type=file>;
this opens the real OS folder browser instead.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from typing import Optional

from crm_build_paths import normalize_build_path_for_windows


def _is_safe_explorer_folder_path(p: str) -> bool:
    """Reject traversal and shell metacharacters; allow UNC and drive-letter paths."""
    if not p or "\x00" in p:
        return False
    if ".." in p:
        return False
    for ch in "|&<>^\n\r":
        if ch in p:
            return False
    if p.startswith("\\\\"):
        return len(p) >= 3
    if len(p) >= 2 and p[1] == ":" and p[0].isalpha():
        return True
    return False


def open_folder_in_explorer(path: str) -> None:
    """
    Open a folder in Windows Explorer, or focus a file in its folder (UNC or ``D:\\...``).
    For files (e.g. ``.jar``), uses ``explorer /select,`` so Explorer opens instead of the
    default handler (which might launch Java).
    Raises RuntimeError / ValueError on failure or unsupported OS.
    """
    if os.name != "nt":
        raise RuntimeError("Opening Explorer requires Windows.")

    p = normalize_build_path_for_windows((path or "").strip())
    if not p:
        raise ValueError("Empty path.")
    if not _is_safe_explorer_folder_path(p):
        raise ValueError("Path is not allowed (use a UNC or drive letter folder path).")

    norm = os.path.normpath(p)

    def _explorer_select(target: str) -> None:
        # explorer.exe exit codes are unreliable; rely on OSError from subprocess if spawn fails.
        subprocess.run(["explorer", "/select,", target], timeout=30)

    try:
        if os.path.isfile(norm):
            _explorer_select(norm)
            return
        if os.path.isdir(norm):
            os.startfile(norm)  # type: ignore[attr-defined]
            return
        parent = os.path.dirname(norm)
        if parent and os.path.isdir(parent):
            _explorer_select(norm)
            return
        raise ValueError("Path does not exist or is not reachable from this PC.")
    except OSError as e:
        raise RuntimeError(f"Could not open Explorer: {e}") from e


def pick_folder(initial_dir: Optional[str] = None) -> Optional[str]:
    """
    Show a folder picker. Returns selected path, None if cancelled or empty.
    On non-Windows, returns None.
    """
    if os.name != "nt":
        return None

    init = normalize_build_path_for_windows((initial_dir or "").strip())
    if init and not os.path.isdir(init):
        init = ""

    code = textwrap.dedent(
        f"""
        import sys
        import os
        try:
            import tkinter as tk
            from tkinter import filedialog
        except ImportError:
            sys.exit(2)
        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)
        kw = dict(title='Select CRM build folder')
        init = {repr(init)}
        if init and os.path.isdir(init):
            kw['initialdir'] = init
        try:
            path = filedialog.askdirectory(**kw)
        finally:
            root.destroy()
        if path:
            print(path, end='')
        """
    )

    try:
        r = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=600,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("Folder picker timed out") from None

    if r.returncode == 2:
        raise RuntimeError(
            "tkinter is not available — install Python with tcl/tk or use a full Windows Python build"
        )
    if r.returncode != 0:
        err = (r.stderr or "").strip() or f"exit {r.returncode}"
        raise RuntimeError(err)

    path = (r.stdout or "").strip()
    return path if path else None
