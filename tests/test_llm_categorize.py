"""Tests for Phase 7 Part 1 — LLM categorization.

The LLMClient is mocked; no network I/O.
"""

from __future__ import annotations

from finance.analysis.enrich import enrich_transactions
from finance.db.store import connect, init_schema
from finance.llm.categorize import (
    CategorizeResponse,
    CategoryResult,
    categorize_uncategorized,
    collect_uncategorized,
    load_taxonomy,
)
from finance.llm.client import (
    LLMClient,
    LLMUsage,
    ParsedResult,
    log_run,
    redact_key,
    resolve_api_key,
)


class FakeClient(LLMClient):
    """Stand-in for anthropic.Anthropic — returns a pre-canned response."""

    def __init__(self, canned_results: list[CategoryResult]):
        # Do NOT call super().__init__ — that requires ANTHROPIC_API_KEY.
        self._canned = canned_results
        self._calls = 0

    def parse_structured(self, **kwargs):
        self._calls += 1
        return ParsedResult(
            parsed=CategorizeResponse(results=list(self._canned)),
            usage=LLMUsage(input_tokens=500, output_tokens=200),
            model=kwargs.get("model", "test-model"),
        )


def _seed_with_merchants(conn, canonicals: list[str]):
    conn.execute(
        "INSERT INTO sessions (session_id, aspsp_name, aspsp_country, valid_until, created_at)"
        " VALUES ('s1', 'BNP Paribas', 'FR', '2099-01-01', '2026-01-01')"
    )
    conn.execute(
        "INSERT INTO accounts (account_uid, session_id, iban, name, currency, account_type, raw_json)"
        " VALUES ('a1', 's1', 'FR00', 'Checking', 'EUR', 'CACC', '{}')"
    )
    for i, name in enumerate(canonicals):
        memo = f"FACTURE CARTE DU 050126 {name} CARTE 0000XXXXXXXX0000"
        conn.execute(
            "INSERT INTO transactions (transaction_id, account_uid, booking_date, amount, currency,"
            " remittance_info, raw_json, fetched_at) VALUES (?, 'a1', '2026-01-05', -10.0, 'EUR', ?, '{}', '2026-01-01')",
            (f"tx_{i}", memo),
        )
    conn.commit()


def test_taxonomy_loads():
    tax = load_taxonomy()
    assert "Subscriptions" in tax
    assert "Groceries" in tax
    assert len(tax) > 10


def test_collect_uncategorized_orders_by_spend(tmp_path):
    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
        _seed_with_merchants(conn, ["BIG SPEND", "SMALL SPEND"])
        # Weight BIG SPEND so it has more outflow
        conn.execute(
            "INSERT INTO transactions (transaction_id, account_uid, booking_date, amount, currency,"
            " remittance_info, raw_json, fetched_at) VALUES ('big_extra', 'a1', '2026-01-06', -500.0, 'EUR', 'FACTURE CARTE DU 060126 BIG SPEND CARTE 0000XXXXXXXX0000', '{}', '2026-01-01')"
        )
        conn.commit()
        enrich_transactions(conn)

        items = collect_uncategorized(conn)
        names = [m.canonical_name for m in items]
        assert "BIG SPEND" in names
        assert names.index("BIG SPEND") < names.index("SMALL SPEND")


def test_categorize_writes_high_confidence(tmp_path):
    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
        _seed_with_merchants(conn, ["NETFLIX"])
        enrich_transactions(conn)

        fake = FakeClient(
            [
                CategoryResult(
                    canonical_name="NETFLIX",
                    category="Subscriptions",
                    confidence=0.98,
                    reasoning="well-known streaming service",
                ),
            ]
        )
        summary = categorize_uncategorized(conn, client=fake, limit=10)
        assert summary.proposed == 1
        assert summary.written == 1
        row = conn.execute(
            "SELECT category, category_source, category_confidence FROM merchants"
            " WHERE canonical_name='NETFLIX'"
        ).fetchone()
        assert row[0] == "Subscriptions"
        assert row[1] == "llm"
        assert abs(row[2] - 0.98) < 0.01


def test_categorize_skips_low_confidence(tmp_path):
    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
        _seed_with_merchants(conn, ["AMBIGUOUS MERCHANT"])
        enrich_transactions(conn)

        fake = FakeClient(
            [
                CategoryResult(
                    canonical_name="AMBIGUOUS MERCHANT",
                    category="Shopping",
                    confidence=0.60,
                    reasoning="unclear",
                ),
            ]
        )
        summary = categorize_uncategorized(conn, client=fake, limit=10)
        assert summary.proposed == 1
        assert summary.written == 0
        assert summary.low_confidence == 1
        # Merchant category must remain NULL
        row = conn.execute(
            "SELECT category, category_source FROM merchants WHERE canonical_name='AMBIGUOUS MERCHANT'"
        ).fetchone()
        assert row[0] is None
        assert row[1] is None
        # The proposal must be persisted for later review.
        prop = conn.execute(
            "SELECT p.category, p.confidence, p.reasoning FROM llm_proposals p"
            " JOIN merchants m ON m.merchant_id = p.merchant_id"
            " WHERE m.canonical_name='AMBIGUOUS MERCHANT'"
        ).fetchone()
        assert prop is not None
        assert prop[0] == "Shopping"
        assert abs(prop[1] - 0.60) < 0.01


