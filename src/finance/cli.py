from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC
from pathlib import Path

import typer

from finance.auth.keys import (
    KeyPaths,
    encrypt_and_store,
    generate_keypair,
    load_private_key,
    write_keys,
)
from finance.categorize import DEFAULT_RULES_YAML, load_rules
from finance.config import AppConfig, get_settings, load_config, save_config
from finance.db import store
from finance.eb.client import EnableBankingClient
from finance.eb.flows import list_aspsps
from finance.sync import sync_all_accounts
from finance.web.app import AppState, create_app

app = typer.Typer(
    add_completion=False,
    help="Personal finance — French bank accounts via Enable Banking",
)
config_app = typer.Typer(help="Manage config.toml")
app.add_typer(config_app, name="config")


def _passphrase_prompt(settings) -> str:
    if settings.key_passphrase:
        return settings.key_passphrase
    pw = typer.prompt(
        "Passphrase to encrypt private key", hide_input=True, confirmation_prompt=True
    )
    if not pw:
        typer.echo("Passphrase cannot be empty", err=True)
        raise typer.Exit(code=1)
    return pw


def _decrypt_passphrase(settings, action: str) -> str:
    if settings.key_passphrase:
        return settings.key_passphrase
    return typer.prompt(f"Passphrase to decrypt private key ({action})", hide_input=True)


def _load_client(settings, cfg: AppConfig) -> EnableBankingClient:
    if not cfg.app_id:
        typer.echo("app_id not set — run 'finance config set-app-id <id>' first", err=True)
        raise typer.Exit(code=1)
    if not settings.private_key_path.exists():
        typer.echo(
            "Private key missing — run 'finance import-key <path>' first "
            "(or 'finance init' if self-generating)",
            err=True,
        )
        raise typer.Exit(code=1)
    passphrase = _decrypt_passphrase(settings, "sign JWT")
    private_pem = load_private_key(settings.private_key_path, passphrase)
    return EnableBankingClient(app_id=cfg.app_id, private_key_pem=private_pem)


@app.command()
def init(force: bool = typer.Option(False, "--force", help="Overwrite existing keys")) -> None:
    """Generate RSA keypair + self-signed cert. Encrypts private key with age passphrase."""
    settings = get_settings()
    paths = KeyPaths(
        private_key_age=settings.private_key_path, public_cert=settings.public_cert_path
    )

    if paths.private_key_age.exists() and not force:
        typer.echo(f"Refusing to overwrite {paths.private_key_age} (use --force)", err=True)
        raise typer.Exit(code=1)

    passphrase = _passphrase_prompt(settings)
    typer.echo("Generating 4096-bit RSA keypair…")
    private_pem, cert_pem = generate_keypair()
    write_keys(paths, private_pem, cert_pem, passphrase)

    # Ensure a config file exists with defaults
    cfg = load_config(settings)
    save_config(settings, cfg)

    typer.echo(f"\nPrivate key (age-encrypted): {paths.private_key_age}")
    typer.echo(f"Public cert:                 {paths.public_cert}")
    typer.echo(f"Config file:                 {settings.config_file}")
    typer.echo("\nNext steps:")
    typer.echo("  1. Sign in at https://enablebanking.com/ (sandbox control panel).")
    typer.echo(f"  2. Create an application and upload: {paths.public_cert}")
    typer.echo("  3. Copy the returned app_id, then run:")
    typer.echo("       uv run finance config set-app-id <APP_ID>")
    typer.echo("  4. Smoke test:  uv run finance aspsps --country FR")


@config_app.command("show")
def config_show() -> None:
    settings = get_settings()
    cfg = load_config(settings)
    typer.echo(f"config_file:  {settings.config_file}")
    typer.echo(f"app_id:       {cfg.app_id or '(unset)'}")
    typer.echo(f"callback_url: {cfg.callback_url}")
    typer.echo(f"db_path:      {settings.db_path}")


@config_app.command("set-app-id")
def config_set_app_id(app_id: str) -> None:
    settings = get_settings()
    cfg = load_config(settings)
    cfg.app_id = app_id
    save_config(settings, cfg)
    typer.echo(f"app_id set in {settings.config_file}")


UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


