# Security And Privacy

## Summary

The app is designed for single-user localhost use, and several security
choices are good under that threat model: encrypted EB private key storage,
config chmod, JWT TTL and `jti`, parameterized SQL, key redaction, and
IBAN redaction in EB error bodies.

Tier U added a local browser boundary for the dashboard: startup token login,
HttpOnly SameSite cookie, CSRF/origin checks, local JS/CSS assets, CSP, and
security headers. That makes localhost use materially safer, but the repo is
still not hardened as a multi-user or cloud-facing service.

## Positive Controls

- EB private keys are age-encrypted and chmodded `0600` in
  [auth/keys.py](../../src/finance/auth/keys.py#L46).
- Config file is chmodded `0600` in
  [config.py](../../src/finance/config.py#L61).
- JWT signing uses RS256, bounded TTL, and random `jti` in
  [auth/jwt.py](../../src/finance/auth/jwt.py#L11).
- EB error bodies redact IBAN-shaped strings and truncate long responses in
  [eb/client.py](../../src/finance/eb/client.py#L15).
- Most SQL paths use placeholders.
- Anthropic-looking keys are redacted before LLM run logging in
  [llm/client.py](../../src/finance/llm/client.py#L86).
- `finance serve` issues a per-process dashboard token in
  [cli.py](../../src/finance/cli.py#L304), and the web middleware enforces
  token/cookie auth, CSRF, origin checks, and security headers in
  [web/app.py](../../src/finance/web/app.py#L155).
- Browser assets are served locally from
  [web/static/app.js](../../src/finance/web/static/app.js) and
  [web/static/app.css](../../src/finance/web/static/app.css).

## Findings

### S1. No Dashboard Authentication (Fixed For Local Serve In Tier U)

Initial evidence:

- `create_app()` mounts routes directly in
  [web/app.py](../../src/finance/web/app.py#L149).
- Mutating routes exist across [web/app.py](../../src/finance/web/app.py)
  and [web/dashboard.py](../../src/finance/web/dashboard.py).
- `finance serve` defaults to `127.0.0.1`, but `--host` is configurable in
  [cli.py](../../src/finance/cli.py#L271).

Current status:

Tier U generates a random dashboard token in `finance serve`, prints a local
URL containing it, strips the token from the browser URL on first GET, and
sets an HttpOnly SameSite cookie. Requests without the cookie, bearer token,
or query token receive `401`.

Remaining risk:

This is single-user local auth, not a hardened multi-user deployment model.
Do not treat `--host 0.0.0.0` plus the printed token as sufficient for a
shared network or cloud service.

Remaining recommendation:

Keep the token-cookie boundary for local use. Add a deliberate deployment
profile, TLS/proxy guidance, and user/session model before broader exposure.

### S2. No CSRF Protection On Mutating Routes (Fixed In Tier U)

Initial evidence:

- `/sync` mutates in [web/app.py](../../src/finance/web/app.py#L268).
- Rules, settings, merchants, streams, accounts, and advice POST routes live
  in [web/dashboard.py](../../src/finance/web/dashboard.py).

Current status:

Tier U enforces same-origin checks and a server-generated CSRF token for
unsafe methods. The local dashboard JS sends `X-CSRF-Token`, and traditional
forms can submit `_csrf`.

Remaining recommendation:

Keep all future mutating routes behind the same middleware and add
regression tests when new forms or non-JS POST paths are introduced.

### S3. CDN Scripts Run On Sensitive Pages (Fixed In Tier U)

Initial evidence:

- Tailwind and HTMX are loaded from CDNs in
  [base.html](../../src/finance/web/templates/base.html#L7).
- Chart.js is loaded from CDN in
  [index.html](../../src/finance/web/templates/index.html#L88).

Current status:

Tier U removed those CDN dependencies and inline browser handlers. The
dashboard now uses local `app.css` and `app.js`, and responses include a
self-only CSP that does not allow inline scripts.

Remaining recommendation:

Keep the CSP restrictive. If a future dependency is added, vendor/pin it
locally or document why an external source is necessary.

### S4. LLM Privacy Boundary Is Not Explicit Enough

Evidence:

- Categorization gathers raw memos in
  [llm/categorize.py](../../src/finance/llm/categorize.py#L93).
- The prompt includes merchant names and memo snippets in
  [llm/categorize.py](../../src/finance/llm/categorize.py#L131).
- Anthropic API call happens in
  [llm/client.py](../../src/finance/llm/client.py#L124).
- Claude CLI prompt explicitly permits WebSearch in
  [llm/providers.py](../../src/finance/llm/providers.py#L83).

Risk:

Personal financial context can leave the machine. Claude CLI with
WebSearch increases ambiguity about what text may be used with tools.

Recommendation:

Add an LLM privacy policy in code and UI:

- default to minimized merchant names, not full memos
- redact card/account/reference tokens before prompt construction
- require explicit opt-in for tool-enabled providers
- add a dry-run preview of exactly what will be sent
- persist a redaction version with `llm_runs`

### S5. SQLite Stores Sensitive Raw Payloads Indefinitely

Evidence:

- Accounts store `iban` and `raw_json` in
  [schema.sql](../../src/finance/db/schema.sql#L10).
- Transactions store `remittance_info` and `raw_json` in
  [schema.sql](../../src/finance/db/schema.sql#L34).
- `persist_session()` writes raw account JSON in
  [store.py](../../src/finance/db/store.py#L98).
- `sync_account()` writes full raw transaction JSON in
  [sync.py](../../src/finance/sync.py#L109).
- The initial audit found that `store.connect()` created DB files without
  chmod hardening.

Current status:

Tier R/S/T chmods the data directory to `0700` and DB file to `0600`
best-effort when opening the SQLite store. Raw payload retention remains.

Risk:

The SQLite file is a high-value personal data store. Raw payload retention
may exceed what the app actually needs.

Recommendation:

- add a purge/minimize command
- consider optional SQLCipher or filesystem-level encrypted storage
- store raw provider payloads only behind an explicit debug/forensics flag

### S6. Full IBAN Was Rendered In The Web UI (Fixed In Tier Q)

Initial evidence:

- Account rows render `a.iban` directly in
  [_account_row.html](../../src/finance/web/templates/_account_row.html#L8).

Current status:

Tier Q added masked account display through
[privacy.py](../../src/finance/web/privacy.py) and account-row template
changes. The DB still stores full IBANs.

Risk:

Shoulder-surfing and screenshots reveal full bank identifiers.

Remaining recommendation:

Keep masked display by default. Add explicit reveal only if there is a
real workflow need and it is protected by local auth.

### S7. Direct HTMLResponse f-strings Could Reflect Unescaped Text (Partly Fixed In Tier Q)

Evidence:

- Claude/provider errors are interpolated in
  [dashboard.py](../../src/finance/web/dashboard.py#L243).
- LLM progress labels are interpolated in
  [dashboard.py](../../src/finance/web/dashboard.py#L302).
- Keyring errors are interpolated in
  [dashboard.py](../../src/finance/web/dashboard.py#L794).

Risk:

Dynamic error text can become reflected HTML. Some values are local or
provider-derived, but the pattern is unsafe.

Recommendation:

Tier Q escaped the direct dynamic fragments identified in this pass and
added regression tests. The longer-term cleanup is to move raw HTMX string
responses into Jinja partials or a shared escaping fragment helper.

### S8. User Regex Rules Can ReDoS Re-Enrichment

Evidence:

- Web route accepts arbitrary regex in
  [dashboard.py](../../src/finance/web/dashboard.py#L655).
- Rules compile with Python `re` in
  [categorize.py](../../src/finance/categorize.py#L16).
- Rules run during classification in
  [classify.py](../../src/finance/analysis/classify.py#L79).

Risk:

A pathological regex can hang re-enrichment over many merchant names.

Recommendation:

Limit regex length, validate patterns, consider the third-party `regex`
module with timeouts, or replace arbitrary regex with simpler contains/glob
rules for the web UI.

### S9. OAuth Callback Codes Could Be Logged (Fixed In Tier Q Default Path)

Evidence:

- Callback takes `code` in query string in
  [web/app.py](../../src/finance/web/app.py#L96).
- Uvicorn starts with normal info logging in
  [cli.py](../../src/finance/cli.py#L311).

Risk:

Access logs can contain OAuth codes in URLs.

Recommendation:

Tier Q disables uvicorn access logs by default for `finance serve`. If a
future deployment adds a reverse proxy or external access logs, redact
`/callback?...` query strings there too.

### S10. Dependency Audit Initially Failed (Fixed Except No-Fix Pip CVE)

Evidence from `pip-audit`:

| Package | Version | Finding |
|---|---:|---|
| `python-multipart` | 0.0.26 | CVE-2026-42561, fix 0.0.27. |
| `jupyter-server` | 2.17.0 | 4 CVEs, fix 2.18.0. |
| `jupyterlab` | 4.5.6 | 2 CVEs, fix 4.5.7. |
| `mistune` | 3.2.0 | CVE-2026-33079, fix 3.2.1. |
| `pip` | 26.0.1 | 2 CVEs, one fix 26.1. |

`python-multipart` was runtime in
[pyproject.toml](../../pyproject.toml#L19). Jupyter was dev-only in
[pyproject.toml](../../pyproject.toml#L43). CI allowed audit failure with
`continue-on-error: true`.

Current status:

Tier Q upgraded the vulnerable locked packages and made CI `pip-audit`
blocking with one explicit ignore for no-fixed-version `CVE-2026-3219`.

## Security Priorities

1. Define LLM prompt minimization and explicit opt-in for tool-enabled LLMs.
2. Add purge/minimize/encryption policy for raw SQLite payloads.
3. Add a deliberate deployment profile before any non-loopback use.
4. Limit or timebox user regex rules used during re-enrichment.
5. Keep future mutating routes covered by the Tier U CSRF/origin middleware.
