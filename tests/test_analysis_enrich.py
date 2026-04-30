"""Tests for the enrich orchestrator — idempotency + user override preservation."""

from __future__ import annotations

import re

from finance.analysis.enrich import enrich_transactions
from finance.categorize import Rule
from finance.db.store import connect, init_schema


def _seed_raw(conn, n=5):
    conn.execute(
        "INSERT INTO sessions (session_id, aspsp_name, aspsp_country, valid_until, created_at)"
        " VALUES ('s1', 'BNP Paribas', 'FR', '2099-01-01', '2026-01-01')"
    )
    conn.execute(
        "INSERT INTO accounts (account_uid, session_id, iban, name, currency, account_type, raw_json)"
        " VALUES ('a1', 's1', 'FR00', 'Checking', 'EUR', 'CACC', '{}')"
    )
    memos = [
        (
            "t1",
            -25.0,
            "2026-01-05",
            "FACTURE CARTE DU 050126 FRANPRIX 5063 PARIS 19 CARTE 0000XXXXXXXX0000",
        ),
        (
            "t2",
            -80.0,
            "2026-01-10",
            "PRLV SEPA ENGIE ECH/100126 ID EMETTEUR/FR03SYM002381 MDT/00S015919519 REF/REF1 LIB/MANDAT",
        ),
        (
            "t3",
            -35.0,
            "2026-01-15",
            "FACTURE CARTE DU 150126 DELIVEROO CARTE 0000XXXXXXXX0000 FRA 35,94EUR",
        ),
        (
            "t4",
            2500.0,
            "2026-01-28",
            "VIR SEPA INST RECU /DE ACME CORP /REF ABC /MOTIF SALAIRE JEAN",
        ),
        (
            "t5",
            -15.49,
            "2026-02-05",
            "PRLV SEPA ENGIE ECH/050226 ID EMETTEUR/FR03SYM002381 MDT/00S015919519 REF/REF2 LIB/MANDAT",
        ),
    ]
    for tid, amount, bdate, memo in memos[:n]:
        conn.execute(
            "INSERT INTO transactions (transaction_id, account_uid, booking_date, amount, currency, remittance_info, raw_json, fetched_at)"
            " VALUES (?, 'a1', ?, ?, 'EUR', ?, '{}', '2026-01-01')",
            (tid, bdate, amount, memo),
        )
    conn.commit()


def test_enrich_basic(tmp_path):
    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
        _seed_raw(conn)
        summary = enrich_transactions(conn)
        assert summary.newly_enriched == 5
        assert summary.merchants_created > 0
        assert summary.streams_computed > 0

        enriched = conn.execute("SELECT COUNT(*) FROM tx_enrichment").fetchone()[0]
        assert enriched == 5
        merchants = conn.execute("SELECT COUNT(*) FROM merchants").fetchone()[0]
        assert merchants >= 3  # FRANPRIX, ENGIE, DELIVEROO, ACME CORP


def test_enrich_idempotent_second_run(tmp_path):
    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
        _seed_raw(conn)
        s1 = enrich_transactions(conn)
        s2 = enrich_transactions(conn)
        assert s2.newly_enriched == 0  # nothing new
        assert s2.already_enriched == 5


def test_reenrich_preserves_user_overrides(tmp_path):
    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
        _seed_raw(conn)
        enrich_transactions(conn)

        # User corrects ENGIE's category
        mid = conn.execute(
            "SELECT merchant_id FROM merchants WHERE canonical_name='ENGIE'"
        ).fetchone()[0]
        conn.execute(
            "UPDATE merchants SET category='Utilities (Gas)', category_source='user' WHERE merchant_id=?",
            (mid,),
        )
        conn.commit()

        # Re-enrich with a rule that would classify ENGIE differently
        rules = [Rule(match=re.compile("ENGIE"), category="Energy")]
        enrich_transactions(conn, reenrich=True, rules=rules)

        row = conn.execute(
            "SELECT category, category_source FROM merchants WHERE merchant_id=?", (mid,)
        ).fetchone()
        assert row[0] == "Utilities (Gas)"  # user override preserved
        assert row[1] == "user"


