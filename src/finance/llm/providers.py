"""LLM provider abstraction — API (per-token billing) vs Claude Code CLI
(subscription-backed, with built-in WebSearch).

Both expose the same `parse_structured(...) -> ParsedResult` signature as
`LLMClient`, so callers (categorize.py, advise.py) don't care which is used.

The "api" provider IS `LLMClient` — `make_provider("api")` returns it
directly. The `.name` attribute only exists on providers that need to
behave differently from the API default (currently only `ClaudeCLIProvider`,
which takes smaller batches due to subprocess overhead). Callers use
`getattr(client, "name", "api")` and branch on that.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from typing import TypeVar

from pydantic import BaseModel

from finance.llm.client import LLMClient, LLMUsage, ParsedResult

T = TypeVar("T", bound=BaseModel)


# ─────────────────────────────────────────────────────────────────────────────
# Claude Code CLI provider — shells out to `claude -p`.
# ─────────────────────────────────────────────────────────────────────────────

# Find any JSON object in a response text block. Claude Code responses sometimes
# include preamble / narration before the actual JSON.
_JSON_OBJ_RE = re.compile(r"\{[\s\S]*\}")


class ClaudeCLIError(RuntimeError):
    pass


class ClaudeCLIProvider:
    """Invokes `claude -p <prompt>` and parses its final text as JSON.

    Pros: uses your Claude Code subscription, has WebSearch available
    automatically for merchant lookup.
    Cons: per-call subprocess startup, subject to Claude Code's daily rate
    limit, output parsing is best-effort.
    """

    name = "claude-cli"

    def __init__(self, binary: str = "claude", timeout: int = 300):
        if not shutil.which(binary):
            raise ClaudeCLIError(
                f"'{binary}' CLI not found in PATH. Install Claude Code and "
                f"log in (`claude login`) to use this provider."
            )
        self._binary = binary
        self._timeout = timeout

    def parse_structured(
        self,
        *,
        model: str,
        system: str,
        user: str,
        schema: type[T],
        max_tokens: int = 4096,
    ) -> ParsedResult:
        # Stitch system + user into a single prompt with an explicit JSON-only
        # postamble. Claude Code's `-p` mode doesn't accept a separate system
        # prompt via flag reliably; embedding it in the user turn is safest.
        schema_hint = self._schema_hint(schema)
        prompt = (
            f"{system}\n\n"
            f"---\n"
            f"USER INPUT:\n{user}\n\n"
            f"---\n"
            f"Respond with ONLY a single JSON object matching this schema "
            f"(no prose, no Markdown code fences, no commentary):\n"
            f"{schema_hint}\n\n"
            f"Use the WebSearch tool for merchant names that are ambiguous "
            f"or unfamiliar (local restaurants, acronyms, Paris-specific "
            f"businesses). Don't search for well-known global merchants."
        )

        try:
            proc = subprocess.run(
                [self._binary, "-p", prompt],
                capture_output=True,
                text=True,
                timeout=self._timeout,
            )
        except subprocess.TimeoutExpired as e:
            raise ClaudeCLIError(f"Claude Code timed out after {self._timeout}s") from e

        if proc.returncode != 0:
            raise ClaudeCLIError(f"Claude Code exited {proc.returncode}: {proc.stderr[:500]}")

        raw = proc.stdout.strip()
        parsed_obj = self._extract_json(raw, schema)

        return ParsedResult(
            parsed=parsed_obj,
            # Claude Code doesn't expose token counts per subscription call;
            # leave at zero so llm_runs reflects "unknown but subscription-paid".
            usage=LLMUsage(),
            model=f"{model} (via claude-cli)",
        )

    def _schema_hint(self, schema: type[T]) -> str:
        """Render a minimal JSON-schema hint for the prompt."""
        try:
            return json.dumps(schema.model_json_schema(), indent=2)
        except Exception:
            return f"(Pydantic model: {schema.__name__})"

    def _extract_json(self, raw: str, schema: type[T]) -> T:
        # Try direct parse first.
        try:
            return schema.model_validate_json(raw)
        except Exception:
            pass
        # Strip common code fences.
        stripped = raw.strip()
        if stripped.startswith("```"):
            stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
            stripped = re.sub(r"\s*```\s*$", "", stripped)
        try:
            return schema.model_validate_json(stripped)
        except Exception:
            pass
        # Fallback: find the first {…} block in the text.
        m = _JSON_OBJ_RE.search(stripped)
        if not m:
            raise ClaudeCLIError(
                f"Could not find JSON in Claude Code output (first 300 chars): {raw[:300]}"
            )
        try:
            return schema.model_validate_json(m.group(0))
        except Exception as e:
            raise ClaudeCLIError(
                f"Claude Code output didn't match schema: {e}\nFirst 300 chars: {raw[:300]}"
            ) from e


# ─────────────────────────────────────────────────────────────────────────────
# Factory
# ─────────────────────────────────────────────────────────────────────────────


def make_provider(kind: str):
    """Instantiate the named provider. `kind` ∈ {'api', 'claude-cli'}.

    The "api" branch returns an `LLMClient` directly (no wrapper). Callers
    that care about provider identity use `getattr(client, "name", "api")`.
    """
    if kind == "api":
        return LLMClient()
    if kind == "claude-cli":
        return ClaudeCLIProvider()
    raise ValueError(f"unknown provider: {kind!r} — expected 'api' or 'claude-cli'")
