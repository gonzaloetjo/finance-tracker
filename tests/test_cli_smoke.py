"""End-to-end smoke tests for the Typer CLI.

These exercise one read path (`accounts ls`), one write path (`label`), and the
empty-DB initialization path (`sessions ls`). They don't aim for exhaustive
coverage — they prove the `_open_db()` contextmanager + `FINANCE_DATA_DIR`
override lets a `CliRunner` run any CLI command against a disposable database.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from typer.testing import CliRunner

from finance.cli import app


def _seed_session_and_account(
    conn,
    *,
    session_id: str = "sess-1",
    account_uid: str = "acct-1",
    account_name: str = "Compte de chèques",
    iban: str = "FR7630006000011234567890189",
    currency: str = "EUR",
) -> None:
    now = datetime.now(UTC).isoformat()
    with conn:
        conn.execute(
            "INSERT INTO sessions (session_id, aspsp_name, aspsp_country, valid_until, created_at)"
            " VALUES (?, 'BNP Paribas', 'FR', ?, ?)",
            (session_id, now, now),
        )
        conn.execute(
            "INSERT INTO accounts (account_uid, session_id, iban, name, currency, account_type,"
            " raw_json) VALUES (?, ?, ?, ?, ?, 'CACC', '{}')",
            (account_uid, session_id, iban, account_name, currency),
        )


def test_sessions_ls_empty(cli_db) -> None:
    """Zero-seed path — proves the CM initializes schema on a fresh DB."""
    conn, _db_path = cli_db

    # Sanity: sessions table exists (schema was initialized by the fixture).
    conn.execute("SELECT 1 FROM sessions").fetchone()

    result = CliRunner().invoke(app, ["sessions", "ls"])
    assert result.exit_code == 0, result.output
    assert "(no sessions)" in result.output


def test_accounts_ls_shows_connected_account(cli_db) -> None:
    """Read path — seed one account, invoke `accounts ls`, verify output."""
    conn, _db_path = cli_db
    iban = "FR7630006000011234567890189"
    _seed_session_and_account(conn, iban=iban)

    result = CliRunner().invoke(app, ["accounts", "ls"])
    assert result.exit_code == 0, result.output
    # `accounts ls` prints the account_uid, not the IBAN — assert on a substring
    # of data we seeded, not a formatting assumption.
    assert "acct-1" in result.output
    assert "BNP Paribas" in result.output
    assert "Compte de chèques" in result.output


def test_label_writes_tx_override(cli_db) -> None:
    """Write path — invoke `label`, verify the tx_overrides row landed."""
    conn, _db_path = cli_db
    _seed_session_and_account(conn)
    tx_id = "tx-xyz"
    with conn:
        conn.execute(
            "INSERT INTO transactions (transaction_id, account_uid, booking_date, amount,"
            " currency, remittance_info, raw_json, fetched_at)"
            " VALUES (?, 'acct-1', '2026-04-01', -15.49, 'EUR', 'BURGER KING', '{}',"
            " '2026-04-01T00:00:00Z')",
            (tx_id,),
        )

    result = CliRunner().invoke(app, ["label", tx_id, "--category", "Dining"])
    assert result.exit_code == 0, result.output

    # The CLI wrote via a separate connection; verify it's visible from ours.
    row = conn.execute("SELECT category FROM tx_overrides WHERE tx_id = ?", (tx_id,)).fetchone()
    assert row is not None, "label command didn't persist a tx_overrides row"
    assert row["category"] == "Dining"


def test_backup_create_redacted_removes_raw_personal_fields(cli_db, tmp_path) -> None:
    conn, _db_path = cli_db
    iban = "FR7630006000011234567890189"
    memo = "VIR SEPA INST RECU /DE Jean Dupont /REF ABC123456789"
    _seed_session_and_account(conn, iban=iban, account_name="Jean Dupont")
    with conn:
        conn.execute(
            "INSERT INTO transactions (transaction_id, account_uid, booking_date, amount,"
            " currency, creditor_name, debtor_name, remittance_info, raw_json, fetched_at)"
            " VALUES ('tx-redact', 'acct-1', '2026-04-01', 10.0, 'EUR', 'Jean Dupont',"
            " 'Employer', ?, ?, '2026-04-01T00:00:00Z')",
            (memo, f'{{"iban":"{iban}","memo":"{memo}"}}'),
        )

    out = tmp_path / "backup-redacted.db"
    result = CliRunner().invoke(app, ["backup", "create", "--output", str(out), "--redacted"])

    assert result.exit_code == 0, result.output
    with sqlite3.connect(out) as redacted:
        account = redacted.execute("SELECT iban, name, raw_json FROM accounts").fetchone()
        tx = redacted.execute(
            "SELECT creditor_name, debtor_name, remittance_info, raw_json, provider_transaction_id"
            " FROM transactions"
        ).fetchone()
    assert account == (None, "[REDACTED]", "{}")
    assert tx == (None, None, "[REDACTED]", "{}", None)


def test_privacy_purge_raw_clears_raw_json(cli_db) -> None:
    conn, _db_path = cli_db
    _seed_session_and_account(conn)
    with conn:
        conn.execute(
            "INSERT INTO transactions (transaction_id, account_uid, booking_date, amount,"
            " currency, raw_json, fetched_at)"
            " VALUES ('tx-raw', 'acct-1', '2026-04-01', -1.0, 'EUR', '{\"secret\":true}',"
            " '2026-04-01T00:00:00Z')"
        )

    result = CliRunner().invoke(app, ["privacy", "purge-raw"])

    assert result.exit_code == 0, result.output
    assert conn.execute("SELECT raw_json FROM accounts").fetchone()[0] == "{}"
    assert conn.execute("SELECT raw_json FROM transactions").fetchone()[0] == "{}"


def test_sync_exits_nonzero_when_any_account_errors(cli_db, monkeypatch) -> None:
    """`finance sync` previously echoed errors to stderr but exited 0,
    which let cron / systemd think the run succeeded. Tier N: exit 1 if
    any account ended in `status='error'`.
    """
    from contextlib import nullcontext

    conn, _db_path = cli_db
    _seed_session_and_account(conn)

    from finance import cli as cli_module
    from finance.sync import SyncResult

    def fake_sync_all_accounts(*_args, **_kwargs):
        return [
            SyncResult(
                account_uid="acct-1",
                added=0,
                fetched=0,
                status="error",
                error="EB 401 unauthorized",
            )
        ]

    # Bypass the real EB client load (avoids the keypair/passphrase prompt)
    # and the actual sync — the CLI calls both via the imported symbols in
    # `finance.cli`.
    monkeypatch.setattr(cli_module, "sync_all_accounts", fake_sync_all_accounts)
    monkeypatch.setattr(cli_module, "_load_client", lambda *_a, **_k: nullcontext())

    result = CliRunner().invoke(app, ["sync"])
    assert result.exit_code == 1, f"expected nonzero exit; got 0:\n{result.output}"
    assert "ERROR" in result.output or "EB 401" in result.output
