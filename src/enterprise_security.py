"""
Enterprise security controls for the CRM Compare Tool.

Designed for Windows Server / corporate LAN deployment:
- API token required for non-localhost clients when CRM_API_TOKEN is set
- Corporate network allowlist for remote clients (CRM_NETWORK_RESTRICT)
- No client-supplied identity spoofing from remote hosts
- Admin and dev routes disabled by default
- Firewall rules opt-in; LAN bind requires token by default
"""
from __future__ import annotations

import hmac
import ipaddress
import os
import secrets
from typing import Mapping, Optional, Union
from urllib.parse import parse_qs

DEFAULT_JAR_SECRET = "local-dev-secret"

# Default RFC1918 ranges used on the corporate network (override via CRM_NETWORK_ALLOWLIST).
DEFAULT_AMDOCS_NETWORK_ALLOWLIST: tuple[str, ...] = (
    "10.0.0.0/8",
    "172.16.0.0/12",
)

LOCALHOSTS = frozenset({"127.0.0.1", "::1", "localhost", ""})

ADMIN_PATH_PREFIXES = (
    "/foundations/",
    "/admin-tool/",
)

DEV_ROUTES = frozenset({"/run-demo-tests", "/run-crm-comparison"})

LOCALHOST_ONLY_PATHS = frozenset({
    "/api/windows-folder-picker",
    "/api/open-path-in-explorer",
    "/debug_sessions",
})


