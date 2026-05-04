from __future__ import annotations

import time

import jwt as pyjwt
from cryptography.hazmat.primitives import serialization

from finance.auth.jwt import sign
from finance.auth.keys import generate_keypair
from finance.config import EB_AUDIENCE, EB_ISSUER


def test_jwt_roundtrip():
    app_id = "cf589be3-3755-465b-a8df-a90a16a31403"
    private_pem, cert_pem = generate_keypair()
    token = sign(app_id, private_pem, ttl_seconds=600)

    # Load the public key from the self-signed cert we just made
    from cryptography import x509

    cert = x509.load_pem_x509_certificate(cert_pem)
    pub_pem = cert.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    headers = pyjwt.get_unverified_header(token)
    assert headers == {"typ": "JWT", "alg": "RS256", "kid": app_id}

    claims = pyjwt.decode(token, pub_pem, algorithms=["RS256"], audience=EB_AUDIENCE)
    assert claims["iss"] == EB_ISSUER
    assert claims["aud"] == EB_AUDIENCE
    assert claims["exp"] - claims["iat"] == 600
    assert abs(claims["iat"] - int(time.time())) < 5
    assert isinstance(claims["jti"], str)
    assert len(claims["jti"]) == 32

    second_token = sign(app_id, private_pem, ttl_seconds=600)
    second_claims = pyjwt.decode(second_token, pub_pem, algorithms=["RS256"], audience=EB_AUDIENCE)
    assert second_claims["jti"] != claims["jti"]


def test_jwt_rejects_ttl_over_24h():
    import pytest

    private_pem, _ = generate_keypair()
    with pytest.raises(ValueError, match="24h"):
        sign("whatever", private_pem, ttl_seconds=86401)
