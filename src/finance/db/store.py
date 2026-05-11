from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from pathlib import Path
from typing import Any
from uuid import uuid4

from finance.eb.models import SessionResponse


def _now() -> str:
    return datetime.now(UTC).isoformat()


# ─────────────────────────────────────────────────────────────────────────────
# Connection management
# ─────────────────────────────────────────────────────────────────────────────


def _chmod_private(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        # Best-effort privacy hardening. Some filesystems ignore chmod.
        pass


def load_sqlcipher_driver() -> Any:
    """Return a DB-API compatible SQLCipher driver.

    Prefer pysqlcipher3 when present, but accept sqlcipher3-binary's module too
    so the reproducible devenv shell can provide working encryption without a
    local C extension build.
    """
    try:
        import pysqlcipher3.dbapi2 as driver  # type: ignore[import-not-found]

        return driver
    except Exception as first_error:  # noqa: BLE001
        try:
            import sqlcipher3.dbapi2 as driver  # type: ignore[import-not-found,import-untyped,no-redef]

            return driver
        except Exception as second_error:  # noqa: BLE001
            raise RuntimeError(
                "SQLCipher support requires pysqlcipher3 or sqlcipher3-binary"
            ) from (second_error or first_error)


def sql_literal(value: str) -> str:
    # SQLite PRAGMA statements do not support DB-API bind parameters.
    return "'" + value.replace("'", "''") + "'"


def apply_sqlcipher_key(conn: Any, passphrase: str) -> None:
    conn.execute(f"PRAGMA key = {sql_literal(passphrase)}")


def connect(db_path: Path, *, passphrase: str | None = None) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    _chmod_private(db_path.parent, 0o700)
    driver = sqlite3
    if passphrase:
        driver = load_sqlcipher_driver()
    conn = driver.connect(db_path, timeout=30.0)
    conn.row_factory = getattr(driver, "Row", sqlite3.Row)
    if passphrase:
        apply_sqlcipher_key(conn, passphrase)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    if db_path.exists():
        _chmod_private(db_path, 0o600)
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    schema = files("finance.db").joinpath("schema.sql").read_text()
    conn.executescript(schema)
    _apply_migrations(conn)
    conn.commit()


@contextmanager
def open_db(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Open the DB at `db_path`, ensure the schema is up-to-date, close on exit.

    The yielded connection is NOT wrapped in a transaction — callers that write
    should still use `with conn:` around a unit of work so commits / rollbacks
    are explicit. Read-only callers get cleanup as a free bonus.
    """
    conn = connect(db_path)
    try:
        init_schema(conn)
        yield conn
    finally:
        conn.close()


_ALLOWED_MIGRATIONS = frozenset(
    {
        ("accounts", "excluded_from_spend", "INTEGER NOT NULL DEFAULT 0"),
        ("streams", "subscription_override", "INTEGER"),
        ("streams", "txn_type_class", "TEXT"),
        ("streams", "amount_sign", "INTEGER"),
        ("streams", "currency", "TEXT"),
        ("streams", "median_amount_minor", "INTEGER"),
        ("transactions", "amount_minor", "INTEGER"),
        ("sync_runs", "transactions_fetched", "INTEGER"),
        ("sync_runs", "date_from", "TEXT"),
    }
)

_MIGRATIONS = (
    (
        "0001_accounts_excluded_from_spend",
        "accounts",
        "excluded_from_spend",
        "INTEGER NOT NULL DEFAULT 0",
    ),
    ("0002_streams_subscription_override", "streams", "subscription_override", "INTEGER"),
    ("0003_sync_runs_transactions_fetched", "sync_runs", "transactions_fetched", "INTEGER"),
    ("0004_sync_runs_date_from", "sync_runs", "date_from", "TEXT"),
    ("0006_streams_txn_type_class", "streams", "txn_type_class", "TEXT"),
    ("0007_streams_amount_sign", "streams", "amount_sign", "INTEGER"),
    ("0008_streams_currency", "streams", "currency", "TEXT"),
    ("0009_transactions_amount_minor", "transactions", "amount_minor", "INTEGER"),
    ("0010_streams_median_amount_minor", "streams", "median_amount_minor", "INTEGER"),
)


def _apply_migrations(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
          version TEXT PRIMARY KEY,
          applied_at TEXT NOT NULL
        )
        """
    )
    applied = {
        r["version"] for r in conn.execute("SELECT version FROM schema_migrations").fetchall()
    }
    now = _now()
    for version, table, column, definition in _MIGRATIONS:
        if version in applied:
            continue
        _ensure_column(conn, table, column, definition)
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (version, now),
        )
    _migrate_transaction_identity(conn, applied_versions=applied, applied_at=now)
    _backfill_minor_units(conn)
    _ensure_transaction_identity_indexes(conn)
    _ensure_transaction_compat_trigger(conn)


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    # `PRAGMA table_info(...)` and `ALTER TABLE ... ADD COLUMN ...` cannot be
    # parameterized, so the table/column/definition are interpolated directly.
    # Keep the inputs locked to a known set so this stays injection-safe even
    # if a future caller ever passes through user input by accident.
    if (table, column, definition) not in _ALLOWED_MIGRATIONS:
        raise ValueError(
            f"_ensure_column refuses unknown migration: {(table, column, definition)!r}"
        )
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _migrate_transaction_identity(
    conn: sqlite3.Connection, *, applied_versions: set[str], applied_at: str
) -> None:
    version = "0005_transaction_identity_tx_uid"
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(transactions)")}
    if "tx_uid" in cols:
        if version not in applied_versions:
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (version, applied_at),
            )
        return

    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.executescript(
            """
            CREATE TABLE transactions_new (
              tx_uid TEXT PRIMARY KEY,
              transaction_id TEXT,
              account_uid TEXT NOT NULL REFERENCES accounts(account_uid),
              provider_transaction_id TEXT,
              provider_entry_reference TEXT,
              source_key TEXT,
              booking_date TEXT,
              value_date TEXT,
              amount REAL NOT NULL,
              amount_minor INTEGER,
              currency TEXT NOT NULL,
              creditor_name TEXT,
              debtor_name TEXT,
              remittance_info TEXT,
              raw_json TEXT NOT NULL,
              fetched_at TEXT NOT NULL,
              UNIQUE(account_uid, source_key)
            );

            INSERT INTO transactions_new (
              tx_uid, transaction_id, account_uid, provider_transaction_id,
              provider_entry_reference, source_key, booking_date, value_date,
              amount, amount_minor, currency, creditor_name, debtor_name, remittance_info,
              raw_json, fetched_at
            )
            SELECT
              transaction_id, transaction_id, account_uid, transaction_id,
              NULL, transaction_id, booking_date, value_date,
              amount, CAST(ROUND(amount * 100) AS INTEGER), currency, creditor_name, debtor_name, remittance_info,
              raw_json, fetched_at
            FROM transactions;

            CREATE TABLE tx_enrichment_new (
              tx_id TEXT PRIMARY KEY REFERENCES transactions(tx_uid) ON DELETE CASCADE,
              txn_type TEXT,
              merchant_id INTEGER REFERENCES merchants(merchant_id),
              stream_id TEXT REFERENCES streams(stream_id),
              memo_merchant_raw TEXT,
              enriched_at TEXT NOT NULL
            );
            INSERT INTO tx_enrichment_new (
              tx_id, txn_type, merchant_id, stream_id, memo_merchant_raw, enriched_at
            )
            SELECT tx_id, txn_type, merchant_id, stream_id, memo_merchant_raw, enriched_at
            FROM tx_enrichment;

            CREATE TABLE tx_overrides_new (
              tx_id TEXT PRIMARY KEY REFERENCES transactions(tx_uid) ON DELETE CASCADE,
              category TEXT NOT NULL,
              note TEXT,
              created_at TEXT NOT NULL
            );
            INSERT INTO tx_overrides_new (tx_id, category, note, created_at)
            SELECT tx_id, category, note, created_at
            FROM tx_overrides;

            DROP TABLE tx_overrides;
            ALTER TABLE tx_overrides_new RENAME TO tx_overrides;
            DROP TABLE tx_enrichment;
            ALTER TABLE tx_enrichment_new RENAME TO tx_enrichment;
            DROP TABLE transactions;
            ALTER TABLE transactions_new RENAME TO transactions;
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (version, applied_at),
        )
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def _ensure_transaction_identity_indexes(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tx_provider_id ON transactions(provider_transaction_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tx_account_date ON transactions(account_uid, booking_date DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tx_booking_date ON transactions(booking_date DESC)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_enrichment_merchant ON tx_enrichment(merchant_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_enrichment_stream ON tx_enrichment(stream_id)")


def _backfill_minor_units(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        UPDATE transactions
           SET amount_minor = CAST(ROUND(amount * 100) AS INTEGER)
         WHERE amount_minor IS NULL
           AND currency = 'EUR'
        """
    )
    conn.execute(
        """
        UPDATE streams
           SET median_amount_minor = CAST(ROUND(median_amount * 100) AS INTEGER)
         WHERE median_amount_minor IS NULL
           AND currency = 'EUR'
        """
    )


