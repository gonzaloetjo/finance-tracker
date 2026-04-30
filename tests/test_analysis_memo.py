"""Tests for parse_memo against real BNP Particulier memo patterns."""

from __future__ import annotations

import pytest

from finance.analysis.bank_profile import FR_BNP_PROFILE
from finance.analysis.memo import parse_memo

# ---------------------------------------------------------------------------
# FACTURE
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "memo, expected_merchant",
    [
        (
            "FACTURE CARTE DU 110426 FRANPRIX 5063 PARIS 19 CARTE 0000XXXXXXXX0000",
            "FRANPRIX 5063 PARIS 19",
        ),
        (
            "FACTURE CARTE DU 050326 UBER *ONE MEM CARTE 0000XXXXXXXX0000 NLD 5,99EUR",
            "UBER *ONE MEM",
        ),
        ("FACTURE CARTE DU 070326 DELIVEROO CARTE 0000XXXXXXXX0000 FRA 35,94EUR", "DELIVEROO"),
        (
            "FACTURE CARTE DU 250126 APPLE.COM/BILL CARTE 0000XXXXXXXX0000 IRL 11,99EUR",
            "APPLE.COM/BILL",
        ),
        (
            "FACTURE CARTE DU 120226 UBER * EATS P CARTE 0000XXXXXXXX0000 NLD 36,44EUR",
            "UBER * EATS P",
        ),
        ("FACTURE CARTE DU 300126 AYKO PARIS CARTE 0000XXXXXXXX0000", "AYKO PARIS"),
        (
            "FACTURE CARTE DU 030226 PAYPAL *AIRBNB LUXEMBOURG CARTE 0000XXXXXXXX0000",
            "PAYPAL *AIRBNB LUXEMBOURG",
        ),
        (
            "FACTURE CARTE DU 030326 CANAL PLUS FR ISSY LES MOUL CARTE 0000XXXXXXXX0000",
            "CANAL PLUS FR ISSY LES MOUL",
        ),
        (
            "FACTURE CARTE DU 230326 YUTORI CARTE 0000XXXXXXXX0000 USA 15,00USD+COMMISSION : 1,27",
            "YUTORI",
        ),
        (
            "FACTURE CARTE DU 120426 RENELACHANCE.BI CARTE 0000XXXXXXXX0000 FRA 520,00EUR",
            "RENELACHANCE.BI",
        ),
    ],
)
def test_facture_merchant_extraction(memo, expected_merchant):
    result = parse_memo(memo, profile=FR_BNP_PROFILE)
    assert result.txn_type == "FACTURE"
    assert result.merchant_raw == expected_merchant


def test_facture_date_token():
    result = parse_memo(
        "FACTURE CARTE DU 110426 FRANPRIX 5063 PARIS 19 CARTE 0000XXXXXXXX0000",
        profile=FR_BNP_PROFILE,
    )
    assert result.date_token == "110426"


# ---------------------------------------------------------------------------
# PRLV
# ---------------------------------------------------------------------------


def test_prlv_simple():
    memo = "PRLV SEPA ENGIE ECH/090426 ID EMETTEUR/FR03SYM002381 MDT/00S015919519 REF/54379765646900051937003320260407 LIB/MANDAT 00S015919519"
    result = parse_memo(memo, profile=FR_BNP_PROFILE)
    assert result.txn_type == "PRLV"
    assert result.merchant_raw == "ENGIE"
    assert result.date_token == "090426"
    assert result.ref_token is not None


def test_prlv_long_creditor():
    memo = "PRLV SEPA SYNDICAT 104 BOLIVAR 5544 ECH/130426 ID EMETTEUR/FR83ZZZ874B7F MDT/W0202007407235359135080 REF/E2E-69B74CB7B0C042C6C62EACB2 LIB/APPEL PROVISIONS SUR CHARGES 01 04 2026 3 4 1 3 PRELEVEMENT"
    result = parse_memo(memo, profile=FR_BNP_PROFILE)
    assert result.txn_type == "PRLV"
    assert result.merchant_raw == "SYNDICAT 104 BOLIVAR 5544"
    assert result.date_token == "130426"


