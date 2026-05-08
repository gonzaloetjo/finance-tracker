"""Phase 7 Part 1 — LLM categorization of the uncategorized-merchant long tail.

Pipeline:
  1. Query merchants with `category IS NULL` (or where `category_source` is
     already `llm`).
  2. For each, gather up to 3 example memos.
  3. One structured-output LLM call returns (category, confidence, reasoning).
  4. Confidence-gated auto-write: ≥ AUTO_WRITE_THRESHOLD writes to
     merchants.category with `source='llm'`; lower-confidence proposals
     surface in a candidates table.

User, curated, and rule-based categories are preserved; the LLM only fills
empty merchants or refreshes its own prior assignments.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib.resources import files

import yaml
from pydantic import BaseModel, Field

from finance.llm.client import (
    DEFAULT_CATEGORIZE_MODEL,
    LLMUsage,
    _utcnow,
    finish_run,
    start_run,
)

# Confidence threshold for auto-write. Below this, we persist the proposal
# to `llm_proposals` for user review via the Uncategorized page.
AUTO_WRITE_THRESHOLD = 0.73


def load_taxonomy() -> list[str]:
    path = files("finance.llm.prompts").joinpath("taxonomy.yaml")
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, list):
        return []
    return [str(c).strip() for c in data if c]


def _load_system_prompt() -> str:
    template = files("finance.llm.prompts").joinpath("categorize_system.md").read_text()
    taxonomy = load_taxonomy()
    taxonomy_bullets = "\n".join(f"- {c}" for c in taxonomy)
    return template.replace("{taxonomy}", taxonomy_bullets)


# -------- Input gathering --------------------------------------------------


@dataclass
class MerchantInput:
    merchant_id: int
    canonical_name: str
    example_memos: list[str]


def collect_uncategorized(
    conn: sqlite3.Connection,
    *,
    limit: int = 150,
) -> list[MerchantInput]:
    """Merchants whose category is NULL or already LLM-sourced.

    Ordered by total outflow (biggest spend first) so a --limit cut still
    covers the highest-value merchants.
    """
    rows = conn.execute(
        """
        SELECT
          m.merchant_id,
          m.canonical_name,
          COALESCE(SUM(CASE WHEN t.amount < 0 THEN -t.amount ELSE 0 END), 0) AS spend
        FROM merchants m
        LEFT JOIN tx_enrichment e ON e.merchant_id = m.merchant_id
        LEFT JOIN transactions t  ON t.transaction_id = e.tx_id
        WHERE (m.category IS NULL
               OR m.category_source = 'llm')
        GROUP BY m.merchant_id
        ORDER BY spend DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

    inputs: list[MerchantInput] = []
    for r in rows:
        memos = conn.execute(
            """
            SELECT t.remittance_info
            FROM tx_enrichment e
            JOIN transactions t ON t.transaction_id = e.tx_id
            WHERE e.merchant_id = ?
            ORDER BY t.booking_date DESC
            LIMIT 3
            """,
            (r["merchant_id"],),
        ).fetchall()
        inputs.append(
            MerchantInput(
                merchant_id=r["merchant_id"],
                canonical_name=r["canonical_name"],
                example_memos=[m[0] for m in memos if m[0]],
            )
        )
    return inputs


# -------- Structured output schema ----------------------------------------


class CategoryResult(BaseModel):
    canonical_name: str = Field(description="Merchant canonical name as given in the input.")
    category: str = Field(description="One category from the allowed taxonomy.")
    confidence: float = Field(ge=0.0, le=1.0, description="0..1 confidence.")
    reasoning: str = Field(description="One short sentence justifying the assignment.")


class CategorizeResponse(BaseModel):
    results: list[CategoryResult]


# -------- User message formatting -----------------------------------------


def build_user_message(merchants: list[MerchantInput]) -> str:
    lines: list[str] = [
        "Categorize each of the following merchants. Return one result per merchant, in the same order.",
        "",
    ]
    for i, m in enumerate(merchants, start=1):
        lines.append(f"{i}. {m.canonical_name}")
        if m.example_memos:
            for memo in m.example_memos:
                lines.append(f"   - {memo[:140]}")
        else:
            lines.append("   (no example memos)")
    return "\n".join(lines)


# -------- Orchestrator -----------------------------------------------------


@dataclass
class CategorizeSummary:
    proposed: int = 0
    written: int = 0
    low_confidence: int = 0
    usage: LLMUsage = field(default_factory=LLMUsage)
    proposals: list[CategoryResult] = field(default_factory=list)
    model: str = ""
    errors: list[str] = field(default_factory=list)
    batches: int = 0
    failed_batches: int = 0