@app.command("import-key")
def import_key(
    pem_path: Path = typer.Argument(..., exists=True, readable=True, resolve_path=True),
    app_id: str = typer.Option(
        None, "--app-id", help="Override app_id (default: infer from filename)"
    ),
    delete_plaintext: bool = typer.Option(
        True, "--delete/--keep", help="Shred the plaintext PEM after encrypting"
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite existing encrypted key"),
) -> None:
    """Import the private key PEM you downloaded from the Enable Banking control panel.

    Enable Banking's default registration flow generates the keypair in your browser
    and downloads the private key as `<app_id>.pem`. This command encrypts it at
    rest with age and records the app_id in config.toml.
    """
    settings = get_settings()

    if app_id is None:
        match = UUID_RE.search(pem_path.stem)
        if not match:
            typer.echo(
                f"Could not infer app_id from filename '{pem_path.name}'. Pass --app-id.",
                err=True,
            )
            raise typer.Exit(code=1)
        app_id = match.group(0)

    if settings.private_key_path.exists() and not force:
        typer.echo(f"Refusing to overwrite {settings.private_key_path} (use --force)", err=True)
        raise typer.Exit(code=1)

    pem_bytes = pem_path.read_bytes()
    passphrase = _passphrase_prompt(settings)
    encrypt_and_store(settings.private_key_path, pem_bytes, passphrase)

    cfg = load_config(settings)
    cfg.app_id = app_id
    save_config(settings, cfg)

    # Remove any stale files from the earlier `finance init` flow (they belong to
    # a different keypair that Enable Banking doesn't know about).
    stale = settings.public_cert_path
    if stale.exists():
        stale.unlink()

    typer.echo(f"Imported key for app_id: {app_id}")
    typer.echo(f"  encrypted key: {settings.private_key_path}")
    typer.echo(f"  config:        {settings.config_file}")

    if delete_plaintext:
        # Overwrite then unlink — best-effort on ext4; not a true secure erase
        # on SSDs but better than nothing for a plaintext key.
        try:
            pem_path.write_bytes(b"\x00" * len(pem_bytes))
        finally:
            pem_path.unlink(missing_ok=True)
        typer.echo(f"  deleted:       {pem_path}")
    else:
        typer.echo(f"\n⚠  Plaintext key still at {pem_path} — delete it yourself when done.")

    typer.echo("\nNext: uv run finance aspsps --country FR")


@app.command()
def aspsps(
    country: str = typer.Option("FR", "--country", "-c", help="ISO country code"),
    service: str = typer.Option("AIS", "--service", help="AIS or PIS"),
    psu_type: str = typer.Option("personal", "--psu-type", help="personal or business"),
) -> None:
    """List ASPSPs (banks) available via Enable Banking for the given country."""
    settings = get_settings()
    cfg = load_config(settings)
    with _load_client(settings, cfg) as client:
        items = list_aspsps(client, country=country, service=service, psu_type=psu_type)
    if not items:
        typer.echo(f"No ASPSPs found for country={country} service={service} psu_type={psu_type}")
        raise typer.Exit(code=1)
    for a in items:
        days = (a.maximum_consent_validity or 0) // 86400
        beta = " [beta]" if a.beta else ""
        typer.echo(f"  {a.name:<40} {a.country}  consent≤{days:>3}d{beta}")
    typer.echo(f"\n{len(items)} ASPSP(s)")


@app.command()
def sync(
    cold_start_days: int = typer.Option(
        90, "--cold-start-days", help="How far back to go on first sync"
    ),
) -> None:
    """Fetch new transactions from Enable Banking for all active accounts."""
    settings = get_settings()
    cfg = load_config(settings)
    rules = load_rules(settings.rules_path)
    with _load_client(settings, cfg) as client, _open_db() as conn, conn:
        results = sync_all_accounts(conn, client, cold_start_days=cold_start_days, rules=rules)

    if not results:
        typer.echo("No accounts — run 'finance serve' and complete a consent flow first")
        raise typer.Exit(code=1)
    total = 0
    for r in results:
        if r.status == "ok":
            typer.echo(f"  {r.account_uid[:8]}…  +{r.added} new  ({r.fetched} fetched)")
            total += r.added
        else:
            typer.echo(f"  {r.account_uid[:8]}…  ERROR: {r.error}", err=True)
    typer.echo(f"\n{total} new transaction(s)")
    if any(r.status == "error" for r in results):
        raise typer.Exit(code=1)


@app.command("list")
def list_transactions(
    since: str = typer.Option(None, "--since", help="YYYY-MM-DD (default: 30 days ago)"),
    limit: int = typer.Option(50, "--limit", "-n"),
    account_uid: str = typer.Option(None, "--account-uid"),
) -> None:
    """Print recent transactions from the local SQLite store."""
    from datetime import date as _date
    from datetime import timedelta as _td

    if since is None:
        since = (_date.today() - _td(days=30)).isoformat()

    with _open_db() as conn:
        q = "SELECT booking_date, amount, currency, creditor_name, debtor_name, remittance_info FROM transactions WHERE booking_date >= ?"
        params: list = [since]
        if account_uid:
            q += " AND account_uid = ?"
            params.append(account_uid)
        q += " ORDER BY booking_date DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(q, params).fetchall()

    if not rows:
        typer.echo(f"No transactions since {since}")
        return
    for r in rows:
        party = r["creditor_name"] or r["debtor_name"] or ""
        memo = (r["remittance_info"] or "")[:60]
        typer.echo(
            f"  {r['booking_date']}  {r['amount']:>10.2f} {r['currency']}  {party:<28}  {memo}"
        )


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8000, "--port"),
    tls: bool = typer.Option(
        False, "--tls", help="Serve over HTTPS (self-signed cert for localhost)"
    ),
) -> None:
    """Start the local FastAPI dashboard + OAuth callback server."""
    import uvicorn

    settings = get_settings()
    cfg = load_config(settings)
    if not cfg.app_id:
        typer.echo("app_id not set — run 'finance config set-app-id <id>' first", err=True)
        raise typer.Exit(code=1)
    if not settings.private_key_path.exists():
        typer.echo("Private key missing — run 'finance import-key <path>' first", err=True)
        raise typer.Exit(code=1)

    # Decrypt the key once, at startup, and hold it in memory for the server lifetime.
    passphrase = _decrypt_passphrase(settings, "start server")
    private_pem = load_private_key(settings.private_key_path, passphrase)
    # Capture `app_id` in a local so its narrowed type (`str`, not `str | None`)
    # is visible to the closure below. Reading `cfg.app_id` inside the closure
    # would re-widen to the attribute's declared type.
    assert cfg.app_id is not None
    app_id = cfg.app_id

    def client_factory() -> EnableBankingClient:
        return EnableBankingClient(app_id=app_id, private_key_pem=private_pem)

    rules = load_rules(settings.rules_path)
    state = AppState(
        client_factory=client_factory,
        db_path=settings.db_path,
        callback_url=cfg.callback_url,
        rules=rules,
    )
    web_app = create_app(state)

    # Keep OAuth callback query parameters out of access logs by default.
    kwargs: dict = {"host": host, "port": port, "log_level": "info", "access_log": False}
    scheme = "http"
    if tls or cfg.callback_url.startswith("https://"):
        from finance.web.tls import ensure_localhost_cert

        tls_paths = ensure_localhost_cert(settings.keys_dir)
        kwargs["ssl_keyfile"] = str(tls_paths.key)
        kwargs["ssl_certfile"] = str(tls_paths.cert)
        scheme = "https"
        typer.echo(f"  TLS: self-signed cert at {tls_paths.cert}")
        typer.echo("  Browser will warn 'Not Secure' — that's normal for localhost; accept once.")
    typer.echo(f"→ Open {scheme}://{host}:{port}/  (callback: {cfg.callback_url})")
    uvicorn.run(web_app, **kwargs)


rules_app = typer.Typer(help="Manage categorization rules")
app.add_typer(rules_app, name="rules")


@rules_app.command("init")
def rules_init(
    force: bool = typer.Option(False, "--force", help="Overwrite existing rules file"),
) -> None:
    """Write a starter rules.yaml to ~/.config/finance/rules.yaml."""
    settings = get_settings()
    if settings.rules_path.exists() and not force:
        typer.echo(f"Already exists: {settings.rules_path} (use --force to overwrite)")
        raise typer.Exit(code=1)
    settings.rules_path.parent.mkdir(parents=True, exist_ok=True)
    settings.rules_path.write_text(DEFAULT_RULES_YAML)
    typer.echo(f"Wrote {settings.rules_path}")
    typer.echo("Edit it to taste, then run:  uv run finance analyze enrich --reenrich")


@rules_app.command("show")
def rules_show() -> None:
    settings = get_settings()
    rules = load_rules(settings.rules_path)
    if not rules:
        typer.echo(
            f"No rules at {settings.rules_path} (run 'finance rules init' to create a starter)"
        )
        return
    for r in rules:
        typer.echo(f"  {r.category:<16}  {r.match.pattern}")


@config_app.command("set-callback-url")
def config_set_callback_url(url: str) -> None:
    settings = get_settings()
    cfg = load_config(settings)
    cfg.callback_url = url
    save_config(settings, cfg)
    typer.echo(f"callback_url set to {url}")


@config_app.command("set-llm-key")
def config_set_llm_key(
    from_stdin: bool = typer.Option(
        False, "--stdin", help="Read the key from stdin instead of prompting"
    ),
) -> None:
    """Store the Anthropic API key in the OS keyring.

    Without --stdin, the prompt is hidden-input so the key never hits your
    shell history. Priority at runtime: env var > keyring.
    """
    import sys

    from finance.llm.client import store_api_key

    if from_stdin:
        key = sys.stdin.read().strip()
    else:
        key = typer.prompt("Anthropic API key", hide_input=True)
    if not key:
        typer.echo("No key provided", err=True)
        raise typer.Exit(code=1)
    import keyring.errors

    try:
        store_api_key(key)
    except keyring.errors.KeyringError as err:
        typer.echo(
            f"Could not write to OS keyring: {err}\n"
            "On headless Linux without a D-Bus secret service "
            "(gnome-keyring / keepassxc), export the key directly instead:\n"
            "  export ANTHROPIC_API_KEY=sk-ant-...\n"
            "Finance checks the environment variable before keyring, so this "
            "works identically for the runtime.",
            err=True,
        )
        raise typer.Exit(code=1) from None
    typer.echo("API key stored in OS keyring (service=finance-anthropic).")
    typer.echo("Finance will pick it up automatically when ANTHROPIC_API_KEY is unset.")


