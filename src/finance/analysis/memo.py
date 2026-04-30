"""Stage B — parse_memo: extract structured fields from remittance_info.

The parser is a pure dispatcher. It walks `profile.branches` (an ordered
tuple of self-gating branch functions provided by the caller's
`BankProfile`), returns on the first match, otherwise calls
`profile.fallback`.

Each branch function takes a stripped memo string and returns either a
`ParsedMemo` (it recognized its prefix) or `None` (let the next branch
try). Branches are bank-agnostic by themselves; what makes a profile
"BNP" or "generic" is which branches are in its tuple and in what
order. See `finance.analysis.bank_profile` for the assembled profiles.

There is no `is_bnp` flag in this module. Adding a new bank profile
means assembling a new branch tuple and a fallback in `bank_profile.py`;
this file does not need to change.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from finance.analysis.bank_profile import BankProfile

# Card number pattern: CARTE followed by a masked PAN (0000XXXXXXXX0000)
_CARTE_RE = re.compile(r"\s+CARTE\s+\d{4}X{4,}(?:\d{2,4})?")

# PayPal generic-placeholder pattern: "PAYPAL *PAIEMEN LU LUXEMBOURG" means
# "a PayPal payment, no merchant detail was passed through". Every occurrence
# is a different underlying transaction but the memo collapses them into a
# fake merchant. Treat as unknown — emit no merchant_raw.
_PAYPAL_PLACEHOLDER_RE = re.compile(r"PAYPAL\s*\*\s*PAIEMEN", re.IGNORECASE)

# Trailing amount + currency: FRA 35,94EUR or NLD 5,99EUR or 520,00EUR+COMMISSION...
_AMOUNT_TAIL_RE = re.compile(r"\s+(?:[A-Z]{2,3}\s+)?\d+[.,]\d{2}\s*(?:EUR|USD|GBP|CHF)\S*$")

# DDMMYY date token (as in "FACTURE CARTE DU 110426")
_DDMMYY_RE = re.compile(r"\b(\d{6})\b")

# DD/MM/YY date token (as in "RETRAIT DAB 08/04/26")
_DDMMYY_SLASH_RE = re.compile(r"\b(\d{2}/\d{2}/\d{2})\b")

# EB Mock ASPSP sandbox memo: "Akseli Korhonen-DBIT-4.36-nsotg"
_MOCK_ASPSP_RE = re.compile(r"^(.+?)-(?:DBIT|CRDT)-[\d.]+-.+$")


@dataclass(frozen=True)
class ParsedMemo:
    txn_type: str
    merchant_raw: str | None
    date_token: str | None
    ref_token: str | None


def parse_memo(
    memo_raw: str | None,
    *,
    profile: BankProfile,
    creditor_name: str | None = None,
    debtor_name: str | None = None,
) -> ParsedMemo:
    """Parse an EB-delivered `remittance_info` string into structured fields.

    Callers must pass an explicit `profile` (typically resolved per-account
    via `bank_profile.get_account_profile`). `creditor_name` / `debtor_name`
    are used as a merchant fallback when no branch matches (typical for
    non-BNP card debits).
    """
    party_fallback = creditor_name or debtor_name

    if not memo_raw or not memo_raw.strip():
        return ParsedMemo(
            txn_type="OTHER", merchant_raw=party_fallback, date_token=None, ref_token=None
        )

    memo = memo_raw.strip()
    for branch in profile.branches:
        result = branch(memo)
        if result is not None:
            return result
    return profile.fallback(memo, party_fallback)


# ─────────────────────────────────────────────────────────────────────────────
# Branch functions — each one is self-gating: it inspects the memo prefix and
# returns either a ParsedMemo (it claimed the memo) or None (the next branch
# in the profile's tuple should try). Profiles assemble these in the order
# they want to evaluate; ordering matters where one prefix is a substring of
# another (FACTURE before VIR — see bank_profile.py docstring on each tuple).
# ─────────────────────────────────────────────────────────────────────────────


def _try_facture(memo: str) -> ParsedMemo | None:
    """FACTURE CARTE DU DDMMYY <MERCHANT...> CARTE 0000XXXXXXXX####..."""
    if not memo.startswith("FACTURE"):
        return None
    date_token = None
    m = _DDMMYY_RE.search(memo)
    if m:
        date_token = m.group(1)

    # PayPal generic placeholder ("PAYPAL *PAIEMEN LU ...") — no merchant info.
    # Don't cluster these; they're different real merchants collapsed to one
    # opaque memo.
    if _PAYPAL_PLACEHOLDER_RE.search(memo):
        return ParsedMemo(
            txn_type="FACTURE", merchant_raw=None, date_token=date_token, ref_token=None
        )

    body = re.sub(r"^FACTURE\s+CARTE\s+DU\s+\d{6}\s+", "", memo)
    body = _CARTE_RE.split(body)[0]
    body = _AMOUNT_TAIL_RE.sub("", body)
    merchant = body.strip() or None
    return ParsedMemo(
        txn_type="FACTURE", merchant_raw=merchant, date_token=date_token, ref_token=None
    )


