"""Bank profile — assemble per-bank memo-parser dispatch from branches.

A `BankProfile` is a tuple of `_try_*` branch functions plus a
fallback. `parse_memo` walks the tuple and returns the first non-None
result, otherwise calls the fallback. There is no flag check anywhere
— a profile cannot run a branch it doesn't include.

Two profiles are defined:

- `FR_BNP_PROFILE` includes BNP-proprietary prefixes (FACTURE CARTE DU,
  VIR CPTE A CPTE, VIREMENT FAVEUR TIERS, REMBOURST CB DU) plus the
  pan-French-SEPA prefixes (PRLV, VIR, FRAIS, RETRAIT, INTERETS,
  COMMISSIONS) and the EB Mock ASPSP sandbox-fallback regex.
- `FR_GENERIC_PROFILE` includes only the pan-French-SEPA prefixes and
  falls back to ISO-20022 `creditor.name` / `debtor.name`. Suitable for
  any French retail bank delivered via Enable Banking when we don't
  have bank-specific knowledge of card-debit memo formats (Crédit
  Agricole's `PAIEMENT CB`, Société Générale's `ACHAT CB`, etc.).

To add a new bank profile (e.g. Crédit Agricole-aware): write a new
`_try_paiement_cb` branch in `memo.py`, assemble a new
`BankProfile(name="fr_ca", branches=(...,), fallback=...)` here, and
register a name-matcher in `_PROFILE_REGISTRY`. Nothing in
`parse_memo` itself needs to change.

Selection from a session: `from_aspsp_name` walks `_PROFILE_REGISTRY`
and returns the first profile whose matcher fires, or
`FR_GENERIC_PROFILE` if none do.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from finance.analysis.memo import (
    ParsedMemo,
    _fallback_party_only,
    _fallback_with_mock_aspsp,
    _try_commissions,
    _try_facture,
    _try_frais,
    _try_interets,
    _try_prlv,
    _try_rembourst,
    _try_retrait,
    _try_transfer,
    _try_vir,
    _try_virement,
)

ParseBranch = Callable[[str], "ParsedMemo | None"]
FallbackFn = Callable[[str, "str | None"], "ParsedMemo"]


@dataclass(frozen=True)
class BankProfile:
    """Ordered branch tuple + fallback. Order matters where one branch's
    prefix is a substring of another's (FACTURE before VIR, transfer
    before generic VIR — see the per-profile comments below)."""

    name: str
    branches: tuple[ParseBranch, ...]
    fallback: FallbackFn


FR_BNP_PROFILE = BankProfile(
    name="fr_bnp",
    branches=(
        # FACTURE must come first — its memos start with "FACTURE CARTE DU"
        # but the rest of the string can resemble nothing in particular.
        _try_facture,
        _try_prlv,
        # VIR CPTE A CPTE must precede _try_vir — both start with "VIR ".
        _try_transfer,
        _try_vir,
        _try_virement,
        _try_frais,
        _try_retrait,
        _try_interets,
        _try_commissions,
        _try_rembourst,
    ),
    fallback=_fallback_with_mock_aspsp,
)


FR_GENERIC_PROFILE = BankProfile(
    name="fr_generic",
    branches=(
        # Pan-FR-SEPA only. BNP-proprietary branches are intentionally absent
        # — they cannot fire under this profile.
        _try_prlv,
        _try_vir,
        _try_frais,
        _try_retrait,
        _try_interets,
        _try_commissions,
    ),
    fallback=_fallback_party_only,
)


# Ordered list of (name-matcher, profile) pairs. First match wins. Adding
# a new profile is one tuple entry — no edit to from_aspsp_name needed.
_PROFILE_REGISTRY: tuple[tuple[Callable[[str], bool], BankProfile], ...] = (
    (lambda n: "bnp" in n.lower(), FR_BNP_PROFILE),
)


def from_aspsp_name(name: str | None) -> BankProfile:
    if name:
        for matches, profile in _PROFILE_REGISTRY:
            if matches(name):
                return profile
    return FR_GENERIC_PROFILE


def get_account_profile(conn: sqlite3.Connection, account_uid: str) -> BankProfile:
    row = conn.execute(
        """
        SELECT s.aspsp_name
          FROM sessions s
          JOIN accounts a ON a.session_id = s.session_id
         WHERE a.account_uid = ?
        """,
        (account_uid,),
    ).fetchone()
    return from_aspsp_name(row[0] if row else None)