@config_app.command("clear-llm-key")
def config_clear_llm_key() -> None:
    """Remove the Anthropic API key from the OS keyring."""
    import keyring

    from finance.llm.client import KEYRING_SERVICE, KEYRING_USER

    try:
        keyring.delete_password(KEYRING_SERVICE, KEYRING_USER)
        typer.echo("API key removed from keyring.")
    except keyring.errors.PasswordDeleteError:
        typer.echo("No key stored in keyring.")


# ──────────────────────────────────────────────────────────────────────────────
# Phase 6 Stage D — `analyze`, `label`, `merchant` subgroups
# ──────────────────────────────────────────────────────────────────────────────

analyze_app = typer.Typer(help="Enrichment + analyses over the local store")
app.add_typer(analyze_app, name="analyze")

merchant_app = typer.Typer(help="Merchant bookkeeping (curate / rename / merge)")
app.add_typer(merchant_app, name="merchant")

accounts_app = typer.Typer(help="Manage connected accounts + spend-exclusion flags")
app.add_typer(accounts_app, name="accounts")

sessions_app = typer.Typer(help="Manage Enable Banking consent sessions")
app.add_typer(sessions_app, name="sessions")


@contextmanager
def _open_db() -> Iterator:
    """CLI helper — open the canonical SQLite store, init schema, close on exit.

    The db_path is resolved via `get_settings()` on each call; override it in
    tests by setting FINANCE_DATA_DIR to a tmp_path. The yielded connection is
    NOT wrapped in a transaction — callers that write wrap their unit of work
    with `with conn:` themselves.
    """
    with store.open_db(get_settings().db_path) as conn:
        yield conn


@analyze_app.command("enrich")
def analyze_enrich(
    since: str | None = typer.Option(None, "--since", help="YYYY-MM-DD lower bound"),
    reenrich: bool = typer.Option(False, "--reenrich", help="Reprocess all transactions"),
) -> None:
    """Run the enrichment pipeline (parse → normalize → classify → group streams)."""
    from finance.analysis.enrich import enrich_transactions

    rules = load_rules(get_settings().rules_path)
    with _open_db() as conn, conn:
        summary = enrich_transactions(conn, since=since, reenrich=reenrich, rules=rules)

    typer.echo(f"  processed:    {summary.newly_enriched}")
    typer.echo(f"  already enr.: {summary.already_enriched}")
    typer.echo(f"  new merchants: {summary.merchants_created}")
    typer.echo(f"  classified:   {summary.merchants_classified}")
    typer.echo(f"  streams:      {summary.streams_computed}")
    if summary.errors:
        for e in summary.errors:
            typer.echo(f"  ! {e}", err=True)


def _fmt(csv: bool, json_: bool):
    from finance.analysis.reports import fmt_from_flags

    return fmt_from_flags(csv, json_)


@analyze_app.command("recurring")
def analyze_recurring(
    active_only: bool = typer.Option(False, "--active-only"),
    csv: bool = typer.Option(False, "--csv"),
    json_: bool = typer.Option(False, "--json"),
) -> None:
    from finance.analysis.recurring import find_recurring
    from finance.analysis.reports import emit

    with _open_db() as conn, conn:
        df = find_recurring(conn, active_only=active_only)
    emit(df, _fmt(csv, json_))
    if not df.empty and _fmt(csv, json_) == "table":
        typer.echo(
            f"\n  {len(df)} stream(s)  ·  monthly total |sum| = "
            f"€{df['monthly_cost'].abs().sum():.2f}"
        )


@analyze_app.command("subscriptions")
def analyze_subscriptions(
    overlaps: bool = typer.Option(False, "--overlaps", help="Show domain overlaps instead"),
    csv: bool = typer.Option(False, "--csv"),
    json_: bool = typer.Option(False, "--json"),
) -> None:
    from finance.analysis.reports import emit
    from finance.analysis.subscriptions import find_overlaps, find_subscriptions

    with _open_db() as conn, conn:
        df = find_overlaps(conn) if overlaps else find_subscriptions(conn)
    emit(df, _fmt(csv, json_))
    if not df.empty and _fmt(csv, json_) == "table":
        if overlaps:
            typer.echo(
                f"\n  {len(df)} domain(s)  ·  combined monthly = "
                f"€{df['monthly_cost'].abs().sum():.2f}"
            )
        else:
            typer.echo(
                f"\n  {len(df)} subscription(s)  ·  total monthly = "
                f"€{df['monthly_cost'].abs().sum():.2f}"
            )


@analyze_app.command("trends")
def analyze_trends(
    months: int = typer.Option(6, "--months"),
    growth: bool = typer.Option(False, "--growth", help="Per-category growth over window"),
    spend_only: bool = typer.Option(
        False, "--spend-only", help="Drop accounts flagged excluded_from_spend"
    ),
    csv: bool = typer.Option(False, "--csv"),
    json_: bool = typer.Option(False, "--json"),
) -> None:
    from finance.analysis.reports import emit
    from finance.analysis.trends import category_growth, mom_changes

    with _open_db() as conn, conn:
        if growth:
            df = category_growth(conn, months=months, spend_only=spend_only)
        else:
            df = mom_changes(conn, months=months, spend_only=spend_only)
    emit(df, _fmt(csv, json_))


@analyze_app.command("forecast")
def analyze_forecast(
    days: int = typer.Option(30, "--days", help="Horizon in days"),
    csv: bool = typer.Option(False, "--csv"),
    json_: bool = typer.Option(False, "--json"),
) -> None:
    from finance.analysis.forecast import next_expected_charges
    from finance.analysis.reports import emit

    with _open_db() as conn, conn:
        df = next_expected_charges(conn, horizon_days=days)
    emit(df, _fmt(csv, json_))
    if not df.empty and _fmt(csv, json_) == "table":
        out = df[df["typical_amount"] < 0]["typical_amount"].abs().sum()
        inn = df[df["typical_amount"] > 0]["typical_amount"].sum()
        typer.echo(f"\n  {len(df)} expected hit(s)  ·  outflow €{out:.2f}  ·  inflow €{inn:.2f}")


@analyze_app.command("alerts")
def analyze_alerts(
    since: str | None = typer.Option(None, "--since", help="YYYY-MM-DD"),
    threshold: float = typer.Option(500.0, "--threshold", help="Large-charge floor in EUR"),
    stopped: bool = typer.Option(False, "--stopped", help="Show stopped subscriptions instead"),
    csv: bool = typer.Option(False, "--csv"),
    json_: bool = typer.Option(False, "--json"),
) -> None:
    from finance.analysis.alerts import new_large_merchants, subscription_stopped
    from finance.analysis.reports import emit

    with _open_db() as conn, conn:
        df = (
            subscription_stopped(conn)
            if stopped
            else new_large_merchants(conn, since=since, amount_threshold=threshold)
        )
    emit(df, _fmt(csv, json_))
    if not df.empty and _fmt(csv, json_) == "table":
        if stopped:
            typer.echo(
                f"\n  {len(df)} stopped sub(s)  ·  ~ monthly saved = "
                f"€{df['estimated_saved'].sum():.2f}"
            )
        else:
            typer.echo(
                f"\n  {len(df)} alert(s)  ·  total |amount| = €{df['amount'].abs().sum():.2f}"
            )


