"""Tests for merchant bookkeeping: deep_dive, set_category, rename, merge."""

from __future__ import annotations

from datetime import date, timedelta

from finance.analysis.enrich import enrich_transactions
from finance.analysis.merchants import (
    apply_curated_merges,
    deep_dive,
    load_curated_merges,
    merge_merchants,
    rename_canonical,
    set_category,
    top_merchants,
)
from finance.db.store import connect, init_schema


def _seed(conn):
    conn.execute(
        "INSERT INTO sessions (session_id, aspsp_name, aspsp_country, valid_until, created_at)"
        " VALUES ('s1', 'BNP Paribas', 'FR', '2099-01-01', '2026-01-01')"
    )
    conn.execute(
        "INSERT INTO accounts (account_uid, session_id, iban, name, currency, account_type, raw_json)"
        " VALUES ('a1', 's1', 'FR00', 'Checking', 'EUR', 'CACC', '{}')"
    )
    today = date.today()
    for i in range(3):
        bdate = (today - timedelta(days=30 * i)).isoformat()
        memo = "FACTURE CARTE DU 050126 FRANPRIX 5063 PARIS CARTE 0000XXXXXXXX0000"
        conn.execute(
            "INSERT INTO transactions (transaction_id, account_uid, booking_date, amount, currency,"
            " remittance_info, raw_json, fetched_at)"
            " VALUES (?, 'a1', ?, -25.0, 'EUR', ?, '{}', '2026-01-01')",
            (f"fp_{i}", bdate, memo),
        )
    conn.commit()


def test_deep_dive_returns_history(tmp_path):
    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
        _seed(conn)
        enrich_transactions(conn)
        dd = deep_dive(conn, "FRANPRIX")
        assert dd is not None
        assert dd["merchant"] == "FRANPRIX"
        assert dd["count"] == 3
        assert abs(dd["total_spend"] - 75.0) < 0.01
        assert len(dd["aliases"]) >= 1


def test_deep_dive_resolves_alias(tmp_path):
    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
        _seed(conn)
        enrich_transactions(conn)
        # Alias is the raw parsed form before normalization
        raw_alias = "FRANPRIX 5063 PARIS"
        dd = deep_dive(conn, raw_alias)
        assert dd is not None
        assert dd["merchant"] == "FRANPRIX"


def test_deep_dive_missing(tmp_path):
    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
        _seed(conn)
        enrich_transactions(conn)
        assert deep_dive(conn, "NONEXISTENT") is None


def test_set_category_marks_user_source(tmp_path):
    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
        _seed(conn)
        enrich_transactions(conn)
        assert set_category(conn, "FRANPRIX", "Groceries") is True
        row = conn.execute(
            "SELECT category, category_source FROM merchants WHERE canonical_name = 'FRANPRIX'"
        ).fetchone()
        assert row[0] == "Groceries"
        assert row[1] == "user"


def test_rename_preserves_history(tmp_path):
    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
        _seed(conn)
        enrich_transactions(conn)
        assert rename_canonical(conn, "FRANPRIX", "Franprix Épicerie") is True
        # Now deep_dive should find it under the new name
        dd = deep_dive(conn, "FRANPRIX ÉPICERIE")
        assert dd is not None
        assert dd["count"] == 3
        # And the old alias still resolves
        assert deep_dive(conn, "FRANPRIX 5063 PARIS") is not None


