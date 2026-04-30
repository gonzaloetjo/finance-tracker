"""Tests for Stage C forecast."""

from __future__ import annotations

from datetime import date, timedelta

from finance.analysis.enrich import enrich_transactions
from finance.analysis.forecast import next_expected_charges
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


def test_forecast_surfaces_next_charge(tmp_path):
    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
        _seed_prlv(conn, "NETFLIX", -15.49)
        enrich_transactions(conn)

        df = next_expected_charges(conn, horizon_days=45)
        assert not df.empty
        assert (df["merchant"] == "NETFLIX").any()
        # days_until should be non-negative and ≤ horizon
        assert (df["days_until"] >= 0).all()
        assert (df["days_until"] <= 45).all()


def test_forecast_skips_inactive_streams(tmp_path):
    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
        # Last seen 500 days ago → inactive
        conn.execute(
            "INSERT INTO sessions (session_id, aspsp_name, aspsp_country, valid_until, created_at)"
            " VALUES ('s1', 'BNP Paribas', 'FR', '2099-01-01', '2026-01-01')"
        )
        conn.execute(
            "INSERT INTO accounts (account_uid, session_id, iban, name, currency, account_type, raw_json)"
            " VALUES ('a1', 's1', 'FR00', 'Checking', 'EUR', 'CACC', '{}')"
        )
        for i in range(4):
            bdate = (date.today() - timedelta(days=500 + 30 * i)).isoformat()
            memo = f"PRLV SEPA ANCIENNE ECH/010124 ID EMETTEUR/X MDT/M REF/R{i} LIB/L"
            conn.execute(
                "INSERT INTO transactions (transaction_id, account_uid, booking_date, amount, currency,"
                " remittance_info, raw_json, fetched_at) VALUES (?, 'a1', ?, -10.0, 'EUR', ?, '{}', '2026-01-01')",
                (f"old_{i}", bdate, memo),
            )
        conn.commit()
        enrich_transactions(conn)
        df = next_expected_charges(conn, horizon_days=30)
        assert df.empty