def categorize_uncategorized(
    conn: sqlite3.Connection,
    *,
    client,  # LLMClient | ClaudeCLIProvider — duck-typed on parse_structured
    limit: int = 150,
    batch_size: int | None = None,
    model: str = DEFAULT_CATEGORIZE_MODEL,
    dry_run: bool = False,
) -> CategorizeSummary:
    """Run the LLM pass in batches. Writes merchants.category for confident
    proposals, surfaces low-confidence ones in the returned summary.

    `batch_size=None` auto-picks based on provider:
      - API (fast, single call per batch)           → 40 merchants
      - Claude CLI (subprocess per batch, slow)     → 8 merchants
    Smaller batches on claude-cli mean more frequent progress updates in the
    `llm_runs` table, which the web UI polls.
    """
    merchants = collect_uncategorized(conn, limit=limit)
    summary = CategorizeSummary(model=model)
    if not merchants:
        return summary

    system = _load_system_prompt()
    taxonomy = set(load_taxonomy())
    now = datetime.now(UTC).isoformat()
    id_by_name = {m.canonical_name: m.merchant_id for m in merchants}

    if batch_size is None:
        # claude-cli is SLOW — smaller batches = more progress checkpoints.
        provider_name = getattr(client, "name", "api")
        batch_size = 8 if provider_name == "claude-cli" else 40

    batches = [merchants[i : i + batch_size] for i in range(0, len(merchants), batch_size)]
    total = len(batches)

    for idx, batch in enumerate(batches, start=1):
        user = build_user_message(batch)
        label = f"batch {idx}/{total} ({len(batch)} merchants)"
        started = _utcnow()
        # Insert a 'running' row up-front so /merchants/llm-progress can show
        # live state while the subprocess blocks.
        row_id = start_run(conn, kind="categorize", model=model, started_at=started, error=label)
        try:
            result = client.parse_structured(
                model=model,
                system=system,
                user=user,
                schema=CategorizeResponse,
                max_tokens=8192,
            )
        except Exception as e:
            finish_run(conn, row_id, ended_at=_utcnow(), usage=None, status="error", error=str(e))
            summary.errors.append(str(e))
            summary.failed_batches += 1
            continue
        summary.batches += 1
        finish_run(conn, row_id, ended_at=_utcnow(), usage=result.usage, status="ok", error=label)

        parsed: CategorizeResponse = result.parsed
        summary.usage.input_tokens += result.usage.input_tokens
        summary.usage.output_tokens += result.usage.output_tokens
        summary.proposals.extend(parsed.results)

        _apply_proposals(
            conn,
            parsed.results,
            id_by_name,
            taxonomy,
            now,
            dry_run,
            summary,
        )

    conn.commit()
    return summary


def _apply_proposals(
    conn,
    proposals,
    id_by_name,
    taxonomy,
    now,
    dry_run,
    summary,
) -> None:
    for proposal in proposals:
        summary.proposed += 1
        if proposal.category not in taxonomy:
            # Hallucinated category — treat as low confidence, do not write.
            summary.low_confidence += 1
            continue
        mid = id_by_name.get(proposal.canonical_name)
        if mid is None:
            # LLM invented a merchant — skip.
            summary.low_confidence += 1
            continue
        if dry_run:
            continue

        # Never persist / auto-write 'Uncategorized' — it's the LLM saying
        # "no idea", not an actionable tag. Treat as a failed categorization.
        if proposal.category == "Uncategorized":
            summary.low_confidence += 1
            conn.execute("DELETE FROM llm_proposals WHERE merchant_id = ?", (mid,))
            continue

        if proposal.confidence >= AUTO_WRITE_THRESHOLD:
            cur = conn.execute(
                """
                UPDATE merchants
                   SET category = ?,
                       category_source = 'llm',
                       category_confidence = ?,
                       updated_at = ?
                 WHERE merchant_id = ?
                   AND (category_source IS NULL
                        OR category_source = 'llm')
                """,
                (proposal.category, proposal.confidence, now, mid),
            )
            if cur.rowcount > 0:
                summary.written += 1
                # Clean up any stale proposal — now that we have an auto-write.
                conn.execute("DELETE FROM llm_proposals WHERE merchant_id = ?", (mid,))
        else:
            # Persist low-confidence proposal for user review. Upsert on
            # merchant_id — latest LLM run wins.
            summary.low_confidence += 1
            conn.execute(
                """
                INSERT INTO llm_proposals
                  (merchant_id, category, confidence, reasoning, model, generated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(merchant_id) DO UPDATE SET
                  category = excluded.category,
                  confidence = excluded.confidence,
                  reasoning = excluded.reasoning,
                  model = excluded.model,
                  generated_at = excluded.generated_at
                """,
                (
                    mid,
                    proposal.category,
                    proposal.confidence,
                    proposal.reasoning,
                    summary.model,
                    now,
                ),
            )