@analyze_app.command("overview")
def analyze_overview(
    months: int = typer.Option(3, "--months", help="MoM-trends window"),
    top: int = typer.Option(15, "--top", help="Top-N merchants to show"),
    forecast_days: int = typer.Option(30, "--forecast-days"),
    spend_only: bool = typer.Option(
        False, "--spend-only", help="Drop excluded accounts + Transfer rows"
    ),
    threshold: float = typer.Option(500.0, "--threshold", help="Alert floor in EUR"),
) -> None:
    """One-page structural dashboard: accounts, totals, trends, top merchants,
    recurring, subscriptions, overlaps, forecast, alerts. No LLM, no cost."""
    from finance.analysis.overview import build_overview
    from finance.analysis.reports import emit

    with _open_db() as conn, conn:
        data = build_overview(
            conn,
            months=months,
            top_n=top,
            forecast_days=forecast_days,
            spend_only=spend_only,
            threshold=threshold,
        )

    def _section(title: str, df) -> None:
        typer.echo(f"\n## {title}")
        if df is None or (hasattr(df, "empty") and df.empty):
            typer.echo("  (no data)")
            return
        emit(df, "table")

    # Accounts — custom rendering, not a DataFrame
    typer.echo("## Accounts")
    if not data.accounts:
        typer.echo("  (no accounts connected)")
    else:
        for a in data.accounts:
            flag = "  EXCLUDED" if a.excluded_from_spend else ""
            typer.echo(
                f"  {a.aspsp_name:<14} {(a.name or '')[:32]:<32}"
                f"  {a.currency or '':<4} {a.n_tx:>5} tx{flag}"
            )
        if spend_only:
            typer.echo(
                "  (spend_only=True — EXCLUDED accounts + Transfer rows dropped from spend analyses)"
            )

    # Totals — the headline numbers
    t = data.totals
    typer.echo(f"\n## Totals ({t.window_months}-month window)")
    typer.echo(f"  monthly subscriptions:   €{t.monthly_subscriptions:>9.2f}")
    typer.echo(f"  monthly recurring spend: €{t.monthly_recurring_spend:>9.2f}")
    typer.echo(f"  monthly spend average:   €{t.monthly_spend_avg:>9.2f}")
    typer.echo(f"  monthly income average:  €{t.monthly_income_avg:>9.2f}")
    if t.spend_by_category:
        typer.echo("  monthly by category:")
        for cat, amt in list(t.spend_by_category.items())[:8]:
            typer.echo(f"    {cat:<20} €{amt:>9.2f}")

    _section(f"MoM trends (last {months} months)", data.trends)
    _section(f"Top {top} merchants by outflow", data.top_merchants)
    _section("Active recurring streams", data.recurring)
    _section("Active subscriptions", data.subscriptions)
    _section("Subscription overlaps", data.overlaps)
    _section(f"Forecast — next {forecast_days} days", data.forecast)
    _section(f"New large / PRLV alerts (>€{threshold:.0f})", data.new_large)
    _section("Recently stopped subscriptions", data.stopped)

    typer.echo("\nDrill into any section with:")
    typer.echo(
        "  finance analyze {trends|recurring|subscriptions|forecast|alerts|merchants|merchant <name>}"
    )


@analyze_app.command("totals")
def analyze_totals(
    months: int = typer.Option(3, "--months"),
    spend_only: bool = typer.Option(True, "--spend-only/--include-all"),
) -> None:
    """Rollup: monthly subs, monthly recurring, monthly spend avg, monthly income."""
    from finance.analysis.totals import compute_totals

    with _open_db() as conn, conn:
        t = compute_totals(conn, months=months, spend_only=spend_only)

    typer.echo(f"Window: {t.window_months} months  (spend_only={spend_only})")
    typer.echo(f"  monthly subscriptions:   €{t.monthly_subscriptions:>9.2f}")
    typer.echo(f"  monthly recurring spend: €{t.monthly_recurring_spend:>9.2f}")
    typer.echo(f"  monthly spend average:   €{t.monthly_spend_avg:>9.2f}")
    typer.echo(f"  monthly income average:  €{t.monthly_income_avg:>9.2f}")

    typer.echo("\n  SPEND BREAKDOWN (buckets sum to monthly_spend_avg)")
    typer.echo(
        f"    essential (recurring):  €{t.monthly_essential:>9.2f}"
        f"  utilities / housing / loan / insurance / telecom / transport"
    )
    for cat, amt in t.essential_by_category.items():
        typer.echo(f"      {cat:<18} €{amt:>9.2f}")
    typer.echo(
        f"    optional (recurring):   €{t.monthly_optional:>9.2f}"
        f"  subscriptions / entertainment / AI / SaaS / health"
    )
    for cat, amt in t.optional_by_category.items():
        typer.echo(f"      {cat:<18} €{amt:>9.2f}")
    if t.monthly_other_recurring > 0:
        typer.echo(
            f"    other recurring:        €{t.monthly_other_recurring:>9.2f}"
            f"  (uncategorized recurring streams)"
        )
    typer.echo(
        f"    variable / one-off:     €{t.monthly_variable:>9.2f}"
        f"  one-off dining / shopping / travel / uncategorized lifestyle"
    )
    check_sum = (
        t.monthly_essential + t.monthly_optional + t.monthly_other_recurring + t.monthly_variable
    )
    typer.echo("    ————————————————————————————————")
    typer.echo(
        f"    total:                  €{check_sum:>9.2f}"
        f"  (= monthly_spend_avg €{t.monthly_spend_avg:.2f})"
    )

    if t.spend_by_category:
        typer.echo("\n  spend by category (all outflows, monthly avg):")
        for cat, amt in t.spend_by_category.items():
            typer.echo(f"    {cat:<20} €{amt:>9.2f}")


@analyze_app.command("merchants")
def analyze_merchants(
    top: int = typer.Option(30, "--top"),
    uncategorized: bool = typer.Option(
        False, "--uncategorized", help="Only merchants still needing a category"
    ),
    spend_only: bool = typer.Option(False, "--spend-only"),
    since: str | None = typer.Option(None, "--since", help="YYYY-MM-DD lower bound"),
    csv: bool = typer.Option(False, "--csv"),
    json_: bool = typer.Option(False, "--json"),
) -> None:
    """Ranked merchant table by outflow (cross-merchant overview)."""
    from finance.analysis.merchants import top_merchants
    from finance.analysis.reports import emit

    with _open_db() as conn, conn:
        df = top_merchants(
            conn,
            limit=top,
            spend_only=spend_only,
            since=since,
            uncategorized_only=uncategorized,
        )
    emit(df, _fmt(csv, json_))
    if not df.empty and _fmt(csv, json_) == "table":
        typer.echo(
            f"\n  {len(df)} merchant(s)  ·  "
            f"{int(df['txns'].sum())} transactions  ·  "
            f"out −€{df['total_spend'].sum():.2f}  ·  "
            f"in +€{df['total_income'].sum():.2f}"
        )


