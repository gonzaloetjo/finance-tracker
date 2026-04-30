"""Tests for Stage C subscription view + overlap detection."""

from __future__ import annotations

from datetime import date, timedelta

from finance.analysis.enrich import enrich_transactions
from finance.analysis.subscriptions import find_overlaps, find_subscriptions
from finance.db.store import connect, init_schema


def _seed_prlv(conn, merchant_raw: str, amount: float, n: int = 4):
    conn.execute(
        "INSERT INTO sessions (session_id, aspsp_name, aspsp_country, valid_until, created_at)"
        " VALUES ('s1', 'BNP Paribas', 'FR', '2099-01-01', '2026-01-01') ON CONFLICT DO NOTHING"
    )
    conn.execute(
        "INSERT INTO accounts (account_uid, session_id, iban, name, currency, account_type, raw_json)"
        " VALUES ('a1', 's1', 'FR00', 'Checking', 'EUR', 'CACC', '{}') ON CONFLICT DO NOTHING"
    )
    today = date.today()
    for i in range(n):
        bdate = (today - timedelta(days=30 * i)).isoformat()
        memo = f"PRLV SEPA {merchant_raw} ECH/010126 ID EMETTEUR/X MDT/M REF/R{i} LIB/L"
        conn.execute(
            "INSERT INTO transactions (transaction_id, account_uid, booking_date, amount, currency,"
            " remittance_info, raw_json, fetched_at) VALUES (?, 'a1', ?, ?, 'EUR', ?, '{}', '2026-01-01')",
            (f"tx_{merchant_raw}_{i}", bdate, amount, memo),
        )
    conn.commit()


def test_subscriptions_includes_uncategorized_prlv(tmp_path):
    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
        _seed_prlv(conn, "MYSTERY SUBSCRIPTION", -9.99)
        enrich_transactions(conn)

        df = find_subscriptions(conn)
        assert len(df) == 1
        # Category may well be NULL — that's fine
        assert df.iloc[0]["merchant"] == "MYSTERY SUBSCRIPTION"
        assert df.iloc[0]["classification"] == "monthly"


def test_overlaps_groups_by_domain(tmp_path):
    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
        _seed_prlv(conn, "NETFLIX", -15.49)
        _seed_prlv(conn, "DISNEY PLUS", -8.99)
        _seed_prlv(conn, "SPOTIFY", -9.99)  # different domain, shouldn't be in streaming
        enrich_transactions(conn)

        df = find_overlaps(conn)
        assert "streaming" in df["domain"].values
        streaming = df.set_index("domain").loc["streaming"]
        assert streaming["services_count"] == 2
        assert set(streaming["services"]) == {"NETFLIX", "DISNEY PLUS"}
        # Total monthly cost of the streaming overlap
        assert abs(abs(streaming["monthly_cost"]) - (15.49 + 8.99)) < 0.01


def test_overlaps_needs_at_least_two(tmp_path):
    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
        _seed_prlv(conn, "NETFLIX", -15.49)
        enrich_transactions(conn)
        df = find_overlaps(conn)
        # Only one streaming service → not an overlap
        assert df.empty or "streaming" not in df["domain"].values


def test_sub_candidates_surface_blocked_category(tmp_path):
    """Dining-categorized monthly stream with stable amounts: NOT a sub by default,
    but should appear in find_sub_candidates for user review."""
    from finance.analysis.enrich import enrich_transactions
    from finance.analysis.subscriptions import find_sub_candidates

    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
        conn.execute(
            "INSERT INTO sessions (session_id, aspsp_name, aspsp_country, valid_until, created_at)"
            " VALUES ('s1', 'BNP Paribas', 'FR', '2099-01-01', '2026-01-01')"
        )
        conn.execute(
            "INSERT INTO accounts (account_uid, session_id, iban, name, currency, account_type, raw_json)"
            " VALUES ('a1', 's1', 'FR00', 'Checking', 'EUR', 'CACC', '{}')"
        )
        today = date.today()
        # 4 monthly hits at exactly €40 each (0% spread) — e.g. a meal kit.
        for i in range(4):
            bdate = (today - timedelta(days=30 * i)).isoformat()
            conn.execute(
                "INSERT INTO transactions (transaction_id, account_uid, booking_date, amount, currency,"
                " remittance_info, raw_json, fetched_at)"
                " VALUES (?, 'a1', ?, -40.0, 'EUR',"
                " 'FACTURE CARTE DU 100426 MEALKIT CARTE 0000XXXXXXXX0000', '{}', '2026-01-01')",
                (f"mk_{i}", bdate),
            )
        conn.commit()
        enrich_transactions(conn)
        # Pin Dining category manually (simulate LLM result).
        conn.execute(
            "UPDATE merchants SET category='Dining', category_source='llm'"
            " WHERE canonical_name='MEALKIT'"
        )
        conn.commit()
        # Re-run streams so the category gate applies.
        from finance.analysis.streams import group_streams

        group_streams(conn)
        conn.commit()

        df = find_sub_candidates(conn)
        assert not df.empty
        assert "MEALKIT" in df["merchant"].values
        row = df[df["merchant"] == "MEALKIT"].iloc[0]
        assert row["category"] == "Dining"
        assert row["amount_spread_pct"] == 0.0


