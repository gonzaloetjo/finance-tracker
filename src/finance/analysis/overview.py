"""Stage C — composed dashboard across all structural analyses.

Pure composition. No new SQL specific to overview — every DataFrame comes
from an existing Stage C function. Rendering lives in the CLI.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

import pandas as pd

from finance.analysis.alerts import new_large_merchants, subscription_stopped
from finance.analysis.forecast import next_expected_charges
from finance.analysis.merchants import top_merchants
from finance.analysis.recurring import find_recurring
from finance.analysis.subscriptions import find_overlaps, find_subscriptions
from finance.analysis.totals import Totals, compute_totals
from finance.analysis.trends import mom_changes


@dataclass
class AccountSummary:
    account_uid: str
    aspsp_name: str
    name: str | None
    currency: str | None
    n_tx: int
    excluded_from_spend: bool


@dataclass
class OverviewData:
    accounts: list[AccountSummary] = field(default_factory=list)
    totals: Totals = field(default_factory=Totals)
    trends: pd.DataFrame = field(default_factory=pd.DataFrame)
    top_merchants: pd.DataFrame = field(default_factory=pd.DataFrame)
    recurring: pd.DataFrame = field(default_factory=pd.DataFrame)
    subscriptions: pd.DataFrame = field(default_factory=pd.DataFrame)
    overlaps: pd.DataFrame = field(default_factory=pd.DataFrame)
    forecast: pd.DataFrame = field(default_factory=pd.DataFrame)
    new_large: pd.DataFrame = field(default_factory=pd.DataFrame)
    stopped: pd.DataFrame = field(default_factory=pd.DataFrame)


def _load_accounts(conn: sqlite3.Connection) -> list[AccountSummary]:
    rows = conn.execute(
        """
        SELECT a.account_uid, a.name, a.currency,
               COALESCE(a.excluded_from_spend, 0) AS excluded,
               s.aspsp_name,
               (SELECT COUNT(*) FROM transactions t WHERE t.account_uid = a.account_uid) AS n_tx
        FROM accounts a
        JOIN sessions s ON s.session_id = a.session_id
        WHERE s.revoked_at IS NULL
        ORDER BY s.aspsp_name, a.name
        """
    ).fetchall()
    return [
        AccountSummary(
            account_uid=r["account_uid"],
            aspsp_name=r["aspsp_name"],
            name=r["name"],
            currency=r["currency"],
            n_tx=r["n_tx"],
            excluded_from_spend=bool(r["excluded"]),
        )
        for r in rows
    ]


def build_overview(
    conn: sqlite3.Connection,
    *,
    months: int = 3,
    top_n: int = 15,
    forecast_days: int = 30,
    spend_only: bool = False,
    threshold: float = 500.0,
) -> OverviewData:
    """Compose all Stage C views into one result object.

    `spend_only` is only honored where it has meaning (trends, top merchants).
    Stream-based views (recurring/subscriptions/forecast/alerts) aggregate
    across accounts by merchant, so excluding an account at the stream level
    would require rebuilding the streams table — out of scope.
    """
    return OverviewData(
        accounts=_load_accounts(conn),
        totals=compute_totals(conn, months=months, spend_only=spend_only),
        trends=mom_changes(conn, months=months, spend_only=spend_only),
        top_merchants=top_merchants(
            conn,
            limit=top_n,
            spend_only=spend_only,
        ),
        recurring=find_recurring(conn, active_only=True),
        subscriptions=find_subscriptions(conn, active_only=True),
        overlaps=find_overlaps(conn),
        forecast=next_expected_charges(conn, horizon_days=forecast_days),
        new_large=new_large_merchants(conn, amount_threshold=threshold),
        stopped=subscription_stopped(conn),
    )
