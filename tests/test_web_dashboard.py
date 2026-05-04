"""Phase 8 dashboard tests: read routes return 200 with expected content,
write routes (HTMX) round-trip to SQLite.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from finance.analysis.enrich import enrich_transactions
from finance.db import store
from finance.llm.advise import persist_advice
from finance.web.app import AppState, create_app
from tests.web_client import ASGITestClient


@pytest.fixture
def dashboard_client(tmp_path):
    """Slim fixture — no Enable Banking mock needed for dashboard routes."""

    def client_factory():  # pragma: no cover — never called in these tests
        raise AssertionError("client_factory should not be invoked in dashboard tests")

    state = AppState(
        client_factory=client_factory,
        db_path=tmp_path / "finance.db",
        callback_url="http://localhost:8000/callback",
    )
    app = create_app(state)
    return ASGITestClient(app, follow_redirects=False), state


@pytest.fixture
def seeded_dashboard(dashboard_client):
    """dashboard_client + a small seed: 1 session, 1 account, 4 PRLV NETFLIX txns."""
    client, state = dashboard_client
    with store.connect(state.db_path) as conn:
        store.init_schema(conn)
        conn.execute(
            "INSERT INTO sessions (session_id, aspsp_name, aspsp_country, valid_until, created_at)"
            " VALUES ('s1', 'BNP Paribas', 'FR', '2099-01-01', '2026-01-01')"
        )
        conn.execute(
            "INSERT INTO accounts (account_uid, session_id, iban, name, currency, account_type, raw_json)"
            " VALUES ('a1', 's1', 'FR00', 'Checking', 'EUR', 'CACC', '{}')"
        )
        today = date.today()
        for i in range(4):
            bdate = (today - timedelta(days=30 * i)).isoformat()
            memo = f"PRLV SEPA NETFLIX ECH/010126 ID EMETTEUR/X MDT/M REF/R{i} LIB/L"
            conn.execute(
                "INSERT INTO transactions (transaction_id, account_uid, booking_date, amount, currency,"
                " remittance_info, raw_json, fetched_at)"
                " VALUES (?, 'a1', ?, -15.49, 'EUR', ?, '{}', '2026-01-01')",
                (f"n_{i}", bdate, memo),
            )
        conn.commit()
        enrich_transactions(conn)
    return client, state


# ═══════════════════════════════════════════════════════════════════════════
# Read routes
# ═══════════════════════════════════════════════════════════════════════════


def test_overview_empty(dashboard_client):
    client, _ = dashboard_client
    resp = client.get("/")
    assert resp.status_code == 200
    # Tailwind + HTMX loaded, nav present
    assert "htmx.org" in resp.text
    assert "Merchants" in resp.text


def test_overview_with_data(seeded_dashboard):
    client, _ = seeded_dashboard
    resp = client.get("/")
    assert resp.status_code == 200
    assert "NETFLIX" in resp.text
    assert "Overview" in resp.text


def test_merchants_page(seeded_dashboard):
    client, _ = seeded_dashboard
    resp = client.get("/merchants")
    assert resp.status_code == 200
    assert "NETFLIX" in resp.text
    # inline picker rendered
    assert 'name="category"' in resp.text


def test_merchants_uncategorized_filter(seeded_dashboard):
    """`uncategorized=true` means strictly `category IS NULL`. The seeded
    fixture has no rules loaded so NETFLIX stays NULL and appears here.
    Categorized merchants (via set_category) would be filtered out."""
    client, state = seeded_dashboard
    resp = client.get("/merchants?uncategorized=true")
    assert resp.status_code == 200
    assert "NETFLIX" in resp.text
    assert "Uncategorized merchants" in resp.text

    # Once categorized, NETFLIX disappears from the view.
    with store.connect(state.db_path) as conn:
        conn.execute(
            "UPDATE merchants SET category='Subscriptions', category_source='user'"
            " WHERE canonical_name='NETFLIX'"
        )
        conn.commit()
    resp2 = client.get("/merchants?uncategorized=true")
    assert resp2.status_code == 200
    assert "NETFLIX" not in resp2.text


def test_merchant_detail(seeded_dashboard):
    client, _ = seeded_dashboard
    resp = client.get("/merchants/NETFLIX")
    assert resp.status_code == 200
    assert "NETFLIX" in resp.text
    assert "Transactions" in resp.text


def test_merchant_detail_404(dashboard_client):
    client, _ = dashboard_client
    resp = client.get("/merchants/NOT_A_REAL_MERCHANT")
    assert resp.status_code == 404


def test_recurring_page(seeded_dashboard):
    client, _ = seeded_dashboard
    resp = client.get("/recurring")
    assert resp.status_code == 200
    assert "NETFLIX" in resp.text


def test_subscriptions_page(seeded_dashboard):
    client, _ = seeded_dashboard
    resp = client.get("/subscriptions")
    assert resp.status_code == 200
    assert "Active subscriptions" in resp.text
    assert "NETFLIX" in resp.text


def test_forecast_page(seeded_dashboard):
    client, _ = seeded_dashboard
    resp = client.get("/forecast?days=45")
    assert resp.status_code == 200
    assert "Upcoming charges" in resp.text


def test_alerts_page(seeded_dashboard):
    client, _ = seeded_dashboard
    resp = client.get("/alerts")
    assert resp.status_code == 200
    assert "New large" in resp.text or "PRLV" in resp.text


def test_advice_page_empty(dashboard_client):
    client, _ = dashboard_client
    resp = client.get("/advice")
    assert resp.status_code == 200
    assert "No advice on file" in resp.text


def test_advice_page_with_item(dashboard_client):
    client, state = dashboard_client
    with store.connect(state.db_path) as conn:
        store.init_schema(conn)
        persist_advice(
            conn,
            kind="subscription_overlap",
            input_hash="h1",
            model="claude-haiku-4-5",
            payload={
                "recommendations": [
                    {
                        "domain": "streaming",
                        "action": "consolidate",
                        "services": ["NETFLIX", "DISNEY PLUS"],
                        "suggested_services": ["NETFLIX"],
                        "monthly_savings": 8.99,
                        "rationale": "Overlapping catalogs.",
                    },
                ]
            },
        )
    resp = client.get("/advice")
    assert resp.status_code == 200
    assert "streaming" in resp.text
    assert "Overlapping catalogs" in resp.text


# ═══════════════════════════════════════════════════════════════════════════
# Write routes (HTMX)
# ═══════════════════════════════════════════════════════════════════════════


def test_merchants_set_category_roundtrip(seeded_dashboard):
    client, state = seeded_dashboard
    # Find NETFLIX's merchant_id
    with store.connect(state.db_path) as conn:
        mid = conn.execute(
            "SELECT merchant_id FROM merchants WHERE canonical_name='NETFLIX'"
        ).fetchone()[0]

    resp = client.post(f"/merchants/{mid}/category", data={"category": "Entertainment"})
    assert resp.status_code == 200
    assert "Entertainment" in resp.text

    with store.connect(state.db_path) as conn:
        row = conn.execute(
            "SELECT category, category_source FROM merchants WHERE merchant_id = ?",
            (mid,),
        ).fetchone()
    assert row[0] == "Entertainment"
    assert row[1] == "user"


def test_merchants_clear_category(seeded_dashboard):
    """Empty category submission clears the override."""
    client, state = seeded_dashboard
    with store.connect(state.db_path) as conn:
        mid = conn.execute(
            "SELECT merchant_id FROM merchants WHERE canonical_name='NETFLIX'"
        ).fetchone()[0]

    client.post(f"/merchants/{mid}/category", data={"category": "Entertainment"})
    resp = client.post(f"/merchants/{mid}/category", data={"category": ""})
    assert resp.status_code == 200

    with store.connect(state.db_path) as conn:
        row = conn.execute(
            "SELECT category, category_source FROM merchants WHERE merchant_id = ?",
            (mid,),
        ).fetchone()
    assert row[0] is None
    assert row[1] is None


def test_accounts_toggle(seeded_dashboard):
    client, state = seeded_dashboard
    resp = client.post("/accounts/a1/toggle")
    assert resp.status_code == 200
    assert "EXCLUDED" in resp.text

    with store.connect(state.db_path) as conn:
        val = conn.execute(
            "SELECT excluded_from_spend FROM accounts WHERE account_uid='a1'"
        ).fetchone()[0]
    assert val == 1

    # Toggle back
    resp = client.post("/accounts/a1/toggle")
    assert resp.status_code == 200
    with store.connect(state.db_path) as conn:
        val = conn.execute(
            "SELECT excluded_from_spend FROM accounts WHERE account_uid='a1'"
        ).fetchone()[0]
    assert val == 0


def test_accounts_toggle_404(dashboard_client):
    client, _ = dashboard_client
    resp = client.post("/accounts/nonexistent/toggle")
    assert resp.status_code == 404


def test_advice_dismiss_roundtrip(dashboard_client):
    client, state = dashboard_client
    with store.connect(state.db_path) as conn:
        store.init_schema(conn)
        rid = persist_advice(
            conn,
            kind="cutback",
            input_hash="h1",
            model="m",
            payload={"suggestions": []},
        )

    resp = client.post(f"/advice/{rid}/dismiss")
    assert resp.status_code == 200

    with store.connect(state.db_path) as conn:
        row = conn.execute(
            "SELECT dismissed_at FROM advice WHERE id = ?",
            (rid,),
        ).fetchone()
    assert row[0] is not None


def test_advice_dismiss_404(dashboard_client):
    client, _ = dashboard_client
    resp = client.post("/advice/99999/dismiss")
    assert resp.status_code == 404


def test_uncategorized_page_shows_llm_buttons(seeded_dashboard):
    """Both provider buttons appear on the Uncategorized view."""
    client, _ = seeded_dashboard
    resp = client.get("/merchants?uncategorized=true")
    assert resp.status_code == 200
    assert "AI (API)" in resp.text
    assert "AI (Claude Code" in resp.text
    assert "provider=api" in resp.text
    assert "provider=claude-cli" in resp.text


def test_merchants_page_hides_llm_button(seeded_dashboard):
    """LLM buttons only show on the uncategorized view."""
    client, _ = seeded_dashboard
    resp = client.get("/merchants")
    assert resp.status_code == 200
    assert "AI (API)" not in resp.text
    assert "AI (Claude Code" not in resp.text


def test_rules_page_renders(tmp_path, dashboard_client):
    """/rules renders even when there's no rules file yet."""
    client, _ = dashboard_client
    resp = client.get("/rules")
    assert resp.status_code == 200
    assert "Categorization rules" in resp.text
    assert "Add a new rule" in resp.text


