from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyrage
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


@dataclass
class KeyPaths:
    private_key_age: Path
    public_cert: Path


def generate_keypair() -> tuple[bytes, bytes]:
    """Return (private_key_pem, self_signed_cert_pem) for Enable Banking upload."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=4096)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )

    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "finance-local")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + timedelta(days=365))
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    return private_pem, cert_pem


def write_keys(paths: KeyPaths, private_pem: bytes, cert_pem: bytes, passphrase: str) -> None:
    paths.private_key_age.parent.mkdir(parents=True, exist_ok=True)
    encrypted = pyrage.passphrase.encrypt(private_pem, passphrase)
    paths.private_key_age.write_bytes(encrypted)
    os.chmod(paths.private_key_age, 0o600)
    paths.public_cert.write_bytes(cert_pem)
    os.chmod(paths.public_cert, 0o644)


def load_private_key(path: Path, passphrase: str) -> bytes:
    """Decrypt the age-encrypted private key and return PEM bytes."""
    encrypted = path.read_bytes()
    try:
        return pyrage.passphrase.decrypt(encrypted, passphrase)
    except Exception as e:  # pyrage raises its own error types; surface cleanly
        raise ValueError("Failed to decrypt private key (wrong passphrase?)") from e


def encrypt_and_store(target: Path, pem_bytes: bytes, passphrase: str) -> None:
    """Age-encrypt a private key PEM and write it to `target` (0600)."""
    # Validate it's actually a PEM private key before we encrypt garbage
    serialization.load_pem_private_key(pem_bytes, password=None)
    target.parent.mkdir(parents=True, exist_ok=True)
    encrypted = pyrage.passphrase.encrypt(pem_bytes, passphrase)
    target.write_bytes(encrypted)
    os.chmod(target, 0o600)
