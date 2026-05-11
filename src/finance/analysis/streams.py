"""Stage B — stream grouping, cadence detection, subscription/recurring flags.

A "stream" = transactions sharing merchant, amount band, sign, currency, and
transaction type class over time.
"""

from __future__ import annotations

import contextlib
import hashlib
import math
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime
from statistics import median, stdev

from finance.taxonomy import assert_subset_of_taxonomy

_BAND_WIDTH = 0.15  # flat ±15% for stream_id computation

# Merchant categories that should NEVER be flagged as a subscription even when
# the structural rule (recurring + monthly + stable amount + low variance)
# matches.
#
# Transport and Entertainment are NOT in this set — they legitimately have
# real subscriptions (Navigo monthly transit €90.80, Spotify, etc.) and the
# amount-variance check (±5% for monthly) already rules out false positives
# like "€5.99 monthly Uber coincidence".
#
#   - Dining / Groceries: no realistic monthly subscription pattern; always
#     a coincidence worth ignoring.
#   - Income / Transfer / Investment: structurally the opposite of a
#     subscription (salary, internal moves, broker deposits).
NON_SUBSCRIPTION_CATEGORIES = frozenset(
    {
        "Dining",
        "Groceries",
        "Income",
        "Transfer",
        "Investment",
    }
)
assert_subset_of_taxonomy(NON_SUBSCRIPTION_CATEGORIES, source="analysis/streams.py")


@dataclass
class StreamInfo:
    stream_id: str
    merchant_id: int
    txn_type: str | None
    txn_type_class: str
    amount_sign: int
    currency: str
    median_amount: float
    amount_tolerance: float
    median_days: int | None
    regularity: float
    classification: str
    is_recurring: bool
    is_subscription: bool
    active: bool
    first_seen: str
    last_seen: str
    count: int


@dataclass(frozen=True)
class StreamOverrideIssue:
    old_stream_id: str
    subscription_override: int
    new_stream_ids: tuple[str, ...]
    tx_ids: tuple[str, ...]


@dataclass(frozen=True)
class StreamOverrideReport:
    preserved: dict[str, str]
    split: tuple[StreamOverrideIssue, ...]
    orphaned: tuple[StreamOverrideIssue, ...]

    @property
    def has_issues(self) -> bool:
        return bool(self.split or self.orphaned)


def _band_bucket(amount: float) -> int:
    """Map an amount to a discrete band bucket.

    Amounts within ±15% of each other map to the same bucket.
    The bucket key is floor(log_{1+2*w}(|amount|)).
    """
    abs_amt = abs(amount) if amount != 0 else 0.01
    base = 1 + 2 * _BAND_WIDTH  # 1.30
    return int(math.floor(math.log(abs_amt, base)))


def _amount_sign(amount: float) -> int:
    if amount > 0:
        return 1
    if amount < 0:
        return -1
    return 0


def _txn_type_class(txn_type: str | None) -> str:
    if txn_type in {"PRLV", "FACTURE", "FRAIS", "RETRAIT", "INTERETS"}:
        return txn_type
    if txn_type in {"VIR", "VIREMENT", "TRANSFER"}:
        return "TRANSFER"
    return "OTHER"


def _make_stream_id(
    merchant_id: int,
    bucket: int,
    amount_sign: int = 0,
    currency: str | None = None,
    txn_type_class: str | None = None,
) -> str:
    raw = f"{merchant_id}:{bucket}:{amount_sign}:{currency or ''}:{txn_type_class or ''}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def _classify_cadence(median_days: float | None) -> str:
    if median_days is None:
        return "irregular"
    if median_days <= 10:
        return "weekly"
    if median_days <= 45:
        return "monthly"
    if median_days <= 120:
        return "quarterly"
    if median_days <= 400:
        return "annual"
    return "irregular"


def _amount_tolerance_for(classification: str) -> float:
    return {
        "weekly": 0.25,
        "monthly": 0.05,
        "quarterly": 0.10,
        "annual": 0.10,
        "irregular": 0.15,
    }.get(classification, 0.15)


