"""Tests for Stage C alerts."""

from __future__ import annotations

from datetime import date, timedelta

from finance.analysis.alerts import new_large_merchants, subscription_stopped
from finance.analysis.enrich import enrich_transactions
from finance.db.store import connect, init_schema


def _boot(conn):
    conn.execute(
        "INSERT INTO sessions (session_id, aspsp_name, aspsp_country, valid_until, created_at)"
        " VALUES ('s1', 'BNP Paribas', 'FR', '2099-01-01', '2026-01-01')"
    )
    conn.execute(
        "INSERT INTO accounts (account_uid, session_id, iban, name, currency, account_type, raw_json)"
        " VALUES ('a1', 's1', 'FR00', 'Checking', 'EUR', 'CACC', '{}')"
    )


def test_new_large_merchant_flagged(tmp_path):
    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
        _boot(conn)
        today = date.today()
        memo = "FACTURE CARTE DU 120426 RENELACHANCE.BI CARTE 0000XXXXXXXX0000 FRA 520,00EUR"
        conn.execute(
            "INSERT INTO transactions (transaction_id, account_uid, booking_date, amount, currency,"
            " remittance_info, raw_json, fetched_at)"
            " VALUES ('big1', 'a1', ?, -520.0, 'EUR', ?, '{}', '2026-01-01')",
            (today.isoformat(), memo),
        )
        conn.commit()
        enrich_transactions(conn)

        df = new_large_merchants(conn, amount_threshold=500.0, new_merchant_days=30)
        assert len(df) == 1
        assert df.iloc[0]["tx_id"] == "big1"
        assert "large charge" in df.iloc[0]["reason"]


def test_prlv_from_new_merchant_always_flagged(tmp_path):
    """PRLV from a merchant first-seen within the window is flagged regardless of amount."""
    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
        _boot(conn)
        today = date.today()
        memo = "PRLV SEPA SNEAKYSUB ECH/010126 ID EMETTEUR/X MDT/M REF/R LIB/L"
        conn.execute(
            "INSERT INTO transactions (transaction_id, account_uid, booking_date, amount, currency,"
            " remittance_info, raw_json, fetched_at)"
            " VALUES ('p1', 'a1', ?, -4.99, 'EUR', ?, '{}', '2026-01-01')",
            (today.isoformat(), memo),
        )
        conn.commit()
        enrich_transactions(conn)

        df = new_large_merchants(conn, amount_threshold=500.0)
        assert len(df) == 1
        assert "new-merchant PRLV" in df.iloc[0]["reason"]


def test_subscription_stopped_surfaces_dormant_sub(tmp_path):
    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
        _boot(conn)
        today = date.today()
        # Hits spaced ~30 days, last one 80 days ago → active=0, still within 120-day window
        for i in range(4):
            bdate = (today - timedelta(days=80 + 30 * i)).isoformat()
            memo = f"PRLV SEPA NETFLIX ECH/010125 ID EMETTEUR/X MDT/M REF/R{i} LIB/L"
            conn.execute(
                "INSERT INTO transactions (transaction_id, account_uid, booking_date, amount, currency,"
                " remittance_info, raw_json, fetched_at)"
                " VALUES (?, 'a1', ?, -15.49, 'EUR', ?, '{}', '2026-01-01')",
                (f"n_{i}", bdate, memo),
            )
        conn.commit()
        enrich_transactions(conn)

        df = subscription_stopped(conn, window_days=120)
        assert len(df) == 1
        row = df.iloc[0]
        assert row["merchant"] == "NETFLIX"
        assert abs(row["estimated_saved"] - 15.49) < 0.01
        assert row["months_since_last"] >= 2.0
