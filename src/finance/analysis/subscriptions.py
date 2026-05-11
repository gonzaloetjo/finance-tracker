"""Stage C — subscription view + overlap detection.

Subscriptions: `streams WHERE is_subscription=1`.
Overlaps: cluster subscriptions by service-domain (YAML-driven) and surface
counts / totals. No recommendation text here — that's Phase 7 LLM territory.
"""

from __future__ import annotations

import sqlite3
from importlib.resources import files

import pandas as pd
import yaml

_SUB_COLUMNS: tuple[str, ...] = (
    "stream_id",
    "merchant",
    "category",
    "txn_type",
    "classification",
    "median_days",
    "typical_amount",
    "count",
    "first_seen",
    "last_seen",
    "next_expected",
    "monthly_cost",
    "active",
)

_OVERLAP_COLUMNS: tuple[str, ...] = (
    "domain",
    "services_count",
    "services",
    "monthly_cost",
)

_CANDIDATE_COLUMNS: tuple[str, ...] = (
    "stream_id",
    "merchant",
    "category",
    "classification",
    "typical_amount",
    "monthly_cost",
    "count",
    "amount_spread_pct",
    "first_seen",
    "last_seen",
)


def _load_domain_map() -> dict[str, list[str]]:
    """Load the bundled service-domains YAML (domain → list of merchant substrings)."""
    path = files("finance.data").joinpath("service_domains.yaml")
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
        return {}
    out: dict[str, list[str]] = {}
    for domain, terms in data.items():
        if not isinstance(terms, list):
            continue
        out[str(domain)] = [str(t).strip().upper() for t in terms if t]
    return out


