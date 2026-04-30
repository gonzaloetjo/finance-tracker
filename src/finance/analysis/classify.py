"""Stage B — layered category resolution.

Precedence (highest wins):
  1. tx_overrides.category (tx-level; handled in io.py COALESCE, not here)
  2. merchants.category with category_source='user' (already written, skip)
  3. merchants_seed.yaml (curated canonical→category map)
  4. rules.yaml regex rules (legacy, from categorize.py)
  5. NULL → Phase 7 LLM fills this
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path

import yaml

from finance.categorize import Rule, load_rules


def _load_seed() -> dict[str, str]:
    """Load the bundled merchants_seed.yaml (canonical_name → category)."""
    seed_path = files("finance.data").joinpath("merchants_seed.yaml")
    text = seed_path.read_text()
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        return {}
    return {str(k).strip(): str(v).strip() for k, v in data.items() if k and v}


def _load_user_seed(path: Path | None) -> dict[str, str]:
    """Load a user-provided seed file (same format)."""
    if not path or not path.exists():
        return {}
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
        return {}
    return {str(k).strip(): str(v).strip() for k, v in data.items() if k and v}


def classify_merchant(
    merchant_id: int,
    canonical_name: str,
    conn: sqlite3.Connection,
    *,
    rules: list[Rule] | None = None,
    rules_path: Path | None = None,
    seed_overrides: dict[str, str] | None = None,
) -> tuple[str | None, str | None]:
    """Resolve category for a merchant. Returns (category, source) or (None, None).

    Does NOT overwrite merchants.category when source='user'. Writes to merchants
    table when a new classification is found.
    """
    now = datetime.now(UTC).isoformat()

    # Already classified by user → never overwrite
    row = conn.execute(
        "SELECT category, category_source FROM merchants WHERE merchant_id = ?",
        (merchant_id,),
    ).fetchone()
    if row and row[1] == "user" and row[0]:
        return row[0], "user"

    # Curated seed (bundled + user file)
    seed = _load_seed()
    if seed_overrides:
        seed.update(seed_overrides)
    if canonical_name in seed:
        cat = seed[canonical_name]
        _write_merchant_category(conn, merchant_id, cat, "curated", now)
        return cat, "curated"

    # Regex rules
    if rules is None and rules_path:
        rules = load_rules(rules_path)
    if rules:
        for r in rules:
            if r.match.search(canonical_name):
                _write_merchant_category(conn, merchant_id, r.category, "rule", now)
                return r.category, "rule"

    return None, None


def _write_merchant_category(
    conn: sqlite3.Connection,
    merchant_id: int,
    category: str,
    source: str,
    now: str,
) -> None:
    conn.execute(
        "UPDATE merchants SET category = ?, category_source = ?, updated_at = ? WHERE merchant_id = ?",
        (category, source, now, merchant_id),
    )