def _try_prlv(memo: str) -> ParsedMemo | None:
    """PRLV SEPA <CREDITOR> ECH/DDMMYY ID EMETTEUR/... MDT/... REF/... LIB/..."""
    if not memo.startswith("PRLV"):
        return None
    date_token = None
    ref_token = None

    body = re.sub(r"^PRLV\s+SEPA\s+", "", memo)
    parts = body.split(" ECH/", 1)
    creditor = parts[0].strip() if parts else None

    if len(parts) > 1:
        rest = parts[1]
        ech_m = re.match(r"(\d{6})", rest)
        if ech_m:
            date_token = ech_m.group(1)
        ref_m = re.search(r"REF/(\S+)", rest)
        if ref_m:
            ref_token = ref_m.group(1)

    return ParsedMemo(
        txn_type="PRLV", merchant_raw=creditor or None, date_token=date_token, ref_token=ref_token
    )


def _try_transfer(memo: str) -> ParsedMemo | None:
    """VIR CPTE A CPTE EMIS /MOTIF ... /BEN <BENEFICIARY> /REFDO ...
    or VIR CPTE A CPTE RECU /DE <SENDER> /MOTIF ... /REF ...

    BNP's explicit "compte à compte" tag — an internal transfer between
    accounts at the same bank (including between the user's own accounts).
    Treated distinctly from generic VIR so the classifier can tag these as
    Transfer and drop them from spend totals. Profiles that include this
    branch must place it before `_try_vir` (otherwise the generic VIR
    branch would eat it).
    """
    if not (memo.startswith("VIR CPTE A CPTE") or memo.startswith("VIR CPT A CPT")):
        return None
    fields: dict[str, str] = {}
    for m in re.finditer(r"/(\w+)\s+(.*?)(?=\s+/\w+|$)", memo):
        fields[m.group(1).upper()] = m.group(2).strip()

    party = fields.get("DE") or fields.get("BEN")
    ref_token = fields.get("REF") or fields.get("REFDO")
    if ref_token in ("", "NOTPROVIDED"):
        ref_token = None

    return ParsedMemo(
        txn_type="TRANSFER", merchant_raw=party or None, date_token=None, ref_token=ref_token
    )


def _try_vir(memo: str) -> ParsedMemo | None:
    """VIR SEPA [INST] RECU /DE <SENDER> /MOTIF ... /REF ...
    or VIR ... EMIS /MOTIF ... /BEN <BENEFICIARY> /REFDO ..."""
    if not (memo.startswith("VIR ") or memo.startswith("VIR\t")):
        return None
    ref_token = None
    party = None

    fields: dict[str, str] = {}
    for m in re.finditer(r"/(\w+)\s+(.*?)(?=\s+/\w+|$)", memo):
        fields[m.group(1).upper()] = m.group(2).strip()

    if "DE" in fields:
        party = fields["DE"]
    elif "BEN" in fields:
        party = fields["BEN"]

    ref_token = fields.get("REF") or fields.get("REFDO")
    if ref_token in ("", "NOTPROVIDED"):
        ref_token = None

    return ParsedMemo(
        txn_type="VIR", merchant_raw=party or None, date_token=None, ref_token=ref_token
    )


