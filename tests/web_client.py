from __future__ import annotations

import asyncio
from typing import Any

import httpx
from fastapi import FastAPI


class ASGITestClient:
    """Synchronous wrapper around httpx's ASGI transport for tests."""

    def __init__(self, app: FastAPI, *, follow_redirects: bool = False):
        self._app = app
        self._follow_redirects = follow_redirects

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        async def _request() -> httpx.Response:
            transport = httpx.ASGITransport(app=self._app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
                follow_redirects=self._follow_redirects,
            ) as client:
                return await client.request(method, url, **kwargs)

        return asyncio.run(_request())

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("POST", url, **kwargs)