def test_merge_repoints_everything(tmp_path):
    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
        _seed(conn)
        enrich_transactions(conn)
        # Create a second merchant to merge from
        conn.execute(
            "INSERT INTO merchants (canonical_name, updated_at) VALUES ('FRANPRIX MONOP', '2026-01-01')"
        )
        mid_src = conn.execute(
            "SELECT merchant_id FROM merchants WHERE canonical_name = 'FRANPRIX MONOP'"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO transactions (transaction_id, account_uid, booking_date, amount, currency,"
            " remittance_info, raw_json, fetched_at)"
            " VALUES ('extra1', 'a1', '2026-03-15', -10.0, 'EUR', 'memo', '{}', '2026-01-01')"
        )
        conn.execute(
            "INSERT INTO tx_enrichment (tx_id, txn_type, merchant_id, enriched_at)"
            " VALUES ('extra1', 'FACTURE', ?, '2026-03-15')",
            (mid_src,),
        )
        conn.commit()

        assert merge_merchants(conn, "FRANPRIX MONOP", "FRANPRIX") is True
        # Source gone
        assert (
            conn.execute(
                "SELECT 1 FROM merchants WHERE canonical_name = 'FRANPRIX MONOP'"
            ).fetchone()
            is None
        )
        # tx_enrichment now points at target
        dd = deep_dive(conn, "FRANPRIX")
        assert dd["count"] >= 4  # 3 original + 1 merged


def test_load_curated_merges_parses_yaml():
    merges = load_curated_merges()
    # Bundled file has at least the ORANGE SA-ORANGE entry
    assert "ORANGE SA-ORANGE" in merges
    assert merges["ORANGE SA-ORANGE"] == "ORANGE"


def test_apply_curated_merges_skips_when_src_missing(tmp_path):
    """Only dst exists → skipped (already canonical, nothing to do)."""
    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
        # Only ORANGE exists — not ORANGE SA-ORANGE
        conn.execute(
            "INSERT INTO merchants (canonical_name, updated_at) VALUES ('ORANGE', '2026-01-01')"
        )
        conn.commit()
        results = apply_curated_merges(conn)
        orange_row = [r for r in results if r[0] == "ORANGE SA-ORANGE"]
        assert orange_row and orange_row[0][2] == "skipped"


def test_apply_curated_merges_merges_when_both_exist(tmp_path):
    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
        conn.execute(
            "INSERT INTO merchants (canonical_name, updated_at) VALUES"
            " ('ORANGE', '2026-01-01'), ('ORANGE SA-ORANGE', '2026-01-01')"
        )
        conn.commit()
        results = apply_curated_merges(conn)
        row = [r for r in results if r[0] == "ORANGE SA-ORANGE"]
        assert row and row[0][2] == "merged"
        assert (
            conn.execute(
                "SELECT 1 FROM merchants WHERE canonical_name = 'ORANGE SA-ORANGE'"
            ).fetchone()
            is None
        )


def test_top_merchants_orders_by_spend(tmp_path):
    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
        _seed(conn)  # FRANPRIX — 3 * €25 = €75
        enrich_transactions(conn)
        # Add a bigger spender
        conn.execute(
            "INSERT INTO transactions (transaction_id, account_uid, booking_date, amount, currency,"
            " remittance_info, raw_json, fetched_at)"
            " VALUES ('big1', 'a1', '2026-04-10', -500.0, 'EUR',"
            " 'FACTURE CARTE DU 100426 BIGSTORE CARTE 0000XXXXXXXX0000', '{}', '2026-01-01')"
        )
        conn.commit()
        enrich_transactions(conn)

        df = top_merchants(conn, limit=10)
        assert not df.empty
        names = list(df["merchant"])
        assert names[0] == "BIGSTORE"
        assert "FRANPRIX" in names
        assert names.index("BIGSTORE") < names.index("FRANPRIX")
        # Column contract
        for col in (
            "merchant_id",
            "merchant",
            "category",
            "category_source",
            "txns",
            "total_spend",
            "first_seen",
            "last_seen",
            "aliases_count",
        ):
            assert col in df.columns


def test_top_merchants_uncategorized_filter(tmp_path):
    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
        _seed(conn)
        enrich_transactions(conn)
        # Pin FRANPRIX as user-set; should be filtered out by --uncategorized
        set_category(conn, "FRANPRIX", "Groceries")

        df_all = top_merchants(conn)
        df_unc = top_merchants(conn, uncategorized_only=True)
        assert "FRANPRIX" in df_all["merchant"].values
        assert "FRANPRIX" not in df_unc["merchant"].values


def test_top_merchants_income_only_merchants_surface(tmp_path):
    """Incoming-only merchants (KENKO refund, AIRBNB earnings) used to show
    total_spend=0 and sink to the bottom. Verify they now surface via
    total_income and are ordered by max(out, in)."""
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
        # SPENDER: €50 outflow.
        conn.execute(
            "INSERT INTO transactions (transaction_id, account_uid, booking_date, amount, currency,"
            " remittance_info, raw_json, fetched_at)"
            " VALUES ('s1', 'a1', '2026-04-10', -50.0, 'EUR',"
            " 'FACTURE CARTE DU 100426 SPENDER CARTE 0000XXXXXXXX0000', '{}', '2026-01-01')"
        )
        # BIGINFLOWER: €500 inflow (like KENKO / AIRBNB in real data).
        conn.execute(
            "INSERT INTO transactions (transaction_id, account_uid, booking_date, amount, currency,"
            " remittance_info, raw_json, fetched_at)"
            " VALUES ('i1', 'a1', '2026-04-15', 500.0, 'EUR',"
            " 'VIR SEPA RECU /DE BIGINFLOWER /MOTIF refund /REF X', '{}', '2026-01-01')"
        )
        conn.commit()
        enrich_transactions(conn)

        df = top_merchants(conn, limit=10)
        assert "total_income" in df.columns
        assert "total_spend" in df.columns
        assert "net_amount" in df.columns

        # Income-only merchant is NOT sorted to the bottom anymore.
        names = list(df["merchant"])
        assert "BIGINFLOWER" in names
        assert "SPENDER" in names
        # BIGINFLOWER has bigger absolute flow → should rank above SPENDER.
        assert names.index("BIGINFLOWER") < names.index("SPENDER")

        biginflower = df[df["merchant"] == "BIGINFLOWER"].iloc[0]
        assert float(biginflower["total_spend"]) == 0.0
        assert float(biginflower["total_income"]) == 500.0
        assert float(biginflower["net_amount"]) == 500.0


def test_top_merchants_spend_only_honors_flag(tmp_path):
    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
        # Two accounts, one flagged
        conn.execute(
            "INSERT INTO sessions (session_id, aspsp_name, aspsp_country, valid_until, created_at)"
            " VALUES ('s1', 'BNP Paribas', 'FR', '2099-01-01', '2026-01-01')"
        )
        conn.execute(
            "INSERT INTO accounts (account_uid, session_id, iban, name, currency, account_type, raw_json, excluded_from_spend)"
            " VALUES ('checking', 's1', 'FR1', 'Checking', 'EUR', 'CACC', '{}', 0),"
            "        ('savings',  's1', 'FR2', 'Savings',  'EUR', 'CACC', '{}', 1)"
        )
        for i, (tid, acc, amt) in enumerate(
            [
                ("c1", "checking", -25.0),
                ("s1", "savings", -100.0),
            ]
        ):
            conn.execute(
                "INSERT INTO transactions (transaction_id, account_uid, booking_date, amount, currency,"
                " remittance_info, raw_json, fetched_at)"
                " VALUES (?, ?, '2026-04-10', ?, 'EUR',"
                " 'FACTURE CARTE DU 100426 MERCHANTX CARTE 0000XXXXXXXX0000', '{}', '2026-01-01')",
                (tid, acc, amt),
            )
        conn.commit()
        enrich_transactions(conn)

        df_all = top_merchants(conn)
        df_spend = top_merchants(conn, spend_only=True)
        # Same single merchant, but spend_only drops the €100 savings outflow
        assert float(df_all[df_all["merchant"] == "MERCHANTX"]["total_spend"].iloc[0]) == 125.0
        assert float(df_spend[df_spend["merchant"] == "MERCHANTX"]["total_spend"].iloc[0]) == 25.0


def test_apply_curated_merges_renames_when_dst_missing(tmp_path):
    """Only src exists → rename (this is the `NOTION LABS,` → `NOTION LABS` case)."""
    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
        conn.execute(
            "INSERT INTO merchants (canonical_name, updated_at) VALUES"
            " ('NOTION LABS,', '2026-01-01')"
        )
        conn.commit()
        results = apply_curated_merges(conn)
        row = [r for r in results if r[0] == "NOTION LABS,"]
        assert row and row[0][2] == "renamed"
        assert (
            conn.execute("SELECT 1 FROM merchants WHERE canonical_name = 'NOTION LABS'").fetchone()
            is not None
        )
