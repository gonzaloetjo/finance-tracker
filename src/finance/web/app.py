from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from urllib.parse import parse_qs, urlencode

import httpx
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from finance.categorize import Rule
from finance.db import store
from finance.eb.client import EnableBankingClient
from finance.eb.flows import finalize_session, list_aspsps, start_auth
from finance.sync import sync_all_accounts
from finance.web.privacy import mask_iban


@dataclass
class PendingAuth:
    aspsp_name: str
    aspsp_country: str


@dataclass
class AppState:
    client_factory: Callable[[], EnableBankingClient]
    db_path: Path
    callback_url: str
    auth_token: str | None = None
    csrf_token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
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


def _unsafe_method(method: str) -> bool:
    return method.upper() not in {"GET", "HEAD", "OPTIONS", "TRACE"}


def _same_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    if origin:
        return origin == f"{request.url.scheme}://{request.headers.get('host', request.url.netloc)}"
    fetch_site = request.headers.get("sec-fetch-site")
    return fetch_site not in {"cross-site"}


def _authenticated(request: Request, state: AppState) -> bool:
    if state.auth_token is None:
        return True
    if request.cookies.get("finance_auth") == state.auth_token:
        return True
    auth = request.headers.get("authorization", "")
    if auth == f"Bearer {state.auth_token}":
        return True
    return request.query_params.get("token") == state.auth_token


async def _csrf_token_from_request(request: Request) -> str | None:
    header = request.headers.get("x-csrf-token")
    if header:
        return header
    query_token = request.query_params.get("_csrf")
    if query_token:
        return query_token
    content_type = request.headers.get("content-type", "")
    if "application/x-www-form-urlencoded" not in content_type:
        return None
    body = await request.body()
    # Re-inject the consumed body for FastAPI's form parser.
    consumed = False

    async def receive() -> dict[str, object]:
        nonlocal consumed
        if consumed:
            return {"type": "http.request", "body": b"", "more_body": False}
        consumed = True
        return {"type": "http.request", "body": body, "more_body": False}

    request._receive = receive  # type: ignore[attr-defined]
    parsed = parse_qs(body.decode("utf-8", errors="replace"))
    values = parsed.get("_csrf")
    return values[0] if values else None


def _strip_token_from_url(request: Request) -> str:
    params = [
        (key, value) for key, value in request.query_params.multi_items() if key != "token"
    ]
    query = urlencode(params, doseq=True)
    return request.url.path + (f"?{query}" if query else "")


def _set_auth_cookie(response: Response, state: AppState) -> None:
    if state.auth_token is None:
        return
    response.set_cookie(
        "finance_auth",
        state.auth_token,
        httponly=True,
        secure=False,
        samesite="lax",
        max_age=60 * 60 * 12,
    )


def _add_security_headers(response: Response) -> None:
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none'",
    )


def create_app(state: AppState) -> FastAPI:
    app = FastAPI(title="finance", docs_url=None, redoc_url=None)
    templates = Jinja2Templates(directory=_templates_dir())
    app.state.finance = state
    app.state.finance_templates = templates  # exposed to dashboard.py router

    @app.middleware("http")
    async def security_boundary(request: Request, call_next):
        if request.url.path.startswith("/static/"):
            response = await call_next(request)
            _add_security_headers(response)
            return response

        token_login = (
            state.auth_token is not None
            and request.method == "GET"
            and request.query_params.get("token") == state.auth_token
        )
        if token_login:
            response = RedirectResponse(_strip_token_from_url(request), status_code=303)
            _set_auth_cookie(response, state)
            _add_security_headers(response)
            return response

        if not _authenticated(request, state):
            response = HTMLResponse(
                """
                <h1>Dashboard locked</h1>
                <p>Open the local URL printed by <code>finance serve</code>.</p>
                """,
                status_code=401,
            )
            _add_security_headers(response)
            return response

        if _unsafe_method(request.method):
            if not _same_origin(request):
                response = HTMLResponse("Cross-site request rejected", status_code=403)
                _add_security_headers(response)
                return response
            token = await _csrf_token_from_request(request)
            if token != state.csrf_token:
                response = HTMLResponse("CSRF token missing or invalid", status_code=403)
                _add_security_headers(response)
                return response

        response = await call_next(request)
        _add_security_headers(response)
        return response

    @app.get("/static/{asset}")
    async def static_asset(asset: str):
        if asset not in {"app.css", "app.js"}:
            raise HTTPException(404, "static asset not found")
        path = files("finance.web").joinpath("static", asset)
        media_type = "text/css" if asset.endswith(".css") else "application/javascript"
        response = Response(path.read_text(), media_type=media_type)
        _add_security_headers(response)
        return response

    # Mount Phase 8 dashboard router (overview, merchants, recurring, subs,
    # forecast, alerts, advice + HTMX write fragments).
    from finance.web.dashboard import router as dashboard_router

    app.include_router(dashboard_router)

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

        with store.open_db(state.db_path) as conn:
            store.persist_session(conn, session)

        return RedirectResponse("/", status_code=303)

    @app.post("/sync", response_class=HTMLResponse)
    async def sync_now(request: Request):
        with state.client_factory() as client, store.open_db(state.db_path) as conn:
            results = sync_all_accounts(conn, client, rules=state.rules)
        return templates.TemplateResponse(request, "_sync_result.html", {"results": results})

    @app.get("/transactions", response_class=HTMLResponse)
    async def transactions_page(request: Request, since: str | None = None, limit: int = 100):
        from datetime import date as _date
        from datetime import timedelta as _td

        since = since or (_date.today() - _td(days=30)).isoformat()
        with store.open_db(state.db_path) as conn:
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
        with store.open_db(state.db_path) as conn:
            accounts = store.list_accounts(conn)
        accounts = [{**a, "iban_masked": mask_iban(a.get("iban"))} for a in accounts]
        return templates.TemplateResponse(request, "accounts.html", {"accounts": accounts})

    return app
