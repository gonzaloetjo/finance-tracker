"""Stage C — simple, legible alerts.

`new_large_merchants`: large charges to merchants first seen recently, plus
every PRLV whose merchant is new.

`subscription_stopped`: active→inactive subscriptions whose last charge is
within a recent window — a positive-signal companion ("Netflix stopped 60d
ago — ~€15.49/mo saved") without judging whether it was wanted.

Replaces the full fraud-scoring approach until there's more data to tune it.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

import pandas as pd

_NEW_LARGE_COLUMNS: tuple[str, ...] = (
    "tx_id",
    "booking_date",
    "merchant",
    "category",
    "txn_type",
    "amount",
    "reason",
)

_STOPPED_COLUMNS: tuple[str, ...] = (
    "stream_id",
    "merchant",
    "category",
    "classification",
    "typical_amount",
    "last_seen",
    "months_since_last",
    "estimated_saved",
)


def new_large_merchants(
    conn: sqlite3.Connection,
    *,
    since: str | None = None,
    amount_threshold: float = 500.0,
    new_merchant_days: int = 30,
) -> pd.DataFrame:
    """Flag transactions that are either:
      (a) |amount| > threshold AND merchant first_seen within `new_merchant_days`, or
      (b) PRLV to a merchant first_seen within `new_merchant_days` regardless of amount.

    `since` filters on booking_date (YYYY-MM-DD). Defaults to the same window
    as `new_merchant_days`.
    """
    if since is None:
        since = (date.today() - timedelta(days=new_merchant_days)).isoformat()
    first_seen_cutoff = (date.today() - timedelta(days=new_merchant_days)).isoformat()

    query = """
        SELECT
          t.transaction_id        AS tx_id,
          t.booking_date          AS booking_date,
          t.amount                AS amount,
          t.currency              AS currency,
          m.canonical_name        AS merchant,
          m.category              AS category,
          m.first_seen            AS m_first_seen,
          e.txn_type              AS txn_type
        FROM transactions t
        JOIN tx_enrichment e ON e.tx_id = t.transaction_id
        LEFT JOIN merchants m ON m.merchant_id = e.merchant_id
        WHERE t.booking_date >= ?
          AND t.currency = 'EUR'
          AND m.merchant_id IS NOT NULL
          AND (
            (ABS(t.amount) > ? AND m.first_seen >= ?)
            OR (e.txn_type = 'PRLV' AND m.first_seen >= ?)
          )
        ORDER BY t.booking_date DESC, ABS(t.amount) DESC
    """
    df = pd.read_sql_query(
        query,
        conn,
        params=[since, amount_threshold, first_seen_cutoff, first_seen_cutoff],
    )
    if df.empty:
        return pd.DataFrame(columns=list(_NEW_LARGE_COLUMNS))

    df["booking_date"] = pd.to_datetime(df["booking_date"], errors="coerce")

    def _reason(r: pd.Series) -> str:
        reasons: list[str] = []
        if r["txn_type"] == "PRLV":
            reasons.append("new-merchant PRLV")
        if abs(r["amount"]) > amount_threshold:
            reasons.append(f"large charge > {amount_threshold:.0f}")
        return " + ".join(reasons) or "new merchant"

    df["reason"] = df.apply(_reason, axis=1)
    return df[list(_NEW_LARGE_COLUMNS)].reset_index(drop=True)


def subscription_stopped(
    conn: sqlite3.Connection,
    *,
    window_days: int = 120,
) -> pd.DataFrame:
    """Subscriptions that have gone inactive recently.

    A stream is "stopped" when is_subscription=1 AND active=0 AND its last_seen
    is within the trailing window. Returns estimated monthly savings assuming
    the stream won't resume.
    """
    cutoff = (date.today() - timedelta(days=window_days)).isoformat()
    query = """
        SELECT
          s.stream_id       AS stream_id,
          m.canonical_name  AS merchant,
          m.category        AS category,
          s.classification  AS classification,
          s.median_amount   AS typical_amount,
          s.last_seen       AS last_seen
        FROM streams s
        JOIN merchants m ON m.merchant_id = s.merchant_id
        WHERE s.is_subscription = 1 AND s.active = 0 AND s.last_seen >= ?
        ORDER BY ABS(s.median_amount) DESC
    """
    df = pd.read_sql_query(query, conn, params=[cutoff])
    if df.empty:
        return pd.DataFrame(columns=list(_STOPPED_COLUMNS))

    df["last_seen"] = pd.to_datetime(df["last_seen"], errors="coerce")
    today = pd.Timestamp.today().normalize()
    df["months_since_last"] = ((today - df["last_seen"]).dt.days / 30.4375).round(1)

    def _saved(row: pd.Series) -> float:
        amt = abs(float(row["typical_amount"] or 0.0))
        c = row["classification"]
        if c == "monthly":
            return amt
        if c == "quarterly":
            return amt / 3
        if c == "annual":
            return amt / 12
        return amt

    df["estimated_saved"] = df.apply(_saved, axis=1)
    return df[list(_STOPPED_COLUMNS)].reset_index(drop=True)