def test_sub_candidates_filters_by_spread(tmp_path):
    """Streams with high amount variance shouldn't appear as candidates
    (they're clearly not subscriptions)."""
    from finance.analysis.enrich import enrich_transactions
    from finance.analysis.streams import group_streams
    from finance.analysis.subscriptions import find_sub_candidates

    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
        conn.execute(
            "INSERT INTO sessions (session_id, aspsp_name, aspsp_country, valid_until, created_at)"
            " VALUES ('s1', 'BNP Paribas', 'FR', '2099-01-01', '2026-01-01')"
        )
        conn.execute(
            "INSERT INTO accounts (account_uid, session_id, iban, name, currency, account_type, raw_json)"
            " VALUES ('a1', 's1', 'FR00', 'Checking', 'EUR', 'CACC', '{}')"
        )
        today = date.today()
        # Monthly hits but varying amounts: €20, €30, €50, €80 (huge spread)
        for i, amt in enumerate([-20.0, -30.0, -50.0, -80.0]):
            bdate = (today - timedelta(days=30 * i)).isoformat()
            conn.execute(
                "INSERT INTO transactions (transaction_id, account_uid, booking_date, amount, currency,"
                " remittance_info, raw_json, fetched_at)"
                " VALUES (?, 'a1', ?, ?, 'EUR',"
                " 'FACTURE CARTE DU 100426 RESTAURANTX CARTE 0000XXXXXXXX0000', '{}', '2026-01-01')",
                (f"r_{i}", bdate, amt),
            )
        conn.commit()
        enrich_transactions(conn)
        conn.execute(
            "UPDATE merchants SET category='Dining', category_source='llm' WHERE canonical_name='RESTAURANTX'"
        )
        conn.commit()
        group_streams(conn)
        conn.commit()

        df = find_sub_candidates(conn)
        assert "RESTAURANTX" not in df["merchant"].values


def test_sub_candidates_respects_override(tmp_path):
    """Once the user has rejected/accepted, the candidate shouldn't reappear."""
    from finance.analysis.enrich import enrich_transactions
    from finance.analysis.streams import group_streams
    from finance.analysis.subscriptions import find_sub_candidates

    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
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
            conn.execute(
                "INSERT INTO transactions (transaction_id, account_uid, booking_date, amount, currency,"
                " remittance_info, raw_json, fetched_at)"
                " VALUES (?, 'a1', ?, -40.0, 'EUR',"
                " 'FACTURE CARTE DU 100426 MEALKIT CARTE 0000XXXXXXXX0000', '{}', '2026-01-01')",
                (f"mk_{i}", bdate),
            )
        conn.commit()
        enrich_transactions(conn)
        conn.execute(
            "UPDATE merchants SET category='Dining', category_source='llm' WHERE canonical_name='MEALKIT'"
        )
        conn.commit()
        group_streams(conn)
        conn.commit()

        df_before = find_sub_candidates(conn)
        assert "MEALKIT" in df_before["merchant"].values
        sid = df_before[df_before["merchant"] == "MEALKIT"].iloc[0]["stream_id"]

        # User rejects — set override=0.
        conn.execute(
            "UPDATE streams SET subscription_override = 0, is_subscription = 0 WHERE stream_id = ?",
            (sid,),
        )
        conn.commit()

        df_after = find_sub_candidates(conn)
        assert "MEALKIT" not in df_after["merchant"].values


def test_overlaps_custom_domain_map(tmp_path):
    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
        _seed_prlv(conn, "FOOFIRST PLUS", -5.00)
        _seed_prlv(conn, "FOOSECOND PLUS", -5.00)
        enrich_transactions(conn)
        df = find_overlaps(conn, domain_map={"foo-things": ["FOO"]})
        assert not df.empty
        assert df.iloc[0]["domain"] == "foo-things"
        assert df.iloc[0]["services_count"] == 2
