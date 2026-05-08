from __future__ import annotations

from finance.core.analytics import MetricRegistry, MetricSpec

FINANCE_METRICS = (
    MetricSpec(
        name="monthly_totals",
        dataset="finance.transactions",
        description="Average monthly spend, income, recurring spend, and subscriptions.",
        inputs=("transactions", "tx_enrichment", "merchants", "accounts"),
        measures=("monthly_spend_avg", "monthly_income_avg", "monthly_subscriptions"),
        dimensions=("category", "account_uid"),
        freshness="after sync/enrich",
    ),
    MetricSpec(
        name="subscription_streams",
        dataset="finance.transactions",
        description="Active recurring streams classified as subscriptions.",
        inputs=("transactions", "tx_enrichment", "streams", "merchants"),
        measures=("monthly_cost", "regularity", "median_amount"),
        dimensions=("merchant", "category", "classification"),
        freshness="after enrich",
    ),
    MetricSpec(
        name="merchant_outflow",
        dataset="finance.transactions",
        description="Ranked merchant spend and income flows.",
        inputs=("transactions", "tx_enrichment", "merchants", "merchant_aliases"),
        measures=("total_spend", "total_income", "net_amount"),
        dimensions=("merchant", "category"),
        freshness="after sync/enrich",
    ),
)

FINANCE_METRIC_REGISTRY = MetricRegistry(FINANCE_METRICS)

__all__ = ["FINANCE_METRICS", "FINANCE_METRIC_REGISTRY"]
