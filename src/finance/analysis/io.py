"""Stage A — the only module that reads SQLite for analysis.

`load_transactions` returns a canonical, stable DataFrame that joins raw
transactions with the Phase 6 enrichment layer (merchants, tx_enrichment,
tx_overrides). Columns are a contract; Stage B/C/D consume this frame.
"""

from __future__ import annotations

import sqlite3
from typing import Literal

import pandas as pd

from finance.taxonomy import assert_subset_of_taxonomy

CategorySource = Literal["override", "user", "curated", "rule", "rule-stream", "llm", "legacy"]

# Categories that represent cash repositioning, not consumption. Excluded from
# `monthly_spend_avg` and all `--spend-only` analyses. Loan is deliberately NOT
# in this set — loan repayments are cash outflows and should count as spend.
NON_SPEND_CATEGORIES = frozenset({"Transfer", "Investment"})
assert_subset_of_taxonomy(NON_SPEND_CATEGORIES, source="analysis/io.py")

_CANONICAL_COLUMNS: tuple[str, ...] = (
    "tx_id",
    "account_uid",
    "aspsp",
    "booking_date",
    "value_date",
    "amount",
    "currency",
    "currency_excluded",
    "memo_raw",
    "txn_type",
    "merchant_id",
    "merchant_canonical",
    "category",
    "category_source",
    "stream_id",
)


def load_transactions(
    conn: sqlite3.Connection,
    *,
    since: str | None = None,
    account_uid: str | None = None,
    spend_only: bool = False,
) -> pd.DataFrame:
    """Return the canonical enriched-transaction DataFrame.

    Resolution precedence for `category`:
      1. tx_overrides.category            → source='override'
      2. merchants.category (+source)     → source from merchants.category_source
      3. NULL                             → source=NULL

    `currency_excluded` is True for any non-EUR transaction (v1 aggregations
    must filter on it explicitly; see Phase 6 architecture principle 7).

    `spend_only=True` drops transactions from accounts flagged
    `excluded_from_spend` — useful when a joint savings / investment account
    is connected but shouldn't pollute spending analyses.
    """
    query = """
        SELECT
          t.transaction_id                              AS tx_id,
          t.account_uid                                 AS account_uid,
          s.aspsp_name                                  AS aspsp,
          t.booking_date                                AS booking_date,
          t.value_date                                  AS value_date,
          t.amount                                      AS amount,
          t.currency                                    AS currency,
          t.remittance_info                             AS memo_raw,
          e.txn_type                                    AS txn_type,
          e.merchant_id                                 AS merchant_id,
          m.canonical_name                              AS merchant_canonical,
          COALESCE(o.category, m.category)              AS category,
          CASE
            WHEN o.category IS NOT NULL THEN 'override'
            WHEN m.category IS NOT NULL THEN m.category_source
            ELSE NULL
          END                                           AS category_source,
          e.stream_id                                   AS stream_id
        FROM transactions t
        JOIN accounts a  ON a.account_uid = t.account_uid
        JOIN sessions s  ON s.session_id  = a.session_id
        LEFT JOIN tx_enrichment e ON e.tx_id = t.transaction_id
        LEFT JOIN merchants m     ON m.merchant_id = e.merchant_id
        LEFT JOIN tx_overrides o  ON o.tx_id = t.transaction_id
        WHERE 1=1
    """
    params: list = []
    if since is not None:
        query += " AND t.booking_date >= ?"
        params.append(since)
    if account_uid is not None:
        query += " AND t.account_uid = ?"
        params.append(account_uid)
    if spend_only:
        query += " AND COALESCE(a.excluded_from_spend, 0) = 0"
        # Drop cash-repositioning categories (Transfer = internal account-to-
        # account moves; Investment = broker / crypto / retirement deposits).
        # They're not consumption, so they shouldn't inflate spend totals.
        ph = ",".join("?" for _ in NON_SPEND_CATEGORIES)
        query += " AND (COALESCE(o.category, m.category) IS NULL"
        query += f" OR COALESCE(o.category, m.category) NOT IN ({ph}))"
        params.extend(sorted(NON_SPEND_CATEGORIES))
    query += " ORDER BY t.booking_date DESC, t.transaction_id"

    df = pd.read_sql_query(query, conn, params=params)

    # Types
    df["booking_date"] = pd.to_datetime(df["booking_date"], errors="coerce")
    df["value_date"] = pd.to_datetime(df["value_date"], errors="coerce")
    df["amount"] = df["amount"].astype(float)
    df["currency_excluded"] = df["currency"].fillna("") != "EUR"

    # Guarantee column order + presence. New installs without any enrichment
    # still see all columns (values will be NaN/None as appropriate).
    for col in _CANONICAL_COLUMNS:
        if col not in df.columns:
            df[col] = pd.NA
    df = df[list(_CANONICAL_COLUMNS)]

    return df


def canonical_columns() -> tuple[str, ...]:
    """Public accessor so tests + downstream modules don't duplicate the tuple."""
    return _CANONICAL_COLUMNS
