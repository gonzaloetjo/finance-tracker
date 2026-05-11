from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from finance.categorize import Rule
from finance.db import store
from finance.eb.client import EnableBankingClient
from finance.eb.flows import iter_transactions


@dataclass
class SyncResult:
    account_uid: str
    added: int
    fetched: int
    status: str  # "ok" | "error"
    error: str | None = None


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _signed_amount(tx: dict) -> tuple[float, str]:
    amt = tx.get("transaction_amount") or {}
    try:
        value = float(amt.get("amount", "0"))
    except (TypeError, ValueError):
        value = 0.0
    if tx.get("credit_debit_indicator") == "DBIT":
        value = -abs(value)
    return value, amt.get("currency", "EUR")


def _transaction_key(tx: dict) -> str:
    """Stable unique key for a transaction. Prefers provider IDs, falls back to a hash."""
    return _transaction_source_key(tx)


def _transaction_source_key(tx: dict) -> str:
    """Provider-scoped source key used for per-account reconciliation."""
    for field in ("transaction_id", "entry_reference"):
        if tx.get(field):
            return str(tx[field])
    # Last-resort: hash the raw payload. Stable as long as the bank returns the
    # same bytes on re-fetch; good enough to dedupe repeat syncs.
    blob = json.dumps(tx, sort_keys=True, separators=(",", ":")).encode()
    return "h_" + hashlib.sha256(blob).hexdigest()[:32]


def _transaction_uid(account_uid: str, source_key: str) -> str:
    raw = f"{account_uid}:{source_key}".encode()
    return "tx_" + hashlib.sha256(raw).hexdigest()[:32]


def _cold_start_from(cold_start_days: int) -> date:
    return datetime.now(UTC).date() - timedelta(days=cold_start_days)


def _last_booking_date(conn: sqlite3.Connection, account_uid: str) -> date | None:
    row = conn.execute(
        "SELECT MAX(booking_date) AS d FROM transactions WHERE account_uid = ?",
        (account_uid,),
    ).fetchone()
    if row and row["d"]:
        return date.fromisoformat(row["d"])
    return None


def estimate_sync_overlap_days(
    conn: sqlite3.Connection,
    *,
    fallback_days: int = 21,
    minimum_days: int = 14,
    cap_days: int = 45,
) -> int:
    """Estimate sync lookback from observed booking-date to fetch lag."""
    rows = conn.execute(
        """
        SELECT booking_date, fetched_at
        FROM transactions
        WHERE booking_date IS NOT NULL
          AND fetched_at IS NOT NULL
        """
    ).fetchall()
    lags: list[int] = []
    for row in rows:
        try:
            booking = date.fromisoformat(str(row["booking_date"])[:10])
            fetched = datetime.fromisoformat(str(row["fetched_at"]).replace("Z", "+00:00")).date()
        except (TypeError, ValueError):
            continue
        lags.append(max(0, (fetched - booking).days))
    if not lags:
        return fallback_days
    lags.sort()
    idx = min(len(lags) - 1, math.ceil(0.99 * len(lags)) - 1)
    return min(cap_days, max(minimum_days, lags[idx] + 3))


def recover_stale_sync_runs(
    conn: sqlite3.Connection,
    *,
    account_uid: str | None = None,
    stale_after: timedelta = timedelta(hours=6),
) -> int:
    """Mark old interrupted sync runs as errors before starting new work."""
    now_dt = datetime.now(UTC)
    cutoff = (now_dt - stale_after).isoformat()
    params: list[str] = [now_dt.isoformat(), cutoff]
    account_filter = ""
    if account_uid is not None:
        account_filter = " AND account_uid = ?"
        params.append(account_uid)
    cur = conn.execute(
        f"""
        UPDATE sync_runs
           SET ended_at = ?,
               status = 'error',
               error = 'recovered stale running sync after interrupted process'
         WHERE status = 'running'
           AND ended_at IS NULL
           AND started_at <= ?
           {account_filter}
        """,
        params,
    )
    conn.commit()
    return cur.rowcount


