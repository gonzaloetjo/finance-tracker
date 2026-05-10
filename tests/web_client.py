from __future__ import annotations

import asyncio
from typing import Any

import httpx
from fastapi import FastAPI


class ASGITestClient:
    """Synchronous wrapper around httpx's ASGI transport for tests."""

    def __init__(
        self,
        app: FastAPI,
        *,
        follow_redirects: bool = False,
        auto_csrf: bool = True,
    ):
        self._app = app
        self._follow_redirects = follow_redirects
        self._auto_csrf = auto_csrf
        self._cookies = httpx.Cookies()

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        headers = dict(kwargs.pop("headers", {}) or {})
        if self._auto_csrf and method.upper() not in {"GET", "HEAD", "OPTIONS", "TRACE"}:
            state = getattr(self._app.state, "finance", None)
            token = getattr(state, "csrf_token", None)
            if token and "x-csrf-token" not in {k.lower() for k in headers}:
                headers["X-CSRF-Token"] = token

        async def _request() -> httpx.Response:
            transport = httpx.ASGITransport(app=self._app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
                follow_redirects=self._follow_redirects,
                cookies=self._cookies,
            ) as client:
                response = await client.request(method, url, headers=headers, **kwargs)
                self._cookies.update(response.cookies)
                return response

        return asyncio.run(_request())

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("POST", url, **kwargs)
