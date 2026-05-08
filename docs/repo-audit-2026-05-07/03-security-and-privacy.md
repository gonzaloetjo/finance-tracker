# Security And Privacy

## Summary

The app is designed for single-user localhost use, and several security
choices are good under that threat model: encrypted EB private key storage,
config chmod, JWT TTL and `jti`, parameterized SQL, key redaction, and
IBAN redaction in EB error bodies.

The repo is not hardened for network exposure. If the dashboard is
reachable by another browser/user/process, sensitive financial data and
write actions are exposed.

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

## Findings

### S1. No Dashboard Authentication

Evidence:

- `create_app()` mounts routes directly in
  [web/app.py](../../src/finance/web/app.py#L57).
- Mutating routes exist across [web/app.py](../../src/finance/web/app.py)
  and [web/dashboard.py](../../src/finance/web/dashboard.py).
- `finance serve` defaults to `127.0.0.1`, but `--host` is configurable in
  [cli.py](../../src/finance/cli.py#L271).

Risk:

Binding to `0.0.0.0`, reverse proxying, or accidental local exposure gives
any reachable user read/write access to accounts, transactions, categories,
rules, sync, LLM calls, and key storage.

Recommendation:

Add local authentication before any broader use:

- random local bearer token printed at startup, or
- password/session cookie, or
- loopback-only guard that refuses non-loopback hosts unless auth is enabled.

### S2. No CSRF Protection On Mutating Routes

Evidence:

- `/sync` mutates in [web/app.py](../../src/finance/web/app.py#L122).
- Rules, settings, merchants, streams, accounts, and advice POST routes live
  in [web/dashboard.py](../../src/finance/web/dashboard.py).

Risk:

A malicious webpage can submit forms to `localhost` if the dashboard is
running. This matters even if the app remains loopback-only.

Recommendation:

Add CSRF tokens for forms and HTMX requests. At minimum, enforce `Origin`
or `Sec-Fetch-Site` checks and require an app-issued token on unsafe methods.

### S3. CDN Scripts Run On Sensitive Pages

Evidence:

- Tailwind and HTMX are loaded from CDNs in
  [base.html](../../src/finance/web/templates/base.html#L7).
- Chart.js is loaded from CDN in
  [index.html](../../src/finance/web/templates/index.html#L88).

Risk:

Compromised CDN JavaScript can read DOM-rendered bank data and API-key form
contents.

Recommendation:

Vendor these assets locally, pin versions, and add a Content Security
Policy. Avoid inline scripts or move them behind nonces/hashes.

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

1. Add auth and CSRF before any non-loopback use.
2. Vendor scripts and add CSP/security headers.
3. Define LLM prompt minimization and explicit opt-in for tool-enabled LLMs.
4. chmod/minimize/encrypt the SQLite data store.
5. Limit or timebox user regex rules used during re-enrichment.
