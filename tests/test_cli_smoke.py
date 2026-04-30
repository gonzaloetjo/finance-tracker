"""End-to-end smoke tests for the Typer CLI.

These exercise one read path (`accounts ls`), one write path (`label`), and the
empty-DB initialization path (`sessions ls`). They don't aim for exhaustive
coverage — they prove the `_open_db()` contextmanager + `FINANCE_DATA_DIR`
override lets a `CliRunner` run any CLI command against a disposable database.
"""

from __future__ import annotations

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