# ---------------------------------------------------------------------------
# VIR
# ---------------------------------------------------------------------------


def test_vir_sepa_recu():
    memo = "VIR SEPA RECU /DE AIRBNB PAYMENTS LUXEMBOURG SA /MOTIF G-FCSATL3IXWHDH /REF G-FCSATL3IXWHDH"
    result = parse_memo(memo, profile=FR_BNP_PROFILE)
    assert result.txn_type == "VIR"
    assert result.merchant_raw == "AIRBNB PAYMENTS LUXEMBOURG SA"
    assert result.ref_token == "G-FCSATL3IXWHDH"


def test_vir_sepa_inst_recu():
    memo = "VIR SEPA INST RECU /DE ACME CORP /REF 589A016BE9094DBF9AE7504BB4738163 /MOTIF SALAIRE JEAN DUPONT - 01/2026"
    result = parse_memo(memo, profile=FR_BNP_PROFILE)
    assert result.txn_type == "VIR"
    assert result.merchant_raw == "ACME CORP"


def test_vir_cpte_emis():
    """Pre-bug-fix this returned txn_type='VIR'. The new TRANSFER tag is the
    intended behavior — these are internal (account-to-account) movements."""
    memo = "VIR CPTE A CPTE EMIS /MOTIF VIREMENT VERS COMPTE DE CHEQUES /BEN DUPONT LE GRAND /REFDO /REFBEN"
    result = parse_memo(memo, profile=FR_BNP_PROFILE)
    assert result.txn_type == "TRANSFER"
    assert result.merchant_raw == "DUPONT LE GRAND"


def test_vir_ref_notprovided():
    memo = "VIR SEPA INST RECU /DE MELLE DURAND ANNE /REF NOTPROVIDED /MOTIF WE HEIDELBERG"
    result = parse_memo(memo, profile=FR_BNP_PROFILE)
    assert result.txn_type == "VIR"
    assert result.merchant_raw == "MELLE DURAND ANNE"
    assert result.ref_token is None  # NOTPROVIDED → None


# ---------------------------------------------------------------------------
# VIREMENT
# ---------------------------------------------------------------------------


def test_virement():
    memo = "VIREMENT FAVEUR TIERS VR.PERMANENT VIREMENT DE M DUPONT J OU MME LE"
    result = parse_memo(memo, profile=FR_BNP_PROFILE)
    assert result.txn_type == "VIR"
    assert result.merchant_raw == "M DUPONT J"


# ---------------------------------------------------------------------------
# TRANSFER — BNP's explicit account-to-account tag (internal transfers)
# ---------------------------------------------------------------------------


def test_transfer_emis_extracts_beneficiary():
    memo = "VIR CPTE A CPTE EMIS /MOTIF VIREMENT VERS COMPTE DE CHEQUES /BEN DUPONT LE GRAND /REFDO /REFBEN"
    result = parse_memo(memo, profile=FR_BNP_PROFILE)
    assert result.txn_type == "TRANSFER"
    assert result.merchant_raw == "DUPONT LE GRAND"


def test_transfer_recu_extracts_sender():
    memo = (
        "VIR CPTE A CPTE RECU /DE MR DUPONT JEAN PIERRE /MOTIF VIREMENT VERS COMPTE DE CHEQUES /REF"
    )
    result = parse_memo(memo, profile=FR_BNP_PROFILE)
    assert result.txn_type == "TRANSFER"
    assert result.merchant_raw == "MR DUPONT JEAN PIERRE"