def _ensure_transaction_compat_trigger(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TRIGGER IF NOT EXISTS transactions_fill_compat_ids
        AFTER INSERT ON transactions
        WHEN NEW.tx_uid IS NULL OR NEW.transaction_id IS NULL OR NEW.source_key IS NULL
          OR NEW.amount_minor IS NULL
        BEGIN
          UPDATE transactions
             SET tx_uid = COALESCE(tx_uid, transaction_id),
                 transaction_id = COALESCE(transaction_id, tx_uid),
                 amount_minor = COALESCE(
                   amount_minor,
                   CASE WHEN currency = 'EUR' THEN CAST(ROUND(amount * 100) AS INTEGER) END
                 ),
                 provider_transaction_id = COALESCE(
                   provider_transaction_id,
                   transaction_id,
                   tx_uid
                 ),
                 source_key = COALESCE(
                   source_key,
                   provider_transaction_id,
                   provider_entry_reference,
                   transaction_id,
                   tx_uid
                 )
           WHERE rowid = NEW.rowid;
        END;
        """
    )


@dataclass(frozen=True)
class JobLock:
    lock_key: str
    owner: str
    acquired_at: str
    expires_at: str


def try_acquire_job_lock(
    conn: sqlite3.Connection,
    lock_key: str,
    *,
    ttl_seconds: int = 3600,
    owner: str | None = None,
) -> JobLock | None:
    """Acquire a DB-backed long-job lock, returning None if it is held.

    Expired locks are cleared on acquisition so a crashed process cannot block
    the local app forever.
    """
    owner = owner or uuid4().hex
    now_dt = datetime.now(UTC)
    now = now_dt.isoformat()
    expires = (now_dt + timedelta(seconds=ttl_seconds)).isoformat()
    conn.execute("DELETE FROM job_locks WHERE lock_key = ? AND expires_at <= ?", (lock_key, now))
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO job_locks (lock_key, owner, acquired_at, expires_at)
        VALUES (?, ?, ?, ?)
        """,
        (lock_key, owner, now, expires),
    )
    conn.commit()
    if cur.rowcount == 0:
        return None
    return JobLock(lock_key=lock_key, owner=owner, acquired_at=now, expires_at=expires)


