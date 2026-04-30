from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import Any

from finance.eb.models import SessionResponse


def _now() -> str:
    return datetime.now(UTC).isoformat()


# ─────────────────────────────────────────────────────────────────────────────
# Connection management
# ─────────────────────────────────────────────────────────────────────────────


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    schema = files("finance.db").joinpath("schema.sql").read_text()
    conn.executescript(schema)
    # Additive migrations for columns introduced after initial release.
    # SQLite's CREATE TABLE IF NOT EXISTS doesn't add missing columns.
    _ensure_column(conn, "accounts", "excluded_from_spend", "INTEGER NOT NULL DEFAULT 0")
    # User override for is_subscription: NULL=auto, 1=force-sub, 0=force-not-sub.
    # Persists across re-enrichment so decisions stick.
    _ensure_column(conn, "streams", "subscription_override", "INTEGER")
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


_ALLOWED_MIGRATION_TABLES = frozenset({"accounts", "streams"})


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    # `PRAGMA table_info(...)` and `ALTER TABLE ... ADD COLUMN ...` cannot be
    # parameterized, so the table/column/definition are interpolated directly.
    # Keep the inputs locked to a known set so this stays injection-safe even
    # if a future caller ever passes through user input by accident.
    if table not in _ALLOWED_MIGRATION_TABLES:
        raise ValueError(f"_ensure_column refuses unknown table: {table!r}")
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


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


def month_to_date_totals(conn: sqlite3.Connection) -> dict[str, float]:
    """Return {spent, income, net} for the current calendar month."""
    from datetime import date

    today = date.today()
    start = today.replace(day=1).isoformat()
    row = conn.execute(
        """
        SELECT
          COALESCE(SUM(CASE WHEN amount < 0 THEN -amount ELSE 0 END), 0) AS spent,
          COALESCE(SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END), 0) AS income
        FROM transactions WHERE booking_date >= ?
        """,
        (start,),
    ).fetchone()
    spent = row["spent"] or 0.0
    income = row["income"] or 0.0
    return {"spent": spent, "income": income, "net": income - spent}


def monthly_series(conn: sqlite3.Connection, months: int = 6) -> list[dict[str, Any]]:
    """Return per-month aggregates for the last N months, oldest first."""
    rows = conn.execute(
        """
        SELECT
          strftime('%Y-%m', booking_date) AS month,
          SUM(CASE WHEN amount < 0 THEN -amount ELSE 0 END) AS spent,
          SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END) AS income
        FROM transactions
        WHERE booking_date >= date('now', ? )
        GROUP BY month
        ORDER BY month
        """,
        (f"-{months} months",),
    ).fetchall()
    return [dict(r) for r in rows]


def top_categories(conn: sqlite3.Connection, since: str, limit: int = 8) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
          COALESCE(category, 'Uncategorized') AS category,
          SUM(-amount) AS spent
        FROM transactions
        WHERE amount < 0 AND booking_date >= ?
        GROUP BY COALESCE(category, 'Uncategorized')
        ORDER BY spent DESC
        LIMIT ?
        """,
        (since, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def recent_transactions(conn: sqlite3.Connection, limit: int = 10) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT booking_date, amount, currency, creditor_name, debtor_name, remittance_info, category
        FROM transactions
        ORDER BY booking_date DESC, rowid DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def list_accounts(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT a.*, s.aspsp_name, s.aspsp_country, s.valid_until
        FROM accounts a
        JOIN sessions s ON s.session_id = a.session_id
        WHERE s.revoked_at IS NULL
        ORDER BY a.name
        """
    ).fetchall()
    return [dict(r) for r in rows]