@analyze_app.command("merchant")
def analyze_merchant(
    name: str = typer.Argument(..., help="Canonical name or alias"),
    csv: bool = typer.Option(False, "--csv"),
    json_: bool = typer.Option(False, "--json"),
) -> None:
    """Deep dive on one merchant: transactions + aliases + total spend."""
    from finance.analysis.merchants import deep_dive
    from finance.analysis.reports import emit

    with _open_db() as conn, conn:
        dd = deep_dive(conn, name)

    if dd is None:
        typer.echo(f"No merchant matching '{name}'", err=True)
        raise typer.Exit(code=1)

    typer.echo(f"merchant:       {dd['merchant']}")
    typer.echo(f"category:       {dd['category']}  (source={dd['category_source']})")
    typer.echo(f"first / last:   {dd['first_seen']} → {dd['last_seen']}")
    typer.echo(f"count / spend:  {dd['count']}  (€{dd['total_spend']:.2f} outflow)")
    typer.echo(f"aliases:        {', '.join(dd['aliases']) or '(none)'}")
    typer.echo("")
    emit(dd["transactions"], _fmt(csv, json_))


@app.command()
def label(
    tx_id: str = typer.Argument(...),
    category: str = typer.Option(..., "--category"),
    note: str | None = typer.Option(None, "--note"),
) -> None:
    """Set a transaction-level category override (highest precedence)."""
    from datetime import datetime

    with _open_db() as conn, conn:
        exists = conn.execute(
            "SELECT 1 FROM transactions WHERE transaction_id = ?", (tx_id,)
        ).fetchone()
        if not exists:
            typer.echo(f"No transaction with id '{tx_id}'", err=True)
            raise typer.Exit(code=1)
        conn.execute(
            "INSERT INTO tx_overrides (tx_id, category, note, created_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(tx_id) DO UPDATE SET category = excluded.category,"
            " note = excluded.note, created_at = excluded.created_at",
            (tx_id, category, note, datetime.now(UTC).isoformat()),
        )
    typer.echo(f"tx {tx_id}: override category = {category!r}")


@merchant_app.command("set-category")
def merchant_set_category(
    canonical: str = typer.Argument(..., help="Canonical name or alias"),
    category: str = typer.Argument(...),
) -> None:
    """Set a merchant-level category (source='user', survives --reenrich)."""
    from finance.analysis.merchants import set_category

    with _open_db() as conn, conn:
        ok = set_category(conn, canonical, category)
    if not ok:
        typer.echo(f"No merchant matching '{canonical}'", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"{canonical}: category = {category!r} (source=user)")


@merchant_app.command("rename")
def merchant_rename(
    old: str = typer.Argument(...),
    new: str = typer.Argument(...),
) -> None:
    """Rename a merchant's canonical_name. Aliases + history survive."""
    from finance.analysis.merchants import rename_canonical

    with _open_db() as conn, conn:
        ok = rename_canonical(conn, old, new)
    if not ok:
        typer.echo(f"Could not rename '{old}' → '{new}' (missing or name clash)", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"{old} → {new.upper().strip()}")


@merchant_app.command("merge")
def merchant_merge(
    src: str = typer.Argument(..., help="Source canonical (deleted after merge)"),
    into: str = typer.Argument(..., help="Destination canonical (kept)"),
) -> None:
    """Merge `src` into `into`. Re-points aliases, tx_enrichment, streams."""
    from finance.analysis.merchants import merge_merchants

    with _open_db() as conn, conn:
        ok = merge_merchants(conn, src, into)
    if not ok:
        typer.echo(f"Could not merge '{src}' into '{into}'", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"merged {src} → {into}")
    typer.echo("Consider re-running:  uv run finance analyze enrich --reenrich")


