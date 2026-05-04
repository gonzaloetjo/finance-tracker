from __future__ import annotations

import re
import time
from typing import Any

import httpx

from finance.auth.jwt import sign
from finance.config import EB_BASE_URL

_IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{4,}\b")


def _safe_error_body(text: str, *, limit: int = 400) -> str:
    redacted = _IBAN_RE.sub("[REDACTED-IBAN]", text)
    if len(redacted) <= limit:
        return redacted
    return redacted[:limit] + "...[truncated]"


class EnableBankingClient:
    """Thin httpx wrapper that injects a fresh JWT on each request.

    The JWT is cached in-memory and refreshed before expiry. App-level retries
    and pagination are handled by the caller (flows.py).
    """

    def __init__(
        self,
        app_id: str,
        private_key_pem: bytes,
        base_url: str = EB_BASE_URL,
        transport: httpx.BaseTransport | None = None,
        token_ttl: int = 3600,
    ):
        self.app_id = app_id
        self._key = private_key_pem
        self._token: str | None = None
        self._token_exp: float = 0.0
        self._ttl = token_ttl
        self._http = httpx.Client(
            base_url=base_url,
            timeout=30.0,
            transport=transport,
            headers={"Accept": "application/json"},
        )

    def _token_headers(self) -> dict[str, str]:
        now = time.time()
        # Refresh 60s before actual expiry to avoid race with server clock skew
        if self._token is None or now >= self._token_exp - 60:
            self._token = sign(self.app_id, self._key, ttl_seconds=self._ttl)
            self._token_exp = now + self._ttl
        return {"Authorization": f"Bearer {self._token}"}

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        headers = kwargs.pop("headers", {}) or {}
        headers.update(self._token_headers())
        resp = self._http.request(method, path, headers=headers, **kwargs)
        if resp.status_code >= 400:
            # Surface the body, but keep account identifiers out of exception strings.
            raise httpx.HTTPStatusError(
                f"{method} {path} → {resp.status_code}: {_safe_error_body(resp.text)}",
                request=resp.request,
                response=resp,
            )
        return resp

    def get(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("POST", path, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("DELETE", path, **kwargs)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> EnableBankingClient:
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()