def group_streams(conn: sqlite3.Connection) -> list[StreamInfo]:
    """Compute streams from enriched transactions. Writes/updates `streams` table.

    1. Group enriched tx by (merchant_id, band_bucket).
    2. For each group: compute cadence, regularity, recurring/subscription flags.
    3. Upsert into streams table.
    4. Update tx_enrichment.stream_id for each member.
    """
    now = datetime.now(UTC).isoformat()

    rows = conn.execute("""
        SELECT e.tx_id, e.stream_id AS old_stream_id, e.merchant_id, e.txn_type,
               t.amount, t.currency, t.booking_date
        FROM tx_enrichment e
        JOIN transactions t ON t.tx_uid = e.tx_id
        WHERE e.merchant_id IS NOT NULL
        ORDER BY e.merchant_id, t.booking_date
    """).fetchall()
    override_report = report_orphan_overrides(conn, rows=rows)
    carried_overrides = {
        new_sid: int(
            conn.execute(
                "SELECT subscription_override FROM streams WHERE stream_id = ?",
                (old_sid,),
            ).fetchone()["subscription_override"]
        )
        for old_sid, new_sid in override_report.preserved.items()
    }

    # Group by (merchant_id, band_bucket, sign, currency, transaction type class)
    groups: dict[str, list[dict]] = {}
    for r in rows:
        merchant_id = r["merchant_id"]
        amount = r["amount"]
        bucket = _band_bucket(amount)
        sign = _amount_sign(amount)
        currency = r["currency"] or ""
        txn_class = _txn_type_class(r["txn_type"])
        sid = _make_stream_id(merchant_id, bucket, sign, currency, txn_class)
        groups.setdefault(sid, []).append(
            {
                "tx_id": r["tx_id"],
                "merchant_id": merchant_id,
                "txn_type": r["txn_type"],
                "txn_type_class": txn_class,
                "amount_sign": sign,
                "currency": currency,
                "amount": amount,
                "booking_date": r["booking_date"],
            }
        )

    results: list[StreamInfo] = []
    for sid, txns in groups.items():
        merchant_id = txns[0]["merchant_id"]
        txn_type = txns[0]["txn_type"]
        txn_type_class = txns[0]["txn_type_class"]
        amount_sign = txns[0]["amount_sign"]
        currency = txns[0]["currency"]
        amounts = [t["amount"] for t in txns]
        dates = sorted(t["booking_date"] for t in txns if t["booking_date"])

        med_amount = median(amounts)
        med_amount_minor = round(float(med_amount) * 100) if currency == "EUR" else None
        count = len(txns)

        # Cadence from consecutive date diffs
        median_days_val: float | None = None
        reg = 0.0
        if len(dates) >= 2:
            parsed = []
            for d in dates:
                with contextlib.suppress(ValueError, TypeError):
                    parsed.append(date.fromisoformat(d[:10]))
            if len(parsed) >= 2:
                diffs = [(parsed[i + 1] - parsed[i]).days for i in range(len(parsed) - 1)]
                diffs = [d for d in diffs if d > 0]
                if diffs:
                    median_days_val = median(diffs)
                    if len(diffs) >= 2 and median_days_val > 0:
                        reg = max(0.0, 1.0 - (stdev(diffs) / median_days_val))

        classification = _classify_cadence(median_days_val)
        tolerance = _amount_tolerance_for(classification)

        # Non-PRLV streams need 3+ hits, good regularity, and long enough span
        # to rule out a short coincidence. 45 days covers three ~28-day cycles
        # (Google bills every 28d, not 30). Lower than that, a "burst of 3
        # purchases in 6 weeks" could false-positive.
        is_recurring = txn_type == "PRLV" or (
            reg > 0.7 and count >= 3 and len(dates) >= 2 and _span_days(dates) >= 45
        )

        # Amount variance check for subscription flag
        amt_var_ok = True
        if med_amount != 0 and len(amounts) >= 2:
            amt_spread = max(abs(a - med_amount) for a in amounts) / abs(med_amount)
            amt_var_ok = amt_spread <= tolerance

        # Category gate — see NON_SUBSCRIPTION_CATEGORIES above.
        cat_row = conn.execute(
            "SELECT category FROM merchants WHERE merchant_id = ?",
            (merchant_id,),
        ).fetchone()
        current_cat = cat_row[0] if cat_row else None

        # Two ways a stream gets flagged as a subscription:
        #   (a) Strong semantic signal: merchant is categorized 'Subscriptions'
        #       + has a periodic cadence + at least 2 hits. Trust the label —
        #       skip the strict regularity/span/variance gymnastics.
        #   (b) Structural detection: recurring + stable amount + monthly-ish
        #       + not in NON_SUBSCRIPTION_CATEGORIES.
        # Categories that ~always imply a subscription when the stream is
        # monthly-ish with 2+ hits. Structural fallback handles the rest.
        is_labeled_sub = (
            current_cat in ("Subscriptions", "AI", "SaaS")
            and classification in ("monthly", "quarterly", "annual")
            and count >= 2
        )
        computed_sub = is_labeled_sub or (
            is_recurring
            and classification in ("monthly", "quarterly", "annual")
            and amt_var_ok
            and current_cat not in NON_SUBSCRIPTION_CATEGORIES
        )
        # User override wins — NULL → use computed, 0 → force False, 1 → force True.
        override_row = conn.execute(
            "SELECT subscription_override FROM streams WHERE stream_id = ?",
            (sid,),
        ).fetchone()
        override = override_row[0] if override_row else carried_overrides.get(sid)
        is_sub = computed_sub if override is None else bool(override)

        first_seen = dates[0] if dates else now
        last_seen = dates[-1] if dates else now

        # Active check
        try:
            ls = date.fromisoformat(str(last_seen)[:10])
            days_since = (date.today() - ls).days
            active = median_days_val is not None and days_since < 1.5 * median_days_val
        except (ValueError, TypeError):
            active = False

        info = StreamInfo(
            stream_id=sid,
            merchant_id=merchant_id,
            txn_type=txn_type,
            txn_type_class=txn_type_class,
            amount_sign=amount_sign,
            currency=currency,
            median_amount=med_amount,
            amount_tolerance=tolerance,
            median_days=int(median_days_val) if median_days_val is not None else None,
            regularity=round(reg, 3),
            classification=classification,
            is_recurring=is_recurring,
            is_subscription=is_sub,
            active=active,
            first_seen=str(first_seen),
            last_seen=str(last_seen),
            count=count,
        )
        results.append(info)

        # Upsert stream
        conn.execute(
            """
            INSERT INTO streams (stream_id, merchant_id, txn_type, txn_type_class,
              amount_sign, currency, median_amount, median_amount_minor,
              amount_tolerance, median_days, regularity, classification,
              is_recurring, is_subscription, subscription_override, active, first_seen,
              last_seen, count, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(stream_id) DO UPDATE SET
              txn_type=excluded.txn_type,
              txn_type_class=excluded.txn_type_class,
              amount_sign=excluded.amount_sign,
              currency=excluded.currency,
              median_amount=excluded.median_amount,
              median_amount_minor=excluded.median_amount_minor,
              amount_tolerance=excluded.amount_tolerance,
              median_days=excluded.median_days,
              regularity=excluded.regularity,
              classification=excluded.classification,
              is_recurring=excluded.is_recurring,
              is_subscription=excluded.is_subscription,
              subscription_override=COALESCE(streams.subscription_override, excluded.subscription_override),
              active=excluded.active,
              first_seen=excluded.first_seen,
              last_seen=excluded.last_seen,
              count=excluded.count,
              updated_at=excluded.updated_at
        """,
            (
                sid,
                merchant_id,
                txn_type,
                txn_type_class,
                amount_sign,
                currency,
                med_amount,
                med_amount_minor,
                tolerance,
                info.median_days,
                reg,
                classification,
                int(is_recurring),
                int(is_sub),
                override,
                int(active),
                str(first_seen),
                str(last_seen),
                count,
                now,
            ),
        )

        # Update tx_enrichment.stream_id for members
        tx_ids = [t["tx_id"] for t in txns]
        for tid in tx_ids:
            conn.execute("UPDATE tx_enrichment SET stream_id = ? WHERE tx_id = ?", (sid, tid))

    live_stream_ids = list(groups)
    if live_stream_ids:
        placeholders = ",".join("?" for _ in live_stream_ids)
        conn.execute(
            f"DELETE FROM streams WHERE stream_id NOT IN ({placeholders})",
            live_stream_ids,
        )
    else:
        conn.execute("DELETE FROM streams")

    return results


