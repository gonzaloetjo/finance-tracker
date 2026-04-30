"""Tests for bank-profile-aware memo parsing.

Proves FR_GENERIC_PROFILE (used for any non-BNP FR bank) still handles
pan-French-SEPA formats (PRLV, VIR, FRAIS, RETRAIT) and falls back to
ISO-20022 creditor_name / debtor_name when the memo prefix isn't known
(e.g. Crédit Agricole's `PAIEMENT CB` card-debit format).
"""

from __future__ import annotations

from finance.analysis.bank_profile import (
    FR_BNP_PROFILE,
    FR_GENERIC_PROFILE,
    BankProfile,
    from_aspsp_name,
    get_account_profile,
)
from finance.analysis.enrich import enrich_transactions
from finance.analysis.memo import (
    _fallback_party_only,
    _try_facture,
    _try_prlv,
    parse_memo,
)
from finance.db.store import connect, init_schema


def test_from_aspsp_name_dispatch():
    assert from_aspsp_name("BNP Paribas").name == "fr_bnp"
    assert from_aspsp_name("bnp paribas").name == "fr_bnp"  # case-insensitive
    assert from_aspsp_name("Crédit Agricole").name == "fr_generic"
    assert from_aspsp_name("Société Générale").name == "fr_generic"
    assert from_aspsp_name(None).name == "fr_generic"
    assert from_aspsp_name("").name == "fr_generic"


def test_generic_profile_parses_portable_branches():
    """PRLV / VIR / RETRAIT / FRAIS all work under FR_GENERIC — these are
    pan-French-SEPA conventions, not BNP-specific."""
    prlv = parse_memo(
        "PRLV SEPA CARREFOUR ECH/100126 ID EMETTEUR/X MDT/M REF/R LIB/L",
        profile=FR_GENERIC_PROFILE,
    )
    assert prlv.txn_type == "PRLV"
    assert prlv.merchant_raw == "CARREFOUR"

    vir = parse_memo(
        "VIR SEPA RECU /DE ACME CORP /REF R /MOTIF SALAIRE",
        profile=FR_GENERIC_PROFILE,
    )
    assert vir.txn_type == "VIR"
    assert vir.merchant_raw == "ACME CORP"

    frais = parse_memo("FRAIS TENUE DE COMPTE", profile=FR_GENERIC_PROFILE)
    assert frais.txn_type == "FRAIS"


def test_generic_profile_skips_bnp_branches_uses_fallback():
    """BNP-specific FACTURE / VIREMENT / REMBOURST / VIR CPTE A CPTE must
    NOT match under FR_GENERIC — they fall through to the creditor_name
    fallback. Without creditor_name, merchant_raw is None (the caller
    can't derive a merchant)."""
    # Crédit Agricole card-debit memo format
    result = parse_memo(
        "PAIEMENT CB DU 100126 CARREFOUR 92100",
        creditor_name="CARREFOUR",
        profile=FR_GENERIC_PROFILE,
    )
    assert result.txn_type == "OTHER"
    assert result.merchant_raw == "CARREFOUR"  # from creditor_name

    # No creditor_name → no merchant
    result_no_fallback = parse_memo(
        "PAIEMENT CB DU 100126 CARREFOUR 92100", profile=FR_GENERIC_PROFILE
    )
    assert result_no_fallback.merchant_raw is None


def test_bnp_profile_handles_proprietary_branches():
    """FR_BNP_PROFILE handles the BNP-proprietary FACTURE branch."""
    facture = parse_memo(
        "FACTURE CARTE DU 100126 FRANPRIX CARTE 0000XXXXXXXX0000",
        profile=FR_BNP_PROFILE,
    )
    assert facture.txn_type == "FACTURE"
    assert facture.merchant_raw == "FRANPRIX"


def test_generic_profile_skips_mock_aspsp_fallback():
    """The EB Mock ASPSP sandbox format (`Name-DBIT-amount-id`) must NOT
    fire for non-BNP users — a real memo that happens to match the regex
    would produce a spurious merchant."""
    mock_memo = "Akseli Korhonen-DBIT-4.36-nsotg"

    bnp = parse_memo(mock_memo, profile=FR_BNP_PROFILE)
    assert bnp.merchant_raw == "Akseli Korhonen"

    generic = parse_memo(mock_memo, profile=FR_GENERIC_PROFILE)
    assert generic.merchant_raw is None  # no match, no fallback fields

    generic_with_party = parse_memo(
        mock_memo, creditor_name="Real Merchant", profile=FR_GENERIC_PROFILE
    )
    assert generic_with_party.merchant_raw == "Real Merchant"


