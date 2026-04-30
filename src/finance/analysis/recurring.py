"""Stage C — recurring stream view.

Reads `streams WHERE is_recurring=1` joined with merchants; returns a DataFrame
with the user-facing columns. No DB writes.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta

import pandas as pd

_COLUMNS: tuple[str, ...] = (
    "stream_id",
    "merchant",
    "category",
    "txn_type",
    "classification",
    "median_days",
    "regularity",
    "typical_amount",
    "count",
    "first_seen",
    "last_seen",
    "next_expected",
    "monthly_cost",
    "active",
)


def find_recurring(
    conn: sqlite3.Connection,
    *,
    active_only: bool = False,
) -> pd.DataFrame:
    """Return recurring streams as a DataFrame.

    `monthly_cost` normalizes typical_amount to a per-month figure using the
    classification cadence (weekly → *52/12, quarterly → /3, annual → /12).
    Sign-preserving: charges remain negative, incoming recurring transfers
    positive.
    """
    query = """
        SELECT
          s.stream_id       AS stream_id,
          m.canonical_name  AS merchant,
          m.category        AS category,
          s.txn_type        AS txn_type,
          s.classification  AS classification,
          s.median_days     AS median_days,
          s.regularity      AS regularity,
          s.median_amount   AS typical_amount,
          s.count           AS count,
          s.first_seen      AS first_seen,
          s.last_seen       AS last_seen,
          s.active          AS active
        FROM streams s
        JOIN merchants m ON m.merchant_id = s.merchant_id
        WHERE s.is_recurring = 1
    """
    if active_only:
        query += " AND s.active = 1"
    query += " ORDER BY ABS(s.median_amount) DESC"

    df = pd.read_sql_query(query, conn)
    if df.empty:
        return pd.DataFrame(columns=list(_COLUMNS))

    df["first_seen"] = pd.to_datetime(df["first_seen"], errors="coerce")
    df["last_seen"] = pd.to_datetime(df["last_seen"], errors="coerce")
    df["active"] = df["active"].astype(bool)

    df["next_expected"] = df.apply(_next_expected, axis=1)
    df["monthly_cost"] = df.apply(_monthly_cost, axis=1)

    return df[list(_COLUMNS)]


def _next_expected(row: pd.Series) -> pd.Timestamp | None:
    last = row["last_seen"]
    mdays = row["median_days"]
    if pd.isna(last) or pd.isna(mdays):
        return pd.NaT
    try:
        return last + timedelta(days=int(mdays))
    except (ValueError, TypeError):
        return pd.NaT


def _monthly_cost(row: pd.Series) -> float:
    amount = row["typical_amount"]
    if pd.isna(amount):
        return 0.0
    classification = row["classification"]
    if classification == "weekly":
        return float(amount) * 52 / 12
    if classification == "monthly":
        return float(amount)
    if classification == "quarterly":
        return float(amount) / 3
    if classification == "annual":
        return float(amount) / 12
    # irregular: fall back to count / span
    first = row["first_seen"]
    last = row["last_seen"]
    if pd.isna(first) or pd.isna(last):
        return float(amount)
    span_days = max(1, (last - first).days)
    months = max(1.0, span_days / 30.4375)
    return float(amount) * row["count"] / months


def recurring_columns() -> tuple[str, ...]:
    return _COLUMNS
