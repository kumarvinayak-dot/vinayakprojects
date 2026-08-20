#!/usr/bin/env python3
"""Create a self-signed TLS certificate (no OpenSSL required). Writes .ssl/server.crt and .ssl/server.key."""
from __future__ import annotations

import argparse
import ipaddress
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = PROJECT_ROOT / ".ssl"


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate dev HTTPS certificate for the comparison server.")
    parser.add_argument(
        "--ip",
        default="10.19.164.180",
        help="IP address for Subject Alternative Name (default: 10.19.164.180)",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=DEFAULT_OUT,
        help=f"Output directory (default: {DEFAULT_OUT})",
    )
    args = parser.parse_args()

    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError:
        print(
            "Missing package 'cryptography'. Install with:\n"
            "  py -m pip install cryptography",
            file=sys.stderr,
        )
        return 1

    try:
        ipaddress.ip_address(args.ip)
    except ValueError:
        print(f"Invalid IP: {args.ip}", file=sys.stderr)
        return 1

    args.out_dir.mkdir(parents=True, exist_ok=True)
    cert_path = args.out_dir / "server.crt"
    key_path = args.out_dir / "server.key"

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, "US"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Dev"),
            x509.NameAttribute(NameOID.COMMON_NAME, args.ip),
        ]
    )
    san = x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(args.ip))])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=3650))
        .add_extension(san, critical=False)
        .sign(key, hashes.SHA256())
    )

    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    cert_path.write_bytes(cert_pem)
    key_path.write_bytes(key_pem)

    print(f"Certificate: {cert_path}")
    print(f"Private key: {key_path}")
    print()
    print("Built-in server (HTTPS on all interfaces):")
    print(
        f'  py builtin_server.py --host 0.0.0.0 --port 5000 --ssl-cert "{cert_path}" --ssl-key "{key_path}"'
    )
    print()
    print("Flask app:")
    print(f'  py web_app.py --host 0.0.0.0 --port 5000 --ssl-cert "{cert_path}" --ssl-key "{key_path}"')
    print()
    print(f"Optional: set PUBLIC_HOST={args.ip} so startup prints your URL.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