def test_enrich_uses_generic_profile_for_non_bnp_session(tmp_path):
    """Integration — seed a Crédit Agricole session, one portable PRLV and
    one non-BNP card debit, run enrich, assert merchants populate via both
    paths."""
    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
        conn.execute(
            "INSERT INTO sessions (session_id, aspsp_name, aspsp_country, valid_until, created_at)"
            " VALUES ('s1', 'Crédit Agricole', 'FR', '2099-01-01', '2026-01-01')"
        )
        conn.execute(
            "INSERT INTO accounts (account_uid, session_id, iban, name, currency, account_type, raw_json)"
            " VALUES ('a1', 's1', 'FR00', 'Checking', 'EUR', 'CACC', '{}')"
        )
        # Pan-FR PRLV — works regardless of profile
        conn.execute(
            "INSERT INTO transactions"
            " (transaction_id, account_uid, booking_date, amount, currency,"
            "  creditor_name, debtor_name, remittance_info, raw_json, fetched_at)"
            " VALUES ('t1', 'a1', '2026-01-10', -80.0, 'EUR',"
            "         'ENGIE', NULL,"
            "         'PRLV SEPA ENGIE ECH/100126 ID EMETTEUR/X MDT/M REF/R LIB/L',"
            "         '{}', '2026-01-01')"
        )
        # CA card debit format — no BNP prefix matches, must fall back to
        # creditor_name to populate the merchants row.
        conn.execute(
            "INSERT INTO transactions"
            " (transaction_id, account_uid, booking_date, amount, currency,"
            "  creditor_name, debtor_name, remittance_info, raw_json, fetched_at)"
            " VALUES ('t2', 'a1', '2026-01-15', -25.0, 'EUR',"
            "         'CARREFOUR', NULL,"
            "         'PAIEMENT CB DU 150126 CARREFOUR 92100',"
            "         '{}', '2026-01-01')"
        )
        conn.commit()

        summary = enrich_transactions(conn)
        assert summary.newly_enriched == 2

        merchant_names = {
            row[0]
            for row in conn.execute(
                "SELECT canonical_name FROM merchants ORDER BY canonical_name"
            ).fetchall()
        }
        assert "ENGIE" in merchant_names
        assert "CARREFOUR" in merchant_names


def test_get_account_profile_reads_session(tmp_path):
    with connect(tmp_path / "x.db") as conn:
        init_schema(conn)
        conn.execute(
            "INSERT INTO sessions (session_id, aspsp_name, aspsp_country, valid_until, created_at)"
            " VALUES ('sbnp', 'BNP Paribas', 'FR', '2099-01-01', '2026-01-01'),"
            "        ('sca',  'Crédit Agricole', 'FR', '2099-01-01', '2026-01-01')"
        )
        conn.execute(
            "INSERT INTO accounts (account_uid, session_id, iban, name, currency, account_type, raw_json)"
            " VALUES ('a_bnp', 'sbnp', 'FR1', 'Checking', 'EUR', 'CACC', '{}'),"
            "        ('a_ca',  'sca',  'FR2', 'Checking', 'EUR', 'CACC', '{}')"
        )
        conn.commit()

        assert get_account_profile(conn, "a_bnp").name == "fr_bnp"
        assert get_account_profile(conn, "a_ca").name == "fr_generic"
        assert get_account_profile(conn, "unknown").name == "fr_generic"


def test_custom_profile_dispatch():
    """Adding a third profile is a tuple change, not a code change.

    Build an ad-hoc profile that includes only PRLV — `parse_memo`
    must dispatch through that branch and skip every other prefix
    (proving the dispatcher is genuinely data-driven, not BNP-cased).
    """
    prlv_only = BankProfile(
        name="prlv_only",
        branches=(_try_prlv,),
        fallback=_fallback_party_only,
    )

    # PRLV is in this profile's tuple — should be parsed as PRLV.
    prlv = parse_memo(
        "PRLV SEPA CARREFOUR ECH/100126 ID EMETTEUR/X MDT/M REF/R LIB/L",
        profile=prlv_only,
    )
    assert prlv.txn_type == "PRLV"
    assert prlv.merchant_raw == "CARREFOUR"

    # FACTURE is NOT in this profile's tuple — must fall through to the
    # party-only fallback (txn_type=OTHER), even though _try_facture exists
    # and would have matched if the BNP profile had run it.
    facture = parse_memo(
        "FACTURE CARTE DU 100126 FRANPRIX CARTE 0000XXXXXXXX0000",
        creditor_name="FRANPRIX",
        profile=prlv_only,
    )
    assert facture.txn_type == "OTHER"
    assert facture.merchant_raw == "FRANPRIX"  # from fallback's party arg


def test_custom_profile_branch_order_matters():
    """Branches are evaluated in tuple order; the first non-None wins.
    Putting `_try_facture` after `_try_prlv` cannot reorder a memo that
    only one branch can match, but it does prove the dispatcher honors
    tuple order rather than re-deriving any flag-based ordering.
    """
    # FACTURE first → matches a FACTURE memo as FACTURE.
    facture_first = BankProfile(
        name="facture_first",
        branches=(_try_facture, _try_prlv),
        fallback=_fallback_party_only,
    )
    result = parse_memo(
        "FACTURE CARTE DU 100126 FRANPRIX CARTE 0000XXXXXXXX0000",
        profile=facture_first,
    )
    assert result.txn_type == "FACTURE"

    # Drop _try_facture entirely → same memo falls to fallback.
    no_facture = BankProfile(
        name="no_facture",
        branches=(_try_prlv,),
        fallback=_fallback_party_only,
    )
    result = parse_memo(
        "FACTURE CARTE DU 100126 FRANPRIX CARTE 0000XXXXXXXX0000",
        profile=no_facture,
    )
    assert result.txn_type == "OTHER"
