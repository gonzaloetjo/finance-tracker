"""Tests for Stage A — load_transactions."""

from __future__ import annotations

import pandas as pd

from finance.analysis.io import canonical_columns, load_transactions
from finance.db.store import connect, init_schema


def _seed(conn) -> None:
    conn.execute(
        "INSERT INTO sessions (session_id, aspsp_name, aspsp_country, valid_until, created_at)"
        " VALUES ('sess1', 'BNP Paribas', 'FR', '2099-01-01', '2026-01-01')"
    )
    conn.execute(
        "INSERT INTO accounts (account_uid, session_id, iban, name, currency, account_type, raw_json)"
        " VALUES ('acc1', 'sess1', 'FR00', 'Checking', 'EUR', 'CACC', '{}')"
    )

    def tx(tid: str, amount: float, currency: str, memo: str):
        conn.execute(
            "INSERT INTO transactions"
            " (transaction_id, account_uid, booking_date, value_date, amount, currency,"
            "  remittance_info, raw_json, fetched_at)"
            " VALUES (?, 'acc1', ?, ?, ?, ?, ?, '{}', '2026-01-02')",
            (tid, "2026-01-01", "2026-01-01", amount, currency, memo),
        )

    tx("t2", -80.00, "EUR", "PRLV ORANGE TELECOM")  # unenriched, uncategorized
    tx("t3", -15.00, "EUR", "FACTURE CB NETFLIX")  # will be override + merchant-tagged
    tx("t4", -9.99, "USD", "FACTURE CB SOMETHING FOREIGN")  # currency_excluded

    # merchant record for t3 with category_source='curated'
    conn.execute(
        "INSERT INTO merchants (canonical_name, category, category_source, updated_at)"
        " VALUES ('NETFLIX', 'Subscriptions', 'curated', '2026-01-02')"
    )
    conn.execute(
        "INSERT INTO tx_enrichment (tx_id, txn_type, merchant_id, memo_merchant_raw, enriched_at)"
        " SELECT 't3', 'FACTURE', merchant_id, 'NETFLIX', '2026-01-02' FROM merchants WHERE canonical_name='NETFLIX'"
    )
    # tx-level override should beat the merchants.category for t3
    conn.execute(
        "INSERT INTO tx_overrides (tx_id, category, note, created_at)"
        " VALUES ('t3', 'Entertainment', 'reclassified by user', '2026-01-03')"
    )
    conn.commit()


def test_load_transactions_shape_and_columns(tmp_path):
    db = tmp_path / "x.db"
    with connect(db) as conn:
        init_schema(conn)
        _seed(conn)
        df = load_transactions(conn)
    assert list(df.columns) == list(canonical_columns())
    assert len(df) == 3
    assert pd.api.types.is_datetime64_any_dtype(df["booking_date"])


def test_category_resolution_precedence(tmp_path):
    db = tmp_path / "x.db"
    with connect(db) as conn:
        init_schema(conn)
        _seed(conn)
        df = load_transactions(conn).set_index("tx_id")

    # t2: nothing categorized anywhere
    assert pd.isna(df.loc["t2", "category"])
    assert pd.isna(df.loc["t2", "category_source"])

    # t3: tx override wins over merchants.category
    assert df.loc["t3", "category"] == "Entertainment"
    assert df.loc["t3", "category_source"] == "override"
    assert df.loc["t3", "merchant_canonical"] == "NETFLIX"
    assert df.loc["t3", "txn_type"] == "FACTURE"


def test_currency_excluded_flag(tmp_path):
    db = tmp_path / "x.db"
    with connect(db) as conn:
        init_schema(conn)
        _seed(conn)
        df = load_transactions(conn).set_index("tx_id")
    assert bool(df.loc["t4", "currency_excluded"]) is True
    assert bool(df.loc["t2", "currency_excluded"]) is False


def test_filters_since_and_account(tmp_path):
    db = tmp_path / "x.db"
    with connect(db) as conn:
        init_schema(conn)
        _seed(conn)
        # since in the future → empty
        assert len(load_transactions(conn, since="2099-01-01")) == 0
        # unknown account → empty
        assert len(load_transactions(conn, account_uid="nope")) == 0
        # real account → full
        assert len(load_transactions(conn, account_uid="acc1")) == 3


def test_empty_db_returns_all_columns(tmp_path):
    db = tmp_path / "x.db"
    with connect(db) as conn:
        init_schema(conn)
        df = load_transactions(conn)
    assert list(df.columns) == list(canonical_columns())
    assert len(df) == 0


def test_spend_only_drops_transfer_category(tmp_path):
    """Transfer-categorized transactions should be dropped under spend_only."""
    db = tmp_path / "x.db"
    with connect(db) as conn:
        init_schema(conn)
        conn.execute(
            "INSERT INTO sessions (session_id, aspsp_name, aspsp_country, valid_until, created_at)"
            " VALUES ('s1', 'BNP Paribas', 'FR', '2099-01-01', '2026-01-01')"
        )
        conn.execute(
            "INSERT INTO accounts (account_uid, session_id, iban, name, currency, account_type, raw_json)"
            " VALUES ('a1', 's1', 'FR00', 'Checking', 'EUR', 'CACC', '{}')"
        )
        # One Transfer (via merchants.category) + one normal Dining.
        conn.execute(
            "INSERT INTO merchants (merchant_id, canonical_name, category, category_source, updated_at)"
            " VALUES (1, 'FRIEND', 'Transfer', 'rule-stream', '2026-01-01'),"
            "        (2, 'DELIVEROO', 'Dining', 'rule', '2026-01-01')"
        )
        conn.execute(
            "INSERT INTO transactions (transaction_id, account_uid, booking_date, amount, currency,"
            " remittance_info, raw_json, fetched_at)"
            " VALUES ('xfer', 'a1', '2026-04-10', -500.0, 'EUR', 'VIR CPTE A CPTE EMIS', '{}', '2026-01-01'),"
            "        ('food', 'a1', '2026-04-11', -25.0, 'EUR', 'FACTURE DELIVEROO', '{}', '2026-01-01')"
        )
        conn.execute(
            "INSERT INTO tx_enrichment (tx_id, txn_type, merchant_id, enriched_at)"
            " VALUES ('xfer', 'TRANSFER', 1, '2026-01-01'),"
            "        ('food', 'FACTURE',  2, '2026-01-01')"
        )
        conn.commit()

        df_all = load_transactions(conn)
        df_spend = load_transactions(conn, spend_only=True)
        assert len(df_all) == 2
        assert len(df_spend) == 1
        assert df_spend.iloc[0]["tx_id"] == "food"
