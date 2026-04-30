"""Stage C — spending trends over the enriched join.

Pivots expense (amount < 0) by month × category; computes MoM %, top
growers/shrinkers. All read-only, pandas only.
"""

from __future__ import annotations

import sqlite3

import pandas as pd

from finance.analysis.io import load_transactions

_UNCATEGORIZED_LABEL = "Uncategorized"


def _monthly_category_pivot(
    conn: sqlite3.Connection,
    months: int,
    *,
    spend_only: bool = False,
) -> pd.DataFrame:
    """Return a DataFrame indexed by YYYY-MM, columns=category, values=|spend|.

    Excludes non-EUR transactions (principle 7). Income (amount > 0) is dropped;
    only outflows are shown. `spend_only=True` also drops accounts flagged as
    `excluded_from_spend`.
    """
    df = load_transactions(conn, spend_only=spend_only)
    if df.empty:
        return pd.DataFrame()

    df = df[~df["currency_excluded"]]
    df = df[df["amount"] < 0].copy()
    df["category"] = df["category"].fillna(_UNCATEGORIZED_LABEL).replace("", _UNCATEGORIZED_LABEL)
    df["spend"] = df["amount"].abs()
    df["month"] = df["booking_date"].dt.to_period("M").astype(str)

    cutoff = (pd.Timestamp.today() - pd.DateOffset(months=months)).to_period("M").strftime("%Y-%m")
    df = df[df["month"] >= cutoff]

    if df.empty:
        return pd.DataFrame()

    pivot = df.pivot_table(
        index="month",
        columns="category",
        values="spend",
        aggfunc="sum",
        fill_value=0.0,
    ).sort_index()
    return pivot


def mom_changes(
    conn: sqlite3.Connection,
    *,
    months: int = 6,
    spend_only: bool = False,
) -> pd.DataFrame:
    """Month-over-month category spending with absolute + percentage deltas.

    Columns: category, prev_month, curr_month, prev_spend, curr_spend,
    delta_abs, delta_pct.

    Compares the two most recent months in the window. Returns empty if fewer
    than two months of data. `spend_only` drops `excluded_from_spend` accounts.
    """
    pivot = _monthly_category_pivot(conn, months, spend_only=spend_only)
    if pivot.shape[0] < 2:
        return pd.DataFrame(
            columns=[
                "category",
                "prev_month",
                "curr_month",
                "prev_spend",
                "curr_spend",
                "delta_abs",
                "delta_pct",
            ]
        )

    prev_month, curr_month = pivot.index[-2], pivot.index[-1]
    prev = pivot.loc[prev_month]
    curr = pivot.loc[curr_month]

    out = pd.DataFrame(
        {
            "category": prev.index,
            "prev_month": prev_month,
            "curr_month": curr_month,
            "prev_spend": prev.values,
            "curr_spend": curr.values,
        }
    )
    out["delta_abs"] = out["curr_spend"] - out["prev_spend"]
    out["delta_pct"] = out.apply(lambda r: _pct_change(r["prev_spend"], r["curr_spend"]), axis=1)
    out = out.sort_values("delta_abs", ascending=False).reset_index(drop=True)
    return out


def category_growth(
    conn: sqlite3.Connection,
    *,
    months: int = 6,
    spend_only: bool = False,
) -> pd.DataFrame:
    """Per-category growth over the full window.

    Columns: category, total_spend, first_month_spend, last_month_spend,
    trend_pct, avg_monthly.

    `trend_pct` compares first-month vs last-month spending; coarse but legible.
    """
    pivot = _monthly_category_pivot(conn, months, spend_only=spend_only)
    if pivot.empty:
        return pd.DataFrame(
            columns=[
                "category",
                "total_spend",
                "first_month_spend",
                "last_month_spend",
                "trend_pct",
                "avg_monthly",
            ]
        )

    first = pivot.iloc[0]
    last = pivot.iloc[-1]
    total = pivot.sum(axis=0)
    n_months = pivot.shape[0]

    out = pd.DataFrame(
        {
            "category": total.index,
            "total_spend": total.values,
            "first_month_spend": first.reindex(total.index).fillna(0.0).values,
            "last_month_spend": last.reindex(total.index).fillna(0.0).values,
        }
    )
    out["avg_monthly"] = out["total_spend"] / max(n_months, 1)
    out["trend_pct"] = out.apply(
        lambda r: _pct_change(r["first_month_spend"], r["last_month_spend"]), axis=1
    )
    out = out.sort_values("total_spend", ascending=False).reset_index(drop=True)
    return out


def _pct_change(prev: float, curr: float) -> float:
    if prev == 0 and curr == 0:
        return 0.0
    if prev == 0:
        return float("inf")
    return (curr - prev) / prev * 100.0
