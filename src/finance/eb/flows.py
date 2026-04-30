from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta

from finance.eb.client import EnableBankingClient
from finance.eb.models import (
    AspspRef,
    AspspSummary,
    AuthAccess,
    AuthRequest,
    AuthResponse,
    BalancesResponse,
    SessionResponse,
    TransactionsPage,
)


def list_aspsps(
    client: EnableBankingClient, country: str, service: str = "AIS", psu_type: str = "personal"
) -> list[AspspSummary]:
    params = {"country": country, "service": service, "psu_type": psu_type}
    resp = client.get("/aspsps", params=params)
    raw = resp.json().get("aspsps", [])
    return [AspspSummary.model_validate(a) for a in raw]


def start_auth(
    client: EnableBankingClient,
    aspsp_name: str,
    aspsp_country: str,
    redirect_url: str,
    valid_for_days: int = 180,
    state: str | None = None,
    psu_type: str = "personal",
) -> AuthResponse:
    valid_until = (datetime.now(UTC) + timedelta(days=valid_for_days)).strftime(
        "%Y-%m-%dT%H:%M:%S.000+00:00"
    )
    req = AuthRequest(
        aspsp=AspspRef(name=aspsp_name, country=aspsp_country),
        access=AuthAccess(valid_until=valid_until),
        redirect_url=redirect_url,
        state=state,
        psu_type=psu_type,
    )
    resp = client.post("/auth", json=req.model_dump(exclude_none=True))
    return AuthResponse.model_validate(resp.json())


def finalize_session(client: EnableBankingClient, code: str) -> SessionResponse:
    resp = client.post("/sessions", json={"code": code})
    data = resp.json()
    # Accounts come back verbose — stash the raw dict so we don't lose fields.
    accounts = []
    for a in data.get("accounts", []):
        accounts.append({**a, "raw": a})
    data["accounts"] = accounts
    return SessionResponse.model_validate(data)


def revoke_session(client: EnableBankingClient, session_id: str) -> None:
    client.delete(f"/sessions/{session_id}")


def fetch_balances(client: EnableBankingClient, account_uid: str) -> BalancesResponse:
    resp = client.get(f"/accounts/{account_uid}/balances")
    return BalancesResponse.model_validate(resp.json())


def iter_transactions(
    client: EnableBankingClient,
    account_uid: str,
    date_from: date | None = None,
    date_to: date | None = None,
) -> Iterator[dict]:
    """Yield raw transaction dicts, transparently paginating with continuation_key."""
    params: dict[str, str] = {}
    if date_from:
        params["date_from"] = date_from.isoformat()
    if date_to:
        params["date_to"] = date_to.isoformat()

    seen_keys: set[str] = set()
    while True:
        resp = client.get(f"/accounts/{account_uid}/transactions", params=params)
        page = TransactionsPage.model_validate(resp.json())
        yield from page.transactions
        if not page.continuation_key:
            return
        if page.continuation_key in seen_keys:
            # Defensive: ASPSP replayed the same cursor. Stop rather than loop forever.
            return
        seen_keys.add(page.continuation_key)
        params = {**params, "continuation_key": page.continuation_key}
