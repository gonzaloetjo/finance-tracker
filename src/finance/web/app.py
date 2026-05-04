from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path

import httpx
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from finance.categorize import Rule
from finance.db import store
from finance.eb.client import EnableBankingClient
from finance.eb.flows import finalize_session, list_aspsps, start_auth
from finance.sync import sync_all_accounts


@dataclass
class PendingAuth:
    aspsp_name: str
    aspsp_country: str


@dataclass
class AppState:
    client_factory: Callable[[], EnableBankingClient]
    db_path: Path
    callback_url: str
    rules: list[Rule] = field(default_factory=list)
    pending: dict[str, PendingAuth] = field(default_factory=dict)


def _templates_dir() -> str:
    return str(files("finance.web").joinpath("templates"))


def _explain_eb_error(e: httpx.HTTPStatusError) -> str:
    status = e.response.status_code
    body = e.response.text
    if status == 403 and "not active" in body.lower():
        return (
            "Enable Banking says your application is not active yet. "
            "Go to https://enablebanking.com/ → Control Panel → your application "
            "and complete the activation / self-whitelisting step (they'll ask for your IBAN)."
        )
    if status == 401:
        return (
            "Enable Banking rejected the request as unauthorized. "
            "Check that the app_id in config.toml matches the key you imported."
        )
    return f"Enable Banking returned HTTP {status}: {body}"


def create_app(state: AppState) -> FastAPI:
    app = FastAPI(title="finance", docs_url=None, redoc_url=None)
    templates = Jinja2Templates(directory=_templates_dir())
    app.state.finance = state
    app.state.finance_templates = templates  # exposed to dashboard.py router

    # Mount Phase 8 dashboard router (overview, merchants, recurring, subs,
    # forecast, alerts, advice + HTMX write fragments).
    from finance.web.dashboard import router as dashboard_router

    app.include_router(dashboard_router)

    def db():
        return store.connect(state.db_path)

    @app.get("/connect", response_class=HTMLResponse)
    async def connect_page(request: Request, country: str = "FR"):
        try:
            with state.client_factory() as client:
                aspsps = list_aspsps(client, country=country)
            error = None
        except httpx.HTTPStatusError as e:
            aspsps = []
            error = _explain_eb_error(e)
        return templates.TemplateResponse(
            request, "connect.html", {"aspsps": aspsps, "country": country, "error": error}
        )

    @app.post("/connect")
    async def connect_submit(aspsp_name: str = Form(...), aspsp_country: str = Form(...)):
        auth_state = secrets.token_urlsafe(32)
        with state.client_factory() as client:
            resp = start_auth(
                client,
                aspsp_name=aspsp_name,
                aspsp_country=aspsp_country,
                redirect_url=state.callback_url,
                state=auth_state,
            )
        state.pending[auth_state] = PendingAuth(aspsp_name, aspsp_country)
        return RedirectResponse(resp.url, status_code=303)

    @app.get("/callback", response_class=HTMLResponse)
    async def callback(
        request: Request,
        code: str | None = None,
        error: str | None = None,
    ):
        # Enable Banking's `state` query param name clashes with FastAPI's
        # `app.state` — we can't bind it as a function arg, so read it from
        # `request.query_params` directly.
        actual_state = request.query_params.get("state")
        if error:
            raise HTTPException(400, f"Authorization error: {error}")
        if not code or not actual_state:
            raise HTTPException(400, "Missing code/state in callback")
        if actual_state not in state.pending:
            raise HTTPException(400, "Unknown or expired state parameter")
        state.pending.pop(actual_state)

        with state.client_factory() as client:
            session = finalize_session(client, code=code)

        with db() as conn:
            store.init_schema(conn)
            store.persist_session(conn, session)

        return RedirectResponse("/", status_code=303)

    @app.post("/sync", response_class=HTMLResponse)
    async def sync_now(request: Request):
        with state.client_factory() as client, store.connect(state.db_path) as conn:
            store.init_schema(conn)
            results = sync_all_accounts(conn, client, rules=state.rules)
        return templates.TemplateResponse(request, "_sync_result.html", {"results": results})

    @app.get("/transactions", response_class=HTMLResponse)
    async def transactions_page(request: Request, since: str | None = None, limit: int = 100):
        from datetime import date as _date
        from datetime import timedelta as _td

        since = since or (_date.today() - _td(days=30)).isoformat()
        with db() as conn:
            store.init_schema(conn)
            rows = conn.execute(
                """
                SELECT booking_date, amount, currency, creditor_name, debtor_name, remittance_info
                FROM transactions
                WHERE booking_date >= ?
                ORDER BY booking_date DESC
                LIMIT ?
                """,
                (since, limit),
            ).fetchall()
        return templates.TemplateResponse(
            request, "transactions.html", {"rows": [dict(r) for r in rows], "since": since}
        )

    @app.get("/accounts", response_class=HTMLResponse)
    async def accounts_page(request: Request):
        with db() as conn:
            store.init_schema(conn)
            rows = conn.execute(
                """
                SELECT a.account_uid, a.name, a.iban, a.currency,
                       COALESCE(a.excluded_from_spend, 0) AS excluded,
                       s.aspsp_name, s.aspsp_country, s.valid_until
                FROM accounts a
                JOIN sessions s ON s.session_id = a.session_id
                WHERE s.revoked_at IS NULL
                ORDER BY a.name
                """
            ).fetchall()
            accounts = [dict(r) for r in rows]
        return templates.TemplateResponse(request, "accounts.html", {"accounts": accounts})

    return app