@merchant_app.command("recluster")
def merchant_recluster(
    threshold: int = typer.Option(85, "--threshold", help="rapidfuzz score cutoff"),
    apply: bool = typer.Option(False, "--apply", help="Interactively merge pairs above threshold"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print candidates only (default)"),
) -> None:
    """Explicit re-clustering pass. By default lists candidates.

    With `--apply`, prompt per pair (y/n/a=all/q=quit) to merge. For safety,
    `--apply` defaults `--threshold` to 90 unless overridden.
    """
    from rapidfuzz import fuzz, process

    from finance.analysis.merchants import merge_merchants

    if apply and threshold == 85:
        threshold = 90  # tighter default when writing

    with _open_db() as conn:
        rows = conn.execute(
            "SELECT merchant_id, canonical_name FROM merchants ORDER BY canonical_name"
        ).fetchall()
        if len(rows) < 2:
            typer.echo("Fewer than 2 merchants — nothing to recluster.")
            return

        names = [r[1] for r in rows]
        seen: set[tuple[str, str]] = set()
        candidates: list[tuple[str, str, int]] = []
        for i, name in enumerate(names):
            others = names[:i] + names[i + 1 :]
            hit = process.extractOne(
                name, others, scorer=fuzz.token_sort_ratio, score_cutoff=threshold
            )
            if hit:
                pair = tuple(sorted([name, hit[0]]))
                if pair in seen:
                    continue
                seen.add(pair)
                candidates.append((pair[0], pair[1], int(hit[1])))

        if not candidates:
            typer.echo("No merge candidates above threshold.")
            return

        if not apply:
            for a, b, score in candidates:
                typer.echo(f"  {score:>3}  {a}   <->   {b}")
            typer.echo(f"\n{len(candidates)} candidate pair(s). Re-run with --apply to merge,")
            typer.echo("or merge individually:  uv run finance merchant merge <src> <into>")
            if dry_run:
                typer.echo("(dry-run: nothing was written)")
            return

        # --apply path: prompt per pair
        typer.echo(f"Reviewing {len(candidates)} pair(s). [y]es / [n]o / [a]ll / [q]uit\n")
        accept_all = False
        merged_count = 0
        with conn:
            for a, b, score in candidates:
                # Preserve the longer name as the destination (usually more informative).
                src, dst = (a, b) if len(b) >= len(a) else (b, a)
                prompt = f"  {score:>3}  {src}  →  {dst}  [y/n/a/q]: "
                answer = (
                    "a" if accept_all else typer.prompt(prompt, default="n", show_default=False)
                )
                answer = (answer or "").lower().strip()
                if answer == "q":
                    typer.echo("Stopped.")
                    break
                if answer == "a":
                    accept_all = True
                    answer = "y"
                if answer == "y":
                    ok = merge_merchants(conn, src, dst)
                    if ok:
                        merged_count += 1
                        typer.echo("    merged.")
                    else:
                        typer.echo("    ! merge failed (collision or missing)")
    typer.echo(f"\n{merged_count} pair(s) merged.")
    if merged_count:
        typer.echo("Next: uv run finance analyze enrich --reenrich")


@merchant_app.command("apply-merges")
def merchant_apply_merges(
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview only"),
) -> None:
    """Apply the curated merges from merchant_merges.yaml.

    Each `src: dst` entry is applied as merge (if both exist) or rename
    (if only src exists). Missing entries are skipped quietly.
    """
    from finance.analysis.merchants import apply_curated_merges

    with _open_db() as conn, conn:
        results = apply_curated_merges(conn, dry_run=dry_run)

    if not results:
        typer.echo("merchant_merges.yaml is empty.")
        return

    markers = {"merged": "⇢", "renamed": "→", "skipped": "·"}
    counts = {"merged": 0, "renamed": 0, "skipped": 0}
    for src, dst, action in results:
        counts[action] = counts.get(action, 0) + 1
        suffix = "" if action != "skipped" else "   (not in DB)"
        typer.echo(f"  {markers.get(action, '?')}  {src}  →  {dst}{suffix}")

    typer.echo(
        f"\n{counts['merged']} merged, {counts['renamed']} renamed, {counts['skipped']} skipped"
    )
    if dry_run:
        typer.echo("(dry-run: no changes written)")
    elif counts["merged"] or counts["renamed"]:
        typer.echo("Next: uv run finance analyze enrich --reenrich")


@merchant_app.command("review")
def merchant_review(
    limit: int = typer.Option(20, "--limit", help="Number of merchants to walk through"),
    include_rule: bool = typer.Option(
        False, "--include-rule", help="Also review regex-rule matches (source='rule')"
    ),
    include_llm: bool = typer.Option(
        False, "--include-llm", help="Also review LLM-assigned categories (source='llm')"
    ),
    include_auto: bool = typer.Option(
        False,
        "--include-auto",
        help="Shorthand for everything auto-tagged (rule + rule-stream + llm)",
    ),
) -> None:
    """Interactively re-categorize merchants one at a time (source='user').

    By default only shows **truly uncategorized** merchants — those where
    category_source IS NULL. Auto-detected categories (rule, rule-stream, llm,
    curated, user) are excluded so you don't re-review reliable tags.

    Use flags to widen the scope:
      --include-rule   also show regex-rule matches (text-based, sometimes wrong)
      --include-llm    also show LLM-assigned categories
      --include-auto   shorthand for everything except user/curated

    Prompts per merchant: [number] menu pick, [text] custom category name,
    [empty] skip, [q] quit.

    Differs from `seed-top` — this writes source='user' directly to the DB
    (immediate, no re-enrich needed). `seed-top` appends to a curated YAML
    which applies to future re-enrichments.
    """
    from finance.analysis.merchants import set_category
    from finance.llm.categorize import load_taxonomy

    with _open_db() as conn:
        # Build the "skip this source" allow-list. Defaults exclude every kind of
        # auto-detection so the wizard only surfaces rows that actually need a
        # human decision.
        skip_sources: set[str] = {"user", "curated", "rule-stream"}
        if not include_auto:
            if not include_rule:
                skip_sources.add("rule")
            if not include_llm:
                skip_sources.add("llm")
        # 'rule-stream' is always skipped — structural auto-detection is reliable.

        ph = ",".join("?" for _ in skip_sources)
        source_filter = f"(m.category IS NULL OR m.category_source NOT IN ({ph}))"
        rows = conn.execute(
            f"""
            SELECT m.merchant_id,
                   m.canonical_name,
                   m.category,
                   m.category_source,
                   SUM(-t.amount) AS spent,
                   COUNT(*)       AS n,
                   MAX(t.booking_date) AS last_seen
            FROM transactions t
            JOIN tx_enrichment e ON e.tx_id = t.transaction_id
            JOIN merchants m ON m.merchant_id = e.merchant_id
            JOIN accounts a ON a.account_uid = t.account_uid
            WHERE t.amount < 0 AND t.currency = 'EUR'
              AND COALESCE(a.excluded_from_spend, 0) = 0
              AND {source_filter}
            GROUP BY m.merchant_id
            ORDER BY spent DESC
            LIMIT ?
            """,
            (*sorted(skip_sources), limit),
        ).fetchall()

        if not rows:
            typer.echo("Nothing to review — every merchant is either user/curated or zero-outflow.")
            return

        taxonomy = load_taxonomy()

        typer.echo(
            f"\nReviewing {len(rows)} merchant(s). For each: pick a category.\n"
            f"  [number] one of the menu entries below\n"
            f"  [text]   a custom category name (must be in the taxonomy)\n"
            f"  [empty]  skip (leave as-is)\n"
            f"  [q]      quit and save what's been decided so far\n\n"
            f"Menu:"
        )
        for i, cat in enumerate(taxonomy, 1):
            typer.echo(f"  {i:>2}. {cat}")
        typer.echo("")

        applied = 0
        for idx, r in enumerate(rows, 1):
            header = (
                f"[{idx}/{len(rows)}]  {r['canonical_name']:<40}  "
                f"€{r['spent']:>8.2f}  ({r['n']:>3} tx, last {r['last_seen']})"
            )
            if r["category"]:
                header += f"  [currently: {r['category']} / {r['category_source']}]"
            typer.echo(header)

            memos = conn.execute(
                """
                SELECT t.remittance_info
                FROM tx_enrichment e
                JOIN transactions t ON t.transaction_id = e.tx_id
                WHERE e.merchant_id = ?
                ORDER BY t.booking_date DESC
                LIMIT 3
                """,
                (r["merchant_id"],),
            ).fetchall()
            for m in memos:
                if m[0]:
                    typer.echo(f"    · {m[0][:130]}")

            choice = typer.prompt("    category", default="", show_default=False).strip()
            if not choice:
                typer.echo("    (skipped)\n")
                continue
            if choice.lower() == "q":
                typer.echo("    (quit)\n")
                break

            # Resolve: integer → menu item; text → direct
            category: str | None = None
            if choice.isdigit():
                i = int(choice)
                if 1 <= i <= len(taxonomy):
                    category = taxonomy[i - 1]
            else:
                # Case-insensitive lookup against the taxonomy for resilience.
                for cat in taxonomy:
                    if cat.lower() == choice.lower():
                        category = cat
                        break

            if not category:
                typer.echo(f"    ! '{choice}' is not in the taxonomy — skipped.\n")
                continue

            ok = set_category(conn, r["canonical_name"], category)
            if ok:
                applied += 1
                typer.echo(f"    ✓ tagged as {category}\n")
            else:
                typer.echo("    ! failed to write\n")

    typer.echo(f"Done. {applied} merchant(s) categorized (source='user').")
    if applied:
        typer.echo("Next: uv run finance analyze enrich --reenrich")


@merchant_app.command("seed-top")
def merchant_seed_top(
    limit: int = typer.Option(20, "--limit"),
) -> None:
    """Interactively curate the top-N uncategorized merchants by outflow.

    Appends `CANONICAL: category` entries to src/finance/data/merchants_seed.yaml
    in the working copy (editable). After, run `finance analyze enrich --reenrich`.
    """
    from pathlib import Path

    with _open_db() as conn:
        rows = conn.execute(
            """
            SELECT m.canonical_name, SUM(-t.amount) AS spent, COUNT(*) AS n
            FROM transactions t
            JOIN tx_enrichment e ON e.tx_id = t.transaction_id
            JOIN merchants m ON m.merchant_id = e.merchant_id
            WHERE t.amount < 0 AND t.currency = 'EUR'
              AND (m.category IS NULL OR m.category_source NOT IN ('user', 'curated'))
            GROUP BY m.canonical_name
            ORDER BY spent DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    if not rows:
        typer.echo("No uncategorized merchants to curate.")
        return

    # Locate the seed file in the repo (we want to edit source, not the installed copy).
    repo_seed = Path(__file__).resolve().parent / "data" / "merchants_seed.yaml"
    typer.echo(f"Appending to: {repo_seed}\n")

    additions: list[str] = []
    for r in rows:
        name = r["canonical_name"]
        spent = r["spent"]
        n = r["n"]
        prompt = f"  {name:<40}  €{spent:>8.2f}  ({n:>3} tx) → category (empty to skip): "
        cat = typer.prompt(prompt, default="", show_default=False)
        cat = (cat or "").strip()
        if cat:
            additions.append(f"{name}: {cat}")

    if not additions:
        typer.echo("Nothing appended.")
        return

    with repo_seed.open("a") as f:
        f.write("\n# Appended by `finance merchant seed-top`\n")
        for line in additions:
            f.write(line + "\n")

    typer.echo(f"\nAppended {len(additions)} entr{'y' if len(additions) == 1 else 'ies'}.")
    typer.echo("Next: uv run finance analyze enrich --reenrich")


# ──────────────────────────────────────────────────────────────────────────────
# Accounts / sessions management
# ──────────────────────────────────────────────────────────────────────────────


@accounts_app.command("ls")
def accounts_ls() -> None:
    """List connected accounts with spend-exclusion flags + tx counts."""
    with _open_db() as conn:
        rows = conn.execute(
            """
            SELECT a.account_uid, a.name, a.currency, a.excluded_from_spend,
                   s.aspsp_name,
                   (SELECT COUNT(*) FROM transactions t WHERE t.account_uid = a.account_uid) AS n_tx
            FROM accounts a
            JOIN sessions s ON s.session_id = a.session_id
            ORDER BY s.aspsp_name, a.name
            """
        ).fetchall()
    if not rows:
        typer.echo("(no accounts)")
        return
    typer.echo(f"  {'uid':<38}  {'aspsp':<14}  {'name':<30}  {'ccy':<4}  {'tx':>5}  flag")
    for r in rows:
        flag = "EXCLUDED" if r["excluded_from_spend"] else ""
        typer.echo(
            f"  {r['account_uid']}  {r['aspsp_name']:<14}"
            f"  {(r['name'] or '')[:30]:<30}  {r['currency'] or '':<4}"
            f"  {r['n_tx']:>5}  {flag}"
        )


@accounts_app.command("exclude")
def accounts_exclude(
    account_uid: str = typer.Argument(..., help="Account UID to flag as excluded"),
) -> None:
    """Mark an account so `--spend-only` analyses drop its transactions."""
    with _open_db() as conn, conn:
        cur = conn.execute(
            "UPDATE accounts SET excluded_from_spend = 1 WHERE account_uid = ?",
            (account_uid,),
        )
    if cur.rowcount == 0:
        typer.echo(f"No account with uid={account_uid}", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"flagged excluded: {account_uid}")


@accounts_app.command("include")
def accounts_include(
    account_uid: str = typer.Argument(...),
) -> None:
    """Clear the spend-exclusion flag on an account."""
    with _open_db() as conn, conn:
        cur = conn.execute(
            "UPDATE accounts SET excluded_from_spend = 0 WHERE account_uid = ?",
            (account_uid,),
        )
    if cur.rowcount == 0:
        typer.echo(f"No account with uid={account_uid}", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"cleared exclusion: {account_uid}")


@sessions_app.command("ls")
def sessions_ls() -> None:
    """List consent sessions + days-to-expiry."""
    from datetime import date as _date

    with _open_db() as conn:
        rows = conn.execute(
            """
            SELECT session_id, aspsp_name, aspsp_country, valid_until, created_at, revoked_at
            FROM sessions ORDER BY created_at DESC
            """
        ).fetchall()
    if not rows:
        typer.echo("(no sessions)")
        return
    today = _date.today()
    for r in rows:
        days_left = ""
        try:
            exp = _date.fromisoformat(r["valid_until"][:10])
            days_left = f"{(exp - today).days}d"
        except (ValueError, TypeError):
            pass
        revoked = "REVOKED" if r["revoked_at"] else ""
        typer.echo(
            f"  {r['session_id']}  {r['aspsp_name']:<14}  {r['aspsp_country']}"
            f"  expires:{days_left:>5}  {revoked}"
        )


@sessions_app.command("rm")
def sessions_rm(
    session_id: str = typer.Argument(...),
    force: bool = typer.Option(False, "--force", help="Skip confirmation"),
) -> None:
    """Cascade-delete a session: transactions → balances → sync_runs → accounts → session.

    Use for mock-sandbox cleanup or revoked sessions you no longer want in
    the DB. The actual Enable Banking consent is not revoked by this — use
    the bank's UI for that.
    """
    with _open_db() as conn:
        row = conn.execute(
            "SELECT aspsp_name, created_at FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if not row:
            typer.echo(f"No session with id={session_id}", err=True)
            raise typer.Exit(code=1)

        tx_count = conn.execute(
            "SELECT COUNT(*) FROM transactions t JOIN accounts a ON a.account_uid = t.account_uid"
            " WHERE a.session_id = ?",
            (session_id,),
        ).fetchone()[0]
        acc_count = conn.execute(
            "SELECT COUNT(*) FROM accounts WHERE session_id = ?", (session_id,)
        ).fetchone()[0]

        typer.echo(
            f"Session {session_id}\n"
            f"  aspsp: {row['aspsp_name']}, created {row['created_at'][:19]}\n"
            f"  will delete: {tx_count} transactions, {acc_count} account(s), 1 session"
        )
        if not force and not typer.confirm("Proceed?", default=False):
            typer.echo("Aborted.")
            raise typer.Exit(code=1)

        with conn:
            conn.execute(
                "DELETE FROM transactions WHERE account_uid IN"
                " (SELECT account_uid FROM accounts WHERE session_id = ?)",
                (session_id,),
            )
            conn.execute(
                "DELETE FROM balances WHERE account_uid IN"
                " (SELECT account_uid FROM accounts WHERE session_id = ?)",
                (session_id,),
            )
            conn.execute(
                "DELETE FROM sync_runs WHERE account_uid IN"
                " (SELECT account_uid FROM accounts WHERE session_id = ?)",
                (session_id,),
            )
            conn.execute("DELETE FROM accounts WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
    typer.echo("Done. Consider: uv run finance analyze enrich --reenrich")


# ──────────────────────────────────────────────────────────────────────────────
# Phase 7 — LLM categorization + advisory
# ──────────────────────────────────────────────────────────────────────────────

enrich_app = typer.Typer(help="LLM-backed enrichment of the long tail")
app.add_typer(enrich_app, name="enrich")

advise_app = typer.Typer(help="LLM-backed advisory (subscriptions / cutbacks / bundles)")
app.add_typer(advise_app, name="advise")


def _make_llm_client():
    import anthropic

    from finance.llm.client import LLMClient, redact_key, resolve_api_key

    if not resolve_api_key():
        typer.echo(
            "No Anthropic API key found.\n"
            "  Either:   export ANTHROPIC_API_KEY=...\n"
            "  Or store: uv run finance config set-llm-key",
            err=True,
        )
        raise typer.Exit(code=1)
    try:
        return LLMClient()
    except anthropic.AnthropicError as e:
        typer.echo(f"Could not initialize Anthropic client: {redact_key(str(e))}", err=True)
        raise typer.Exit(code=1) from None


@enrich_app.command("llm-categorize")
def enrich_llm_categorize(
    limit: int = typer.Option(150, "--limit", help="Max merchants per run"),
    model: str | None = typer.Option(None, "--model", help="Override model (default: haiku)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Propose without writing"),
    provider: str = typer.Option(
        "api",
        "--provider",
        help="'api' (pay-per-token, fast, typed output) or 'claude-cli' "
        "(uses your Claude Code subscription; includes WebSearch for "
        "merchant disambiguation).",
    ),
) -> None:
    """Categorize merchants whose `category_source` is not user/curated."""
    from finance.llm.categorize import AUTO_WRITE_THRESHOLD, categorize_uncategorized
    from finance.llm.client import DEFAULT_CATEGORIZE_MODEL
    from finance.llm.providers import ClaudeCLIError, make_provider

    try:
        llm = make_provider(provider) if provider != "api" else _make_llm_client()
    except ClaudeCLIError as e:
        typer.echo(f"Claude Code provider unavailable: {e}", err=True)
        raise typer.Exit(code=1) from None
    except ValueError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1) from None

    chosen_model = model or DEFAULT_CATEGORIZE_MODEL
    with _open_db() as conn, conn:
        summary = categorize_uncategorized(
            conn,
            client=llm,
            limit=limit,
            model=chosen_model,
            dry_run=dry_run,
        )

    if summary.proposed == 0:
        typer.echo("Nothing to categorize — all merchants have a final category.")
        return

    typer.echo(f"model:         {summary.model}")
    typer.echo(
        f"batches ok:    {summary.batches}"
        + (f" (+{summary.failed_batches} failed)" if summary.failed_batches else "")
    )
    typer.echo(f"tokens in:     {summary.usage.input_tokens}")
    typer.echo(f"tokens out:    {summary.usage.output_tokens}")
    typer.echo(f"proposed:      {summary.proposed}")
    typer.echo(f"auto-written:  {summary.written}")
    typer.echo(f"low-conf held: {summary.low_confidence}")
    if dry_run:
        typer.echo("(dry-run: no writes)")
    if summary.errors:
        typer.echo("\nBatch errors:")
        for err in summary.errors:
            typer.echo(f"  ! {err[:140]}")

    if summary.low_confidence:
        typer.echo(f"\nLow-confidence proposals (< {AUTO_WRITE_THRESHOLD}):")
        for p in summary.proposals:
            if p.confidence < AUTO_WRITE_THRESHOLD:
                typer.echo(
                    f"  {p.confidence:>4.2f}  {p.canonical_name:<40}"
                    f"  → {p.category}  ({p.reasoning})"
                )
        typer.echo(
            "\nAccept individually with:\n"
            "  uv run finance merchant set-category <canonical> <category>"
        )


def _emit_advice(payload: dict, fmt: str) -> None:
    """Advisory payloads are nested JSON; default is JSON pretty-print."""
    import json as _json

    if fmt == "csv":
        typer.echo(_json.dumps(payload))  # no natural CSV shape — emit JSON
    elif fmt == "json":
        typer.echo(_json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        typer.echo(_json.dumps(payload, indent=2, ensure_ascii=False))


@advise_app.command("subscriptions")
def advise_subscriptions_cmd(
    refresh: bool = typer.Option(False, "--refresh", help="Bypass cache"),
    model: str | None = typer.Option(None, "--model"),
    csv: bool = typer.Option(False, "--csv"),
    json_: bool = typer.Option(False, "--json"),
) -> None:
    """LLM recommendations over subscription overlaps."""
    from finance.llm.advise_dispatch import advise_subscriptions
    from finance.llm.client import DEFAULT_ADVISE_MODEL

    llm = _make_llm_client()
    chosen = model or DEFAULT_ADVISE_MODEL
    with _open_db() as conn, conn:
        result = advise_subscriptions(conn, client=llm, model=chosen, refresh=refresh)

    marker = "(cache hit)" if result.cached else f"(new call, model={result.model})"
    typer.echo(f"# subscriptions advice  {marker}\n")
    _emit_advice(result.payload, _fmt(csv, json_))


@advise_app.command("cutbacks")
def advise_cutbacks_cmd(
    months: int = typer.Option(6, "--months"),
    refresh: bool = typer.Option(False, "--refresh"),
    model: str | None = typer.Option(None, "--model"),
    csv: bool = typer.Option(False, "--csv"),
    json_: bool = typer.Option(False, "--json"),
) -> None:
    """LLM cutback targets (top-growth categories + under-used subs)."""
    from finance.llm.advise_dispatch import advise_cutbacks
    from finance.llm.client import DEFAULT_ADVISE_MODEL

    llm = _make_llm_client()
    chosen = model or DEFAULT_ADVISE_MODEL
    with _open_db() as conn, conn:
        result = advise_cutbacks(conn, client=llm, months=months, model=chosen, refresh=refresh)

    marker = "(cache hit)" if result.cached else f"(new call, model={result.model})"
    typer.echo(f"# cutbacks advice  {marker}\n")
    _emit_advice(result.payload, _fmt(csv, json_))


@advise_app.command("integral-offers")
def advise_integral_cmd(
    refresh: bool = typer.Option(False, "--refresh"),
    model: str | None = typer.Option(None, "--model"),
    csv: bool = typer.Option(False, "--csv"),
    json_: bool = typer.Option(False, "--json"),
) -> None:
    """LLM suggestions for cross-domain bundled offers."""
    from finance.llm.advise_dispatch import advise_integral
    from finance.llm.client import DEFAULT_ADVISE_MODEL

    llm = _make_llm_client()
    chosen = model or DEFAULT_ADVISE_MODEL
    with _open_db() as conn, conn:
        result = advise_integral(conn, client=llm, model=chosen, refresh=refresh)

    marker = "(cache hit)" if result.cached else f"(new call, model={result.model})"
    typer.echo(f"# integral-offers advice  {marker}\n")
    _emit_advice(result.payload, _fmt(csv, json_))


@advise_app.command("ls")
def advise_ls(
    include_dismissed: bool = typer.Option(False, "--all"),
) -> None:
    """List persisted advice rows."""
    from finance.llm.advise import list_advice

    with _open_db() as conn, conn:
        rows = list_advice(conn, include_dismissed=include_dismissed)
    if not rows:
        typer.echo("(no advice on file)")
        return
    for r in rows:
        mark = "×" if r.get("dismissed_at") else " "
        typer.echo(
            f"  [{mark}] #{r['id']:<4} {r['kind']:<22} {r['generated_at'][:19]}  ({r['model']})"
        )


@advise_app.command("dismiss")
def advise_dismiss(advice_id: int = typer.Argument(...)) -> None:
    """Mark an advice row as dismissed — future runs won't cache-hit it."""
    from finance.llm.advise import dismiss_advice

    with _open_db() as conn, conn:
        ok = dismiss_advice(conn, advice_id)
    if not ok:
        typer.echo(f"No active advice with id={advice_id}", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"dismissed advice #{advice_id}")


if __name__ == "__main__":
    app()
