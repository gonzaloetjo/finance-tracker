from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from finance.categorize import Rule
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
    for field in ("transaction_id", "entry_reference"):
        if tx.get(field):
            return str(tx[field])
    # Last-resort: hash the raw payload. Stable as long as the bank returns the
    # same bytes on re-fetch; good enough to dedupe repeat syncs.
    blob = json.dumps(tx, sort_keys=True, separators=(",", ":")).encode()
    return "h_" + hashlib.sha256(blob).hexdigest()[:32]


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


def sync_account(
    conn: sqlite3.Connection,
    client: EnableBankingClient,
    account_uid: str,
    cold_start_days: int = 90,
) -> SyncResult:
    cur = conn.execute(
        "INSERT INTO sync_runs (account_uid, started_at, status) VALUES (?, ?, ?)",
        (account_uid, _now(), "running"),
    )
    run_id = cur.lastrowid
    conn.commit()

    try:
        date_from = _last_booking_date(conn, account_uid) or _cold_start_from(cold_start_days)
        added = 0
        fetched = 0
        for tx in iter_transactions(client, account_uid, date_from=date_from):
            fetched += 1
            tx_id = _transaction_key(tx)
            amount, currency = _signed_amount(tx)
            booking = tx.get("booking_date")
            value_date = tx.get("value_date")
            creditor_name = (tx.get("creditor") or {}).get("name")
            debtor_name = (tx.get("debtor") or {}).get("name")
            remit = tx.get("remittance_information") or []
            remit_str = " | ".join(r for r in remit if r) if isinstance(remit, list) else str(remit)

            result = conn.execute(
                """
                INSERT OR IGNORE INTO transactions
                    (transaction_id, account_uid, booking_date, value_date,
                     amount, currency, creditor_name, debtor_name, remittance_info,
                     raw_json, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tx_id,
                    account_uid,
                    booking,
                    value_date,
                    amount,
                    currency,
                    creditor_name,
                    debtor_name,
                    remit_str,
                    json.dumps(tx),
                    _now(),
                ),
            )
            if result.rowcount:
                added += 1

        conn.execute(
            "UPDATE sync_runs SET ended_at = ?, transactions_added = ?, status = 'ok' WHERE id = ?",
            (_now(), added, run_id),
        )
        conn.commit()
        return SyncResult(account_uid=account_uid, added=added, fetched=fetched, status="ok")

    except Exception as e:  # noqa: BLE001
        conn.execute(
            "UPDATE sync_runs SET ended_at = ?, status = 'error', error = ? WHERE id = ?",
            (_now(), str(e), run_id),
        )
        conn.commit()
        return SyncResult(account_uid=account_uid, added=0, fetched=0, status="error", error=str(e))


def sync_all_accounts(
    conn: sqlite3.Connection,
    client: EnableBankingClient,
    cold_start_days: int = 90,
    rules: list[Rule] | None = None,
) -> list[SyncResult]:
    rows = conn.execute(
        """
        SELECT a.account_uid FROM accounts a
        JOIN sessions s ON s.session_id = a.session_id
        WHERE s.revoked_at IS NULL
        """
    ).fetchall()
    results = [sync_account(conn, client, r["account_uid"], cold_start_days) for r in rows]

    # Auto-enrich newly synced transactions. Categorization rules apply at the
    # merchant level (via `enrich_transactions` → `classify_merchant`); they
    # are not applied during fetch.
    any_added = any(r.added > 0 for r in results if r.status == "ok")
    if any_added:
        import sys

        from finance.analysis.enrich import enrich_transactions

        # Sync rows are already committed per-account by `sync_account`. If
        # auto-enrich raises here, surface a recovery hint to stderr instead
        # of a full traceback — the user can re-run `finance analyze enrich`
        # without losing the fetched transactions.
        try:
            enrich_transactions(conn, rules=rules)
        except Exception as e:  # noqa: BLE001
            print(
                f"sync: auto-enrich failed ({e!r}) — "
                "run 'finance analyze enrich' manually",
                file=sys.stderr,
            )

    return results