def release_job_lock(conn: sqlite3.Connection, lock: JobLock) -> None:
    was_in_transaction = conn.in_transaction
    conn.execute(
        "DELETE FROM job_locks WHERE lock_key = ? AND owner = ?",
        (lock.lock_key, lock.owner),
    )
    if not was_in_transaction:
        conn.commit()


def persist_session(conn: sqlite3.Connection, session: SessionResponse) -> None:
    """Upsert a session row and its accounts."""
    access = session.access or {}
    valid_until = access.get("valid_until") or ""
    conn.execute(
        """
        INSERT INTO sessions (session_id, aspsp_name, aspsp_country, valid_until, created_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(session_id) DO UPDATE SET
          aspsp_name = excluded.aspsp_name,
          aspsp_country = excluded.aspsp_country,
          valid_until = excluded.valid_until
        """,
        (session.session_id, session.aspsp.name, session.aspsp.country, valid_until, _now()),
    )
    for acc in session.accounts:
        raw = acc.raw or acc.model_dump()
        iban = None
        if isinstance(acc.account_id, dict):
            iban = acc.account_id.get("iban")
        conn.execute(
            """
            INSERT INTO accounts (account_uid, session_id, iban, name, currency, account_type, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(account_uid) DO UPDATE SET
              session_id = excluded.session_id,
              iban = excluded.iban,
              name = excluded.name,
              currency = excluded.currency,
              account_type = excluded.account_type,
              raw_json = excluded.raw_json
            """,
            (
                acc.uid,
                session.session_id,
                iban,
                acc.name,
                acc.currency,
                acc.cash_account_type,
                json.dumps(raw),
            ),
        )
    conn.commit()


