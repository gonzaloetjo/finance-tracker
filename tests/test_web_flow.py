from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from finance.auth.keys import generate_keypair
from finance.db import store
from finance.eb.client import EnableBankingClient
from finance.web.app import AppState, create_app

# Canned Enable Banking responses for a Mock ASPSP sandbox flow.
ASPSPS = {
    "aspsps": [
        {
            "name": "Mock ASPSP",
            "country": "FR",
            "maximum_consent_validity": 15552000,
            "psu_types": ["personal"],
            "auth_methods": [],
        },
        {
            "name": "BBVA",
            "country": "FR",
            "maximum_consent_validity": 7776000,
            "psu_types": ["personal"],
            "auth_methods": [],
        },
    ]
}

AUTH_RESP = {"url": "https://tilisy.enablebanking.com/welcome?sessionid=abc"}

SESSION_RESP = {
    "session_id": "sess-42",
    "aspsp": {"name": "Mock ASPSP", "country": "FR"},
    "access": {"valid_until": "2026-10-12T00:00:00.000+00:00"},
    "accounts": [
        {
            "uid": "acc-1",
            "account_id": {"iban": "FR7612345678901234567890123"},
            "name": "Compte Chèque",
            "currency": "EUR",
            "cash_account_type": "CACC",
            "product": "CurrentAccount",
        }
    ],
}


@pytest.fixture
def client_and_transport(tmp_path):
    captured: dict = {"calls": []}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["calls"].append((request.method, str(request.url)))
        path = request.url.path
        if path == "/aspsps":
            return httpx.Response(200, json=ASPSPS)
        if path == "/auth":
            return httpx.Response(200, json=AUTH_RESP)
        if path == "/sessions":
            return httpx.Response(200, json=SESSION_RESP)
        return httpx.Response(404, json={"error": f"unexpected {path}"})

    transport = httpx.MockTransport(handler)
    private_pem, _ = generate_keypair()

    def client_factory():
        return EnableBankingClient(app_id="app-1", private_key_pem=private_pem, transport=transport)

    state = AppState(
        client_factory=client_factory,
        db_path=tmp_path / "finance.db",
        callback_url="http://localhost:8000/callback",
    )
    app = create_app(state)
    # Don't follow redirects automatically so we can assert on them
    return TestClient(app, follow_redirects=False), state, captured


def test_index_empty(client_and_transport):
    client, _, _ = client_and_transport
    resp = client.get("/")
    assert resp.status_code == 200
    # Phase 8: / renders overview.html; with no accounts connected, it
    # surfaces the empty-state prompt to connect a bank.
    assert "No accounts connected" in resp.text
    assert "Connect a bank" in resp.text


def test_connect_page_lists_aspsps(client_and_transport):
    client, _, captured = client_and_transport
    resp = client.get("/connect?country=FR")
    assert resp.status_code == 200
    assert "Mock ASPSP" in resp.text
    assert "BBVA" in resp.text
    assert any(
        url.endswith("country=FR&service=AIS&psu_type=personal") or "country=FR" in url
        for _, url in captured["calls"]
    )


def test_full_consent_flow(client_and_transport):
    client, state, captured = client_and_transport

    # 1. User POSTs /connect picking Mock ASPSP
    resp = client.post("/connect", data={"aspsp_name": "Mock ASPSP", "aspsp_country": "FR"})
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("https://tilisy.enablebanking.com/")
    # State was stashed server-side for the callback
    assert len(state.pending) == 1
    auth_state = next(iter(state.pending))

    # 2. Enable Banking redirects back to /callback?code=...&state=...
    resp = client.get(f"/callback?code=auth-code-xyz&state={auth_state}")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"
    # state was consumed
    assert state.pending == {}

    # Session + account landed in the DB
    with store.connect(state.db_path) as conn:
        store.init_schema(conn)  # idempotent
        sessions = store.list_sessions(conn)
        accounts = store.list_accounts(conn)
    assert len(sessions) == 1
    assert sessions[0]["session_id"] == "sess-42"
    assert sessions[0]["aspsp_name"] == "Mock ASPSP"
    assert len(accounts) == 1
    assert accounts[0]["iban"] == "FR7612345678901234567890123"
    assert accounts[0]["currency"] == "EUR"

    # 3. /accounts now shows the account
    resp = client.get("/accounts")
    assert resp.status_code == 200
    assert "Compte Chèque" in resp.text
    assert "FR7612345678901234567890123" in resp.text

    # Verify the HTTP calls we made to Enable Banking
    methods_paths = [(m, httpx.URL(u).path) for m, u in captured["calls"]]
    assert ("POST", "/auth") in methods_paths
    assert ("POST", "/sessions") in methods_paths


def test_callback_rejects_unknown_state(client_and_transport):
    client, _, _ = client_and_transport
    resp = client.get("/callback?code=x&state=never-issued")
    assert resp.status_code == 400


def test_callback_rejects_error_param(client_and_transport):
    client, _, _ = client_and_transport
    resp = client.get("/callback?error=access_denied")
    assert resp.status_code == 400
