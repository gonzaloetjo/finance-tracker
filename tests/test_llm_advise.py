"""Tests for Phase 7 Part 2 — advisory scaffolding + 3 commands. LLM is mocked."""

from __future__ import annotations

from datetime import date, timedelta

from pydantic import BaseModel

from finance.analysis.enrich import enrich_transactions
from finance.db.store import connect, init_schema
from finance.llm.advise import (
    dismiss_advice,
    hash_rows,
    list_advice,
    lookup_cached,
    persist_advice,
    run_advisory,
)
from finance.llm.advise_dispatch import (
    Bundle,
    CutbackAdvice,
    CutbackSuggestion,
    IntegralAdvice,
    Recommendation,
    SubscriptionAdvice,
    advise_cutbacks,
    advise_integral,
    advise_subscriptions,
)
from finance.llm.client import LLMClient, LLMUsage, ParsedResult


class ScriptedClient(LLMClient):
    """Returns a list of pre-canned pydantic objects, one per call."""

    def __init__(self, responses: list[BaseModel]):
        self._queue = list(responses)
        self.calls = 0

    def parse_structured(self, **kwargs):
        self.calls += 1
        parsed = self._queue.pop(0)
        return ParsedResult(
            parsed=parsed,
            usage=LLMUsage(input_tokens=500, output_tokens=200),
            model=kwargs.get("model", "test-model"),
        )


# ============================================================================
# Hash + cache plumbing
# ============================================================================


def test_hash_rows_is_deterministic():
    a = hash_rows([("X", 1.0), ("Y", 2.0)])
    b = hash_rows([("Y", 2.0), ("X", 1.0)])
    assert a == b


def test_hash_rows_rounds_matter():
    assert hash_rows([("X", 1.00)]) == hash_rows([("X", 1.00)])
    assert hash_rows([("X", 1.00)]) != hash_rows([("X", 1.01)])


def test_persist_and_lookup(tmp_path):
    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
        rid = persist_advice(conn, kind="k", input_hash="h1", model="m", payload={"x": 1})
        hit = lookup_cached(conn, kind="k", input_hash="h1")
        assert hit is not None
        id_, payload = hit
        assert id_ == rid
        assert payload == {"x": 1}


def test_dismiss_hides_from_lookup(tmp_path):
    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
        rid = persist_advice(conn, kind="k", input_hash="h1", model="m", payload={"x": 1})
        assert dismiss_advice(conn, rid) is True
        assert lookup_cached(conn, kind="k", input_hash="h1") is None
        # Dismissed still visible with include_dismissed
        all_items = list_advice(conn, include_dismissed=True)
        assert len(all_items) == 1
        assert all_items[0]["dismissed_at"] is not None


class _TinySchema(BaseModel):
    hello: str


def test_run_advisory_cache_hit_avoids_llm(tmp_path):
    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
        client = ScriptedClient([_TinySchema(hello="world")])
        r1 = run_advisory(
            conn, kind="t", input_hash="h", system="s", user="u", schema=_TinySchema, client=client
        )
        assert r1.cached is False
        assert client.calls == 1

        r2 = run_advisory(
            conn, kind="t", input_hash="h", system="s", user="u", schema=_TinySchema, client=client
        )
        assert r2.cached is True
        assert client.calls == 1  # no new call


def test_run_advisory_refresh_bypasses_cache(tmp_path):
    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
        client = ScriptedClient(
            [
                _TinySchema(hello="a"),
                _TinySchema(hello="b"),
            ]
        )
        r1 = run_advisory(
            conn, kind="t", input_hash="h", system="s", user="u", schema=_TinySchema, client=client
        )
        r2 = run_advisory(
            conn,
            kind="t",
            input_hash="h",
            system="s",
            user="u",
            schema=_TinySchema,
            client=client,
            refresh=True,
        )
        assert client.calls == 2
        assert r1.payload["hello"] == "a"
        assert r2.payload["hello"] == "b"


# ============================================================================
# End-to-end per advisory subcommand (mocked)
# ============================================================================


def _boot(conn):
    conn.execute(
        "INSERT INTO sessions (session_id, aspsp_name, aspsp_country, valid_until, created_at)"
        " VALUES ('s1', 'BNP Paribas', 'FR', '2099-01-01', '2026-01-01')"
    )
    conn.execute(
        "INSERT INTO accounts (account_uid, session_id, iban, name, currency, account_type, raw_json)"
        " VALUES ('a1', 's1', 'FR00', 'Checking', 'EUR', 'CACC', '{}')"
    )


def _seed_streaming_overlap(conn):
    _boot(conn)
    today = date.today()
    for svc, amt in [("NETFLIX", -15.49), ("DISNEY PLUS", -8.99), ("SPOTIFY", -9.99)]:
        for i in range(4):
            bdate = (today - timedelta(days=30 * i)).isoformat()
            memo = f"PRLV SEPA {svc} ECH/010126 ID EMETTEUR/X MDT/M REF/R{i} LIB/L"
            conn.execute(
                "INSERT INTO transactions (transaction_id, account_uid, booking_date, amount, currency,"
                " remittance_info, raw_json, fetched_at)"
                " VALUES (?, 'a1', ?, ?, 'EUR', ?, '{}', '2026-01-01')",
                (f"{svc}_{i}", bdate, amt, memo),
            )
    conn.commit()


def test_advise_subscriptions_end_to_end(tmp_path):
    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
        _seed_streaming_overlap(conn)
        enrich_transactions(conn)

        canned = SubscriptionAdvice(
            recommendations=[
                Recommendation(
                    domain="streaming",
                    action="consolidate",
                    services=["NETFLIX", "DISNEY PLUS"],
                    suggested_services=["NETFLIX"],
                    monthly_savings=8.99,
                    rationale="Overlapping catalogs; Netflix covers the majority use.",
                ),
            ]
        )
        client = ScriptedClient([canned])
        result = advise_subscriptions(conn, client=client)
        assert result.cached is False
        assert result.payload["recommendations"][0]["domain"] == "streaming"

        # Second call: cache hit
        result2 = advise_subscriptions(conn, client=client)
        assert result2.cached is True
        assert client.calls == 1


def test_advise_cutbacks_end_to_end(tmp_path):
    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
        _seed_streaming_overlap(conn)
        enrich_transactions(conn)

        canned = CutbackAdvice(
            suggestions=[
                CutbackSuggestion(
                    category="Subscriptions",
                    current_monthly=34.47,
                    suggested_monthly=15.49,
                    rationale="Drop duplicate streaming.",
                    specific_actions=["Cancel Disney+", "Cancel Spotify"],
                ),
            ]
        )
        client = ScriptedClient([canned])
        result = advise_cutbacks(conn, client=client, months=3)
        assert result.cached is False
        assert result.payload["suggestions"][0]["category"] == "Subscriptions"


def test_advise_integral_end_to_end(tmp_path):
    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
        _seed_streaming_overlap(conn)
        enrich_transactions(conn)

        canned = IntegralAdvice(
            bundles=[
                Bundle(
                    theme="Streaming bundle",
                    components=["NETFLIX", "DISNEY PLUS"],
                    current_monthly_total=24.48,
                    potential_saving_monthly=4.00,
                    rationale="Some carrier plans bundle streaming; verify with your telco.",
                    caveat="Compare bundled rates vs standalone; savings vary.",
                ),
            ]
        )
        client = ScriptedClient([canned])
        result = advise_integral(conn, client=client)
        assert result.cached is False
        assert result.payload["bundles"][0]["theme"] == "Streaming bundle"