def test_rules_add_and_delete_roundtrip(monkeypatch, tmp_path, dashboard_client):
    """POST /rules/add appends; POST /rules/{i}/delete removes."""
    # Redirect config dir to tmp so we don't touch user's real ~/.config/finance/rules.yaml
    monkeypatch.setenv("FINANCE_CONFIG_DIR", str(tmp_path))
    rules_path = tmp_path / "rules.yaml"

    client, _ = dashboard_client

    # Add one
    resp = client.post("/rules/add", data={"match": "(?i)netflix", "category": "Subscriptions"})
    assert resp.status_code == 200
    assert "(?i)netflix" in resp.text
    assert rules_path.exists()

    # Invalid regex → error fragment
    bad = client.post("/rules/add", data={"match": "(unclosed", "category": "Subscriptions"})
    assert bad.status_code == 400
    assert "Invalid regex" in bad.text

    # Bad category → error
    bad_cat = client.post("/rules/add", data={"match": "valid", "category": "NotInTaxonomy"})
    assert bad_cat.status_code == 400

    # Delete
    resp = client.post("/rules/0/delete")
    assert resp.status_code == 200
    assert "(?i)netflix" not in resp.text
    assert "No rules yet" in resp.text


def test_settings_page_no_key(monkeypatch, dashboard_client):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    import keyring

    monkeypatch.setattr(keyring, "get_password", lambda svc, user: None)

    client, _ = dashboard_client
    resp = client.get("/settings")
    assert resp.status_code == 200
    assert "No key set" in resp.text


