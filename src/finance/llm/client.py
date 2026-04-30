"""Anthropic wrapper via `instructor` — typed structured outputs + retry.

`LLMClient.parse_structured` returns a `ParsedResult[T]` where `T` is the
caller-supplied Pydantic schema. Instructor handles the tool-calling round
trip and retries on 429 / 529 / schema-validation failures.

Defaults come from `finance config`; ANTHROPIC_API_KEY env var is picked up
by the Anthropic SDK automatically (we also consult the OS keyring via
`resolve_api_key`).
"""

from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Generic, TypeVar

import instructor
from anthropic import Anthropic

# Keyring service/user for the Anthropic API key. `keyring` is already a
# dependency (Phase 1); we reuse its OS-native backend here.
KEYRING_SERVICE = "finance-anthropic"
KEYRING_USER = "api-key"

# Redact anything that looks like an Anthropic-style key before persisting to
# llm_runs.error or printing. Covers sk-ant-... and api_... variants.
_KEY_REDACT_RE = re.compile(r"(sk-ant-[A-Za-z0-9_\-]+|api[_-][A-Za-z0-9]{20,})")

if TYPE_CHECKING:
    from pydantic import BaseModel


T = TypeVar("T", bound="BaseModel")


# Defaults per the approved plan: Haiku for structured categorization, Sonnet
# for advisory quality. Both can be overridden via config.toml or CLI flags.
DEFAULT_CATEGORIZE_MODEL = "claude-haiku-4-5"
DEFAULT_ADVISE_MODEL = "claude-sonnet-4-6"


@dataclass
class LLMUsage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class ParsedResult(Generic[T]):
    """Envelope returned by LLMClient.parse_structured. `parsed` carries the
    schema type the caller passed in, so callers access fields without a
    `type: ignore[assignment]` dance."""

    parsed: T
    usage: LLMUsage
    model: str


def resolve_api_key() -> str | None:
    """Return the API key from env, then OS keyring. None if neither set."""
    env = os.environ.get("ANTHROPIC_API_KEY")
    if env:
        return env
    try:
        import keyring

        stored = keyring.get_password(KEYRING_SERVICE, KEYRING_USER)
        return stored or None
    except Exception:
        # Keyring backend may be unavailable (headless CI, broken dbus) —
        # fall through silently; SDK will raise a clear AuthError.
        return None


def store_api_key(key: str) -> None:
    """Persist the API key into the OS keyring."""
    import keyring

    keyring.set_password(KEYRING_SERVICE, KEYRING_USER, key)


def redact_key(text: str | None) -> str | None:
    """Replace any Anthropic-key-looking substring with a redaction marker."""
    if not text:
        return text
    return _KEY_REDACT_RE.sub("[REDACTED-KEY]", text)


class LLMClient:
    def __init__(self, api_key: str | None = None):
        # Resolve explicitly so keyring is consulted before the SDK's env
        # default. Passing api_key=None into the SDK would skip the keyring
        # path entirely.
        resolved = api_key or resolve_api_key()
        raw = Anthropic(api_key=resolved) if resolved else Anthropic()
        # instructor.Mode.TOOLS is the recommended Anthropic integration —
        # under the hood it registers the response schema as a tool and
        # parses the tool-call arguments back into a Pydantic instance.
        self._client = instructor.from_anthropic(raw, mode=instructor.Mode.TOOLS)

    def parse_structured(
        self,
        *,
        model: str,
        system: str,
        user: str,
        schema: type[T],
        max_tokens: int = 4096,
        max_retries: int = 2,
    ) -> ParsedResult[T]:
        """Call Anthropic with the given schema; retry on transient failures.

        Instructor's `max_retries` covers 429 / 529 (overloaded) and
        schema-validation failures — on the latter it re-sends the
        validation error to the model so it can self-correct.

        The per-call cost includes ~150-200 tokens of tool-definition
        overhead inherent to Mode.TOOLS.
        """
        parsed, completion = self._client.messages.create_with_completion(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            response_model=schema,
            max_retries=max_retries,
        )

        u = completion.usage
        usage = LLMUsage(
            input_tokens=getattr(u, "input_tokens", 0) or 0,
            output_tokens=getattr(u, "output_tokens", 0) or 0,
        )
        return ParsedResult(parsed=parsed, usage=usage, model=model)


def log_run(
    conn: sqlite3.Connection,
    *,
    kind: str,
    model: str,
    started_at: str,
    ended_at: str | None,
    usage: LLMUsage | None,
    status: str,
    error: str | None = None,
) -> int:
    """Append a row to llm_runs. Returns the row id.

    The `error` field is redacted for key-shaped substrings before insert so
    an unexpected SDK exception can't stash a live key in the DB.
    """
    cur = conn.execute(
        """
        INSERT INTO llm_runs (
          kind, model, started_at, ended_at,
          input_tokens, output_tokens,
          status, error
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            kind,
            model,
            started_at,
            ended_at,
            usage.input_tokens if usage else None,
            usage.output_tokens if usage else None,
            status,
            redact_key(error),
        ),
    )
    conn.commit()
    rid = cur.lastrowid
    assert rid is not None
    return rid


def start_run(
    conn: sqlite3.Connection,
    *,
    kind: str,
    model: str,
    started_at: str,
    error: str | None = None,
) -> int:
    """Insert a 'running' row and return its id. Call finish_run when done.

    `error` here is a misnomer — it's the progress label (e.g. "batch 2/8").
    Reused so we don't need another schema change.
    """
    cur = conn.execute(
        """
        INSERT INTO llm_runs (kind, model, started_at, status, error)
        VALUES (?, ?, ?, 'running', ?)
        """,
        (kind, model, started_at, redact_key(error)),
    )
    conn.commit()
    rid = cur.lastrowid
    assert rid is not None
    return rid


def finish_run(
    conn: sqlite3.Connection,
    row_id: int,
    *,
    ended_at: str,
    usage: LLMUsage | None = None,
    status: str = "ok",
    error: str | None = None,
) -> None:
    """Update a running row to completion."""
    conn.execute(
        """
        UPDATE llm_runs
           SET ended_at = ?, status = ?, error = ?,
               input_tokens = ?, output_tokens = ?
         WHERE id = ?
        """,
        (
            ended_at,
            status,
            redact_key(error),
            usage.input_tokens if usage else None,
            usage.output_tokens if usage else None,
            row_id,
        ),
    )
    conn.commit()


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


__all__ = [
    "DEFAULT_ADVISE_MODEL",
    "DEFAULT_CATEGORIZE_MODEL",
    "KEYRING_SERVICE",
    "KEYRING_USER",
    "LLMClient",
    "LLMUsage",
    "ParsedResult",
    "finish_run",
    "log_run",
    "redact_key",
    "resolve_api_key",
    "start_run",
    "store_api_key",
    "_utcnow",
]
