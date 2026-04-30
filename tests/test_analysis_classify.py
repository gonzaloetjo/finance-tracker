"""Tests for layered category resolution."""

from __future__ import annotations

import re

from finance.analysis.classify import classify_merchant
from finance.categorize import Rule
from finance.db.store import connect, init_schema


def _insert_merchant(conn, canonical_name, category=None, source=None):
    conn.execute(
        "INSERT INTO merchants (canonical_name, category, category_source, updated_at)"
        " VALUES (?, ?, ?, '2026-01-01')",
        (canonical_name, category, source),
    )
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def test_user_source_never_overwritten(tmp_path):
    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
        mid = _insert_merchant(conn, "NETFLIX", "Entertainment", "user")
        cat, src = classify_merchant(
            mid,
            "NETFLIX",
            conn,
            seed_overrides={"NETFLIX": "Subscriptions"},
            rules=[Rule(match=re.compile("NETFLIX"), category="Streaming")],
        )
        assert cat == "Entertainment"
        assert src == "user"
        # DB not changed
        row = conn.execute(
            "SELECT category, category_source FROM merchants WHERE merchant_id=?", (mid,)
        ).fetchone()
        assert row[0] == "Entertainment"


def test_seed_beats_rule(tmp_path):
    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
        mid = _insert_merchant(conn, "CARREFOUR")
        cat, src = classify_merchant(
            mid,
            "CARREFOUR",
            conn,
            seed_overrides={"CARREFOUR": "Groceries"},
            rules=[Rule(match=re.compile("CARREFOUR"), category="Shopping")],
        )
        assert cat == "Groceries"
        assert src == "curated"


def test_rule_fallback(tmp_path):
    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
        mid = _insert_merchant(conn, "DELIVEROO")
        rules = [Rule(match=re.compile("(?i)deliveroo"), category="Dining")]
        cat, src = classify_merchant(mid, "DELIVEROO", conn, rules=rules)
        assert cat == "Dining"
        assert src == "rule"
        # Verify written to DB
        row = conn.execute(
            "SELECT category, category_source FROM merchants WHERE merchant_id=?", (mid,)
        ).fetchone()
        assert row[0] == "Dining"
        assert row[1] == "rule"


def test_null_when_nothing_matches(tmp_path):
    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
        mid = _insert_merchant(conn, "MYSTERY SHOP")
        cat, src = classify_merchant(mid, "MYSTERY SHOP", conn, rules=[])
        assert cat is None
        assert src is None


def test_reenrich_respects_user_override(tmp_path):
    """Simulates --reenrich: rule-classified merchant is overridden by user, then re-classified.
    User override must survive."""
    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
        mid = _insert_merchant(conn, "DELIVEROO", "Dining", "rule")
        # User corrects
        conn.execute(
            "UPDATE merchants SET category='Takeaway', category_source='user', updated_at='2026-02-01' WHERE merchant_id=?",
            (mid,),
        )
        conn.commit()
        # Re-enrich: rule should not override user
        cat, src = classify_merchant(
            mid,
            "DELIVEROO",
            conn,
            rules=[Rule(match=re.compile("DELIVEROO"), category="Dining")],
        )
        assert cat == "Takeaway"
        assert src == "user"
