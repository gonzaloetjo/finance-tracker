"""Tests for the composed overview dashboard.

Verifies shape and composition — overview doesn't run its own queries, it
calls existing Stage C functions and bundles the results.
"""

from __future__ import annotations

from datetime import date, timedelta

from finance.analysis.enrich import enrich_transactions
from finance.analysis.overview import AccountSummary, build_overview
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


def test_overview_empty_db(tmp_path):
    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
        data = build_overview(conn)
        assert data.accounts == []
        assert data.trends.empty
        assert data.top_merchants.empty
        assert data.recurring.empty
        assert data.subscriptions.empty


def test_overview_populates_sections(tmp_path):
    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
        _boot(conn)
        today = date.today()
        # Monthly PRLV Netflix — should populate recurring + subscriptions + overlaps(?)
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

        data = build_overview(conn, months=3, top_n=5, forecast_days=45)

        # Accounts
        assert len(data.accounts) == 1
        assert isinstance(data.accounts[0], AccountSummary)
        assert data.accounts[0].n_tx == 4

        # Top merchants
        assert "NETFLIX" in data.top_merchants["merchant"].values

        # Recurring / subscriptions / forecast
        assert not data.recurring.empty
        assert not data.subscriptions.empty
        assert not data.forecast.empty  # next expected charge within 45d


def test_overview_respects_spend_only(tmp_path):
    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
        conn.execute(
            "INSERT INTO sessions (session_id, aspsp_name, aspsp_country, valid_until, created_at)"
            " VALUES ('s1', 'BNP Paribas', 'FR', '2099-01-01', '2026-01-01')"
        )
        # Two accounts — one flagged excluded
        conn.execute(
            "INSERT INTO accounts (account_uid, session_id, iban, name, currency, account_type, raw_json, excluded_from_spend)"
            " VALUES ('checking', 's1', 'FR1', 'Checking', 'EUR', 'CACC', '{}', 0),"
            "        ('savings',  's1', 'FR2', 'Savings',  'EUR', 'CACC', '{}', 1)"
        )
        for tid, acc, amt in [("c1", "checking", -25.0), ("s1", "savings", -100.0)]:
            conn.execute(
                "INSERT INTO transactions (transaction_id, account_uid, booking_date, amount, currency,"
                " remittance_info, raw_json, fetched_at)"
                " VALUES (?, ?, '2026-04-10', ?, 'EUR',"
                " 'FACTURE CARTE DU 100426 SOMEMERCHANT CARTE 0000XXXXXXXX0000', '{}', '2026-01-01')",
                (tid, acc, amt),
            )
        conn.commit()
        enrich_transactions(conn)

        data_all = build_overview(conn, spend_only=False)
        data_spend = build_overview(conn, spend_only=True)

        # Both accounts reported in the header regardless
        assert len(data_all.accounts) == 2
        assert len(data_spend.accounts) == 2
        # But top_merchants shrinks when spend_only=True
        all_total = float(
            data_all.top_merchants[data_all.top_merchants["merchant"] == "SOMEMERCHANT"][
                "total_spend"
            ].iloc[0]
        )
        spend_total = float(
            data_spend.top_merchants[data_spend.top_merchants["merchant"] == "SOMEMERCHANT"][
                "total_spend"
            ].iloc[0]
        )
        assert all_total == 125.0
        assert spend_total == 25.0
