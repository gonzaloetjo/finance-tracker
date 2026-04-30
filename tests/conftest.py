"""Shared pytest fixtures for the finance test suite."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from finance.db import store

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator


@pytest.fixture
def cli_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[sqlite3.Connection, Path]]:
    """Redirect FINANCE_DATA_DIR to `tmp_path` and yield (conn, db_path).

    The Typer CLI's `_open_db()` resolves the database path through
    `get_settings()`, which reads `FINANCE_DATA_DIR`. Pointing that env var at
    `tmp_path` via `monkeypatch` lets a `CliRunner.invoke(app, [...])` call
    write to / read from a disposable database per test.

    The yielded connection is a separate handle on the same file — use it for
    seeding before the CLI runs and for assertions afterwards. `db_path` is
    exposed so multi-connection scenarios can open additional handles without
    re-deriving the path.
    """
    monkeypatch.setenv("FINANCE_DATA_DIR", str(tmp_path))
    db_path = tmp_path / "finance.db"
    with store.open_db(db_path) as conn:
        yield conn, db_path
