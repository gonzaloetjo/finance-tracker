"""Stage B orchestrator — enrich_transactions.

Ties parse_memo → normalize_merchant → classify_merchant → group_streams
and persists everything in one transaction.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from finance.analysis.bank_profile import BankProfile, get_account_profile
from finance.analysis.classify import classify_merchant
from finance.analysis.memo import parse_memo
from finance.analysis.merchants import normalize_merchant
from finance.analysis.streams import group_streams
from finance.categorize import Rule


@dataclass
class EnrichSummary:
    total_transactions: int = 0
    already_enriched: int = 0
    newly_enriched: int = 0
    merchants_created: int = 0
    merchants_classified: int = 0
    streams_computed: int = 0
    income_tagged: int = 0
    transfer_tagged: int = 0
    errors: list[str] = field(default_factory=list)


def enrich_transactions(
    conn: sqlite3.Connection,
    *,
    since: str | None = None,
    reenrich: bool = False,
    rules: list[Rule] | None = None,
    rules_path: Path | None = None,
    seed_overrides: dict[str, str] | None = None,
) -> EnrichSummary:
    """Run the full enrichment pipeline. Writes to merchants, merchant_aliases,
    tx_enrichment, and streams tables.

    With reenrich=True, reprocesses all transactions (respects user overrides).
    With reenrich=False (default), only processes transactions without a
    tx_enrichment row.
    """
    now = datetime.now(UTC).isoformat()
    summary = EnrichSummary()

    # Count merchants before
    merchants_before = conn.execute("SELECT COUNT(*) FROM merchants").fetchone()[0]

    # Find transactions to enrich
    if reenrich:
        query = """
            SELECT t.transaction_id, t.account_uid, t.creditor_name,
                   t.debtor_name, t.remittance_info
            FROM transactions t
        """
        params: list = []
    else:
        query = """
            SELECT t.transaction_id, t.account_uid, t.creditor_name,
                   t.debtor_name, t.remittance_info
            FROM transactions t
            LEFT JOIN tx_enrichment e ON e.tx_id = t.transaction_id
            WHERE e.tx_id IS NULL
        """
        params = []

    if since:
        where = " AND t.booking_date >= ?" if "WHERE" in query else " WHERE t.booking_date >= ?"
        query += where
        params.append(since)

    rows = conn.execute(query, params).fetchall()
    summary.total_transactions = len(rows)

    # When re-enriching, clear stale rule-based classifications so new or
    # updated rules (and the stream classifier) can fire fresh. User-set and
    # curated categories are preserved. `rule-stream` is also cleared because
    # stream recomputation will re-derive it — and if the stream no longer
    # qualifies, that's the correct outcome.
    if reenrich:
        conn.execute(
            """
            UPDATE merchants
               SET category = NULL,
                   category_source = NULL,
                   category_confidence = NULL
             WHERE category_source IN ('rule', 'rule-stream')
            """
        )

    if not reenrich:
        already = conn.execute("SELECT COUNT(*) FROM tx_enrichment").fetchone()[0]
        summary.already_enriched = already

    # Step 1+2+3: parse_memo → normalize_merchant → write tx_enrichment.
    # Resolve the BankProfile once per account (one SQL per distinct account
    # in the batch), not per transaction. Resolve repeated raw merchant
    # strings once too; fuzzy matching against all canonicals is the expensive
    # part of this loop on larger histories.
    profile_cache: dict[str, BankProfile] = {}
    merchant_cache: dict[str, int] = {}
    classified_count = 0
    for row in rows:
        tx_id = row["transaction_id"]
        account_uid = row["account_uid"]
        memo_raw = row["remittance_info"]
        creditor_name = row["creditor_name"]
        debtor_name = row["debtor_name"]

        if account_uid not in profile_cache:
            profile_cache[account_uid] = get_account_profile(conn, account_uid)

        parsed = parse_memo(
            memo_raw,
            creditor_name=creditor_name,
            debtor_name=debtor_name,
            profile=profile_cache[account_uid],
        )
        merchant_id = None
        if parsed.merchant_raw:
            merchant_id = merchant_cache.get(parsed.merchant_raw)
            if merchant_id is None:
                merchant_id = normalize_merchant(parsed.merchant_raw, conn)
                merchant_cache[parsed.merchant_raw] = merchant_id

            # Classify the merchant (writes to merchants table). Defensive
            # guard: `normalize_merchant` just inserted/looked up this row,
            # so it should always be present — but if a future bug or
            # concurrent state drops it, skip classification rather than
            # crashing the whole batch and losing every other tx_enrichment
            # write in this transaction.
            row = conn.execute(
                "SELECT canonical_name FROM merchants WHERE merchant_id = ?",
                (merchant_id,),
            ).fetchone()
            if row is None:
                summary.errors.append(
                    f"merchant_id {merchant_id} vanished mid-batch (skipping tx {tx_id})"
                )
                merchant_id = None
            else:
                canonical = row["canonical_name"]
                cat, _src = classify_merchant(
                    merchant_id,
                    canonical,
                    conn,
                    rules=rules,
                    rules_path=rules_path,
                    seed_overrides=seed_overrides,
                )
                if cat:
                    classified_count += 1

        # Upsert tx_enrichment
        conn.execute(
            """
            INSERT INTO tx_enrichment (tx_id, txn_type, merchant_id, memo_merchant_raw, enriched_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(tx_id) DO UPDATE SET
              txn_type = excluded.txn_type,
              merchant_id = excluded.merchant_id,
              memo_merchant_raw = excluded.memo_merchant_raw,
              enriched_at = excluded.enriched_at
        """,
            (tx_id, parsed.txn_type, merchant_id, parsed.merchant_raw, now),
        )

    summary.newly_enriched = len(rows) if not reenrich else len(rows)
    summary.merchants_classified = classified_count

    # Count merchants after
    merchants_after = conn.execute("SELECT COUNT(*) FROM merchants").fetchone()[0]
    summary.merchants_created = merchants_after - merchants_before

    # Step 4: group streams — categories available from rules/seed are used
    # to gate `is_subscription`. Some merchants may still be uncategorized.
    stream_results = group_streams(conn)
    summary.streams_computed = len(stream_results)

    # Step 5: stream-context classifier writes Income / Transfer categories
    # based on stream shape (positive monthly VIR, TRANSFER txn type).
    ctx = classify_from_streams(conn)
    summary.income_tagged = ctx["income_tagged"]
    summary.transfer_tagged = ctx["transfer_tagged"]

    # Step 6: re-run group_streams so the new Income/Transfer categories feed
    # into the subscription gate. stream_id is stable, so this is idempotent —
    # only `is_subscription` / `is_recurring` / `amount_tolerance` may flip.
    if ctx["income_tagged"] or ctx["transfer_tagged"]:
        group_streams(conn)

    conn.commit()
    return summary


# ─────────────────────────────────────────────────────────────────────────────
# Stream-context classifier — runs after group_streams
# ─────────────────────────────────────────────────────────────────────────────

# Sources that may be overwritten by rule-stream inference. 'user' and
# 'curated' are off-limits. NULL is also overwritable but is checked
# explicitly via `category_source IS NULL` in the SQL — it's not an
# IN-clause-comparable value.
_OVERWRITABLE_SOURCES = ("rule", "llm", "rule-stream")
# Pre-format the IN clause once at module load. Trusted input only:
# _OVERWRITABLE_SOURCES is a hardcoded constant, never reached from a
# user-supplied path.
_OVERWRITABLE_IN_SQL = "(" + ", ".join(repr(s) for s in _OVERWRITABLE_SOURCES) + ")"


def classify_from_streams(conn: sqlite3.Connection) -> dict[str, int]:
    """Apply stream-context rules to tag Income and Transfer merchants.

    Runs after `group_streams`. Only writes to merchants whose
    category_source is NULL or one of {'rule', 'llm', 'rule-stream'} —
    never touches user-set or curated categories.

    Rules (first match wins):
      1. Any transaction on the merchant has txn_type='TRANSFER' → Transfer.
      2. Merchant has a recurring monthly VIR stream with positive median
         amount → Income.
    """
    now = datetime.now(UTC).isoformat()
    counts = {"income_tagged": 0, "transfer_tagged": 0}

    # Rule 1: any merchant with at least one TRANSFER txn.
    transfer_ids = [
        r[0]
        for r in conn.execute(
            """
            SELECT DISTINCT e.merchant_id
            FROM tx_enrichment e
            WHERE e.txn_type = 'TRANSFER' AND e.merchant_id IS NOT NULL
            """
        ).fetchall()
    ]
    for mid in transfer_ids:
        cur = conn.execute(
            "UPDATE merchants"
            "   SET category = 'Transfer',"
            "       category_source = 'rule-stream',"
            "       category_confidence = 1.0,"
            "       updated_at = ?"
            " WHERE merchant_id = ?"
            "   AND (category_source IS NULL"
            "        OR category_source IN " + _OVERWRITABLE_IN_SQL + ")",
            (now, mid),
        )
        if cur.rowcount:
            counts["transfer_tagged"] += 1

    # Rule 2: merchants with a positive monthly recurring VIR stream → Income.
    # Use median_amount > 0 as the sign indicator.
    income_ids = [
        r[0]
        for r in conn.execute(
            """
            SELECT DISTINCT s.merchant_id
            FROM streams s
            WHERE s.is_recurring = 1
              AND s.classification = 'monthly'
              AND s.txn_type = 'VIR'
              AND s.median_amount > 0
            """
        ).fetchall()
    ]
    for mid in income_ids:
        cur = conn.execute(
            "UPDATE merchants"
            "   SET category = 'Income',"
            "       category_source = 'rule-stream',"
            "       category_confidence = 1.0,"
            "       updated_at = ?"
            " WHERE merchant_id = ?"
            "   AND (category_source IS NULL"
            "        OR category_source IN " + _OVERWRITABLE_IN_SQL + ")",
            (now, mid),
        )
        if cur.rowcount:
            counts["income_tagged"] += 1

    return counts
