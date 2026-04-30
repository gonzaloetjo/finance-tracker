"""Tests for Stage C spending trends."""

from __future__ import annotations

from datetime import date

from finance.analysis.trends import category_growth, mom_changes
from finance.db.store import connect, init_schema


def _seed_with_categories(conn, rows: list[tuple[str, float, str, str]]):
    """rows = [(tx_id, amount, booking_date, category), ...]

    Categories are attached via the canonical merchants → tx_enrichment
    chain, not the dropped `transactions.category` legacy column.
    """
    conn.execute(
        "INSERT INTO sessions (session_id, aspsp_name, aspsp_country, valid_until, created_at)"
        " VALUES ('s1', 'BNP Paribas', 'FR', '2099-01-01', '2026-01-01')"
    )
    conn.execute(
        "INSERT INTO accounts (account_uid, session_id, iban, name, currency, account_type, raw_json)"
        " VALUES ('a1', 's1', 'FR00', 'Checking', 'EUR', 'CACC', '{}')"
    )
    # One merchant per distinct category — gives us a stable merchant_id to
    # link tx_enrichment rows against. category_source='rule' is overwritable
    # but never overwritten in these tests (no enrich_transactions call).
    seen: dict[str, int] = {}
    for tid, amount, bdate, category in rows:
        if category not in seen:
            mid = len(seen) + 1
            conn.execute(
                "INSERT INTO merchants (merchant_id, canonical_name, category,"
                " category_source, updated_at)"
                " VALUES (?, ?, ?, 'rule', '2026-01-01')",
                (mid, f"M_{category}", category),
            )
            seen[category] = mid
        mid = seen[category]
        conn.execute(
            "INSERT INTO transactions (transaction_id, account_uid, booking_date, amount,"
            " currency, remittance_info, raw_json, fetched_at)"
            " VALUES (?, 'a1', ?, ?, 'EUR', ?, '{}', '2026-01-01')",
            (tid, bdate, amount, f"memo_{tid}"),
        )
        conn.execute(
            "INSERT INTO tx_enrichment (tx_id, txn_type, merchant_id, enriched_at)"
            " VALUES (?, 'FACTURE', ?, '2026-01-01')",
            (tid, mid),
        )
    conn.commit()


def test_mom_changes_computes_deltas(tmp_path):
    from datetime import timedelta

    today = date.today()
    curr_month = today.replace(day=15).isoformat()
    # previous month: use day=15 on the month before (handles month-length edge)
    first_of_month = today.replace(day=1)
    prev_month_obj = first_of_month - timedelta(days=1)
    prev_month = prev_month_obj.replace(day=15).isoformat()

    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
        _seed_with_categories(
            conn,
            [
                ("t1", -100.0, prev_month, "Groceries"),
                ("t2", -150.0, curr_month, "Groceries"),
                ("t3", -50.0, prev_month, "Dining"),
                ("t4", -25.0, curr_month, "Dining"),
            ],
        )
        df = mom_changes(conn, months=3)
        assert not df.empty
        groceries = df.set_index("category").loc["Groceries"]
        assert abs(groceries["prev_spend"] - 100.0) < 0.01
        assert abs(groceries["curr_spend"] - 150.0) < 0.01
        assert abs(groceries["delta_abs"] - 50.0) < 0.01
        assert abs(groceries["delta_pct"] - 50.0) < 0.01


def test_mom_changes_returns_empty_without_two_months(tmp_path):
    today = date.today().isoformat()
    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
        _seed_with_categories(conn, [("t1", -10.0, today, "Groceries")])
        df = mom_changes(conn)
        assert df.empty


def test_category_growth_window(tmp_path):
    today = date.today()
    m0 = today.replace(day=10).isoformat()
    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
        _seed_with_categories(
            conn,
            [
                ("t1", -100.0, m0, "Groceries"),
                ("t2", -50.0, m0, "Dining"),
            ],
        )
        df = category_growth(conn, months=3)
        assert not df.empty
        assert set(df["category"]) == {"Groceries", "Dining"}
        groceries = df.set_index("category").loc["Groceries"]
        assert abs(groceries["total_spend"] - 100.0) < 0.01


def test_mom_excludes_non_eur(tmp_path):
    today = date.today()
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
        conn.execute(
            "INSERT INTO merchants (merchant_id, canonical_name, category, category_source,"
            " updated_at) VALUES (1, 'M_Shopping', 'Shopping', 'rule', '2026-01-01')"
        )
        conn.execute(
            "INSERT INTO transactions (transaction_id, account_uid, booking_date, amount, currency,"
            " remittance_info, raw_json, fetched_at)"
            " VALUES ('t1', 'a1', ?, -100.0, 'USD', 'memo', '{}', '2026-01-01')",
            (today.isoformat(),),
        )
        conn.execute(
            "INSERT INTO tx_enrichment (tx_id, txn_type, merchant_id, enriched_at)"
            " VALUES ('t1', 'FACTURE', 1, '2026-01-01')"
        )
        conn.commit()

        # Only non-EUR data → empty pivot
        df = mom_changes(conn)
        assert df.empty