def _try_virement(memo: str) -> ParsedMemo | None:
    """VIREMENT FAVEUR TIERS VR.PERMANENT VIREMENT DE <SENDER>"""
    if not memo.startswith("VIREMENT"):
        return None
    m = re.search(r"VIREMENT\s+DE\s+(.+?)(?:\s+OU\s+|$)", memo)
    party = m.group(1).strip() if m else None
    return ParsedMemo(txn_type="VIR", merchant_raw=party or None, date_token=None, ref_token=None)


def _try_frais(memo: str) -> ParsedMemo | None:
    if not memo.startswith("FRAIS"):
        return None
    return ParsedMemo(txn_type="FRAIS", merchant_raw=None, date_token=None, ref_token=None)


def _try_retrait(memo: str) -> ParsedMemo | None:
    """RETRAIT DAB DD/MM/YY HH:MM <ATM_ID> <LOCATION> <CARD>"""
    if not memo.startswith("RETRAIT"):
        return None
    date_token = None
    m = _DDMMYY_SLASH_RE.search(memo)
    if m:
        date_token = m.group(1)
    return ParsedMemo(txn_type="RETRAIT", merchant_raw=None, date_token=date_token, ref_token=None)


def _try_interets(memo: str) -> ParsedMemo | None:
    if not memo.startswith("INTERETS"):
        return None
    return ParsedMemo(txn_type="INTERETS", merchant_raw=None, date_token=None, ref_token=None)


def _try_commissions(memo: str) -> ParsedMemo | None:
    if not (memo.startswith("COMMISSIONS") or memo.startswith("RETROCESSION")):
        return None
    return ParsedMemo(txn_type="FRAIS", merchant_raw=None, date_token=None, ref_token=None)


def _try_rembourst(memo: str) -> ParsedMemo | None:
    """REMBOURST CB DU DDMMYY <MERCHANT> <LOCATION> CARTE ..."""
    if not memo.startswith("REMBOURST"):
        return None
    date_token = None
    m = _DDMMYY_RE.search(memo)
    if m:
        date_token = m.group(1)

    body = re.sub(r"^REMBOURST\s+CB\s+DU\s+\d{6}\s+", "", memo)
    body = _CARTE_RE.split(body)[0]
    body = _AMOUNT_TAIL_RE.sub("", body)
    merchant = body.strip() or None
    return ParsedMemo(
        txn_type="FACTURE", merchant_raw=merchant, date_token=date_token, ref_token=None
    )


# ─────────────────────────────────────────────────────────────────────────────
# Fallbacks — called when no branch matched. A profile picks one.
# ─────────────────────────────────────────────────────────────────────────────


def _fallback_party_only(memo: str, party_fallback: str | None) -> ParsedMemo:
    """ISO-20022 creditor/debtor party name as merchant, txn_type=OTHER.

    For non-BNP profiles whose card-debit memo prefix isn't in our
    branch list — we don't synthesize a merchant from the memo, but EB
    populates `creditor.name` / `debtor.name` independently from the
    party fields, so we can still resolve a merchant from there.
    """
    return ParsedMemo(
        txn_type="OTHER", merchant_raw=party_fallback, date_token=None, ref_token=None
    )


def _fallback_with_mock_aspsp(memo: str, party_fallback: str | None) -> ParsedMemo:
    """BNP-profile fallback. Try the EB Mock ASPSP sandbox format first
    (`Name-DBIT/CRDT-amount-id`), then the party-name fallback.

    The Mock ASPSP regex must NOT fire for non-BNP profiles because real
    non-BNP memos can coincidentally match the shape and produce a
    spurious merchant.
    """
    m = _MOCK_ASPSP_RE.match(memo)
    if m:
        return ParsedMemo(
            txn_type="OTHER",
            merchant_raw=m.group(1).strip(),
            date_token=None,
            ref_token=None,
        )
    return _fallback_party_only(memo, party_fallback)
