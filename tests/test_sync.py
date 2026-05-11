from __future__ import annotations

import httpx
import pytest

from finance.auth.keys import generate_keypair
from finance.db import store
from finance.eb.client import EnableBankingClient
from finance.eb.models import Account, AspspRef, SessionResponse
from finance.sync import recover_stale_sync_runs, sync_account, sync_all_accounts


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

    assert "date_from=2026-01-01" in captured["url"]


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


def test_sync_rolls_back_partial_page_on_later_failure(conn):
    responses = [
        httpx.Response(200, json=PAGE1),
        httpx.Response(500, json={"error": "page 2 down"}),
    ]

    def handler(_req):
        return responses.pop(0)

    with _make_client(handler) as client:
        result = sync_account(conn, client, "acc-1")

    assert result.status == "error"
    assert result.added == 0
    assert result.fetched == 2
    count = conn.execute("SELECT COUNT(*) AS n FROM transactions").fetchone()["n"]
    assert count == 0
    run = conn.execute(
        """
        SELECT status, transactions_added, transactions_fetched, date_from
        FROM sync_runs
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    assert run["status"] == "error"
    assert run["transactions_added"] == 0
    assert run["transactions_fetched"] == 2
    assert run["date_from"] is not None


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


def test_same_provider_transaction_id_on_two_accounts_persists_two_rows(conn):
    _seed_session(conn, account_uid="acc-2")

    def handler(_req):
        return httpx.Response(
            200,
            json={
                "transactions": [
                    {
                        "transaction_id": "provider-dup",
                        "transaction_amount": {"currency": "EUR", "amount": "5.00"},
                        "credit_debit_indicator": "DBIT",
                        "booking_date": "2026-03-20",
                    }
                ],
                "continuation_key": None,
            },
        )

    with _make_client(handler) as client:
        r1 = sync_account(conn, client, "acc-1")
        r2 = sync_account(conn, client, "acc-2")

    assert r1.added == 1
    assert r2.added == 1
    rows = conn.execute(
        """
        SELECT tx_uid, account_uid, provider_transaction_id, source_key
        FROM transactions
        WHERE provider_transaction_id = 'provider-dup'
        ORDER BY account_uid
        """
    ).fetchall()
    assert [r["account_uid"] for r in rows] == ["acc-1", "acc-2"]
    assert rows[0]["tx_uid"] != rows[1]["tx_uid"]
    assert {r["source_key"] for r in rows} == {"provider-dup"}


def test_corrected_provider_transaction_updates_mutable_fields(conn):
    amounts = ["5.00", "7.50"]

    def handler(_req):
        amount = amounts.pop(0)
        return httpx.Response(
            200,
            json={
                "transactions": [
                    {
                        "transaction_id": "corrected",
                        "transaction_amount": {"currency": "EUR", "amount": amount},
                        "credit_debit_indicator": "DBIT",
                        "booking_date": "2026-03-20",
                        "remittance_information": [f"amount {amount}"],
                    }
                ],
                "continuation_key": None,
            },
        )

    with _make_client(handler) as client:
        r1 = sync_account(conn, client, "acc-1", overlap_days=0)
        r2 = sync_account(conn, client, "acc-1", overlap_days=0)

    assert r1.added == 1
    assert r2.added == 0
    row = conn.execute(
        "SELECT amount, remittance_info FROM transactions WHERE provider_transaction_id = 'corrected'"
    ).fetchone()
    assert row["amount"] == -7.50
    assert row["remittance_info"] == "amount 7.50"


def test_sync_all_accounts_logs_enrich_failure_and_continues(conn, monkeypatch, capsys):
    """If auto-enrich raises, sync_all_accounts must still return its sync
    results and surface a recovery hint to stderr — sync_account already
    committed per-account, so the data is on disk and a manual
    `finance analyze enrich` will recover the merchant layer.
    """

    def boom(conn, *_args, **_kwargs):
        conn.execute("INSERT INTO merchants (canonical_name) VALUES ('PARTIAL WRITE')")
        raise RuntimeError("simulated enrich crash")

    monkeypatch.setattr("finance.analysis.enrich.enrich_transactions", boom)

    def handler(_req):
        return httpx.Response(
            200,
            json={
                "transactions": [
                    {
                        "transaction_id": "tx-en-1",
                        "transaction_amount": {"currency": "EUR", "amount": "5.00"},
                        "credit_debit_indicator": "DBIT",
                        "booking_date": "2026-03-01",
                        "creditor": {"name": "Test Merchant"},
                    }
                ],
                "continuation_key": None,
            },
        )

    with _make_client(handler) as client:
        results = sync_all_accounts(conn, client)

    # sync_account succeeded for the seeded account; the enrich crash
    # surfaced to stderr and was suppressed.
    assert len(results) == 1
    assert results[0].status == "ok"
    assert results[0].added == 1
    captured = capsys.readouterr()
    assert "auto-enrich failed" in captured.err
    assert "simulated enrich crash" in captured.err
    # The transaction is on disk for a manual re-enrich to find.
    row = conn.execute(
        "SELECT transaction_id FROM transactions WHERE transaction_id = 'tx-en-1'"
    ).fetchone()
    assert row is not None
    partial = conn.execute(
        "SELECT merchant_id FROM merchants WHERE canonical_name = 'PARTIAL WRITE'"
    ).fetchone()
    assert partial is None


def test_sync_all_accounts_returns_error_when_lock_is_held(conn):
    lock = store.try_acquire_job_lock(conn, "sync:all", owner="test")
    assert lock is not None

    def handler(_req):
        raise AssertionError("sync should not call Enable Banking while locked")

    try:
        with _make_client(handler) as client:
            results = sync_all_accounts(conn, client)
    finally:
        store.release_job_lock(conn, lock)

    assert len(results) == 1
    assert results[0].status == "error"
    assert results[0].error == "sync already running"


def test_recover_stale_sync_runs_finalizes_interrupted_rows(conn):
    conn.execute(
        """
        INSERT INTO sync_runs (account_uid, started_at, status)
        VALUES ('acc-1', '2026-01-01T00:00:00+00:00', 'running')
        """
    )
    conn.commit()

    recovered = recover_stale_sync_runs(conn, account_uid="acc-1")

    assert recovered == 1
    row = conn.execute(
        "SELECT status, ended_at, error FROM sync_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["status"] == "error"
    assert row["ended_at"] is not None
    assert "recovered stale running sync" in row["error"]
