from __future__ import annotations

import httpx
import pytest

from finance.auth.keys import generate_keypair
from finance.db import store
from finance.eb.client import EnableBankingClient
from finance.eb.models import Account, AspspRef, SessionResponse
from finance.sync import sync_account


def _seed_session(conn, account_uid="acc-1"):
    session = SessionResponse(
        session_id="sess-1",
        aspsp=AspspRef(name="Mock ASPSP", country="FR"),
        access={"valid_until": "2026-10-12T00:00:00Z"},
        accounts=[Account(uid=account_uid, name="Test", currency="EUR", raw={})],
    )
    store.persist_session(conn, session)


def _make_client(handler) -> EnableBankingClient:
    private_pem, _ = generate_keypair()
    transport = httpx.MockTransport(handler)
    return EnableBankingClient(app_id="app-1", private_key_pem=private_pem, transport=transport)


PAGE1 = {
    "transactions": [
        {
            "transaction_id": "tx-a",
            "transaction_amount": {"currency": "EUR", "amount": "12.50"},
            "credit_debit_indicator": "DBIT",
            "booking_date": "2026-03-01",
            "value_date": "2026-03-01",
            "creditor": {"name": "Carrefour"},
            "remittance_information": ["Grocery 1"],
        },
        {
            "transaction_id": "tx-b",
            "transaction_amount": {"currency": "EUR", "amount": "1000.00"},
            "credit_debit_indicator": "CRDT",
            "booking_date": "2026-03-05",
            "value_date": "2026-03-05",
            "debtor": {"name": "Employer SAS"},
            "remittance_information": ["Salary March"],
        },
    ],
    "continuation_key": "page2",
}

PAGE2 = {
    "transactions": [
        {
            "transaction_id": "tx-c",
            "transaction_amount": {"currency": "EUR", "amount": "45.00"},
            "credit_debit_indicator": "DBIT",
            "booking_date": "2026-03-10",
            "value_date": "2026-03-10",
            "creditor": {"name": "EDF"},
            "remittance_information": ["Electricity"],
        },
    ],
    "continuation_key": None,
}


@pytest.fixture
def conn(tmp_path):
    c = store.connect(tmp_path / "finance.db")
    store.init_schema(c)
    _seed_session(c)
    yield c
    c.close()


def test_sync_ingests_and_signs_amounts(conn):
    pages = [PAGE1, PAGE2]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=pages.pop(0))

    with _make_client(handler) as client:
        result = sync_account(conn, client, "acc-1", cold_start_days=90)

    assert result.status == "ok"
    assert result.added == 3
    assert result.fetched == 3

    rows = conn.execute(
        "SELECT transaction_id, amount, creditor_name, debtor_name FROM transactions ORDER BY booking_date"
    ).fetchall()
    amounts = {r["transaction_id"]: r["amount"] for r in rows}
    assert amounts["tx-a"] == -12.50  # DBIT → negative
    assert amounts["tx-b"] == 1000.00  # CRDT → positive
    assert amounts["tx-c"] == -45.00

    # Sync run row recorded
    run = conn.execute("SELECT status, transactions_added FROM sync_runs").fetchone()
    assert run["status"] == "ok"
    assert run["transactions_added"] == 3


def test_sync_is_idempotent(conn):
    """Same transactions returned twice → INSERT OR IGNORE prevents duplicates."""
    single_page = {
        "transactions": PAGE1["transactions"],  # 2 transactions
        "continuation_key": None,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=single_page)

    with _make_client(handler) as client:
        r1 = sync_account(conn, client, "acc-1")
        r2 = sync_account(conn, client, "acc-1")

    assert r1.added == 2
    assert r2.added == 0
    assert r2.fetched == 2

    count = conn.execute("SELECT COUNT(*) AS n FROM transactions").fetchone()["n"]
    assert count == 2


def test_sync_uses_last_booking_date_as_date_from(conn):
    # First sync with one transaction
    def handler_1(_req):
        return httpx.Response(
            200,
            json={
                "transactions": [
                    {
                        "transaction_id": "tx-old",
                        "transaction_amount": {"currency": "EUR", "amount": "5.00"},
                        "credit_debit_indicator": "DBIT",
                        "booking_date": "2026-02-15",
                    }
                ],
                "continuation_key": None,
            },
        )

    with _make_client(handler_1) as client:
        sync_account(conn, client, "acc-1")

    # Second sync: capture date_from sent
    captured = {}

    def handler_2(req):
        captured["url"] = str(req.url)
        return httpx.Response(200, json={"transactions": [], "continuation_key": None})

    with _make_client(handler_2) as client:
        sync_account(conn, client, "acc-1")

    assert "date_from=2026-02-15" in captured["url"]


def test_sync_records_error(conn):
    def handler(_req):
        return httpx.Response(500, json={"error": "down"})

    with _make_client(handler) as client:
        result = sync_account(conn, client, "acc-1")

    assert result.status == "error"
    assert result.added == 0
    run = conn.execute("SELECT status, error FROM sync_runs ORDER BY id DESC LIMIT 1").fetchone()
    assert run["status"] == "error"
    assert "500" in run["error"]


def test_transaction_without_id_gets_stable_hash_key(conn):
    tx = {
        "transaction_amount": {"currency": "EUR", "amount": "9.99"},
        "credit_debit_indicator": "DBIT",
        "booking_date": "2026-03-20",
        "creditor": {"name": "Unknown"},
    }

    def handler(_req):
        return httpx.Response(200, json={"transactions": [tx], "continuation_key": None})

    with _make_client(handler) as client:
        r1 = sync_account(conn, client, "acc-1")
        r2 = sync_account(conn, client, "acc-1")

    assert r1.added == 1
    assert r2.added == 0  # hash-based key still dedupes
