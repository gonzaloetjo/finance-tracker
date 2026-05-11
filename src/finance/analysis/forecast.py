"""Stage C — forecast upcoming recurring charges.

For each active stream, walk forward from `last_seen + median_days` until
`as_of + horizon_days`. Confidence = regularity * decay(months_since_last).
Pure read; no DB writes.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

import pandas as pd

_COLUMNS: tuple[str, ...] = (
    "stream_id",
    "merchant",
    "category",
    "classification",
    "typical_amount",
    "expected_date",
    "days_until",
    "confidence",
)


def next_expected_charges(
    conn: sqlite3.Connection,
    *,
    horizon_days: int = 30,
    as_of: date | None = None,
) -> pd.DataFrame:
    """Forecast every expected hit within `horizon_days` of `as_of`.

    Streams without a median_days or last_seen are skipped. Multiple hits per
    stream are possible if the cadence is short (e.g. weekly over 30 days).
    """
    as_of = as_of or date.today()
    horizon_end = as_of + timedelta(days=horizon_days)

    rows = conn.execute(
        """
        SELECT
          s.stream_id, s.median_days, s.regularity, s.classification,
          s.median_amount, s.last_seen, s.active,
          m.canonical_name AS merchant, m.category
        FROM streams s
        JOIN merchants m ON m.merchant_id = s.merchant_id
        WHERE s.is_recurring = 1 AND s.active = 1
          AND COALESCE(s.currency, 'EUR') = 'EUR'
          AND s.median_days IS NOT NULL AND s.last_seen IS NOT NULL
          AND EXISTS (
            SELECT 1
            FROM tx_enrichment e
            JOIN transactions tx ON tx.tx_uid = e.tx_id
            JOIN accounts a ON a.account_uid = tx.account_uid
            WHERE e.stream_id = s.stream_id
              AND tx.currency = 'EUR'
              AND COALESCE(a.excluded_from_spend, 0) = 0
          )
        """
    ).fetchall()

    out_rows: list[dict] = []
    for r in rows:
        last_seen = _parse_date(r["last_seen"])
        if last_seen is None:
            continue
        mdays = int(r["median_days"])
        if mdays <= 0:
            continue

        # Walk forward in steps of median_days, but never before as_of.
        next_hit = last_seen + timedelta(days=mdays)
        while next_hit <= horizon_end:
            if next_hit >= as_of:
                months_since_last = max(0.0, (next_hit - last_seen).days / 30.4375)
                decay = 0.5 ** max(
                    0.0, months_since_last - 1
                )  # 1st hit full conf; each extra month halves
                conf = float(r["regularity"] or 0.0) * decay
                out_rows.append(
                    {
                        "stream_id": r["stream_id"],
                        "merchant": r["merchant"],
                        "category": r["category"],
                        "classification": r["classification"],
                        "typical_amount": float(r["median_amount"] or 0.0),
                        "expected_date": pd.Timestamp(next_hit),
                        "days_until": (next_hit - as_of).days,
                        "confidence": round(conf, 3),
                    }
                )
            next_hit = next_hit + timedelta(days=mdays)

    df = pd.DataFrame(out_rows, columns=list(_COLUMNS))
    if df.empty:
        return df
    return df.sort_values("expected_date").reset_index(drop=True)


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(str(s)[:10])
    except (ValueError, TypeError):
        return None
