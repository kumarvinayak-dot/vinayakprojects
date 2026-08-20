#!/usr/bin/env python3
"""
Run builtin_server under Windows Task Scheduler (SYSTEM or Interactive user task).

Logs to logs/builtin_server_service.log in the project directory.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = PROJECT_ROOT / "logs"
LOG_FILE = LOG_DIR / "builtin_server_service.log"


def _load_env() -> None:
    src = PROJECT_ROOT / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    from enterprise_security import load_dotenv  # noqa: WPS433

    load_dotenv(str(PROJECT_ROOT))
    os.environ.setdefault("CRM_PROJECT_ROOT", str(PROJECT_ROOT))


class _Tee:
    """Write to log file and keep a reference for flush."""

    def __init__(self, path: Path):
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self._file = open(path, "a", encoding="utf-8", buffering=1)

    def write(self, data: str) -> int:
        if not data:
            return 0
        self._file.write(data)
        return len(data)

    def flush(self) -> None:
        self._file.flush()

    def fileno(self):
        return self._file.fileno()


def _setup_service_logging() -> None:
    tee = _Tee(LOG_FILE)
    sys.stdout = tee  # type: ignore[assignment]
    sys.stderr = tee  # type: ignore[assignment]
    print(f"\n{'=' * 72}")
    print(f"CRM Compare service runner — {datetime.now().isoformat(timespec='seconds')}")
    print(f"PID={os.getpid()}  CWD={PROJECT_ROOT}")
    print(f"{'=' * 72}")


def main() -> int:
    import argparse

    _load_env()

    parser = argparse.ArgumentParser(description="CRM Compare persistent service runner")
    parser.add_argument("--host", default=(os.environ.get("HOST") or "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT") or "5000"))
    parser.add_argument("--https-port", type=int, default=int(os.environ.get("HTTPS_PORT") or "5443"))
    parser.add_argument("--https", action="store_true", default=None, help="Enable HTTPS when certs exist")
    parser.add_argument("--no-https", action="store_true", help="Disable HTTPS")
    args = parser.parse_args()

    os.chdir(PROJECT_ROOT)
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    src = PROJECT_ROOT / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))

    _setup_service_logging()

    host = str(args.host or "0.0.0.0").strip()
    port = int(args.port)
    https_port = int(args.https_port)
    if args.no_https:
        use_https = False
    elif args.https:
        use_https = True
    else:
        use_https = str(os.environ.get("CRM_USE_HTTPS", "0")).strip().lower() in ("1", "true", "yes")
    ssl_cert = PROJECT_ROOT / ".ssl" / "server.crt"
    ssl_key = PROJECT_ROOT / ".ssl" / "server.key"
    if use_https and (not ssl_cert.is_file() or not ssl_key.is_file()):
        print("[WARN] HTTPS requested but .ssl certs missing — HTTP only.")
        use_https = False

    print(f"Starting server host={host} http_port={port} https={'yes' if use_https else 'no'}")
    if use_https:
        print(f"HTTPS port={https_port}")

    import builtin_server  # noqa: WPS433

    try:
        builtin_server.run(
            host=host,
            port=port,
            skip_demos=True,
            https_port=https_port if use_https else None,
            use_default_ssl_if_present=use_https,
        )
    except Exception as exc:
        print(f"[FATAL] Server exited with error: {exc}")
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
