"""Tests for stream grouping + cadence detection."""

from __future__ import annotations

from datetime import date, timedelta

from finance.analysis.streams import _band_bucket, _make_stream_id, group_streams
from finance.db.store import connect, init_schema


def _seed_enriched(conn, merchant_name: str, txn_type: str, amounts_dates: list[tuple[float, str]]):
    """Insert a merchant, transactions, and tx_enrichment rows."""
    conn.execute(
        "INSERT INTO sessions (session_id, aspsp_name, aspsp_country, valid_until, created_at)"
        " VALUES ('s1', 'BNP Paribas', 'FR', '2099-01-01', '2026-01-01')"
        " ON CONFLICT DO NOTHING"
    )
    conn.execute(
        "INSERT INTO accounts (account_uid, session_id, iban, name, currency, account_type, raw_json)"
        " VALUES ('a1', 's1', 'FR00', 'Checking', 'EUR', 'CACC', '{}')"
        " ON CONFLICT DO NOTHING"
    )
    conn.execute(
        "INSERT OR IGNORE INTO merchants (canonical_name, updated_at) VALUES (?, '2026-01-01')",
        (merchant_name,),
    )
    mid = conn.execute(
        "SELECT merchant_id FROM merchants WHERE canonical_name=?", (merchant_name,)
    ).fetchone()[0]

    for i, (amount, bdate) in enumerate(amounts_dates):
        tid = f"tx_{merchant_name}_{i}"
        conn.execute(
            "INSERT INTO transactions (transaction_id, account_uid, booking_date, amount, currency, raw_json, fetched_at)"
            " VALUES (?, 'a1', ?, ?, 'EUR', '{}', '2026-01-01')",
            (tid, bdate, amount),
        )
        conn.execute(
            "INSERT INTO tx_enrichment (tx_id, txn_type, merchant_id, enriched_at)"
            " VALUES (?, ?, ?, '2026-01-01')",
            (tid, txn_type, mid),
        )
    conn.commit()
    return mid


def test_stream_id_immutable_across_runs(tmp_path):
    """stream_id must be identical on two consecutive group_streams calls."""
    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
        today = date.today()
        dates = [(today - timedelta(days=30 * i)).isoformat() for i in range(5)]
        _seed_enriched(conn, "NETFLIX", "PRLV", [(-15.49, d) for d in dates])

        streams1 = group_streams(conn)
        conn.commit()
        streams2 = group_streams(conn)
        conn.commit()

        assert len(streams1) == 1
        assert len(streams2) == 1
        assert streams1[0].stream_id == streams2[0].stream_id


def test_prlv_monthly_is_recurring_and_subscription(tmp_path):
    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
        today = date.today()
        dates = [(today - timedelta(days=30 * i)).isoformat() for i in range(4)]
        _seed_enriched(conn, "ENGIE", "PRLV", [(-80.0, d) for d in dates])

        streams = group_streams(conn)
        conn.commit()

        assert len(streams) == 1
        s = streams[0]
        assert s.is_recurring is True
        assert s.is_subscription is True
        assert s.classification == "monthly"
        assert s.txn_type == "PRLV"


def test_facture_weekly_recurring_not_subscription(tmp_path):
    """Weekly grocery runs: recurring but not subscription (weekly classification)."""
    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
        today = date.today()
        # 12 weekly purchases spanning ~84 days
        dates = [(today - timedelta(days=7 * i)).isoformat() for i in range(12)]
        _seed_enriched(conn, "FRANPRIX", "FACTURE", [(-25.0, d) for d in dates])

        streams = group_streams(conn)
        conn.commit()

        assert len(streams) == 1
        s = streams[0]
        assert s.classification == "weekly"
        assert s.is_recurring is True
        assert s.is_subscription is False  # weekly ∉ {monthly, quarterly, annual}


def test_two_amount_bands_create_two_streams(tmp_path):
    """Same merchant, very different amounts → separate streams."""
    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
        today = date.today()
        dates = [(today - timedelta(days=30 * i)).isoformat() for i in range(4)]
        # Small purchases + large purchases
        small = [(-5.0, d) for d in dates]
        large = [(-500.0, d) for d in dates]
        _seed_enriched(conn, "AMAZON", "FACTURE", small + large)

        streams = group_streams(conn)
        conn.commit()

        assert len(streams) == 2


def test_stream_id_depends_on_merchant_id_not_name(tmp_path):
    """Different merchant_ids with same amount → different stream_ids."""
    bucket = _band_bucket(-15.0)
    sid1 = _make_stream_id(1, bucket)
    sid2 = _make_stream_id(2, bucket)
    assert sid1 != sid2