def test_paypal_paiemen_placeholder_emits_no_merchant():
    """PayPal's 'PAIEMEN' placeholder means the real merchant name wasn't
    passed through — don't cluster these into a fake merchant."""
    memo = "FACTURE CARTE DU 020426 PAYPAL *PAIEMEN LU LUXEMBOURG CARTE 0000XXXXXXXX0000"
    result = parse_memo(memo, profile=FR_BNP_PROFILE)
    assert result.txn_type == "FACTURE"
    assert result.merchant_raw is None

    # Case-insensitive
    memo_lower = "FACTURE CARTE DU 020426 paypal *paiemen lu LUXEMBOURG CARTE 0000XXXXXXXX0000"
    assert parse_memo(memo_lower, profile=FR_BNP_PROFILE).merchant_raw is None

    # Normal PayPal (with an actual merchant) still extracts the merchant
    memo_google = "FACTURE CARTE DU 130426 PAYPAL *GOOGLE LUXEMBOURG CARTE 0000XXXXXXXX0000"
    assert parse_memo(memo_google, profile=FR_BNP_PROFILE).merchant_raw == "PAYPAL *GOOGLE LUXEMBOURG"


def test_transfer_matches_before_vir():
    """VIR CPTE A CPTE must not fall through to the generic VIR parser."""
    memo = "VIR CPTE A CPTE EMIS /BEN A PERSON"
    result = parse_memo(memo, profile=FR_BNP_PROFILE)
    assert result.txn_type == "TRANSFER"
    assert result.txn_type != "VIR"


# ---------------------------------------------------------------------------
# No-merchant types
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "memo, expected_type",
    [
        ("FRAIS TENUE DE COMPTE", "FRAIS"),
        ("INTERETS  DEBITEURS POUR LA PERIODE DU 01.12 AU 28.02.2026", "INTERETS"),
        (
            "COMMISSIONS PERCUES COTISATION DU 010226 AU 310127 CARTE NO 0000000000000000000",
            "FRAIS",
        ),
        ("RETROCESSION PLAFOND FRAIS CLIENTELE EN SITUATION DE FRAGILITE FINANCIERE", "FRAIS"),
    ],
)
def test_no_merchant_types(memo, expected_type):
    result = parse_memo(memo, profile=FR_BNP_PROFILE)
    assert result.txn_type == expected_type
    assert result.merchant_raw is None


# ---------------------------------------------------------------------------
# RETRAIT
# ---------------------------------------------------------------------------


def test_retrait():
    memo = "RETRAIT DAB 08/04/26 18H12 24071A00 2SF SOCIETE DES SERV PARIS 0000000XXXXXXXX0000"
    result = parse_memo(memo, profile=FR_BNP_PROFILE)
    assert result.txn_type == "RETRAIT"
    assert result.merchant_raw is None
    assert result.date_token == "08/04/26"


# ---------------------------------------------------------------------------
# REMBOURST
# ---------------------------------------------------------------------------


def test_rembourst():
    memo = "REMBOURST CB DU 130226 PAYPAL *GOOGLE LUXEMBOURG CARTE 0000XXXXXXXX0000"
    result = parse_memo(memo, profile=FR_BNP_PROFILE)
    assert result.txn_type == "FACTURE"
    assert result.merchant_raw == "PAYPAL *GOOGLE LUXEMBOURG"
    assert result.date_token == "130226"


# ---------------------------------------------------------------------------
# Mock ASPSP / OTHER
# ---------------------------------------------------------------------------


def test_mock_aspsp():
    result = parse_memo("Akseli Korhonen-DBIT-4.36-nsotg", profile=FR_BNP_PROFILE)
    assert result.txn_type == "OTHER"
    assert result.merchant_raw == "Akseli Korhonen"


def test_empty_and_none():
    assert parse_memo(None, profile=FR_BNP_PROFILE).txn_type == "OTHER"
    assert parse_memo("", profile=FR_BNP_PROFILE).txn_type == "OTHER"
    assert parse_memo("  ", profile=FR_BNP_PROFILE).txn_type == "OTHER"