def report_orphan_overrides(
    conn: sqlite3.Connection, *, rows: list[sqlite3.Row] | None = None
) -> StreamOverrideReport:
    """Report existing stream overrides that would not map cleanly after repartitioning."""
    override_rows = conn.execute(
        """
        SELECT stream_id, subscription_override
        FROM streams
        WHERE subscription_override IS NOT NULL
        """
    ).fetchall()
    if not override_rows:
        return StreamOverrideReport(preserved={}, split=(), orphaned=())
    overrides = {r["stream_id"]: int(r["subscription_override"]) for r in override_rows}
    if rows is None:
        rows = conn.execute(
            """
            SELECT e.tx_id, e.stream_id AS old_stream_id, e.merchant_id, e.txn_type,
                   t.amount, t.currency, t.booking_date
            FROM tx_enrichment e
            JOIN transactions t ON t.tx_uid = e.tx_id
            WHERE e.merchant_id IS NOT NULL
            """
        ).fetchall()

    old_to_new: dict[str, set[str]] = {sid: set() for sid in overrides}
    old_to_tx: dict[str, list[str]] = {sid: [] for sid in overrides}
    for r in rows:
        old_sid = r["old_stream_id"]
        if old_sid not in overrides:
            continue
        bucket = _band_bucket(float(r["amount"]))
        sign = _amount_sign(float(r["amount"]))
        new_sid = _make_stream_id(
            int(r["merchant_id"]),
            bucket,
            sign,
            r["currency"] or "",
            _txn_type_class(r["txn_type"]),
        )
        old_to_new[old_sid].add(new_sid)
        old_to_tx[old_sid].append(str(r["tx_id"]))

    preserved: dict[str, str] = {}
    split: list[StreamOverrideIssue] = []
    orphaned: list[StreamOverrideIssue] = []
    for old_sid, override in overrides.items():
        new_sids = tuple(sorted(old_to_new[old_sid]))
        issue = StreamOverrideIssue(
            old_stream_id=old_sid,
            subscription_override=override,
            new_stream_ids=new_sids,
            tx_ids=tuple(old_to_tx[old_sid]),
        )
        if len(new_sids) == 1:
            preserved[old_sid] = new_sids[0]
        elif new_sids:
            split.append(issue)
        else:
            orphaned.append(issue)

    return StreamOverrideReport(
        preserved=preserved,
        split=tuple(split),
        orphaned=tuple(orphaned),
    )


def _span_days(dates: list[str]) -> int:
    if len(dates) < 2:
        return 0
    try:
        d0 = date.fromisoformat(dates[0][:10])
        d1 = date.fromisoformat(dates[-1][:10])
        return (d1 - d0).days
    except (ValueError, TypeError):
        return 0
