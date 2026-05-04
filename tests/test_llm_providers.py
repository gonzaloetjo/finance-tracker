"""Tests for LLM provider abstraction (api / claude-cli)."""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest
from pydantic import BaseModel

from finance.llm.providers import ClaudeCLIError, ClaudeCLIProvider, make_provider


class _Tiny(BaseModel):
    name: str
    n: int


def test_make_provider_unknown_raises():
    with pytest.raises(ValueError):
        make_provider("oopsnotaprovider")


def test_claude_cli_missing_binary(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda b: None)
    with pytest.raises(ClaudeCLIError, match="not found"):
        ClaudeCLIProvider()


def _fake_run(stdout: str, returncode: int = 0):
    """Factory for a subprocess.run stub returning the given stdout."""

    def _run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args,
            returncode=returncode,
            stdout=stdout,
            stderr="",
        )

    return _run


def _with_claude_available():
    """Pretend `claude` is in PATH."""
    return patch("shutil.which", lambda b: f"/usr/local/bin/{b}")


def test_claude_cli_parses_plain_json():
    with _with_claude_available():
        provider = ClaudeCLIProvider()

    with patch("subprocess.run", _fake_run('{"name": "hi", "n": 3}')):
        result = provider.parse_structured(
            model="test",
            system="sys",
            user="u",
            schema=_Tiny,
        )
    assert result.parsed.name == "hi"
    assert result.parsed.n == 3


def test_claude_cli_strips_code_fences():
    with _with_claude_available():
        provider = ClaudeCLIProvider()

    out = '```json\n{"name": "fenced", "n": 7}\n```'
    with patch("subprocess.run", _fake_run(out)):
        result = provider.parse_structured(
            model="test",
            system="sys",
            user="u",
            schema=_Tiny,
        )
    assert result.parsed.name == "fenced"


def test_claude_cli_extracts_json_from_preamble():
    """Claude may narrate before the JSON — we still pick it up."""
    with _with_claude_available():
        provider = ClaudeCLIProvider()

    out = 'Sure, here is the structured response:\n\n{"name": "in-preamble", "n": 1}'
    with patch("subprocess.run", _fake_run(out)):
        result = provider.parse_structured(
            model="test",
            system="sys",
            user="u",
            schema=_Tiny,
        )
    assert result.parsed.name == "in-preamble"


def test_claude_cli_schema_mismatch_raises():
    with _with_claude_available():
        provider = ClaudeCLIProvider()

    out = '{"name": "x"}'  # missing required `n`
    with patch("subprocess.run", _fake_run(out)), pytest.raises(ClaudeCLIError):
        provider.parse_structured(
            model="test",
            system="sys",
            user="u",
            schema=_Tiny,
        )


def test_claude_cli_nonzero_exit_raises():
    with _with_claude_available():
        provider = ClaudeCLIProvider()

    with patch("subprocess.run", _fake_run("", returncode=1)), pytest.raises(
        ClaudeCLIError, match="exited 1"
    ):
        provider.parse_structured(
            model="test",
            system="sys",
            user="u",
            schema=_Tiny,
        )


def test_claude_cli_timeout_raises():
    with _with_claude_available():
        provider = ClaudeCLIProvider(timeout=1)

    def _timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=1)

    with patch("subprocess.run", _timeout), pytest.raises(ClaudeCLIError, match="timed out"):
        provider.parse_structured(
            model="test",
            system="sys",
            user="u",
            schema=_Tiny,
        )