def sync_account(
    conn: sqlite3.Connection,
    client: EnableBankingClient,
    account_uid: str,
    cold_start_days: int = 90,
    overlap_days: int | None = None,
    minimal_retention: bool = False,
) -> SyncResult:
    recover_stale_sync_runs(conn, account_uid=account_uid)
    cur = conn.execute(
        "INSERT INTO sync_runs (account_uid, started_at, status) VALUES (?, ?, ?)",
        (account_uid, _now(), "running"),
    )
    run_id = cur.lastrowid
    conn.commit()

    added = 0
    fetched = 0
    date_from_iso: str | None = None
    try:
        last_booking = _last_booking_date(conn, account_uid)
        if last_booking is None:
            date_from = _cold_start_from(cold_start_days)
        else:
            overlap = overlap_days if overlap_days is not None else estimate_sync_overlap_days(conn)
            date_from = last_booking - timedelta(days=overlap)
        date_from_iso = date_from.isoformat()
        conn.execute("BEGIN")
        for tx in iter_transactions(client, account_uid, date_from=date_from):
            fetched += 1
            source_key = _transaction_source_key(tx)
            tx_uid = _transaction_uid(account_uid, source_key)
            provider_transaction_id = (
                str(tx["transaction_id"]) if tx.get("transaction_id") else None
            )
            provider_entry_reference = (
                str(tx["entry_reference"]) if tx.get("entry_reference") else None
            )
            compat_transaction_id = provider_transaction_id or provider_entry_reference or tx_uid
            amount, currency = _signed_amount(tx)
            amount_minor = round(amount * 100) if currency == "EUR" else None
            booking = tx.get("booking_date")
            value_date = tx.get("value_date")
            creditor_name = (tx.get("creditor") or {}).get("name")
            debtor_name = (tx.get("debtor") or {}).get("name")
            remit = tx.get("remittance_information") or []
            remit_str = " | ".join(r for r in remit if r) if isinstance(remit, list) else str(remit)

            existing = conn.execute(
                """
                SELECT tx_uid
                FROM transactions
                WHERE account_uid = ? AND source_key = ?
                """,
                (account_uid, source_key),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO transactions
                    (tx_uid, transaction_id, account_uid, provider_transaction_id,
                     provider_entry_reference, source_key, booking_date, value_date,
                     amount, amount_minor, currency, creditor_name, debtor_name, remittance_info,
                     raw_json, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_uid, source_key) DO UPDATE SET
                  provider_transaction_id = excluded.provider_transaction_id,
                  provider_entry_reference = excluded.provider_entry_reference,
                  booking_date = excluded.booking_date,
                  value_date = excluded.value_date,
                  amount = excluded.amount,
                  amount_minor = excluded.amount_minor,
                  currency = excluded.currency,
                  creditor_name = excluded.creditor_name,
                  debtor_name = excluded.debtor_name,
                  remittance_info = excluded.remittance_info,
                  raw_json = excluded.raw_json,
                  fetched_at = excluded.fetched_at
                """,
                (
                    tx_uid,
                    compat_transaction_id,
                    account_uid,
                    provider_transaction_id,
                    provider_entry_reference,
                    source_key,
                    booking,
                    value_date,
                    amount,
                    amount_minor,
                    currency,
                    creditor_name,
                    debtor_name,
                    remit_str,
                    "{}" if minimal_retention else json.dumps(tx),
                    _now(),
                ),
            )
            if existing is None:
                added += 1

        conn.execute(
            """
            UPDATE sync_runs
               SET ended_at = ?,
                   transactions_added = ?,
                   transactions_fetched = ?,
                   date_from = ?,
                   status = 'ok'
             WHERE id = ?
            """,
            (_now(), added, fetched, date_from_iso, run_id),
        )
        conn.commit()
        return SyncResult(account_uid=account_uid, added=added, fetched=fetched, status="ok")

    except Exception as e:  # noqa: BLE001
        conn.rollback()
        conn.execute(
            """
            UPDATE sync_runs
               SET ended_at = ?,
                   transactions_added = 0,
                   transactions_fetched = ?,
                   date_from = ?,
                   status = 'error',
                   error = ?
             WHERE id = ?
            """,
            (_now(), fetched, date_from_iso, str(e), run_id),
        )
        conn.commit()
        return SyncResult(
            account_uid=account_uid, added=0, fetched=fetched, status="error", error=str(e)
        )


def sync_all_accounts(
    conn: sqlite3.Connection,
    client: EnableBankingClient,
    cold_start_days: int = 90,
    overlap_days: int | None = None,
    minimal_retention: bool = False,
    rules: list[Rule] | None = None,
    use_lock: bool = True,
) -> list[SyncResult]:
    lock = None
    if use_lock:
        lock = store.try_acquire_job_lock(conn, "sync:all", ttl_seconds=3600)
        if lock is None:
            return [
                SyncResult(
                    account_uid="sync",
                    added=0,
                    fetched=0,
                    status="error",
                    error="sync already running",
                )
            ]

    rows = conn.execute(
        """
        SELECT a.account_uid FROM accounts a
        JOIN sessions s ON s.session_id = a.session_id
        WHERE s.revoked_at IS NULL
        """
    ).fetchall()
    try:
        results = [
            sync_account(
                conn,
                client,
                r["account_uid"],
                cold_start_days,
                overlap_days=overlap_days,
                minimal_retention=minimal_retention,
            )
            for r in rows
        ]

        # Auto-enrich newly synced transactions. Categorization rules apply at the
        # merchant level (via `enrich_transactions` → `classify_merchant`); they
        # are not applied during fetch.
        any_added = any(r.added > 0 for r in results if r.status == "ok")
        if any_added:
            import sys

            from finance.analysis.enrich import enrich_transactions

            enrich_lock = store.try_acquire_job_lock(conn, "enrich:all", ttl_seconds=1800)
            if enrich_lock is None:
                print(
                    "sync: auto-enrich skipped because enrichment is already running — "
                    "run 'finance analyze enrich' manually if needed",
                    file=sys.stderr,
                )
            else:
                # Sync rows are already committed per-account by `sync_account`. If
                # auto-enrich raises here, surface a recovery hint to stderr instead
                # of a full traceback — the user can re-run `finance analyze enrich`
                # without losing the fetched transactions.
                try:
                    with conn:
                        enrich_transactions(conn, rules=rules)
                except Exception as e:  # noqa: BLE001
                    conn.rollback()
                    print(
                        f"sync: auto-enrich failed ({e!r}) — run 'finance analyze enrich' manually",
                        file=sys.stderr,
                    )
                finally:
                    store.release_job_lock(conn, enrich_lock)

        return results
    finally:
        if lock is not None:
            store.release_job_lock(conn, lock)
