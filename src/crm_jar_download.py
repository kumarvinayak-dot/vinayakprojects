"""
Signed download tokens for CRM report HTML: lets the user fetch compared JARs
through the local Flask app without pasting paths into other tools.

Uses stdlib only (HMAC + JSON + base64 + time) so no extra packages are required.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from pathlib import Path
from typing import Any, Optional

_SALT = b"crm-jar-dl-v1"
# Reports may be re-opened days later from /view/
_MAX_AGE_SEC = 60 * 60 * 24 * 30


class BadSignature(ValueError):
    """Token signature invalid or payload malformed."""


class SignatureExpired(BadSignature):
    """Token is older than max age."""


def get_jar_download_secret() -> str:
    return os.environ.get("CRM_JAR_DOWNLOAD_SECRET") or os.environ.get(
        "FLASK_SECRET_KEY", "local-dev-secret"
    )


def _signing_key() -> bytes:
    secret = get_jar_download_secret().encode("utf-8", errors="replace")
    return hmac.new(secret, _SALT, hashlib.sha256).digest()


def _b64url_encode(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def make_jar_download_token(abs_path: str, source_path: Optional[str] = None) -> str:
    """HMAC-signed token encoding a primary path plus optional source/virtual fallback path."""
    p = (abs_path or "").strip()
    if not p:
        raise ValueError("empty path")
    s = (source_path or "").strip()
    now = int(time.time())
    payload: dict[str, Any] = {"p": p, "t": now}
    if s and s != p:
        payload["s"] = s
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
    b = body.encode("utf-8")
    sk = _signing_key()
    mac = hmac.new(sk, b, hashlib.sha256).hexdigest()
    return _b64url_encode(b) + "." + mac


def load_jar_download_payload(token: str) -> dict[str, str]:
    """Return token payload with validated primary path and optional fallback/source path."""
    t = (token or "").strip()
    if not t:
        raise ValueError("missing token")
    try:
        payload_b64, mac = t.rsplit(".", 1)
    except ValueError as e:
        raise BadSignature("malformed token") from e
    try:
        b = _b64url_decode(payload_b64)
    except Exception as e:
        raise BadSignature("invalid payload") from e
    sk = _signing_key()
    expected = hmac.new(sk, b, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, mac):
        raise BadSignature("bad signature")
    try:
        data: dict[str, Any] = json.loads(b.decode("utf-8"))
    except Exception as e:
        raise BadSignature("invalid json") from e
    t0 = int(data.get("t") or 0)
    if t0 <= 0:
        raise BadSignature("missing timestamp")
    if int(time.time()) - t0 > _MAX_AGE_SEC:
        raise SignatureExpired("token expired")
    p = str(data.get("p") or "").strip()
    if not p:
        raise ValueError("empty path in token")
    s = str(data.get("s") or "").strip()
    out = {"p": p}
    if s:
        out["s"] = s
    return out


def is_path_allowed_for_jar_download(abs_path: str, project_root: Path) -> bool:
    """Restrict signed downloads to project tree and optional extra roots."""
    try:
        resolved = Path(abs_path).resolve()
    except OSError:
        return False

    allowed_roots: list[Path] = [project_root.resolve()]
    extra = (os.environ.get("CRM_JAR_DOWNLOAD_ROOT") or "").strip()
    if extra:
        try:
            allowed_roots.append(Path(extra).resolve())
        except OSError:
            pass
    temp_root = (os.environ.get("CRM_CLIENT_TEMP_ROOT") or "").strip()
    if temp_root and "{" not in temp_root:
        try:
            allowed_roots.append(Path(temp_root).resolve())
        except OSError:
            pass

    for root in allowed_roots:
        if not root.exists():
            continue
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def load_jar_path_from_token(token: str) -> str:
    """Return primary file path from a valid token."""
    return load_jar_download_payload(token)["p"]
