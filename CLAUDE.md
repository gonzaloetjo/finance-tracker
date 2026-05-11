# CLAUDE.md — session primer for the finance repo

Single-user self-hosted personal finance tracker. BNP Paribas (FR) transactions
via Enable Banking, SQLite store, Typer CLI + FastAPI dashboard on localhost,
Anthropic LLM layer for categorization + advisories. Python 3.11+, managed
with `uv`.

> **History.** This is the public-mirror working tree. The full per-tier
> commit history (Tiers A–M, every refactor with rationale + verification)
> lives in the private development repo at
> `https://github.com/gonzaloetjo/finance` — not on this remote. If you
> need to trace why a decision was made, reach for that repo's
> `AUDIT.md` and `git log`. Active development continues here.

## Dev workflow

Every command is prefixed with `uv run` (virtualenv-managed via `uv sync`):

```
uv run pytest -q                                      # full suite, 233+ tests
uv run pytest --cov=finance --cov-report=term         # + coverage
uv run pytest tests/test_cli_smoke.py -v              # CLI end-to-end smoke
uv run ruff check .                                   # lint
uv run ruff format .                                  # format (100-char line length)
uv run mypy src/finance                               # types; must stay clean
uv run vulture src/finance --min-confidence 80        # dead code
uv run pip-audit --skip-editable                      # CVE check on locked deps
```

CLI entry point is `finance`:

```
uv run finance analyze overview          # one-shot dashboard snapshot
uv run finance analyze enrich --reenrich # canonical recategorize path
uv run finance serve                     # dashboard at http://localhost:8000
```

Full CLI surface is documented in `README.md`.

Alternative entry point: `devenv shell` (devenv 2.1+) pins Python 3.11 and
system tools (`git`, `jq`, `sqlite`, `shellcheck`, `statix`, `openssl`, `uv`) and
exposes the full check suite as `devenv test`. `uv.lock` stays the Python
source of truth; devenv only wraps it. See `docs/development-environment.md`
and AUDIT Tier V. Don't propose Docker as the default dev runtime — the
app reads local dirs, OS keyring, and the browser OAuth flow.

## Architectural decisions you'll want to know before editing

These are the parts that tripped up prior sessions. Each has a "why" so you
can judge edge cases.

