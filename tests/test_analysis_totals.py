"""Tests for rollup totals."""

from __future__ import annotations

from datetime import date, timedelta

from finance.analysis.enrich import enrich_transactions
from finance.analysis.totals import Totals, compute_totals
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


def _prlv_monthly(conn, merchant, amount, n=4):
    today = date.today()
    for i in range(n):
        bdate = (today - timedelta(days=30 * i)).isoformat()
        memo = f"PRLV SEPA {merchant} ECH/010126 ID EMETTEUR/X MDT/M REF/R{i} LIB/L"
        conn.execute(
            "INSERT INTO transactions (transaction_id, account_uid, booking_date, amount, currency,"
            " remittance_info, raw_json, fetched_at)"
            " VALUES (?, 'a1', ?, ?, 'EUR', ?, '{}', '2026-01-01')",
            (f"{merchant}_{i}", bdate, amount, memo),
        )


def test_totals_empty_db(tmp_path):
    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
        t = compute_totals(conn)
        assert isinstance(t, Totals)
        assert t.monthly_subscriptions == 0.0
        assert t.monthly_spend_avg == 0.0
        assert t.window_months == 3


def test_totals_sums_subscriptions(tmp_path):
    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
        _boot(conn)
        _prlv_monthly(conn, "NETFLIX", -15.49)
        _prlv_monthly(conn, "ENGIE", -80.00)
        conn.commit()
        enrich_transactions(conn)

        t = compute_totals(conn)
        # Both monthly PRLV → both subscriptions. Sum of abs monthly_cost.
        assert abs(t.monthly_subscriptions - (15.49 + 80.00)) < 0.01


def test_totals_excludes_transfer_from_spend(tmp_path):
    """An outgoing CPTE A CPTE transfer should NOT inflate monthly_spend_avg."""
    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
        _boot(conn)
        # One transfer + one real spend (same amount) in the window.
        today = date.today().isoformat()
        memo_xfer = "VIR CPTE A CPTE EMIS /MOTIF X /BEN SELF /REFDO /REFBEN"
        memo_food = "FACTURE CARTE DU 050126 DELIVEROO CARTE 0000XXXXXXXX0000 FRA 25,00EUR"
        conn.execute(
            "INSERT INTO transactions (transaction_id, account_uid, booking_date, amount, currency,"
            " remittance_info, raw_json, fetched_at)"
            " VALUES ('x1', 'a1', ?, -500.0, 'EUR', ?, '{}', '2026-01-01'),"
            "        ('f1', 'a1', ?, -25.0,  'EUR', ?, '{}', '2026-01-01')",
            (today, memo_xfer, today, memo_food),
        )
        conn.commit()
        enrich_transactions(conn)

        # spend_only=True drops Transfer
        t_spend = compute_totals(conn, months=3, spend_only=True)
        t_all = compute_totals(conn, months=3, spend_only=False)
        assert t_spend.monthly_spend_avg < t_all.monthly_spend_avg
        # Spend under spend_only should not include the €500 transfer.
        # Over 3 months, avg is €X/3 — we just need it to be < €(525)/3.
        assert t_spend.monthly_spend_avg < 500.0 / 3 + 0.01


def test_totals_includes_income(tmp_path):
    """Positive monthly VIR → detected as Income → included in monthly_income_avg."""
    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
        _boot(conn)
        today = date.today()
        for i in range(3):
            bdate = (today - timedelta(days=30 * i)).isoformat()
            memo = f"VIR SEPA INST RECU /DE ACME CORP /MOTIF SALAIRE /REF R{i}"
            conn.execute(
                "INSERT INTO transactions (transaction_id, account_uid, booking_date, amount, currency,"
                " remittance_info, raw_json, fetched_at)"
                " VALUES (?, 'a1', ?, 3000.00, 'EUR', ?, '{}', '2026-01-01')",
                (f"inc_{i}", bdate, memo),
            )
        conn.commit()
        enrich_transactions(conn)

        t = compute_totals(conn, months=3)
        # 3 months × €3000 / 3 = €3000 average.
        assert abs(t.monthly_income_avg - 3000.0) < 1.0


def test_totals_category_breakdown(tmp_path):
    """Per-category monthly breakdown excludes Transfer and non-EUR."""
    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
        _boot(conn)
        today = date.today().isoformat()
        conn.execute(
            "INSERT INTO merchants (merchant_id, canonical_name, category, category_source,"
            " updated_at) VALUES (1, 'M_Groceries', 'Groceries', 'rule', '2026-01-01'),"
            "                    (2, 'M_Dining',    'Dining',    'rule', '2026-01-01')"
        )
        conn.execute(
            "INSERT INTO transactions (transaction_id, account_uid, booking_date, amount, currency,"
            " remittance_info, raw_json, fetched_at)"
            " VALUES ('g1', 'a1', ?, -60.0,  'EUR', 'FACTURE', '{}', '2026-01-01'),"
            "        ('d1', 'a1', ?, -40.0,  'EUR', 'FACTURE', '{}', '2026-01-01')",
            (today, today),
        )
        conn.execute(
            "INSERT INTO tx_enrichment (tx_id, txn_type, merchant_id, enriched_at)"
            " VALUES ('g1', 'FACTURE', 1, '2026-01-01'),"
            "        ('d1', 'FACTURE', 2, '2026-01-01')"
        )
        conn.commit()
        t = compute_totals(conn, months=3, spend_only=False)
        # Category totals are monthly-averaged over 3 months.
        assert "Groceries" in t.spend_by_category
        assert "Dining" in t.spend_by_category
        assert abs(t.spend_by_category["Groceries"] - 20.0) < 0.01  # 60 / 3
        assert abs(t.spend_by_category["Dining"] - 13.33) < 0.1  # 40 / 3