def load_dotenv(project_root: Optional[str] = None) -> None:
    """Load KEY=VALUE lines from project .env without overwriting existing env."""
    root = project_root or os.environ.get("CRM_PROJECT_ROOT") or ""
    if not root:
        return
    env_path = os.path.join(root, ".env")
    if not os.path.isfile(env_path):
        return
    try:
        with open(env_path, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError:
        pass


def is_localhost(client_host: str) -> bool:
    host = (client_host or "").strip().lower()
    if host in LOCALHOSTS:
        return True
    if host.startswith("::ffff:"):
        inner = host[7:]
        return inner in ("127.0.0.1", "localhost")
    return False


def is_lan_bind(host: str) -> bool:
    bind = (host or "").strip().lower()
    return bind in ("0.0.0.0", "::", "")


def get_api_token() -> str:
    return (
        os.environ.get("CRM_API_TOKEN")
        or os.environ.get("CRM_ACCESS_TOKEN")
        or ""
    ).strip()


def require_secure_mode() -> bool:
    value = (os.environ.get("CRM_REQUIRE_SECURE") or "1").strip().lower()
    return value not in ("0", "false", "no")


def admin_routes_enabled() -> bool:
    value = (os.environ.get("CRM_ENABLE_ADMIN") or "0").strip().lower()
    return value in ("1", "true", "yes")


def dev_routes_enabled() -> bool:
    value = (os.environ.get("CRM_ENABLE_DEV_ROUTES") or "0").strip().lower()
    return value in ("1", "true", "yes")


def should_open_firewall() -> bool:
    value = (os.environ.get("CRM_OPEN_FIREWALL") or "0").strip().lower()
    return value in ("1", "true", "yes")


def network_restriction_enabled() -> bool:
    """When True, only CRM_NETWORK_ALLOWLIST (default: corporate RFC1918) may access remotely."""
    value = (os.environ.get("CRM_NETWORK_RESTRICT") or "1").strip().lower()
    return value not in ("0", "false", "no")


def get_network_allowlist_cidrs() -> tuple[str, ...]:
    raw = (os.environ.get("CRM_NETWORK_ALLOWLIST") or "").strip()
    if not raw:
        return DEFAULT_AMDOCS_NETWORK_ALLOWLIST
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def get_network_allowlist_networks() -> tuple[Union[ipaddress.IPv4Network, ipaddress.IPv6Network], ...]:
    networks: list[Union[ipaddress.IPv4Network, ipaddress.IPv6Network]] = []
    for cidr in get_network_allowlist_cidrs():
        try:
            networks.append(ipaddress.ip_network(cidr, strict=False))
        except ValueError:
            print(f"[SECURITY WARN] Invalid CRM_NETWORK_ALLOWLIST entry ignored: {cidr}")
    return tuple(networks)


def normalize_client_ip(client_host: str) -> Optional[Union[ipaddress.IPv4Address, ipaddress.IPv6Address]]:
    host = (client_host or "").strip()
    if not host:
        return None
    if host.startswith("::ffff:"):
        host = host[7:]
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def client_in_network_allowlist(client_host: str) -> bool:
    if not network_restriction_enabled():
        return True
    networks = get_network_allowlist_networks()
    if not networks:
        return True
    addr = normalize_client_ip(client_host)
    if addr is None:
        return False
    return any(addr in network for network in networks)


def firewall_remoteip_spec() -> Optional[str]:
    """Comma-separated remoteip list for Windows Firewall, or None when unrestricted."""
    if not network_restriction_enabled():
        return None
    cidrs = get_network_allowlist_cidrs()
    return ",".join(cidrs) if cidrs else None


def reject_ssh_over_plain_http() -> bool:
    value = (os.environ.get("CRM_SSH_HTTPS_ONLY") or "1").strip().lower()
    return value not in ("0", "false", "no")


def ssh_strict_host_keys() -> bool:
    """When True, SSH only connects to hosts already listed in known_hosts."""
    value = (os.environ.get("CRM_SSH_STRICT_HOSTKEY") or "0").strip().lower()
    return value in ("1", "true", "yes")


def generate_api_token() -> str:
    return secrets.token_urlsafe(32)


def validate_startup_security(bind_host: str) -> None:
    """Abort startup when LAN exposure is enabled without required secrets."""
    lan = is_lan_bind(bind_host)
    token = get_api_token()
    jar_secret = (
        os.environ.get("CRM_JAR_DOWNLOAD_SECRET")
        or os.environ.get("FLASK_SECRET_KEY")
        or DEFAULT_JAR_SECRET
    )

    if require_secure_mode() and lan and not token:
        raise SystemExit(
            "SECURITY: Network-wide binding requires CRM_API_TOKEN.\n"
            "  Generate:  python -c \"import secrets; print(secrets.token_urlsafe(32))\"\n"
            "  Or run INSTALL_PERSISTENT_SERVER.bat (creates .env with a token).\n"
            "  Users access: http://<server>:5000/crm?access_token=<token>\n"
            "  Local-only dev: set HOST=127.0.0.1 and CRM_REQUIRE_SECURE=0"
        )

    if lan and not token:
        print("[SECURITY WARN] LAN mode without CRM_API_TOKEN — any network user can access this tool.")
    if lan and jar_secret == DEFAULT_JAR_SECRET:
        print("[SECURITY WARN] Set CRM_JAR_DOWNLOAD_SECRET (or FLASK_SECRET_KEY) for production.")
    if lan and network_restriction_enabled():
        cidrs = ", ".join(get_network_allowlist_cidrs())
        print(f"[SECURITY] Remote access restricted to corporate network: {cidrs}")
        print("           Set CRM_NETWORK_ALLOWLIST to override; CRM_NETWORK_RESTRICT=0 to disable.")


def extract_access_token(
    headers: Mapping[str, str],
    query_string: str = "",
) -> str:
    auth = str(headers.get("Authorization") or headers.get("authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    for header_name in ("X-CRM-Access-Token", "X-Access-Token"):
        value = str(headers.get(header_name) or headers.get(header_name.lower()) or "").strip()
        if value:
            return value
    if query_string:
        qs = parse_qs(query_string, keep_blank_values=False)
        for key in ("access_token", "token", "api_token"):
            values = qs.get(key) or []
            if values and values[0]:
                return str(values[0]).strip()
    return ""


def token_valid(provided: str) -> bool:
    expected = get_api_token()
    if not expected:
        return True
    if not provided:
        return False
    try:
        return hmac.compare_digest(
            provided.encode("utf-8"),
            expected.encode("utf-8"),
        )
    except (TypeError, ValueError):
        return False


def _is_admin_path(path: str) -> bool:
    normalized = (path or "").rstrip("/") or "/"
    if normalized.startswith(ADMIN_PATH_PREFIXES):
        return True
    return normalized in ("/foundations/prepare", "/foundations/update", "/admin-tool/submit")


def check_request_access(
    client_host: str,
    path: str,
    query_string: str,
    method: str,
    headers: Mapping[str, str],
    *,
    is_https: bool = False,
) -> tuple[bool, str, int]:
    """Return (allowed, message, http_status)."""
    local = is_localhost(client_host)
    normalized_path = (path or "/").split("?", 1)[0]

    if normalized_path in LOCALHOST_ONLY_PATHS:
        if not local:
            return False, "This feature is only available on the server console (localhost).", 403

    if not local and network_restriction_enabled() and not client_in_network_allowlist(client_host):
        return False, "Access is restricted to the corporate network.", 403

    if normalized_path in DEV_ROUTES:
        if not dev_routes_enabled() or not local:
            return False, "Not found.", 404

    if _is_admin_path(normalized_path):
        if not admin_routes_enabled():
            return False, "Not found.", 404
        if not local:
            return False, "Admin features are only available on localhost.", 403

    if (
        normalized_path in ("/api/validate-environment-jars", "/api/crm-find-jar-for-class")
        and method.upper() == "POST"
        and reject_ssh_over_plain_http()
        and not local
        and not is_https
    ):
        return False, "SSH validation requires HTTPS for remote clients.", 403

    configured_token = get_api_token()
    if not local and configured_token:
        provided = extract_access_token(headers, query_string)
        if not token_valid(provided):
            return False, "Access denied. A valid access token is required.", 401

    return True, "", 200


def secure_client_identity(
    client_host: str,
    headers: Mapping[str, str],
    qs_user: str,
    *,
    resolve_fn,
) -> str:
    """Ignore spoofable client identity overrides from remote hosts."""
    if is_localhost(client_host):
        return resolve_fn(client_host, headers, qs_user)
    return resolve_fn(client_host, {}, "")


def secure_client_machine(
    client_host: str,
    headers: Mapping[str, str],
    qs_user: str,
    *,
    resolve_fn,
) -> str:
    if is_localhost(client_host):
        return resolve_fn(client_host, headers, qs_user)
    return resolve_fn(client_host, {}, "")


def write_access_denied_response(handler, message: str, status: int = 401) -> None:
    """Write denial response for BaseHTTPRequestHandler."""
    import json

    body = json.dumps({"ok": False, "error": message}).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def client_access_token_bootstrap_js() -> str:
    """Capture ?access_token= from URL; attach token to fetch and forms."""
    return """
(function() {
  try {
    var params = new URLSearchParams(window.location.search);
    var fromUrl = params.get('access_token') || params.get('token') || '';
    if (fromUrl) {
      sessionStorage.setItem('crmAccessToken', fromUrl);
      params.delete('access_token');
      params.delete('token');
      var qs = params.toString();
      history.replaceState({}, '', window.location.pathname + (qs ? '?' + qs : '') + window.location.hash);
    }
  } catch (e) {}

  var nativeFetch = window.fetch;
  if (typeof nativeFetch === 'function') {
    window.fetch = function(url, opts) {
      opts = opts || {};
      var headers = new Headers(opts.headers || {});
      try {
        var token = sessionStorage.getItem('crmAccessToken') || '';
        if (token && !headers.has('X-CRM-Access-Token')) {
          headers.set('X-CRM-Access-Token', token);
        }
      } catch (e2) {}
      opts.headers = headers;
      return nativeFetch.call(this, url, opts);
    };
  }

  function attachTokenToForms() {
    var token = '';
    try { token = sessionStorage.getItem('crmAccessToken') || ''; } catch (e3) {}
    if (!token) return;
    document.querySelectorAll('form').forEach(function(form) {
      if (form.querySelector('input[name="access_token"]')) return;
      var input = document.createElement('input');
      input.type = 'hidden';
      input.name = 'access_token';
      input.value = token;
      form.appendChild(input);
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', attachTokenToForms);
  } else {
    attachTokenToForms();
  }
})();
"""


def is_https_flask_request(request) -> bool:
    if request.is_secure:
        return True
    forwarded = (request.headers.get("X-Forwarded-Proto") or "").lower()
    return forwarded == "https"
