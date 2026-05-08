from __future__ import annotations

from finance.db import store


def _columns(conn, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_connect_applies_sqlite_operational_pragmas(tmp_path):
    path = tmp_path / "finance.db"
    conn = store.connect(path)
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 5000
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        conn.close()


def test_init_schema_migrates_old_tables_and_tracks_versions(tmp_path):
    conn = store.connect(tmp_path / "finance.db")
    try:
        conn.executescript(
            """
            CREATE TABLE accounts (
              account_uid TEXT PRIMARY KEY,
              session_id TEXT NOT NULL,
              iban TEXT,
              name TEXT,
              currency TEXT,
              account_type TEXT,
              raw_json TEXT NOT NULL
            );
            CREATE TABLE streams (
              stream_id TEXT PRIMARY KEY,
              merchant_id INTEGER NOT NULL,
              txn_type TEXT,
              median_amount REAL,
              amount_tolerance REAL,
              median_days INTEGER,
              regularity REAL,
              classification TEXT,
              is_recurring INTEGER NOT NULL DEFAULT 0,
              is_subscription INTEGER NOT NULL DEFAULT 0,
              active INTEGER NOT NULL DEFAULT 1,
              first_seen TEXT,
              last_seen TEXT,
              count INTEGER,
              updated_at TEXT
            );
            CREATE TABLE sync_runs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              account_uid TEXT NOT NULL,
              started_at TEXT NOT NULL,
              ended_at TEXT,
              transactions_added INTEGER,
              status TEXT NOT NULL,
              error TEXT
            );
            """
        )
        store.init_schema(conn)

        assert "excluded_from_spend" in _columns(conn, "accounts")
        assert "subscription_override" in _columns(conn, "streams")
        assert "transactions_fetched" in _columns(conn, "sync_runs")
        assert "date_from" in _columns(conn, "sync_runs")
        assert "job_locks" in {
            r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        versions = {
            r["version"] for r in conn.execute("SELECT version FROM schema_migrations")
        }
        assert {
            "0001_accounts_excluded_from_spend",
            "0002_streams_subscription_override",
            "0003_sync_runs_transactions_fetched",
            "0004_sync_runs_date_from",
        } <= versions
    finally:
        conn.close()


def test_job_lock_acquire_release_and_expiry(tmp_path):
    conn = store.connect(tmp_path / "finance.db")
    try:
        store.init_schema(conn)
        first = store.try_acquire_job_lock(conn, "sync:all", owner="first")
        assert first is not None
        assert store.try_acquire_job_lock(conn, "sync:all", owner="second") is None

        store.release_job_lock(conn, first)
        second = store.try_acquire_job_lock(conn, "sync:all", owner="second")
        assert second is not None
        store.release_job_lock(conn, second)

        conn.execute(
            """
            INSERT INTO job_locks (lock_key, owner, acquired_at, expires_at)
            VALUES ('sync:all', 'stale', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:01+00:00')
            """
        )
        conn.commit()
        fresh = store.try_acquire_job_lock(conn, "sync:all", owner="fresh")
        assert fresh is not None
        assert fresh.owner == "fresh"
    finally:
        conn.close()
