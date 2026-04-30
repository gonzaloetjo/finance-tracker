"""Phase 7 Part 2 — shared advisory scaffolding.

`run_advisory` wraps a per-kind callable (build_prompt, parse_into_schema)
with:
  - deterministic `input_hash` so repeat calls with identical economics hit
    the cache (no LLM call, no cost).
  - persistence of the full structured output in the `advice` table.
  - llm_runs bookkeeping on every non-cache call.

Each advisory subcommand provides its own `build_input_rows`, `hash`, and
`build_prompt` — this module never reaches into the DB itself.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import TypeVar

from pydantic import BaseModel

from finance.llm.client import (
    DEFAULT_ADVISE_MODEL,
    LLMClient,
    _utcnow,
    log_run,
)

T = TypeVar("T", bound=BaseModel)


@dataclass
class AdvisoryResult:
    payload: dict
    cached: bool
    model: str
    advice_id: int | None = None


def hash_rows(rows: list[tuple]) -> str:
    """Deterministic sha1 over a sorted list of tuples. Callers are
    responsible for rounding to avoid cents-level cache misses.
    """
    canonical = json.dumps(sorted(rows), separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()


def lookup_cached(
    conn: sqlite3.Connection, *, kind: str, input_hash: str
) -> tuple[int, dict] | None:
    """Return (id, payload) for a non-dismissed advice row, or None."""
    row = conn.execute(
        """
        SELECT id, payload_json FROM advice
         WHERE kind = ? AND input_hash = ? AND dismissed_at IS NULL
         ORDER BY generated_at DESC LIMIT 1
        """,
        (kind, input_hash),
    ).fetchone()
    if not row:
        return None
    return row[0], json.loads(row[1])


def persist_advice(
    conn: sqlite3.Connection,
    *,
    kind: str,
    input_hash: str,
    model: str,
    payload: dict,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO advice (kind, generated_at, model, input_hash, payload_json)
        VALUES (?, ?, ?, ?, ?)
        """,
        (kind, _utcnow(), model, input_hash, json.dumps(payload)),
    )
    conn.commit()
    rid = cur.lastrowid
    assert rid is not None
    return rid


def run_advisory(
    conn: sqlite3.Connection,
    *,
    kind: str,
    input_hash: str,
    system: str,
    user: str,
    schema: type[T],
    client: LLMClient,
    model: str = DEFAULT_ADVISE_MODEL,
    refresh: bool = False,
    max_tokens: int = 4096,
) -> AdvisoryResult:
    """Cache-aware advisory call.

    On cache hit: return the persisted payload (no LLM call).
    On cache miss: call LLM, persist payload, log llm_runs row.
    `refresh=True` bypasses the cache and forces a fresh call.
    """
    if not refresh:
        hit = lookup_cached(conn, kind=kind, input_hash=input_hash)
        if hit is not None:
            rid, payload = hit
            return AdvisoryResult(payload=payload, cached=True, model=model, advice_id=rid)

    started = _utcnow()
    try:
        result = client.parse_structured(
            model=model,
            system=system,
            user=user,
            schema=schema,
            max_tokens=max_tokens,
        )
    except Exception as e:
        log_run(
            conn,
            kind=kind,
            model=model,
            started_at=started,
            ended_at=_utcnow(),
            usage=None,
            status="error",
            error=str(e),
        )
        raise

    log_run(
        conn,
        kind=kind,
        model=model,
        started_at=started,
        ended_at=_utcnow(),
        usage=result.usage,
        status="ok",
    )

    parsed: T = result.parsed
    payload = parsed.model_dump()
    advice_id = persist_advice(
        conn,
        kind=kind,
        input_hash=input_hash,
        model=model,
        payload=payload,
    )
    return AdvisoryResult(payload=payload, cached=False, model=model, advice_id=advice_id)


def list_advice(conn: sqlite3.Connection, *, include_dismissed: bool = False) -> list[dict]:
    q = "SELECT id, kind, generated_at, model, dismissed_at FROM advice"
    if not include_dismissed:
        q += " WHERE dismissed_at IS NULL"
    q += " ORDER BY generated_at DESC"
    return [dict(r) for r in conn.execute(q).fetchall()]


def dismiss_advice(conn: sqlite3.Connection, advice_id: int) -> bool:
    cur = conn.execute(
        "UPDATE advice SET dismissed_at = ? WHERE id = ? AND dismissed_at IS NULL",
        (_utcnow(), advice_id),
    )
    conn.commit()
    return cur.rowcount > 0