def test_uncategorized_proposals_are_not_stored(tmp_path):
    """LLM returning 'Uncategorized' = 'I don't know' — drop, don't persist."""
    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
        _seed_with_merchants(conn, ["MYSTERY"])
        enrich_transactions(conn)

        fake = FakeClient(
            [
                CategoryResult(
                    canonical_name="MYSTERY",
                    category="Uncategorized",
                    confidence=0.55,
                    reasoning="could be anything",
                ),
            ]
        )
        categorize_uncategorized(conn, client=fake, limit=10)
        # No proposal row should be stored.
        n = conn.execute("SELECT COUNT(*) FROM llm_proposals").fetchone()[0]
        assert n == 0


def test_categorize_proposal_upserts_on_rerun(tmp_path):
    """Second LLM run on the same merchant overwrites the previous proposal."""
    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
        _seed_with_merchants(conn, ["FUZZY"])
        enrich_transactions(conn)

        fake1 = FakeClient(
            [
                CategoryResult(
                    canonical_name="FUZZY",
                    category="Shopping",
                    confidence=0.55,
                    reasoning="first guess",
                ),
            ]
        )
        categorize_uncategorized(conn, client=fake1, limit=10)

        fake2 = FakeClient(
            [
                CategoryResult(
                    canonical_name="FUZZY",
                    category="Dining",
                    confidence=0.70,
                    reasoning="better guess",
                ),
            ]
        )
        categorize_uncategorized(conn, client=fake2, limit=10)

        prop = conn.execute(
            "SELECT p.category, p.confidence FROM llm_proposals p"
            " JOIN merchants m ON m.merchant_id = p.merchant_id"
            " WHERE m.canonical_name='FUZZY'"
        ).fetchone()
        assert prop[0] == "Dining"
        assert abs(prop[1] - 0.70) < 0.01


def test_categorize_dry_run_never_writes(tmp_path):
    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
        _seed_with_merchants(conn, ["NETFLIX"])
        enrich_transactions(conn)

        fake = FakeClient(
            [
                CategoryResult(
                    canonical_name="NETFLIX",
                    category="Subscriptions",
                    confidence=0.99,
                    reasoning="",
                ),
            ]
        )
        summary = categorize_uncategorized(conn, client=fake, limit=10, dry_run=True)
        assert summary.proposed == 1
        assert summary.written == 0
        # No write despite high confidence
        row = conn.execute(
            "SELECT category FROM merchants WHERE canonical_name='NETFLIX'"
        ).fetchone()
        assert row[0] is None


def test_categorize_excludes_user_source(tmp_path):
    """User-set merchants are not re-queried; re-running is a no-op for them."""
    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
        _seed_with_merchants(conn, ["NETFLIX"])
        enrich_transactions(conn)
        # User pinned NETFLIX → Entertainment
        conn.execute(
            "UPDATE merchants SET category='Entertainment', category_source='user'"
            " WHERE canonical_name='NETFLIX'"
        )
        conn.commit()

        items = collect_uncategorized(conn)
        assert all(m.canonical_name != "NETFLIX" for m in items)


def test_categorize_hallucinated_category_skipped(tmp_path):
    """LLM returns a category NOT in the taxonomy — skip instead of writing nonsense."""
    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
        _seed_with_merchants(conn, ["WEIRD"])
        enrich_transactions(conn)

        fake = FakeClient(
            [
                CategoryResult(
                    canonical_name="WEIRD",
                    category="UnknownCustomCategory",
                    confidence=0.99,
                    reasoning="made up",
                ),
            ]
        )
        summary = categorize_uncategorized(conn, client=fake, limit=10)
        assert summary.written == 0
        assert summary.low_confidence == 1


def test_redact_key_removes_anthropic_key():
    assert redact_key("boom: sk-ant-api03-AbCdEf_-123") == "boom: [REDACTED-KEY]"
    assert redact_key(None) is None
    assert redact_key("no key here") == "no key here"


def test_log_run_redacts_key_from_error(tmp_path):
    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
        log_run(
            conn,
            kind="categorize",
            model="m",
            started_at="t0",
            ended_at="t1",
            usage=None,
            status="error",
            error="AuthError: invalid key sk-ant-api03-abc_xyz123456789",
        )
        row = conn.execute("SELECT error FROM llm_runs ORDER BY id DESC LIMIT 1").fetchone()
        assert "sk-ant" not in row[0]
        assert "[REDACTED-KEY]" in row[0]


def test_resolve_api_key_env_takes_precedence(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "env-value")
    # Even if keyring has something, env wins
    assert resolve_api_key() == "env-value"


def test_resolve_api_key_falls_back_to_keyring(monkeypatch):
    import keyring

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # Stub keyring so the test doesn't touch the real OS keychain
    stored = {}
    monkeypatch.setattr(keyring, "get_password", lambda svc, user: stored.get((svc, user)))
    monkeypatch.setattr(
        keyring, "set_password", lambda svc, user, pw: stored.update({(svc, user): pw})
    )
    from finance.llm.client import store_api_key

    store_api_key("keyring-value")
    assert resolve_api_key() == "keyring-value"


def test_categorize_logs_run(tmp_path):
    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
        _seed_with_merchants(conn, ["NETFLIX"])
        enrich_transactions(conn)

        fake = FakeClient(
            [
                CategoryResult(
                    canonical_name="NETFLIX",
                    category="Subscriptions",
                    confidence=0.99,
                    reasoning="",
                ),
            ]
        )
        categorize_uncategorized(conn, client=fake, limit=10)

        row = conn.execute(
            "SELECT kind, status, input_tokens, output_tokens FROM llm_runs"
            " ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row[0] == "categorize"
        assert row[1] == "ok"
        assert row[2] == 500
        assert row[3] == 200
