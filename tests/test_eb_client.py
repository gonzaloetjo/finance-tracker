from __future__ import annotations

from datetime import date
from typing import Any

import httpx
import jwt as pyjwt
import pytest

from finance.auth.keys import generate_keypair
from finance.eb.client import EnableBankingClient
from finance.eb.flows import iter_transactions, list_aspsps


def _make_client(handler) -> EnableBankingClient:
    private_pem, _ = generate_keypair()
    transport = httpx.MockTransport(handler)
    return EnableBankingClient(app_id="app-1234", private_key_pem=private_pem, transport=transport)


def test_list_aspsps_parses_response():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization", "")
        body = {
            "aspsps": [
                {
                    "name": "BNP Paribas",
                    "country": "FR",
                    "bic": "BNPAFRPP",
                    "maximum_consent_validity": 15552000,
                    "psu_types": ["personal"],
                    "auth_methods": [],
                }
            ]
        }
        return httpx.Response(200, json=body)

    with _make_client(handler) as client:
        items = list_aspsps(client, country="FR")

    assert len(items) == 1
    assert items[0].name == "BNP Paribas"
    assert items[0].maximum_consent_validity == 15552000
    assert "country=FR" in captured["url"]
    assert "service=AIS" in captured["url"]
    assert captured["auth"].startswith("Bearer ")
    # Verify the JWT header has the right kid without checking signature
    token = captured["auth"].removeprefix("Bearer ")
    assert pyjwt.get_unverified_header(token) == {"typ": "JWT", "alg": "RS256", "kid": "app-1234"}


def test_iter_transactions_paginates_and_stops():
    page_responses = [
        {
            "transactions": [{"transaction_id": "tx1"}, {"transaction_id": "tx2"}],
            "continuation_key": "page2",
        },
        {
            "transactions": [{"transaction_id": "tx3"}],
            "continuation_key": None,
        },
    ]
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json=page_responses.pop(0))

    with _make_client(handler) as client:
        tx_ids = [
            t["transaction_id"]
            for t in iter_transactions(
                client, "acc-uid-1", date_from=date(2026, 1, 1), date_to=date(2026, 4, 1)
            )
        ]

    assert tx_ids == ["tx1", "tx2", "tx3"]
    assert len(calls) == 2
    assert "date_from=2026-01-01" in calls[0]
    assert "continuation_key" not in calls[0]
    assert "continuation_key=page2" in calls[1]


def test_client_raises_on_error_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    with _make_client(handler) as client, pytest.raises(httpx.HTTPStatusError, match="401"):
        list_aspsps(client, country="FR")


def test_client_caches_jwt_across_requests():
    seen_tokens: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_tokens.append(request.headers["authorization"])
        return httpx.Response(200, json={"aspsps": []})

    with _make_client(handler) as client:
        list_aspsps(client, country="FR")
        list_aspsps(client, country="DE")

    assert len(seen_tokens) == 2
    # Same token reused (cached)
    assert seen_tokens[0] == seen_tokens[1]