def find_subscriptions(
    conn: sqlite3.Connection,
    *,
    active_only: bool = True,
) -> pd.DataFrame:
    """Structural subscriptions: recurring AND classification ∈ {monthly, quarterly, annual}
    AND amount variance within tolerance. Category may be NULL."""
    query = """
        SELECT
          s.stream_id       AS stream_id,
          m.canonical_name  AS merchant,
          m.category        AS category,
          s.txn_type        AS txn_type,
          s.classification  AS classification,
          s.median_days     AS median_days,
          s.median_amount   AS typical_amount,
          s.count           AS count,
          s.first_seen      AS first_seen,
          s.last_seen       AS last_seen,
          s.active          AS active,
          s.regularity      AS regularity
        FROM streams s
        JOIN merchants m ON m.merchant_id = s.merchant_id
        WHERE s.is_subscription = 1
          AND COALESCE(s.currency, 'EUR') = 'EUR'
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
    if active_only:
        query += " AND s.active = 1"
    query += " ORDER BY ABS(s.median_amount) DESC"

    df = pd.read_sql_query(query, conn)
    if df.empty:
        return pd.DataFrame(columns=list(_SUB_COLUMNS))

    df["first_seen"] = pd.to_datetime(df["first_seen"], errors="coerce")
    df["last_seen"] = pd.to_datetime(df["last_seen"], errors="coerce")
    df["active"] = df["active"].astype(bool)

    # Reuse recurring's monthly-cost + next-expected logic for consistency.
    df["next_expected"] = df.apply(_next_expected, axis=1)
    df["monthly_cost"] = df.apply(_monthly_cost_for_sub, axis=1)

    return df[list(_SUB_COLUMNS)]


def find_overlaps(
    conn: sqlite3.Connection,
    *,
    domain_map: dict[str, list[str]] | None = None,
) -> pd.DataFrame:
    """Group active subscriptions by inferred service domain.

    Returns one row per domain with ≥2 active subscriptions; merchants that
    don't map to any domain are simply dropped (they are not overlaps).
    """
    domains = domain_map if domain_map is not None else _load_domain_map()
    subs = find_subscriptions(conn, active_only=True)
    if subs.empty:
        return pd.DataFrame(columns=list(_OVERLAP_COLUMNS))

    subs["domain"] = subs["merchant"].apply(lambda m: _match_domain(m, domains))
    hit = subs[subs["domain"].notna()]
    if hit.empty:
        return pd.DataFrame(columns=list(_OVERLAP_COLUMNS))

    grouped = (
        hit.groupby("domain")
        .agg(
            services_count=("merchant", "count"),
            services=("merchant", lambda s: sorted(s.tolist())),
            monthly_cost=("monthly_cost", "sum"),
        )
        .reset_index()
    )
    grouped = grouped[grouped["services_count"] >= 2]
    grouped = grouped.sort_values("monthly_cost", key=lambda s: s.abs(), ascending=False)
    return grouped[list(_OVERLAP_COLUMNS)].reset_index(drop=True)


def _match_domain(merchant: str | None, domains: dict[str, list[str]]) -> str | None:
    if not merchant:
        return None
    up = str(merchant).upper()
    for domain, terms in domains.items():
        for t in terms:
            if t and t in up:
                return domain
    return None


def _next_expected(row: pd.Series) -> pd.Timestamp:
    from datetime import timedelta

    last = row["last_seen"]
    mdays = row["median_days"]
    if pd.isna(last) or pd.isna(mdays):
        return pd.NaT
    try:
        return last + timedelta(days=int(mdays))
    except (ValueError, TypeError):
        return pd.NaT


def _monthly_cost_for_sub(row: pd.Series) -> float:
    amount = row["typical_amount"]
    if pd.isna(amount):
        return 0.0
    c = row["classification"]
    if c == "monthly":
        return float(amount)
    if c == "quarterly":
        return float(amount) / 3
    if c == "annual":
        return float(amount) / 12
    return float(amount)


def subscription_columns() -> tuple[str, ...]:
    return _SUB_COLUMNS


def overlap_columns() -> tuple[str, ...]:
    return _OVERLAP_COLUMNS


# ─────────────────────────────────────────────────────────────────────────────
# Sub candidates — streams blocked by category gate that LOOK subscription-
# shaped (recurring + monthly + stable amount). User-facing accept/reject.
# ─────────────────────────────────────────────────────────────────────────────

from finance.analysis.streams import NON_SUBSCRIPTION_CATEGORIES


def find_sub_candidates(
    conn: sqlite3.Connection,
    *,
    max_spread_pct: float = 3.0,
) -> pd.DataFrame:
    """Streams that STRUCTURALLY look like subscriptions but are blocked
    from the flag because their merchant falls into a non-subscription
    category (Dining, Groceries, Income, Transfer, Investment).

    Only streams with:
      - active = 1
      - is_recurring = 1
      - classification ∈ {monthly, quarterly, annual}
      - subscription_override IS NULL    ← user hasn't already decided
      - amount spread ≤ `max_spread_pct` (really-same-amount)

    `amount_spread_pct` = (max_amount - min_amount) / |median_amount| × 100.
    Zero means identical amounts every hit.
    """
    placeholders = ",".join("?" for _ in NON_SUBSCRIPTION_CATEGORIES)
    rows = conn.execute(
        f"""
        SELECT s.stream_id, m.canonical_name AS merchant, m.category,
               s.classification, s.median_days, s.median_amount, s.count,
               s.first_seen, s.last_seen
        FROM streams s
        JOIN merchants m ON m.merchant_id = s.merchant_id
        WHERE s.active = 1
          AND s.is_recurring = 1
          AND s.is_subscription = 0
          AND s.subscription_override IS NULL
          AND s.classification IN ('monthly', 'quarterly', 'annual')
          AND m.category IN ({placeholders})
        ORDER BY ABS(s.median_amount) DESC
        """,
        sorted(NON_SUBSCRIPTION_CATEGORIES),
    ).fetchall()

    candidates: list[dict] = []
    for r in rows:
        amts = [
            row[0]
            for row in conn.execute(
                """
                SELECT t.amount FROM tx_enrichment e
                JOIN transactions t ON t.tx_uid = e.tx_id
                WHERE e.stream_id = ?
                """,
                (r["stream_id"],),
            ).fetchall()
        ]
        if not amts:
            continue
        med = abs(r["median_amount"]) or 0.01
        spread_pct = (max(amts) - min(amts)) / med * 100.0
        if spread_pct > max_spread_pct:
            continue

        # monthly-cost normalization copied from find_subscriptions.
        mc = float(r["median_amount"])
        c = r["classification"]
        if c == "quarterly":
            mc /= 3
        elif c == "annual":
            mc /= 12

        candidates.append(
            {
                "stream_id": r["stream_id"],
                "merchant": r["merchant"],
                "category": r["category"],
                "classification": c,
                "typical_amount": float(r["median_amount"]),
                "monthly_cost": mc,
                "count": int(r["count"]),
                "amount_spread_pct": round(spread_pct, 2),
                "first_seen": r["first_seen"],
                "last_seen": r["last_seen"],
            }
        )
    return pd.DataFrame(candidates, columns=list(_CANDIDATE_COLUMNS))
