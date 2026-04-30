from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


@dataclass
class TlsPaths:
    key: Path
    cert: Path


def ensure_localhost_cert(dir: Path) -> TlsPaths:
    """Generate a self-signed TLS cert for localhost + 127.0.0.1 if missing.

    This cert authenticates our local HTTPS server to the browser. It is
    unrelated to the Enable Banking app key (which is a separate RSA key used
    for signing JWTs). The browser will warn on first visit; accept once and
    the site works normally.
    """
    dir.mkdir(parents=True, exist_ok=True)
    paths = TlsPaths(key=dir / "localhost.key", cert=dir / "localhost.crt")
    if paths.key.exists() and paths.cert.exists():
        return paths

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + timedelta(days=3650))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
                    x509.IPAddress(ipaddress.IPv6Address("::1")),
                ]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )

    paths.key.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    os.chmod(paths.key, 0o600)
    paths.cert.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return paths