def test_dining_category_blocks_subscription(tmp_path):
    """Real-data escape: monthly NOODLE FOOD at stable amounts shouldn't be a sub."""
    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
        today = date.today()
        dates = [(today - timedelta(days=30 * i)).isoformat() for i in range(4)]
        mid = _seed_enriched(conn, "NOODLE FOOD", "FACTURE", [(-20.80, d) for d in dates])
        # Pre-tag merchant as Dining (simulates the seed/rule having already run)
        conn.execute(
            "UPDATE merchants SET category='Dining', category_source='rule' WHERE merchant_id=?",
            (mid,),
        )
        conn.commit()

        streams = group_streams(conn)
        conn.commit()
        assert len(streams) == 1
        s = streams[0]
        assert s.is_recurring is True  # structurally recurring
        assert s.is_subscription is False  # but Dining category blocks the sub flag


def test_groceries_category_blocks_subscription(tmp_path):
    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
        today = date.today()
        dates = [(today - timedelta(days=30 * i)).isoformat() for i in range(4)]
        mid = _seed_enriched(conn, "FRANPRIX", "FACTURE", [(-65.00, d) for d in dates])
        conn.execute(
            "UPDATE merchants SET category='Groceries', category_source='rule' WHERE merchant_id=?",
            (mid,),
        )
        conn.commit()
        streams = group_streams(conn)
        conn.commit()
        assert streams[0].is_subscription is False


def test_subscriptions_label_short_circuits_structural_gate(tmp_path):
    """An LLM-labeled 'Subscriptions' merchant with only 2 monthly hits should
    be flagged as a subscription even if it doesn't hit the 45-day span gate."""
    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
        today = date.today()
        # Only 2 hits, 28 days apart — span = 28d, would normally fail the
        # 45-day gate and thus is_recurring=0.
        dates = [(today - timedelta(days=28 * i)).isoformat() for i in range(2)]
        mid = _seed_enriched(conn, "OBSCURE SAAS", "FACTURE", [(-19.99, d) for d in dates])
        conn.execute(
            "UPDATE merchants SET category='Subscriptions', category_source='llm' WHERE merchant_id=?",
            (mid,),
        )
        conn.commit()
        streams = group_streams(conn)
        conn.commit()
        assert len(streams) == 1
        # is_recurring may be False (short span) but is_subscription must be True
        # by the label short-circuit.
        assert streams[0].is_subscription is True


def test_transport_category_allows_subscription(tmp_path):
    """Transport stays eligible for subscription flag (Navigo €90.80/mo etc.).
    Category gate should only drop Dining/Groceries/Income/Transfer/Investment."""
    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
        today = date.today()
        dates = [(today - timedelta(days=30 * i)).isoformat() for i in range(4)]
        mid = _seed_enriched(conn, "SERVICE NAVIGO", "FACTURE", [(-90.80, d) for d in dates])
        conn.execute(
            "UPDATE merchants SET category='Transport', category_source='llm' WHERE merchant_id=?",
            (mid,),
        )
        conn.commit()
        streams = group_streams(conn)
        conn.commit()
        assert streams[0].is_recurring is True
        assert streams[0].is_subscription is True  # ← was False before the fix


def test_subscriptions_category_keeps_sub_flag(tmp_path):
    """A merchant already labeled 'Subscriptions' must still be flagged as such."""
    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
        today = date.today()
        dates = [(today - timedelta(days=30 * i)).isoformat() for i in range(4)]
        mid = _seed_enriched(conn, "NETFLIX", "FACTURE", [(-15.49, d) for d in dates])
        conn.execute(
            "UPDATE merchants SET category='Subscriptions', category_source='rule' WHERE merchant_id=?",
            (mid,),
        )
        conn.commit()
        streams = group_streams(conn)
        conn.commit()
        assert streams[0].is_subscription is True


def test_uncategorized_prlv_still_flagged_as_subscription(tmp_path):
    """Subscription detection is structural, not category-gated."""
    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
        today = date.today()
        dates = [(today - timedelta(days=30 * i)).isoformat() for i in range(4)]
        mid = _seed_enriched(conn, "UNKNOWN_PRLV_MERCHANT", "PRLV", [(-42.0, d) for d in dates])
        # Verify no category set
        row = conn.execute("SELECT category FROM merchants WHERE merchant_id=?", (mid,)).fetchone()
        assert row[0] is None

        streams = group_streams(conn)
        conn.commit()

        assert len(streams) == 1
        assert streams[0].is_subscription is True