def test_settings_page_with_env_key(monkeypatch, dashboard_client):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    client, _ = dashboard_client
    resp = client.get("/settings")
    assert resp.status_code == 200
    assert "Key configured" in resp.text
    assert "environment variable" in resp.text


def test_settings_set_key_stores_in_keyring(monkeypatch, dashboard_client):
    import keyring

    stored: dict = {}
    monkeypatch.setattr(
        keyring, "set_password", lambda svc, user, pw: stored.update({(svc, user): pw})
    )

    client, _ = dashboard_client
    resp = client.post("/settings/llm-key", data={"api_key": "sk-ant-demo"})
    assert resp.status_code == 200
    assert "API key stored" in resp.text
    assert ("finance-anthropic", "api-key") in stored


def test_proposals_shown_on_uncategorized_page(seeded_dashboard):
    """Persisted llm_proposals render on the Uncategorized page with Accept buttons."""
    client, state = seeded_dashboard
    # Seed a proposal for the NETFLIX merchant (still NULL category from fixture)
    with store.connect(state.db_path) as conn:
        mid = conn.execute(
            "SELECT merchant_id FROM merchants WHERE canonical_name='NETFLIX'"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO llm_proposals (merchant_id, category, confidence, reasoning,"
            " model, generated_at) VALUES (?, 'Subscriptions', 0.75, 'monthly card charge',"
            " 'claude-haiku-4-5', '2026-04-15T00:00:00Z')",
            (mid,),
        )
        conn.commit()

    resp = client.get("/merchants?uncategorized=true")
    assert resp.status_code == 200
    assert "LLM suggestions" in resp.text
    assert "NETFLIX" in resp.text
    assert "Subscriptions" in resp.text
    assert "monthly card charge" in resp.text
    assert "Accept" in resp.text and "Ignore" in resp.text


def test_accept_proposal_applies_category(seeded_dashboard):
    client, state = seeded_dashboard
    with store.connect(state.db_path) as conn:
        mid = conn.execute(
            "SELECT merchant_id FROM merchants WHERE canonical_name='NETFLIX'"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO llm_proposals (merchant_id, category, confidence, reasoning,"
            " model, generated_at) VALUES (?, 'Subscriptions', 0.75, 'r', 'm', '2026-04-15')",
            (mid,),
        )
        conn.commit()

    resp = client.post(f"/merchants/{mid}/accept-proposal")
    assert resp.status_code == 200

    with store.connect(state.db_path) as conn:
        row = conn.execute(
            "SELECT category, category_source FROM merchants WHERE merchant_id = ?",
            (mid,),
        ).fetchone()
        prop = conn.execute(
            "SELECT 1 FROM llm_proposals WHERE merchant_id = ?",
            (mid,),
        ).fetchone()
    assert row[0] == "Subscriptions"
    assert row[1] == "user"  # accepting = user approval
    assert prop is None  # proposal cleaned up


def test_ignore_proposal_removes_row(seeded_dashboard):
    client, state = seeded_dashboard
    with store.connect(state.db_path) as conn:
        mid = conn.execute(
            "SELECT merchant_id FROM merchants WHERE canonical_name='NETFLIX'"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO llm_proposals (merchant_id, category, confidence, reasoning,"
            " model, generated_at) VALUES (?, 'Subscriptions', 0.75, 'r', 'm', '2026-04-15')",
            (mid,),
        )
        conn.commit()

    resp = client.post(f"/merchants/{mid}/ignore-proposal")
    assert resp.status_code == 200

    with store.connect(state.db_path) as conn:
        prop = conn.execute(
            "SELECT 1 FROM llm_proposals WHERE merchant_id = ?",
            (mid,),
        ).fetchone()
        row = conn.execute(
            "SELECT category, category_source FROM merchants WHERE merchant_id = ?",
            (mid,),
        ).fetchone()
    assert prop is None  # proposal gone
    assert row[0] is None  # category unchanged (NULL)
    assert row[1] is None


def test_accept_all_proposals(seeded_dashboard):
    """POST /merchants/accept-all-proposals applies every persisted proposal at once."""
    client, state = seeded_dashboard
    with store.connect(state.db_path) as conn:
        conn.execute(
            "INSERT INTO merchants (canonical_name, updated_at) VALUES"
            " ('FOO', '2026-04-15'), ('BAR', '2026-04-15')"
        )
        for name, cat in [("FOO", "Shopping"), ("BAR", "Dining")]:
            mid = conn.execute(
                "SELECT merchant_id FROM merchants WHERE canonical_name = ?",
                (name,),
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO llm_proposals (merchant_id, category, confidence, reasoning,"
                " model, generated_at) VALUES (?, ?, 0.72, 'r', 'm', '2026-04-15')",
                (mid, cat),
            )
        conn.commit()

    resp = client.post("/merchants/accept-all-proposals")
    assert resp.status_code == 200
    assert "Applied 2" in resp.text

    with store.connect(state.db_path) as conn:
        cats = dict(
            conn.execute(
                "SELECT canonical_name, category FROM merchants WHERE canonical_name IN ('FOO', 'BAR')"
            ).fetchall()
        )
        left = conn.execute("SELECT COUNT(*) FROM llm_proposals").fetchone()[0]
    assert cats == {"FOO": "Shopping", "BAR": "Dining"}
    assert left == 0


def test_accept_proposal_404_when_no_proposal(dashboard_client):
    client, _ = dashboard_client
    resp = client.post("/merchants/99999/accept-proposal")
    assert resp.status_code == 404


def test_llm_categorize_endpoint_without_key(monkeypatch, seeded_dashboard):
    """Missing API key → friendly error fragment, not a crash."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    import keyring

    monkeypatch.setattr(keyring, "get_password", lambda svc, user: None)

    client, _ = seeded_dashboard
    resp = client.post("/merchants/llm-categorize")
    assert resp.status_code == 200
    assert "No Anthropic API key" in resp.text