- **`_open_db()` is a contextmanager**, not a `(settings, conn)` tuple. Use
  `with _open_db() as conn:`; for writes, `with _open_db() as conn, conn:`
  (the inner `conn` is sqlite3's transaction context). Defined in
  `src/finance/cli.py`; the underlying `store.open_db(db_path)` lives in
  `src/finance/db/store.py`. See AUDIT Tier B (commit `e4dd345`).

- **Category precedence** (highest wins): `tx_overrides` → `merchants.category`
  with `source='user'` → curated seed YAML (`data/merchants_seed.yaml`) →
  regex rules (`~/.config/finance/rules.yaml`) → LLM → NULL. The write-side
  precedence is in `analysis/classify.py:classify_merchant`; the read-side
  COALESCE is in `analysis/io.py:load_transactions`.

- **`finance recategorize` is gone/deprecated.** Redirect users to
  `finance analyze enrich --reenrich`, which preserves user overrides and
  rebuilds merchant/category/stream state through the current enrichment path.

- **Prompt caching is turned off.** Anthropic's per-block minimum
  (~1024 on Haiku, ~2048 on Sonnet) is larger than this repo's system
  prompts, so `cache_control: ephemeral` was silently ignored — 94 LLM runs
  in the real DB showed zero cache hits. The cache telemetry columns were
  removed; if you grow a system prompt past the threshold, the marker is easy
  to add back in `llm/client.py`. See AUDIT Tier D.

- **`LLMClient` is wrapped with `instructor`** (`Mode.TOOLS`). Don't revert
  to native `anthropic.messages.parse()` — we chose instructor for
  automatic retry on 429/529/validation failures. `parse_structured`
  returns a typed `ParsedResult[T]`. `ClaudeCLIProvider` is independent and
  subprocess-based; it also returns `ParsedResult[T]` by duck-typing. See
  AUDIT Tier G (commit `2aab8bc`).

- **Taxonomy drift guard.** Categories live in
  `src/finance/llm/prompts/taxonomy.yaml` (21 entries). `analysis/totals.py`,
  `analysis/streams.py`, `analysis/io.py` hardcode subsets (essentials,
  optionals, non-subscription, non-spend). They assert at import via
  `finance.taxonomy.assert_subset_of_taxonomy(...)`. A rename in the YAML
  without updating the subset sets will raise `AssertionError` before any
  test runs.

- **Advise subsystem is one module.** `src/finance/llm/advise_dispatch.py`
  holds all three kinds (subscription_overlap / cutback / integral_offer)
  via an `ADVISORY_KINDS` registry + typed wrappers. `run_advisory` in
  `advise.py` owns cache-lookup / LLM-call / persist / llm_runs logging.
  Per-kind code is just `_build_rows_*` + a Pydantic response schema.

- **`accounts exclude` flag for non-spend accounts.** Joint savings,
  brokerage, or anything that shouldn't pollute spending totals can be
  flagged via `finance accounts exclude <uid>`; pass `--spend-only` to
  trend / merchant analyses to drop them. Stream rollups now also require an
  included EUR transaction for the stream so overview/subscriptions/forecast
  stay aligned with the spend-only policy.

- **DB connections are tuned + long jobs are locked (Tier R/S/T).**
  `store.connect()` in `src/finance/db/store.py` applies `foreign_keys=ON`,
  `journal_mode=WAL`, `busy_timeout=5000`, `synchronous=NORMAL`. Tests that
  open SQLite via raw `sqlite3.connect` bypass those — fine for unit logic,
  not for concurrency. Long-running ops (sync, reenrich, llm-categorize)
  acquire a row in the `job_locks` table with owner + expiry; reuse that
  pattern instead of inventing a new overlap guard. Additive schema changes
  go through the `schema_migrations` registry in the same module — don't
  add free-standing `ALTER TABLE` calls.

- **Web dashboard is locked down (Tier U).** `finance serve` issues a
  random dashboard token (HttpOnly SameSite cookie), enforces CSRF +
  same-origin on unsafe methods, sets a strict CSP, and serves only local
  `web/static/app.css` + `app.js`. Don't reintroduce inline `<script>`,
  inline `onclick=`, inline `style=`, or CDN URLs — the CSP will block
  them in the browser even though tests may pass. ASGI tests need to
  round-trip the token cookie + CSRF header (see `tests/test_web_flow.py`).

- **Typer `B008` suppression**. `typer.Argument(...)` / `typer.Option(...)`
  as function defaults is the canonical typer idiom. `pyproject.toml`
  already extends `flake8-bugbear.extend-immutable-calls` — don't
  "fix" this pattern.

## Testing conventions

- **`tests/conftest.py`** exposes `cli_db` fixture that redirects
  `FINANCE_DATA_DIR` to `tmp_path` via `monkeypatch.setenv` and yields
  `(conn, db_path)`. Use it for any `CliRunner`-based test — the CLI's
  `_open_db()` will pick up the redirected path.
- **Non-CLI tests** open a local SQLite file directly via
  `store.connect(tmp_path / "x.db")` + `store.init_schema(conn)` and seed
  with raw `INSERT`s. Many existing tests follow this pattern; `test_sync.py`
  has a reference `_seed_session` helper.
- **LLM tests** subclass `LLMClient` or `ClaudeCLIProvider` with a
  `ScriptedClient` / `FakeClient` pattern (see `test_llm_advise.py:33` and
  `test_llm_categorize.py:27`). They return pre-canned `ParsedResult`
  envelopes; no network calls.
- **`.env*` files are permission-blocked** from read/write by default; if
  the user needs something added to `.env.example`, suggest they run a
  shell one-liner instead of asking Claude to edit it.

## Audit log — `AUDIT.md`

A durable record of every quality / hygiene / refactor pass with findings,
decisions, and fix-commit shas. Keep it append-only; use the two-commit
pattern:

1. The code change itself (e.g. `refactor(llm): ...`).
2. `docs(audit): record Tier X landing (...)` — updates the AUDIT.md
   section with the sha from step 1.

Tier F items in AUDIT.md are explicitly deferred — don't churn on them
unless a pass surfaces them as load-bearing.

## Residual lint noise that's intentional

`uv run ruff check` reports ~100 × E501 (line-too-long). Those are long
SQL strings, URLs, and prompt templates that `ruff format` can't break.
They are accepted; don't sprinkle `noqa` on them.

## When to reach for plan mode

The user prefers `EnterPlanMode` for any multi-file refactor, any change
with >10 call sites, and anything that touches `cli.py` broadly. Small
localized fixes (a single function, a typo, a config tweak) don't need
it.

## Out of scope — don't propose these

- Adding type stubs for `pandas` / `yaml` / `pyrage`. `[tool.mypy]` already
  silences them; installing stubs adds ~50 MB for no runtime benefit.
- Putting `.claude/settings.local.json` into git. It's user-personal,
  gitignored by design.
- Multi-tenant auth, cloud deployment, public hosting. This is a
  single-user self-hosted tool — security posture assumes localhost.
