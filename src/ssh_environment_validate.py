#!/usr/bin/env python3
"""
Remote environment validation over SSH (MobaXterm-style: host, user, password, path).

Uses paramiko when available; otherwise falls back to Windows OpenSSH (ssh.exe)
with SSH_ASKPASS, or PuTTY plink.exe if present under C:\\paramiko_wheels.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import paramiko

    _PARAMIKO_IMPORT_ERROR = ""
except ImportError as exc:  # pragma: no cover
    paramiko = None
    _PARAMIKO_IMPORT_ERROR = str(exc)


def _paramiko_usable() -> bool:
    if paramiko is None:
        return False
    try:
        from cryptography.hazmat.primitives.ciphers import algorithms  # noqa: F401

        return True
    except Exception:
        return False


def _shell_quote(path: str) -> str:
    return shlex.quote(path)


def _parse_manifest_text(text: str) -> Tuple[str, str]:
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


class _SubprocessSshSession:
    """Run remote commands via plink or OpenSSH subprocess."""

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        port: int,
        backend: str,
        executable: str,
        askpass_path: Optional[str] = None,
    ) -> None:
        self.host = host
        self.username = username
        self.password = password
        self.port = port
        self.backend = backend
        self.executable = executable
        self.askpass_path = askpass_path
        self._sftp = None

    def close(self) -> None:
        if self._sftp:
            try:
                self._sftp.close()
            except Exception:
                pass
        if self.askpass_path and os.path.isfile(self.askpass_path):
            try:
                os.remove(self.askpass_path)
            except OSError:
                pass

    def exec_command(self, command: str, timeout: int = 120) -> Tuple[int, str, str]:
        if self.backend == "plink":
            target = f"{self.username}@{self.host}"
            args = [
                self.executable,
                "-ssh",
                target,
                "-P",
                str(self.port),
                "-pw",
                self.password,
                "-batch",
                "-no-antispoof",
                command,
            ]
            env = os.environ.copy()
        else:
            args = [
                self.executable,
                "-p",
                str(self.port),
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "UserKnownHostsFile=NUL",
                "-o",
                "PreferredAuthentications=password,keyboard-interactive",
                "-o",
                "PubkeyAuthentication=no",
                "-o",
                "NumberOfPasswordPrompts=1",
                f"{self.username}@{self.host}",
                command,
            ]
            env = os.environ.copy()
            if self.askpass_path:
                env["SSH_ASKPASS"] = self.askpass_path
                env["SSH_ASKPASS_REQUIRE"] = "force"
                env.setdefault("DISPLAY", "dummy")

        try:
            proc = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return proc.returncode, proc.stdout or "", proc.stderr or ""
        except subprocess.TimeoutExpired as exc:
            raise ValueError(f"SSH command timed out after {timeout}s") from exc
        except OSError as exc:
            raise ValueError(f"SSH command failed: {exc}") from exc

    def open_sftp(self) -> "_SubprocessSshSession":
        if _paramiko_usable():
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(
                hostname=self.host,
                port=self.port,
                username=self.username,
                password=self.password,
                timeout=30,
                allow_agent=False,
                look_for_keys=False,
            )
            self._sftp = client.open_sftp()
        return self


class _ParamikoSshSession:
    def __init__(self, client, sftp=None) -> None:
        self._client = client
        self._sftp = sftp

    def exec_command(self, command: str, timeout: int = 120) -> Tuple[int, str, str]:
        _stdin, stdout, stderr = self._client.exec_command(command, timeout=timeout)
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        code = stdout.channel.recv_exit_status()
        return code, out, err

    def open_sftp(self) -> "_ParamikoSshSession":
        if self._sftp is None:
            self._sftp = self._client.open_sftp()
        return self

    @property
    def sftp(self):
        return self._sftp

    def close(self) -> None:
        if self._sftp:
            try:
                self._sftp.close()
            except Exception:
                pass
        try:
            self._client.close()
        except Exception:
            pass


def _find_plink() -> Optional[str]:
    candidates = [
        os.environ.get("PLINK_PATH", ""),
        r"C:\paramiko_wheels\plink.exe",
        r"C:\Program Files\PuTTY\plink.exe",
        r"C:\Program Files (x86)\PuTTY\plink.exe",
    ]
    for moba_root in (
        os.environ.get("MOBAXTERM_HOME", ""),
        os.path.expandvars(r"%LOCALAPPDATA%\MobaXterm"),
    ):
        if moba_root:
            candidates.append(os.path.join(moba_root, "plink.exe"))
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return None


def _find_openssh() -> Optional[str]:
    for path in (
        r"C:\Windows\System32\OpenSSH\ssh.exe",
        "ssh",
    ):
        if path == "ssh" or os.path.isfile(path):
            return path
    return None


def _create_askpass_script(password: str) -> str:
    fd, path = tempfile.mkstemp(prefix="ssh_askpass_", suffix=".cmd")
    os.close(fd)
    # Escape special chars for cmd echo
    safe = password.replace("^", "^^").replace("%", "%%").replace("&", "^&")
    with open(path, "w", encoding="utf-8", newline="\r\n") as f:
        f.write("@echo off\r\n")
        f.write(f"echo {safe}\r\n")
    return path


def _connect_ssh(
    host: str,
    username: str,
    password: str,
    port: int,
    timeout: int = 30,
) -> Any:
    host = (host or "").strip()
    username = (username or "").strip()
    password = password or ""

    if not host:
        raise ValueError("Host / IP address is required for SSH validation.")
    if not username:
        raise ValueError("Username is required for SSH validation.")
    if not password:
        raise ValueError("Password is required for SSH validation.")

    if _paramiko_usable():
        from enterprise_security import ssh_strict_host_keys

        client = paramiko.SSHClient()
        try:
            client.load_system_host_keys()
        except Exception:
            pass
        if ssh_strict_host_keys():
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
        else:
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                hostname=host,
                port=int(port or 22),
                username=username,
                password=password,
                timeout=timeout,
                allow_agent=False,
                look_for_keys=False,
            )
        except Exception as exc:
            raise ValueError(f"SSH connection failed ({host}:{port}): {exc}") from exc
        return _ParamikoSshSession(client)

    plink = _find_plink()
    if plink:
        session = _SubprocessSshSession(
            host, username, password, int(port or 22), "plink", plink
        )
        code, out, err = session.exec_command("echo connected", timeout=timeout)
        if code != 0 or "connected" not in out:
            session.close()
            raise ValueError(
                f"SSH connection failed via plink ({host}:{port}): {err or out or 'no response'}"
            )
        return session

    ssh_exe = _find_openssh()
    if ssh_exe:
        askpass = _create_askpass_script(password)
        session = _SubprocessSshSession(
            host,
            username,
            password,
            int(port or 22),
            "openssh",
            ssh_exe,
            askpass_path=askpass,
        )
        code, out, err = session.exec_command("echo connected", timeout=timeout)
        if code != 0 or "connected" not in out:
            session.close()
            detail = (err or out or "no response").strip()
            raise ValueError(
                f"SSH connection failed via OpenSSH ({host}:{port}): {detail}"
            )
        return session

    raise ValueError(
        "SSH support is unavailable: paramiko could not load (missing cffi for Python 3.14). "
        "Add cffi-2.0.0-cp314-cp314-win_amd64.whl to C:\\paramiko_wheels and re-run "
        "scripts\\install_paramiko_offline.ps1, or copy plink.exe from MobaXterm to "
        "C:\\paramiko_wheels\\plink.exe."
    )


def _remote_find_jars(session, remote_path: str) -> Dict[str, List[str]]:
    root = remote_path.rstrip("/") or "/"
    cmd = f"find {_shell_quote(root)} -type f -name '*.jar' 2>/dev/null"
    _code, out, _err = session.exec_command(cmd, timeout=600)
    index: Dict[str, List[str]] = {}
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        name = line.rsplit("/", 1)[-1].lower()
        index.setdefault(name, []).append(line)
    return index


def _remote_read_manifest(session, sftp, jar_path: str) -> Tuple[str, str]:
    quoted = _shell_quote(jar_path)

    cmd = f"unzip -p {quoted} META-INF/MANIFEST.MF 2>/dev/null"
    code, out, _err = session.exec_command(cmd, timeout=90)
    if code == 0 and out.strip():
        version, err = _parse_manifest_text(out)
        if version or err:
            return version, err

    py_cmd = (
        "python3 -c "
        + shlex.quote(
            "import zipfile,sys;"
            "z=zipfile.ZipFile(sys.argv[1]);"
            "print(z.read('META-INF/MANIFEST.MF').decode('utf-8','replace'))"
        )
        + f" {quoted} 2>/dev/null"
    )
    code, out, _err = session.exec_command(py_cmd, timeout=90)
    if code == 0 and out.strip():
        version, err = _parse_manifest_text(out)
        if version or err:
            return version, err

    if sftp is None:
        return "", "Could not read manifest remotely (unzip/python3 unavailable)"

    try:
        attr = sftp.stat(jar_path)
        max_bytes = 80 * 1024 * 1024
        if attr.st_size > max_bytes:
            return "", "JAR too large to download for manifest read"
        buf = BytesIO()
        sftp.getfo(jar_path, buf)
        from environment_jar_validate import read_implementation_version_bytes

        return read_implementation_version_bytes(buf.getvalue())
    except Exception as exc:
        return "", f"Remote read failed: {exc}"


def validate_environment_jars_ssh(
    host: str,
    username: str,
    password: str,
    remote_path: str,
    port: int = 22,
    timeout: int = 30,
) -> Tuple[Dict[str, List[str]], Any, Any]:
    """
    Connect via SSH, index jars under remote_path, return (jar_index, session, sftp).
    Caller must close session when done.
    """
    remote_path = (remote_path or "").strip()
    if not remote_path:
        raise ValueError("Remote environment path is required.")

    session = _connect_ssh(host, username, password, port, timeout=timeout)

    code, _out, err = session.exec_command(
        f"test -d {_shell_quote(remote_path)} && echo ok", timeout=60
    )
    if code != 0:
        session.close()
        raise ValueError(f"Remote path does not exist or is not accessible: {remote_path}. {err}".strip())

    sftp = None
    if isinstance(session, _ParamikoSshSession):
        try:
            sftp = session.open_sftp().sftp
        except Exception:
            sftp = None
    elif _paramiko_usable():
        try:
            sftp = session.open_sftp()._sftp
        except Exception:
            sftp = None

    jar_index = _remote_find_jars(session, remote_path)
    return jar_index, session, sftp


def read_remote_jar_manifest(session, sftp, jar_path: str) -> Tuple[str, str]:
    return _remote_read_manifest(session, sftp, jar_path)


def normalize_class_grep_pattern(class_query: str) -> str:
    """Turn a class file name or FQCN into a grep pattern for jar contents."""
    q = (class_query or "").strip()
    if not q:
        return ""
    if q.lower().endswith(".class"):
        q = q[:-6].strip()
    return q


def _parse_grep_jar_paths(raw_output: str, remote_root: str) -> List[str]:
    """Parse `find … -exec grep -Hsli` output into absolute remote jar paths."""
    root = (remote_root or "").rstrip("/") or "/"
    paths: List[str] = []
    for line in (raw_output or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if ":" in line:
            candidate = line.split(":", 1)[0].strip()
            if candidate.lower().endswith(".jar"):
                line = candidate
        if not line.lower().endswith(".jar"):
            continue
        if line.startswith("./"):
            line = f"{root}/{line[2:]}"
        elif not line.startswith("/"):
            line = f"{root}/{line.lstrip('/')}"
        paths.append(line)
    return sorted(set(paths), key=lambda p: p.lower())


def find_jars_containing_class_ssh(
    host: str,
    username: str,
    password: str,
    remote_path: str,
    class_query: str,
    port: int = 22,
    timeout: int = 30,
    command_timeout: int = 900,
    max_results: int = 50,
) -> Dict[str, Any]:
    """
    SSH to a deployment host and run:
      find . -name '*.jar' -exec grep -Hsli <pattern> {} \\;
    under remote_path. Returns jar basenames for copy into class-diff fields.
    """
    pattern = normalize_class_grep_pattern(class_query)
    empty = {
        "query": pattern,
        "jars": [],
        "suggestions": "",
        "remote_host": (host or "").strip(),
        "remote_path": (remote_path or "").strip(),
    }
    if not pattern:
        return {
            **empty,
            "ok": False,
            "error": "Enter a class name or FQCN (e.g. CreateCustomerAccountXB.class or com.example.MyClass).",
        }

    remote_path = (remote_path or "").strip()
    if not remote_path:
        return {**empty, "ok": False, "error": "Remote environment path is required."}

    host = (host or "").strip()
    username = (username or "").strip()
    password = password or ""
    if not host:
        return {**empty, "ok": False, "error": "Host / IP address is required for SSH lookup."}
    if not username:
        return {**empty, "ok": False, "error": "SSH username is required for lookup."}
    if not password:
        return {**empty, "ok": False, "error": "SSH password is required for lookup."}

    session = None
    try:
        session = _connect_ssh(host, username, password, int(port or 22), timeout=timeout)
        root = remote_path.rstrip("/") or "/"
        code, _out, err = session.exec_command(
            f"test -d {_shell_quote(root)} && echo ok", timeout=60
        )
        if code != 0:
            detail = (err or _out or "").strip()
            return {
                **empty,
                "ok": False,
                "error": f"Remote path does not exist or is not accessible: {root}. {detail}".strip(),
                "remote_path": root,
            }

        cmd = (
            f"cd {_shell_quote(root)} && "
            f"find . -name '*.jar' -exec grep -Hsli {_shell_quote(pattern)} {{}} \\;"
        )
        code, out, err = session.exec_command(cmd, timeout=command_timeout)
        if code not in (0, 1):
            detail = (err or out or f"exit code {code}").strip()
            return {
                **empty,
                "ok": False,
                "error": f"Remote grep search failed: {detail}",
                "remote_path": root,
            }

        jar_paths = _parse_grep_jar_paths(out, root)
        seen_jar: set[str] = set()
        jars_out: List[Dict[str, str]] = []
        for fp in jar_paths:
            basename = fp.rsplit("/", 1)[-1]
            key = basename.lower()
            if key in seen_jar:
                continue
            seen_jar.add(key)
            jars_out.append({"jar": basename, "container": fp, "member": ""})
            if len(jars_out) >= max_results:
                break

        suggestions = ", ".join(j["jar"] for j in jars_out)
        if jars_out:
            print(
                f"[CRM] SSH class lookup {pattern!r} on {host}:{root}: "
                f"{len(jars_out)} JAR(s) — {suggestions}",
                flush=True,
            )
        else:
            print(
                f"[CRM] SSH class lookup {pattern!r} on {host}:{root}: not found",
                flush=True,
            )
        return {
            "ok": bool(jars_out),
            "error": None
            if jars_out
            else f"No JAR containing {pattern!r} was found under {root}.",
            "query": pattern,
            "jars": jars_out,
            "suggestions": suggestions,
            "remote_host": host,
            "remote_path": root,
        }
    except ValueError as exc:
        return {**empty, "ok": False, "error": str(exc)}
    finally:
        if session is not None:
            session.close()
