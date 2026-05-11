"""Rollup totals across the enriched store.

One function `compute_totals` returns a dataclass with the four headline
figures (monthly subs / monthly recurring / monthly income avg /
monthly spend avg) plus a per-category breakdown. Pure read; no DB writes.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

import pandas as pd

from finance.analysis.io import load_transactions
from finance.taxonomy import assert_subset_of_taxonomy

# "Essential" recurring costs — things you must pay to live your life (rent,
# utilities, loan repayment, insurance, commute pass, etc.). Cuts here have
# real life impact.
ESSENTIAL_CATEGORIES = frozenset(
    {
        "Utilities",
        "Housing",
        "Loan",
        "Insurance",
        "Telecom",
        "Transport",
    }
)

# "Optional" recurring costs — subscriptions, gym, streaming, AI tools, SaaS.
# Cuttable without logistical disruption.
OPTIONAL_CATEGORIES = frozenset(
    {
        "Subscriptions",
        "Entertainment",
        "Education",
        "Health",
        "AI",
        "SaaS",
    }
)

assert_subset_of_taxonomy(ESSENTIAL_CATEGORIES | OPTIONAL_CATEGORIES, source="analysis/totals.py")

# Back-compat aliases — don't break existing imports if any.
INEVITABLE_CATEGORIES = ESSENTIAL_CATEGORIES
DISCRETIONARY_CATEGORIES = OPTIONAL_CATEGORIES


@dataclass
class Totals:
    monthly_subscriptions: float = 0.0
    monthly_recurring_spend: float = 0.0  # negative flows on recurring streams
    monthly_income_avg: float = 0.0
    monthly_spend_avg: float = 0.0
    spend_by_category: dict[str, float] = field(default_factory=dict)
    # Recurring-spend breakdown into must-pay vs optional + the remainder.
    # Sum: essential + optional + other_recurring + variable = monthly_spend_avg.
    monthly_essential: float = 0.0
    monthly_optional: float = 0.0
    monthly_other_recurring: float = 0.0
    monthly_variable: float = 0.0
    essential_by_category: dict[str, float] = field(default_factory=dict)
    optional_by_category: dict[str, float] = field(default_factory=dict)

    # SUBSCRIPTION-only version of the same split. Sum reconciles to
    # monthly_subscriptions. Used on /subscriptions so the page is internally
    # consistent (Essential + Optional + Other = Monthly Total).
    monthly_sub_essential: float = 0.0
    monthly_sub_optional: float = 0.0
    monthly_sub_other: float = 0.0
    sub_essential_by_category: dict[str, float] = field(default_factory=dict)
    sub_optional_by_category: dict[str, float] = field(default_factory=dict)

    window_months: int = 3

    # Back-compat property aliases so older callers keep working.
    @property
    def monthly_inevitables(self) -> float:
        return self.monthly_essential

    @property
    def monthly_discretionary(self) -> float:
        return self.monthly_optional

    @property
    def inevitables_by_category(self) -> dict[str, float]:
        return self.essential_by_category

    @property
    def discretionary_by_category(self) -> dict[str, float]:
        return self.optional_by_category


def compute_totals(
    conn: sqlite3.Connection,
    *,
    months: int = 3,
    spend_only: bool = True,
) -> Totals:
    """Rollup figures over the trailing `months` window.

    `spend_only=True` drops accounts flagged `excluded_from_spend` AND
    Transfer-categorized rows (see `load_transactions`). Recommended.

    - `monthly_subscriptions`: sum of active is_subscription monthly_cost (abs).
    - `monthly_recurring_spend`: sum of active is_recurring monthly_cost for
      streams with negative typical amounts.
    - `monthly_income_avg`: avg monthly inflow over the window, where
      category='Income' (from Stage D stream classifier) or positive and
      recurring.
    - `monthly_spend_avg`: avg monthly outflow (amount < 0) over the window.
    - `spend_by_category`: per-category monthly avg, sorted desc.
    """
    t = Totals(window_months=months)

    # Streams-driven subscription / recurring rollup (cross-account by design).
    rows = conn.execute(
        """
        SELECT s.classification, s.median_amount, s.median_days,
               s.is_subscription, s.is_recurring, s.txn_type,
               m.category AS category
        FROM streams s
        LEFT JOIN merchants m ON m.merchant_id = s.merchant_id
        WHERE s.active = 1
          AND COALESCE(s.currency, 'EUR') = 'EUR'
          AND (
            ? = 0 OR EXISTS (
              SELECT 1
              FROM tx_enrichment e
              JOIN transactions tx ON tx.tx_uid = e.tx_id
              JOIN accounts a ON a.account_uid = tx.account_uid
              WHERE e.stream_id = s.stream_id
                AND COALESCE(a.excluded_from_spend, 0) = 0
                AND tx.currency = 'EUR'
            )
          )
        """,
        (1 if spend_only else 0,),
    ).fetchall()
    for r in rows:
        monthly = _monthly_cost(r["median_amount"], r["classification"], r["median_days"])
        abs_monthly = abs(monthly)
        cat = r["category"] or "Uncategorized"

        # All-subs rollup (what the /subscriptions "Monthly total" shows).
        if r["is_subscription"]:
            t.monthly_subscriptions += abs_monthly
            # Split subs themselves into essential / optional / other.
            if cat in ESSENTIAL_CATEGORIES:
                t.monthly_sub_essential += abs_monthly
                t.sub_essential_by_category[cat] = (
                    t.sub_essential_by_category.get(cat, 0.0) + abs_monthly
                )
            elif cat in OPTIONAL_CATEGORIES:
                t.monthly_sub_optional += abs_monthly
                t.sub_optional_by_category[cat] = (
                    t.sub_optional_by_category.get(cat, 0.0) + abs_monthly
                )
            else:
                t.monthly_sub_other += abs_monthly

        # All-recurring-outflow split (used on /overview). Slightly different
        # population: catches recurring streams that aren't sub-flagged too.
        if r["is_recurring"] and monthly < 0:
            t.monthly_recurring_spend += abs_monthly
            if cat in ESSENTIAL_CATEGORIES:
                t.monthly_essential += abs_monthly
                t.essential_by_category[cat] = t.essential_by_category.get(cat, 0.0) + abs_monthly
            elif cat in OPTIONAL_CATEGORIES:
                t.monthly_optional += abs_monthly
                t.optional_by_category[cat] = t.optional_by_category.get(cat, 0.0) + abs_monthly
            else:
                t.monthly_other_recurring += abs_monthly

    # Transaction-driven spend / income averages.
    df = load_transactions(conn, spend_only=spend_only)
    if df.empty:
        return t

    df = df[~df["currency_excluded"]].copy()
    cutoff = pd.Timestamp.today().normalize() - pd.DateOffset(months=months)
    df = df[df["booking_date"] >= cutoff]
    if df.empty:
        return t

    outflow = df[df["amount"] < 0].copy()
    inflow = df[df["amount"] > 0].copy()

    # Use actual window-span months (min 1) so a partial month doesn't understate.
    span_months = max(1.0, months)

    t.monthly_spend_avg = float(outflow["amount"].abs().sum()) / span_months
    t.monthly_income_avg = float(inflow["amount"].sum()) / span_months

    outflow["cat"] = outflow["category"].fillna("Uncategorized")
    by_cat = (outflow.groupby("cat")["amount"].sum().abs() / span_months).sort_values(
        ascending=False
    )
    t.spend_by_category = {str(k): float(v) for k, v in by_cat.items()}

    # Sort the essential / optional breakdowns biggest-first for display.
    t.essential_by_category = dict(
        sorted(
            t.essential_by_category.items(),
            key=lambda kv: kv[1],
            reverse=True,
        )
    )
    t.optional_by_category = dict(
        sorted(
            t.optional_by_category.items(),
            key=lambda kv: kv[1],
            reverse=True,
        )
    )
    t.sub_essential_by_category = dict(
        sorted(
            t.sub_essential_by_category.items(),
            key=lambda kv: kv[1],
            reverse=True,
        )
    )
    t.sub_optional_by_category = dict(
        sorted(
            t.sub_optional_by_category.items(),
            key=lambda kv: kv[1],
            reverse=True,
        )
    )

    # Variable = everything outflow that's NOT already in essential / optional /
    # other-recurring. Computing from the side: variable = total_spend - recurring.
    # Can go slightly negative if recurring is extrapolated from streams past
    # the window edge; clamp to zero.
    t.monthly_variable = max(
        0.0,
        t.monthly_spend_avg
        - (t.monthly_essential + t.monthly_optional + t.monthly_other_recurring),
    )

    return t


def _monthly_cost(
    amount: float | None, classification: str | None, median_days: int | None
) -> float:
    if amount is None:
        return 0.0
    amount = float(amount)
    c = classification or ""
    if c == "weekly":
        return amount * 52 / 12
    if c == "monthly":
        return amount
    if c == "quarterly":
        return amount / 3
    if c == "annual":
        return amount / 12
    if median_days and median_days > 0:
        return amount * 30.4375 / median_days
    return amount
