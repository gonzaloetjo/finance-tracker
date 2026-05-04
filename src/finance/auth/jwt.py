from __future__ import annotations

import secrets
import time

import jwt as pyjwt

from finance.config import EB_AUDIENCE, EB_ISSUER


def sign(app_id: str, private_key_pem: bytes, ttl_seconds: int = 3600) -> str:
    """Sign a JWT for Enable Banking API requests.

    See https://enablebanking.com/docs/api/reference — iss, aud, kid are fixed
    per spec; ttl must be <= 86400.
    """
    if ttl_seconds > 86400:
        raise ValueError("Enable Banking rejects tokens with TTL > 24h")
    iat = int(time.time())
    body = {
        "iss": EB_ISSUER,
        "aud": EB_AUDIENCE,
        "iat": iat,
        "exp": iat + ttl_seconds,
        "jti": secrets.token_hex(16),
    }
    return pyjwt.encode(body, private_key_pem, algorithm="RS256", headers={"kid": app_id})