def test_enrich_with_rules(tmp_path):
    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
        _seed_raw(conn)
        rules = [
            Rule(match=re.compile("(?i)franprix"), category="Groceries"),
            Rule(match=re.compile("(?i)deliveroo"), category="Dining"),
        ]
        summary = enrich_transactions(conn, rules=rules)
        assert summary.merchants_classified >= 2

        franprix = conn.execute(
            "SELECT category FROM merchants WHERE canonical_name='FRANPRIX'"
        ).fetchone()
        assert franprix[0] == "Groceries"


def test_classify_from_streams_tags_income(tmp_path):
    """Monthly positive VIR → category=Income, source=rule-stream."""
    from datetime import date, timedelta

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
        for i in range(3):
            bdate = (today - timedelta(days=30 * i)).isoformat()
            memo = f"VIR SEPA INST RECU /DE SOME EMPLOYER /MOTIF SALAIRE /REF R{i}"
            conn.execute(
                "INSERT INTO transactions (transaction_id, account_uid, booking_date, amount, currency,"
                " remittance_info, raw_json, fetched_at)"
                " VALUES (?, 'a1', ?, 3000.00, 'EUR', ?, '{}', '2026-01-01')",
                (f"sal_{i}", bdate, memo),
            )
        conn.commit()
        enrich_transactions(conn)

        row = conn.execute(
            "SELECT category, category_source FROM merchants WHERE canonical_name='SOME EMPLOYER'"
        ).fetchone()
        assert row[0] == "Income"
        assert row[1] == "rule-stream"


def test_classify_from_streams_tags_transfer(tmp_path):
    """VIR CPTE A CPTE → category=Transfer, source=rule-stream."""
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
        memo = (
            "VIR CPTE A CPTE EMIS /MOTIF VIREMENT VERS COMPTE DE CHEQUES /BEN DUPONT LE GRAND /REFDO"
        )
        conn.execute(
            "INSERT INTO transactions (transaction_id, account_uid, booking_date, amount, currency,"
            " remittance_info, raw_json, fetched_at)"
            " VALUES ('t1', 'a1', '2026-04-10', -1750.0, 'EUR', ?, '{}', '2026-01-01')",
            (memo,),
        )
        conn.commit()
        summary = enrich_transactions(conn)
        assert summary.transfer_tagged >= 1

        row = conn.execute(
            "SELECT category, category_source FROM merchants WHERE canonical_name='DUPONT LE GRAND'"
        ).fetchone()
        assert row[0] == "Transfer"
        assert row[1] == "rule-stream"


def test_classify_from_streams_respects_user_override(tmp_path):
    """A user-set category must not be overwritten by the stream classifier."""
    from datetime import date, timedelta

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
        for i in range(3):
            bdate = (today - timedelta(days=30 * i)).isoformat()
            memo = f"VIR SEPA INST RECU /DE BOSS /MOTIF SALARY /REF R{i}"
            conn.execute(
                "INSERT INTO transactions (transaction_id, account_uid, booking_date, amount, currency,"
                " remittance_info, raw_json, fetched_at)"
                " VALUES (?, 'a1', ?, 3000.00, 'EUR', ?, '{}', '2026-01-01')",
                (f"t_{i}", bdate, memo),
            )
        conn.commit()
        enrich_transactions(conn)
        # Pin user override, then reenrich
        conn.execute(
            "UPDATE merchants SET category='Entertainment', category_source='user' WHERE canonical_name='BOSS'"
        )
        conn.commit()
        enrich_transactions(conn, reenrich=True)

        row = conn.execute(
            "SELECT category, category_source FROM merchants WHERE canonical_name='BOSS'"
        ).fetchone()
        assert row[0] == "Entertainment"
        assert row[1] == "user"


def test_tx_overrides_survive_reenrich(tmp_path):
    """tx_overrides.category must take precedence in the load_transactions view even after reenrich."""
    from finance.analysis.io import load_transactions

    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
        _seed_raw(conn, n=1)
        enrich_transactions(conn)

        # Set a tx-level override
        conn.execute(
            "INSERT INTO tx_overrides (tx_id, category, created_at) VALUES ('t1', 'Gifts', '2026-02-01')"
        )
        conn.commit()

        # Re-enrich
        rules = [Rule(match=re.compile("FRANPRIX"), category="Groceries")]
        enrich_transactions(conn, reenrich=True, rules=rules)

        df = load_transactions(conn).set_index("tx_id")
        assert df.loc["t1", "category"] == "Gifts"
        assert df.loc["t1", "category_source"] == "override"
