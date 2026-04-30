from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class AspspSummary(BaseModel):
    """Subset of the /aspsps response we actually use."""

    name: str
    country: str
    bic: str | None = None
    maximum_consent_validity: int | None = None
    psu_types: list[str] = Field(default_factory=list)
    auth_methods: list[dict[str, Any]] = Field(default_factory=list)
    beta: bool = False


class AspspRef(BaseModel):
    name: str
    country: str


class AuthAccess(BaseModel):
    valid_until: str  # ISO 8601


class AuthRequest(BaseModel):
    aspsp: AspspRef
    access: AuthAccess
    redirect_url: str
    state: str | None = None
    psu_type: str = "personal"


class AuthResponse(BaseModel):
    url: str  # tilisy.enablebanking.com/welcome?sessionid=...
    authorization_id: str | None = None


class Account(BaseModel):
    uid: str
    identification_hash: str | None = None
    account_id: dict[str, Any] | None = None  # {iban, other}
    currency: str | None = None
    name: str | None = None
    product: str | None = None
    cash_account_type: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class SessionResponse(BaseModel):
    session_id: str
    accounts: list[Account]
    aspsp: AspspRef
    access: dict[str, Any] = Field(default_factory=dict)
    psu_type: str | None = None


class TransactionsPage(BaseModel):
    transactions: list[dict[str, Any]]
    continuation_key: str | None = None


class Balance(BaseModel):
    balance_amount: dict[str, Any]  # {currency, amount}
    balance_type: str
    reference_date: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class BalancesResponse(BaseModel):
    balances: list[Balance]
