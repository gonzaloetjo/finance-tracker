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


def explain_eb_error(error: httpx.HTTPStatusError) -> str:
    status = error.response.status_code
    body = _safe_error_body(error.response.text)
    lowered = body.lower()
    if status == 403 and ("not active" in lowered or "inactive" in lowered):
        return (
            "Enable Banking says your application is not active yet. "
            "Open the Enable Banking Control Panel, select the application, "
            "and complete activation/self-whitelisting for the app_id and IBAN."
        )
    if status in {401, 403}:
        return (
            "Enable Banking rejected the request. Check that config.toml app_id "
            "matches the imported private key and that the application is active. "
            f"HTTP {status}: {body}"
        )
    return f"Enable Banking returned HTTP {status}: {body}"


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
        max_retries: int = 2,
        retry_backoff: float = 0.25,
    ):
        self.app_id = app_id
        self._key = private_key_pem
        self._token: str | None = None
        self._token_exp: float = 0.0
        self._ttl = token_ttl
        self._max_retries = max_retries
        self._retry_backoff = retry_backoff
        self._http = httpx.Client(
            base_url=base_url,
            timeout=30.0,
            transport=transport,
            headers={"Accept": "application/json"},
        )

    def _token_headers(self, *, force_refresh: bool = False) -> dict[str, str]:
        now = time.time()
        # Refresh 60s before actual expiry to avoid race with server clock skew
        if force_refresh or self._token is None or now >= self._token_exp - 60:
            self._token = sign(self.app_id, self._key, ttl_seconds=self._ttl)
            self._token_exp = now + self._ttl
        return {"Authorization": f"Bearer {self._token}"}

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        base_headers = kwargs.pop("headers", {}) or {}
        attempt = 0
        force_token_refresh = False
        retried_after_401 = False
        while True:
            headers = dict(base_headers)
            headers.update(self._token_headers(force_refresh=force_token_refresh))
            force_token_refresh = False
            try:
                resp = self._http.request(method, path, headers=headers, **kwargs)
            except (httpx.TimeoutException, httpx.NetworkError):
                if attempt >= self._max_retries or not _method_is_retryable(method):
                    raise
                time.sleep(self._retry_backoff * (2**attempt))
                attempt += 1
                continue

            if (
                attempt < self._max_retries
                and _method_is_retryable(method)
                and _status_is_retryable(resp.status_code)
            ):
                delay = _retry_delay(resp, self._retry_backoff * (2**attempt))
                resp.close()
                time.sleep(delay)
                attempt += 1
                continue
            if resp.status_code == 401 and not retried_after_401:
                resp.close()
                retried_after_401 = True
                force_token_refresh = True
                attempt += 1
                continue
            break
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


def _method_is_retryable(method: str) -> bool:
    return method.upper() in {"GET", "DELETE"}


def _status_is_retryable(status_code: int) -> bool:
    return status_code == 429 or 500 <= status_code <= 599


def _retry_delay(resp: httpx.Response, fallback: float) -> float:
    retry_after = resp.headers.get("retry-after")
    if retry_after:
        try:
            return max(0.0, min(float(retry_after), 5.0))
        except ValueError:
            pass
    return fallback