def list_sessions(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM sessions WHERE revoked_at IS NULL ORDER BY created_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def _spend_only_join_where(spend_only: bool) -> tuple[str, str]:
    if not spend_only:
        return "", ""
    return (
        "JOIN accounts a ON a.account_uid = t.account_uid",
        "AND COALESCE(a.excluded_from_spend, 0) = 0",
    )


def month_to_date_totals(conn: sqlite3.Connection, *, spend_only: bool = True) -> dict[str, float]:
    """Return {spent, income, net} for the current calendar month."""
    from datetime import date

    today = date.today()
    start = today.replace(day=1).isoformat()
    join_sql, spend_filter = _spend_only_join_where(spend_only)
    row = conn.execute(
        f"""
        SELECT
          COALESCE(SUM(CASE WHEN t.amount < 0 THEN -t.amount ELSE 0 END), 0) AS spent,
          COALESCE(SUM(CASE WHEN t.amount > 0 THEN t.amount ELSE 0 END), 0) AS income
        FROM transactions t
        {join_sql}
        WHERE t.booking_date >= ?
          {spend_filter}
        """,
        (start,),
    ).fetchone()
    spent = row["spent"] or 0.0
    income = row["income"] or 0.0
    return {"spent": spent, "income": income, "net": income - spent}


def monthly_series(
    conn: sqlite3.Connection,
    months: int = 6,
    *,
    spend_only: bool = True,
) -> list[dict[str, Any]]:
    """Return per-month aggregates for the last N months, oldest first."""
    join_sql, spend_filter = _spend_only_join_where(spend_only)
    rows = conn.execute(
        f"""
        SELECT
          strftime('%Y-%m', t.booking_date) AS month,
          SUM(CASE WHEN t.amount < 0 THEN -t.amount ELSE 0 END) AS spent,
          SUM(CASE WHEN t.amount > 0 THEN t.amount ELSE 0 END) AS income
        FROM transactions t
        {join_sql}
        WHERE t.booking_date >= date('now', ? )
          {spend_filter}
        GROUP BY month
        ORDER BY month
        """,
        (f"-{months} months",),
    ).fetchall()
    return [dict(r) for r in rows]


def list_accounts(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT a.*,
               COALESCE(a.excluded_from_spend, 0) AS excluded,
               s.aspsp_name, s.aspsp_country, s.valid_until
        FROM accounts a
        JOIN sessions s ON s.session_id = a.session_id
        WHERE s.revoked_at IS NULL
        ORDER BY a.name
        """
    ).fetchall()
    return [dict(r) for r in rows]
