"""Phase 7 — unified advisory subsystem.

One module for all `finance advise *` CLI commands. Each advisory kind
(subscription-overlap / cutback / integral-offer) is a registry entry
`AdvisoryKind(kind, build_rows, schema, prompt_file)`. The core
`advise(conn, kind, ...)` looks up the entry, runs the per-kind row
builder, and delegates to `run_advisory` in `finance.llm.advise` — which
owns cache lookup, LLM call, payload persistence, and `llm_runs`
bookkeeping.

The typed `advise_subscriptions` / `advise_cutbacks` / `advise_integral`
wrappers at the bottom are the public API; callers import them the same
way they imported the old per-kind modules, with a single import path
change.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from importlib.resources import files
from typing import Any, Literal

from pydantic import BaseModel, Field

from finance.analysis.subscriptions import find_overlaps, find_subscriptions
from finance.analysis.trends import _monthly_category_pivot
from finance.llm.advise import AdvisoryResult, hash_rows, run_advisory
from finance.llm.client import DEFAULT_ADVISE_MODEL, LLMClient

# ─────────────────────────────────────────────────────────────────────────────
# Response schemas — one group per advisory kind. Exported so callers (and
# tests) can type-check against them.
# ─────────────────────────────────────────────────────────────────────────────


class Recommendation(BaseModel):
    domain: str
    action: Literal["keep", "consolidate", "drop"]
    services: list[str] = Field(description="All active services you saw in the input.")
    suggested_services: list[str] = Field(description="The subset to keep.")
    monthly_savings: float = Field(ge=0.0)
    rationale: str


class SubscriptionAdvice(BaseModel):
    recommendations: list[Recommendation]


class CutbackSuggestion(BaseModel):
    category: str
    current_monthly: float = Field(ge=0.0)
    suggested_monthly: float = Field(ge=0.0)
    rationale: str
    specific_actions: list[str]


class CutbackAdvice(BaseModel):
    suggestions: list[CutbackSuggestion]


class Bundle(BaseModel):
    theme: str
    components: list[str]
    current_monthly_total: float = Field(ge=0.0)
    potential_saving_monthly: float = Field(ge=0.0)
    rationale: str
    caveat: str


class IntegralAdvice(BaseModel):
    bundles: list[Bundle]


# ─────────────────────────────────────────────────────────────────────────────
# Per-kind row builders — the only genuinely per-module code. Each returns
# (rows, user_message). `rows` feeds the deterministic hash used as the cache
# key; `user_message` is the prompt text the LLM sees.
# ─────────────────────────────────────────────────────────────────────────────


def _build_rows_subscriptions(conn: sqlite3.Connection) -> tuple[list[tuple], str]:
    subs = find_subscriptions(conn, active_only=True)
    overlaps = find_overlaps(conn)

    # Hash: stable per plan — sorted by (stream_id, rounded monthly_cost).
    rows: list[tuple] = []
    for _idx, r in subs.iterrows():
        rows.append((str(r["stream_id"]), round(float(r["monthly_cost"]), 2)))

    # User message: overlap table + per-subscription detail so the LLM knows
    # which services it's reasoning about.
    lines: list[str] = [
        "Active subscriptions grouped by service-domain (EUR/month, sign-preserving):",
        "",
    ]
    if overlaps.empty:
        lines.append("(no domains with multiple active subscriptions)")
    else:
        lines.append("| domain | services | monthly_cost |")
        lines.append("|---|---|---|")
        for _, r in overlaps.iterrows():
            services = ", ".join(r["services"])
            lines.append(f"| {r['domain']} | {services} | {float(r['monthly_cost']):.2f} |")

    lines.append("")
    lines.append("Per-subscription detail:")
    lines.append("| merchant | domain-ish (category) | monthly_cost | count |")
    lines.append("|---|---|---|---|")
    for _, r in subs.iterrows():
        cat = r["category"] or "—"
        lines.append(
            f"| {r['merchant']} | {cat} | {float(r['monthly_cost']):.2f} | {int(r['count'])} |"
        )
    return rows, "\n".join(lines)


def _build_rows_cutbacks(conn: sqlite3.Connection, months: int) -> tuple[list[tuple], str]:
    pivot = _monthly_category_pivot(conn, months)
    subs = find_subscriptions(conn, active_only=True)

    # Hash per plan: (category, year_month, round(total, 0)). Integer EUR so
    # sub-euro noise doesn't invalidate the cache.
    rows: list[tuple] = []
    if not pivot.empty:
        for month in pivot.index:
            for cat, spend in pivot.loc[month].items():
                rows.append((str(cat), str(month), int(round(float(spend), 0))))

    lines: list[str] = [
        f"Category spending by month (last {months} months, EUR outflow):",
        "",
    ]
    if pivot.empty:
        lines.append("(no data)")
    else:
        months_header = list(pivot.index)
        lines.append("| category | " + " | ".join(months_header) + " |")
        lines.append("|---|" + "|".join(["---"] * len(months_header)) + "|")
        for cat in pivot.columns:
            row_vals = " | ".join(f"{float(pivot.loc[m, cat]):.2f}" for m in months_header)
            lines.append(f"| {cat} | {row_vals} |")

    lines.append("")
    lines.append("Active subscriptions:")
    lines.append("| merchant | category | monthly_cost | count |")
    lines.append("|---|---|---|---|")
    for _, r in subs.iterrows():
        cat = r["category"] or "—"
        lines.append(
            f"| {r['merchant']} | {cat} | {float(r['monthly_cost']):.2f} | {int(r['count'])} |"
        )
    return rows, "\n".join(lines)


def _build_rows_integral(conn: sqlite3.Connection) -> tuple[list[tuple], str]:
    overlaps = find_overlaps(conn)

    # Hash per plan: (domain, round(monthly_cost, 2)).
    rows: list[tuple] = []
    if not overlaps.empty:
        for _, r in overlaps.iterrows():
            rows.append((str(r["domain"]), round(float(r["monthly_cost"]), 2)))

    lines: list[str] = [
        "Service-domain overlap summary (monthly EUR outflow; negative = you pay):",
        "",
    ]
    if overlaps.empty:
        lines.append("(no overlapping domains)")
    else:
        lines.append("| domain | services_count | services | monthly_cost |")
        lines.append("|---|---|---|---|")
        for _, r in overlaps.iterrows():
            services = ", ".join(r["services"])
            lines.append(
                f"| {r['domain']} | {int(r['services_count'])} | {services}"
                f" | {float(r['monthly_cost']):.2f} |"
            )
    return rows, "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Registry + dispatcher
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AdvisoryKind:
    kind: str
    build_rows: Callable[..., tuple[list[tuple], str]]
    schema: type[BaseModel]
    prompt_file: str


ADVISORY_KINDS: dict[str, AdvisoryKind] = {
    "subscription_overlap": AdvisoryKind(
        kind="subscription_overlap",
        build_rows=_build_rows_subscriptions,
        schema=SubscriptionAdvice,
        prompt_file="advise_subscriptions_system.md",
    ),
    "cutback": AdvisoryKind(
        kind="cutback",
        build_rows=_build_rows_cutbacks,
        schema=CutbackAdvice,
        prompt_file="advise_cutbacks_system.md",
    ),
    "integral_offer": AdvisoryKind(
        kind="integral_offer",
        build_rows=_build_rows_integral,
        schema=IntegralAdvice,
        prompt_file="advise_integral_system.md",
    ),
}


def _load_prompt(filename: str) -> str:
    return files("finance.llm.prompts").joinpath(filename).read_text()


def advise(
    conn: sqlite3.Connection,
    kind: str,
    *,
    client: LLMClient,
    model: str = DEFAULT_ADVISE_MODEL,
    refresh: bool = False,
    **builder_kwargs: Any,
) -> AdvisoryResult:
    """Dispatch a single advisory call. `builder_kwargs` thread through to
    the per-kind row builder (e.g. `months=6` for cutbacks)."""
    entry = ADVISORY_KINDS[kind]
    rows, user = entry.build_rows(conn, **builder_kwargs)
    return run_advisory(
        conn,
        kind=entry.kind,
        input_hash=hash_rows(rows),
        system=_load_prompt(entry.prompt_file),
        user=user,
        schema=entry.schema,
        client=client,
        model=model,
        refresh=refresh,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Typed public wrappers — preserve the old module-level API so callers only
# need to change their import path. The `months` kwarg on cutbacks needs its
# own signature to stay mypy-typed.
# ─────────────────────────────────────────────────────────────────────────────


def advise_subscriptions(
    conn: sqlite3.Connection,
    *,
    client: LLMClient,
    model: str = DEFAULT_ADVISE_MODEL,
    refresh: bool = False,
) -> AdvisoryResult:
    return advise(conn, "subscription_overlap", client=client, model=model, refresh=refresh)


def advise_cutbacks(
    conn: sqlite3.Connection,
    *,
    client: LLMClient,
    months: int = 6,
    model: str = DEFAULT_ADVISE_MODEL,
    refresh: bool = False,
) -> AdvisoryResult:
    return advise(conn, "cutback", client=client, model=model, refresh=refresh, months=months)


def advise_integral(
    conn: sqlite3.Connection,
    *,
    client: LLMClient,
    model: str = DEFAULT_ADVISE_MODEL,
    refresh: bool = False,
) -> AdvisoryResult:
    return advise(conn, "integral_offer", client=client, model=model, refresh=refresh)
