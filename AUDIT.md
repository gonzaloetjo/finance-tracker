# Audit — 2026-04

Rolling record of the audit started after the initial commit (`9e4d732`).
Append-only during the audit. Each pass commits its own section plus its
fix commit(s); the sha of the fix commit is recorded under `Changes`.

Ephemeral in-session progress lives in Claude Code's task list; this file
is the durable counterpart.

> **Note on commit shas in this public mirror.** The shas referenced
> throughout this document point to the original private development
> repository, where the full per-tier commit history lives. The public
> mirror was created as a single squashed `Initial public release.`
> commit, so most historical shas (Tiers A–M) cannot be browsed in this
> repo's history. Tier N onward continues development directly in the
> public repo, but its tier sections still cite the private shas as the
> canonical reference for cross-document continuity.

---

## Pass 1 — hygiene (ruff + pytest + coverage)

### Findings (baseline: commit `9e4d732`)

**Tests:** 195 / 195 passing (`uv run pytest`). Suite is healthy.

**Ruff lint:** 244 violations, 62 auto-fixable. By rule:

| Count | Rule | Character |
|---:|---|---|
| 165 | E501 line-too-long | cosmetic |
| 22 | F401 unused-import | auto-fixable cleanup |
| 17 | UP017 `timezone.utc` → `UTC` | auto-fixable modernization |
| 8 | I001 unsorted imports | auto-fixable |
| 6 | UP035 deprecated-import | modernization |
| 5 | UP006 `List[X]` → `list[X]` | auto-fixable modernization |
| **4** | **B904 raise-without-from** | **real — drops tracebacks** |
| 4 | F541 empty f-string | auto-fixable |
| 4 | SIM117 nested `with` | cosmetic |
| 2 | E702 multi-statement line | cosmetic |
| 2 | F841 unused variable | cleanup |
| **1** | **B008 function-call-in-default-arg** | **typer false positive** |
| 4 | minor (B007, E402, SIM105, SIM108) | cosmetic |

**Ruff format:** 42 files would be reformatted (mostly `tests/`).

**Coverage:** 63% overall (3162 stmts, 1184 missed). Top-10 thinnest modules
(excluding 100%-covered package inits):

| Module | Stmts | Cover |
|---|---:|---:|
| `src/finance/cli.py` | 848 | **0%** |
| `src/finance/analysis/reports.py` | 31 | **0%** |
| `src/finance/web/tls.py` | 27 | **0%** |
| `src/finance/auth/keys.py` | 41 | 61% |
| `src/finance/analysis/recurring.py` | 50 | 68% |
| `src/finance/config.py` | 53 | 74% |
| `src/finance/web/app.py` | 102 | 78% |
| `src/finance/analysis/totals.py` | 103 | 79% |
| `src/finance/sync.py` | 88 | 80% |
| `src/finance/analysis/classify.py` / `subscriptions.py` | 43 / 103 | 81% |

**Observations:**
- `cli.py` at 0% coverage — no end-to-end CLI command is exercised by
  tests; this is the single biggest gap and likely the right place to
  invest once the audit is done.
- `reports.py` at 0% is a second-order effect: it's only imported from
  `cli.py`, which is itself untested.
- `tls.py` at 0% is acceptable (optional self-signed HTTPS path for the
  dashboard; not on the common path).
- `auth/keys.py` at 61% — key generation, age-encryption, and decryption
  are the exact things you want tested before trusting long-lived
  secrets. Worth raising.

### Decisions
- `pytest-cov>=5` added to dev deps so `--cov` is reproducible.
- Cosmetic + modernization violations (165 E501, 22 F401, 17 UP017,
  8 I001, 6 UP035, 5 UP006, 4 F541, 4 SIM117, 2 F841, 2 E702, 4 misc)
  fixed mechanically via `ruff check --fix` + `ruff format`. No
  semantic risk.
- 4× B904 fixed case-by-case:
  - CLI error paths (`cli.py:1321, 1346, 1349`) → `raise typer.Exit(code=1) from None`
    — CLI users don't want traceback chains; the echo above already
    carries the message.
  - Dashboard route (`web/dashboard.py:236`) → `raise HTTPException(400, str(e)) from e`
    — preserves the chain for server-side logs.
- 1× B008 at `cli.py:111` is a typer idiom false-positive (this is the
  documented way to declare typer arguments). Silence by extending
  ruff's `flake8-bugbear.extend-immutable-calls` in `pyproject.toml`
  rather than contorting the code.

### Changes
- `5f815f9` — hygiene pass fixes (ruff autofix + format, B904 × 4, B008 config, pytest-cov dep).

### Remaining ruff noise (intentionally deferred)
- 100 × E501 line-too-long — surviving long lines are single strings
  (SQL, URLs, prompts) that ruff format can't break. Not worth noqa
  clutter; accept as-is.
- 2 × F841, 1 × B007 (unused var / loop var) — touched in pass 2/4 if
  simplify flags the surrounding code.
- 1 × E402, 1 × SIM105, 1 × SIM108, 1 × SIM117 — minor, revisit when
  simplify runs.

---

## Pass 2 — Anthropic skills audit

Three parallel read-only subagents: a `general-purpose` security audit of
the full tree (the `/security-review` skill needs a remote origin to
diff against, which we don't have), plus two `code-simplifier` agents,
one on `src/finance/cli.py`, one on `src/finance/llm/`.

### Findings (baseline: commit `b678af0`)

#### Security — no critical findings for single-user self-hosted threat model
- **1.1 Medium — `.env` passphrase exposure** (`config.py:27`). `FINANCE_KEY_PASSPHRASE` can be read from a `.env` file. `.gitignore` already excludes `.env`; gap is that `.env.example` doesn't warn against setting this field.
- **2.1 Low — `_ensure_column` f-string SQL** (`db/store.py:38,40`). `PRAGMA table_info(...)` and `ALTER TABLE ... ADD COLUMN ...` cannot use placeholders. All callers pass hardcoded strings today. Adding a table-name allowlist hardens the pattern against future misuse.
- **4.1 Low — no allowlist on `callback_url`** (`app.py:89`, `config.py:57`). Single-user self-harm vector; defence-in-depth would enforce `localhost` / `127.0.0.1`.
- **4.3 Low — no CSRF token on HTMX mutating routes**. Accepted risk for localhost-only; worth adding a `X-Requested-With` check if the dashboard is ever exposed over TLS.
- **6.1 Info — OAuth state validation correct** (`secrets.token_urlsafe(32)`, one-time-use pop pattern).
- **7.1 Info — subprocess calls use `shell=False`** (`llm/providers.py:101`). No injection vector.
- **8.1 Info — dependency surface OK**. `uv.lock` committed; audit on future updates.
- _(Other findings low/info — see agent output.)_

#### CLI (`src/finance/cli.py`, 848 stmts, 0% coverage)
- **High — `_fmt` / `_advise_fmt` are duplicates** (`cli.py:470, 1452`). Both call `fmt_from_flags`. Delete `_advise_fmt`, redirect four call sites.
- **High — `_fmt(csv, json_)` resolved twice per command** across ~7 `analyze_*` handlers. Small helper (`_emit_table(df, fmt, summary)`) would absorb the pattern.
- **High — `_open_db()` is a latent footgun** (`cli.py:440-444`). Returns `(settings, conn)` but (a) doesn't call `store.init_schema`, (b) callers inconsistently wrap in `with conn:`. 28 call sites. Converting to a `@contextmanager` that also auto-inits schema and accepts an optional `db_path` unlocks in-process testing for every command.
- **Medium — `_make_llm_client` catches bare `Exception`** (`cli.py:1371-1374`). Narrow to `AuthenticationError | httpx.ConnectError | ValueError`.
- **Medium — inconsistent guard message** between `serve` (says `'finance import-key'`) and `_load_client` (says `'finance init'`) (`cli.py:281` vs `cli.py:52`). The import-key flow is canonical; `_load_client` should match.
- **Low — `split not warranted`**. Typer sub-apps already provide logical grouping; a file split would add a `_common.py` helper import without reducing complexity. Keep as one file.

#### LLM modules (`src/finance/llm/`)
- **High — `advise_*` entry-point duplication ~108 lines across 3 modules.** The real abstraction already exists in `run_advisory` (`advise.py`); duplication is in thin wrappers. Consolidation is tidying, not structural. Low priority unless a fourth advisory is about to land.
- **Medium — taxonomy drift risk.** `prompts/taxonomy.yaml` lists 21 categories; `analysis/totals.py:20-42`, `analysis/streams.py:30-37`, `analysis/io.py:20` hardcode subsets. A category rename in `taxonomy.yaml` would silently stop matching. A single module-import-time assertion (`set(ESSENTIAL) | set(OPTIONAL) <= set(load_taxonomy())`) catches drift.
- **Medium — `APIProvider` is a premature wrapper** (`providers.py:28-37`). Four lines, exists only to expose `.name = "api"` for a single branch in `categorize.py:193`. Could be inlined; `ClaudeCLIProvider` stays as-is (genuinely different).
- **Medium — no 429/529 retry**. Defensible given the `advice` cache is idempotent (re-running resumes from the cache), but it's implicit. Worth a one-line docstring comment in `advise.run_advisory` rather than code.
- **Info — prompt-cache prefix stable for advisories.** All three advisories use static system prompts. `categorize.py` does interpolate `taxonomy.yaml` into the cached prefix — intentional (taxonomy edit should invalidate cache) but worth noting.

### Decisions

Tiered fix plan (each tier a separate commit to keep review tractable):

**Tier A — cheap, clear value, low risk (one commit):**
1. Delete `_advise_fmt`, redirect four call sites to `_fmt`.
2. Inline `APIProvider` — delete the class, pass `LLMClient` directly. `providers.py` keeps `ClaudeCLIProvider` + `make_provider`.
3. Add taxonomy-drift assertion. Create `llm/taxonomy.py` with `load_taxonomy_set()`; import from `totals.py`, `streams.py`, `io.py` and assert subset at module import.
4. Add table-allowlist assertion to `_ensure_column`.
5. Fix `_load_client`'s guard message to reference `finance import-key` (the canonical flow; `finance init` is the fallback per `feedback_enable_banking_flow.md`).
6. Update `.env.example` to call out that `FINANCE_KEY_PASSPHRASE` must never be committed.

**Tier B — testability unlock, bigger surface — plan mode for this one (separate commit):**
7. Refactor `_open_db()` → `@contextmanager` yielding `(settings, conn)`, auto-init schema, accept optional `db_path`. 28 call sites to update.
8. Narrow `except Exception` in `_make_llm_client`.
9. (Follow-on to #7) Add a minimal `CliRunner`-based test covering one read command (e.g. `accounts ls`) and one write command (e.g. `label`) to prove the refactor unlocked testing.

**Tier C — defer:**
10. `_fmt` double-call consolidation into `_emit_table` — wait for Tier B so the helper can live alongside the refactored `_open_db`.
11. `advise_*` entry-point consolidation — revisit if a fourth advisory is added.
12. `callback_url` localhost allowlist — defence-in-depth, not load-bearing.
13. CSRF token on HTMX routes — only if dashboard is exposed beyond localhost.
14. One-line docstring note on no-retry behaviour in `advise.run_advisory`.

### Changes
- `5f5d238` — Tier A fixes: dedupe `_fmt` / `_advise_fmt`, inline
  `APIProvider`, new `finance.taxonomy` with import-time drift guards
  wired up in `analysis/{totals,streams,io}.py`, `_ensure_column` table
  allowlist, `_load_client` guard message updated to reference
  `finance import-key`.

### Tier-A deferred (user-side)
- `.env.example` warning about `FINANCE_KEY_PASSPHRASE`: Claude Code's
  permissions block access to `.env*` files in this session. One-liner
  the user can run:
  ```sh
  cat >> .env.example <<'EOF'

  # SECURITY: if set, never commit this file. .gitignore excludes `.env`,
  # but double-check before sharing a snapshot.
  # FINANCE_KEY_PASSPHRASE=
  EOF
  ```

### Tier B — landed in `e4dd345`
- `store.open_db(db_path)` @contextmanager + `cli._open_db()` thin wrapper.
  Migrated all 30 `_open_db` callers + 3 bypassers (`sync` / `list` /
  `recategorize`) — 33 sites total. Compact `with _open_db() as conn, conn:`
  form for writers; narrow `with _open_db() as conn:` scope on leakers.
- `_make_llm_client` `except Exception` → `except anthropic.AnthropicError`.
- New `tests/conftest.py` with `cli_db` fixture (redirects
  `FINANCE_DATA_DIR` to `tmp_path`, first `CliRunner` infra in the repo).
- New `tests/test_cli_smoke.py` with 3 tests — empty-DB, read path
  (`accounts ls`), write path (`label`).
- **Result:** 198 / 198 tests pass; `cli.py` coverage 0 % → 18 %.
- **Out of scope follow-ups** (for Pass 4): `web/*` DB helpers still use
  their own patterns; `advise_*` transaction semantics during LLM calls
  unchanged.

### Tier C — deferred indefinitely
- `_fmt` double-call consolidation — wait until Tier B reshapes the
  helpers.
- `advise_*` entry-point consolidation — revisit only if a fourth
  advisory is added.
- `callback_url` localhost allowlist, CSRF `X-Requested-With` check,
  `advise.run_advisory` no-retry docstring — all low / info.

## Pass 3 — community tools (mypy, vulture, pip-audit)

Added as dev deps: `mypy>=1.11`, `vulture>=2.11`, `pip-audit>=2.7`. All
three are standard, PyPA-adjacent static-analysis tools (pip-audit is
maintained by PyPA directly) and execute read-only on local code / the
locked dep graph.

### Findings (baseline: commit `ab8918f`)

#### pip-audit — clean
`uv run pip-audit --skip-editable` → **No known vulnerabilities found.**
Full transitive dep tree from `uv.lock` audited against the PyPA
advisory database. Our own `finance` editable package is skipped (it's
not published, so there's nothing to audit against PyPI). Re-run on
every `uv lock` update.

#### vulture — 1 finding
`src/finance/web/app.py:103` — unused variable `state_` in the
`/callback` FastAPI handler. The parameter is declared but the code
reads `request.query_params.get("state")` directly (the underscore
alias was added because `state` clashes with FastAPI's `app.state`).
Fix: rename to `_state` (signals intentional unused) or delete.

#### mypy — 23 errors, 5 real
17 / 23 are `import-untyped` on `pandas`, `yaml`, `pyrage` — missing
library stubs, noise. Fix: add `[tool.mypy]` config to `ignore_missing_imports = True`
for those three packages (installing `types-PyYAML` / `pandas-stubs`
as dev deps is the alternative but adds ~50 MB).

**Real typing issues:**
- `src/finance/llm/client.py:118` — `system` arg to `Messages.parse` is typed
  `list[dict[Any, Any]]`; SDK expects `list[TextBlockParam]`. Cached-prefix
  code still works; fix is a proper type import from
  `anthropic.types`.
- `src/finance/cli.py:292` — `EnableBankingClient(app_id=cfg.app_id, ...)` —
  `cfg.app_id` is `str | None`, constructor expects `str`. Runtime is
  guarded by `if not cfg.app_id: raise` earlier in `serve()`, but mypy
  can't see across the closure. Fix: add a narrowing `assert
  cfg.app_id is not None` before the `client_factory` def.
- `src/finance/web/dashboard.py:253` (really line 255 — `redact_key(str(e))[:240]`):
  `redact_key` is typed `str | None → str | None`. The call always passes a
  `str`, so the result is always `str` in practice, but mypy sees the
  union. Cheapest fix is a `# type: ignore[index]` on the line; cleanest
  is an `@overload` decorator on `redact_key`.
- `src/finance/cli.py:1429-1430` — mypy confused by a loop variable `e`
  reusing the name of an earlier `except … as e:`. Fix: rename the loop
  variable to `err` (Python deletes except-vars on block exit, so it's
  real dead-code confusion, not a bug).

### Decisions

Tier A-light (one commit):
1. Delete / rename unused `state_` in `web/app.py`.
2. Rename `for e in summary.errors:` → `for err in summary.errors:` in
   `cli.py:1429-1430`.
3. Add `assert cfg.app_id is not None` before `client_factory` def in
   `cli.py` serve command.
4. Fix `llm/client.py:118` — import `TextBlockParam` from
   `anthropic.types` and annotate `system_blocks`.
5. Add a single `# type: ignore[index]` on `redact_key(...)[...]` call
   in `dashboard.py` (overload would be principled but is more code for
   the same outcome).
6. Add `[tool.mypy]` to `pyproject.toml` silencing
   `ignore_missing_imports` for `pandas`, `yaml`, `pyrage`.

### Changes
- `92256f2` — mypy/vulture/pip-audit added to dev deps, `[tool.mypy]`
  override for pandas/yaml/pyrage, 5 real typing fixes (TextBlockParam
  annotation, `cfg.app_id` narrowing, `e` loop-var rename, redact_key
  type-ignore), dropped unused `state_` param in `/callback`. mypy +
  vulture + pip-audit all clean; 198 / 198 tests still pass.

## Pass 4 — architecture review

Two parallel Explore subagents on code questions, plus one inline probe
against the user's real `llm_runs` table for the cache-hit question.

### Findings (baseline: commit `eee872a`)

#### Q1 — category-precedence chain testability (LOW-MEDIUM)

The precedence chain described in the README (`tx_overrides → merchants.category
(source='user') → curated seed → regex rules → LLM → NULL`) is split across
two sites, not one function:

- **Read-time** — SQL `COALESCE` + `CASE` inside `load_transactions`
  (`src/finance/analysis/io.py:79-85`) handles levels 1 (`tx_overrides`),
  2 (`merchants.category`), and 5 (legacy `transactions.category`).
- **Write-time** — `classify_merchant` (`src/finance/analysis/classify.py:43-85`)
  enforces levels 2-4 (user → curated → rule) and short-circuits on
  `source='user'` at line 64.

Test coverage is actually decent:
- `tests/test_analysis_io.py::test_category_resolution_precedence` (line 65)
  exercises all three SQL layers simultaneously, confirms
  `tx_overrides` wins.
- `tests/test_analysis_classify.py::test_user_source_never_overwritten` (line 21)
  verifies user beats seed + rule.
- `tests/test_analysis_enrich.py::test_tx_overrides_survive_reenrich` (line 237)
  confirms `tx_overrides` survives `--reenrich`.

**Gap:** no single test exercises all five layers simultaneously (tx with
`tx_override` + merchant with `source='user'` + seed entry + rule match +
legacy `transactions.category`). Pair-wise wins are covered; full-stack
layering is only implied. **Proposed fix (low priority):** extract the
`COALESCE` + `CASE` into a `category_precedence_sql()` helper so the fragment
has a name (and can be referenced from a single-test assertion).

#### Q2 — `advise_*` duplication after Tier A (MEDIUM)

Three entry-point files (`advise_subscriptions.py`, `advise_cutbacks.py`,
`advise_integral.py`) are ~97 lines each, with ~63-66 lines structurally
identical across all three. The duplication lives in:

- Import blocks (7-8 lines each, mostly same).
- `_load_system()` (2 lines each, only filename string differs).
- `KIND` constant (1 line each).
- Entry-point function body (~20 lines each, byte-for-byte identical except
  function name and `cutbacks`'s `months` kwarg).

The only genuinely per-module code is `_build_input_rows()` (30-37 lines) and
the Pydantic schema (~10 lines). `run_advisory`'s signature is stable across
all three callers. **Proposed fix:** consolidate into a single `advise_dispatch.py`
with an `ADVISORY_KINDS` registry keyed by kind string, values holding
`(build_rows_fn, schema, prompt_filename)`. Estimated reduction: 291 lines →
~165 lines, **~125 LoC / ~43%**. `months` kwarg for cutbacks handled via
`functools.partial` or optional dispatcher kwarg.

#### Q3 — prompt caching is not working (HIGH)

Queried `llm_runs` on the user's real DB (`~/.local/share/finance/finance.db`):

- **94 total runs, 0 cache_read_tokens, 0 cache_creation_tokens.** Ever.
- categorize: 84 OK runs, 196 052 input tokens, cache values all 0.
- advise_* (subscription_overlap / cutback / integral_offer): 1 run each, cache
  values 0.

The README promises "`cache_read_tokens > 0` on the second run of any given
`kind`" — this assertion has never held on this DB. Root-cause hypothesis:
Anthropic's prompt-cache minimum is ~1 024 tokens for Haiku, ~2 048 for
Sonnet. Our system prompts (the taxonomy + instructions in
`llm/prompts/*.md`) are well below those thresholds, so
`cache_control: {"type": "ephemeral"}` is silently ignored by the API. The
entire caching machinery in `llm/client.py:111-113` is a no-op in practice.

Alternative root cause: `messages.parse()` (beta API) may not surface
`cache_read_input_tokens` / `cache_creation_input_tokens` on its response —
but the cleaner way to discriminate would be to probe with a deliberately
padded prompt (>= 2 048 tokens) and re-check.

**Proposed fix:** drop the caching layer (`cache_system` parameter,
`cache_control` block, cache_read/cache_creation columns reported in
`--dry-run` output) and remove the README assertion, OR pad the system
prompts past the threshold (adds token cost on first call but saves on
subsequent). Given the advisories run at most a few times per week, the
simplest right answer is **drop the caching machinery entirely** — the
telemetry shows it has never provided value.

#### Q4 — `sync.recategorize_all` vs `analyze enrich --reenrich` (HIGH)

Silent data disagreement between two recategorize paths:

- `recategorize_all` (`src/finance/sync.py:161-175`) writes to
  `transactions.category` (the legacy column, lowest-precedence fallback in
  `load_transactions`). Uses the old `categorize()` function on raw
  `creditor_name` / `debtor_name` / `remittance_info`.
- `analyze enrich --reenrich` writes to `merchants.category` +
  `merchants.category_source` via `classify_merchant`. `load_transactions`
  prefers `merchants.category` over `transactions.category`.

**User impact:** running `recategorize` updates `transactions.category` but
the dashboard/analyses read `load_transactions`, which ignores that write
whenever a merchant-level category exists. It looks like a no-op to the user.

`tx_overrides` are preserved by both paths, but only by accident — via the
SQL `COALESCE` ordering in the view, not by any deliberate check in
`recategorize_all`.

The README's "Category precedence" section (`README.md:141`) documents only
the `classify_merchant` path — i.e. `enrich --reenrich` is canonical;
`recategorize` is legacy. **Proposed fix:** deprecate the `recategorize` CLI
command with a typer.echo warning redirecting to `analyze enrich --reenrich`.
Optionally keep `recategorize_all` internally as a migration utility (not
in scope for this pass).

### Decisions — Tier D (Pass 4 fixes)

Ranked by severity:

**HIGH — both land in one commit:**
1. Drop the caching machinery (`cache_system` param in `parse_structured`,
   `cache_control` block, and the `cache_read_tokens` / `cache_creation_tokens`
   columns' presence in `enrich llm-categorize` output / README claim). If
   you want to keep the DB columns for future use, just stop populating
   them and remove the README "Expect `cache_read_tokens > 0`" paragraph.
2. Deprecate `finance recategorize` CLI command — emit a warning pointing
   at `finance analyze enrich --reenrich`, then exit. Keep
   `sync.recategorize_all` internally (used by test_sync.py). Update the
   README's `rules init` onboarding paragraph.

**MEDIUM — plan mode territory, separate commit:**
3. Consolidate `advise_*` to a single `advise_dispatch.py` with a registry.
   Saves ~125 LoC. Bigger refactor; plan mode.

**LOW — defer:**
4. Extract `category_precedence_sql()` helper. One function, small. Either
   bundle with #1/#2 or defer indefinitely.

### Changes
- `0adb89a` — Tier D: removed the `cache_control` ephemeral block +
  `cache_system` parameter (the cache has never been hit in 94 runs —
  prompts below the per-block minimum); `llm_runs` schema columns kept
  for forward compatibility. Deprecated `finance recategorize` CLI
  command — now emits a warning redirecting to `analyze enrich --reenrich`
  and exits 1. README "Cost + cache observability" section rewritten to
  reflect reality.

### Tier E — landed in `11f4e20`
- Consolidated `advise_subscriptions.py` + `advise_cutbacks.py` +
  `advise_integral.py` into `advise_dispatch.py` with an
  `ADVISORY_KINDS` registry, one `advise(conn, kind, ...)` dispatcher,
  and three typed wrappers preserving the public API. `_build_rows_*`
  bodies stay per-kind (the only genuinely different code); Pydantic
  schemas moved verbatim. Caller migration: three lazy imports in
  `cli.py` + one consolidated import block in `tests/test_llm_advise.py`
  — no call-site changes.
- **Result:** 280 → ~200 lines in the advise module (~80 LoC removed,
  ~28 %). Kept the typed wrappers so `months: int = 6` on cutbacks
  stays mypy-checkable — cost ~45 LoC of the theoretical max reduction,
  bought a pure import-path-only migration.
- 198 / 198 tests pass; mypy / vulture / pip-audit all still clean.

**Audit structured-fix queue: empty.**

---

## Tier G — `instructor` adoption (forward-looking, not an audit finding)

Added on the strength of a merit-only comparison after Tier E, not because
any pass flagged a defect. The decisive gain is automatic retry on
transient Anthropic failures (429 / 529) and schema-validation errors; the
user's `llm_runs` had 5 `status='error'` categorize rows that would likely
have recovered with retries. Secondary gains: shorter
`parse_structured` body (~35 → ~20 lines), generic-parametrized
`ParsedResult[T]` that removes `# type: ignore[assignment]` at both call
sites, no more `TextBlockParam` gymnastics.

Trade-off: left Anthropic's beta `messages.parse(output_format=schema)`
endpoint for `messages.create` + `instructor.Mode.TOOLS`. Adds
~150-200 tokens of tool-definition overhead per request (rounding error at
Haiku prices on the observed workload); loses automatic inheritance of
future `parse()`-beta features.

### Changes
- `2aab8bc` — one-module migration in `llm/client.py`. `LLMClient.__init__`
  now wraps `Anthropic(...)` with `instructor.from_anthropic(raw,
  mode=instructor.Mode.TOOLS)`; `parse_structured` calls
  `create_with_completion(response_model=schema, max_retries=2, ...)` and
  returns `ParsedResult[T]`. `ClaudeCLIProvider` unchanged. Tests still
  subclass `LLMClient` and override `parse_structured` — no behavioural
  change. 198 / 198 tests green; mypy / vulture / ruff clean.

### Caveat
Live smoke tests (`finance advise subscriptions --refresh` against the
real DB + the deliberate invalid-key retry test) were not run at commit
time; they require network + API credit. Run manually before trusting
the new path on production data.

### Tier G follow-ups (deferred, out of scope)
- Hook-based per-retry `llm_runs` logging via
  `client.on("completion:response", ...)`. Worth once retry behaviour
  has been observed in practice.
- `create_partial(response_model=...)` streaming for live advisory
  rendering in the web dashboard.
- Benchmark `Mode.ANTHROPIC_TOOLS` vs `Mode.TOOLS` for token cost on
  categorize batches.

### Tier F — deferred indefinitely
- Extract `category_precedence_sql()` helper in `analysis/io.py` —
  small, low-impact; bundle with another refactor if one touches this
  area.
- Add an end-to-end test exercising all five precedence layers
  simultaneously (tx with `tx_override` + merchant `source='user'` +
  seed entry + rule match + legacy `transactions.category`). Pair-wise
  coverage exists; full-stack is only implied.

---

## Pass 5 — reusability audit (for another local user)

Four parallel read-only `Explore` subagents on separate axes:
setup/onboarding friction, hardcoded FR/BNP assumptions, architectural
extensibility seams, redundancy / dead-code / stale fixtures. Framing
question: *could a friend clone this and run it?*

### Findings (baseline: commit `3391a8c`)

#### Setup (4/10) — technical friend could get it running, with blockers
- No `uv` install pointer in `README.md:10`; quick-start opens with
  `uv sync` with no fallback.
- Enable Banking "activation / self-whitelisting" step only documented
  inside an error handler (`src/finance/web/app.py:43-48`), not in the
  setup docs. First-connect 403s with no guidance.
- Redirect URI `http://localhost:8000/callback` (`src/finance/config.py:57`)
  must be pre-registered at EB — never stated in README.
- `keyring` silently no-ops on headless Linux (`src/finance/llm/client.py:76`)
  while README frames it as preferred.
- `rules_init` tells users to run the deprecated `finance recategorize`
  (`src/finance/cli.py:339`), contradicting the deprecation warning at
  `cli.py:357`.
- `finance init` post-run output and auto-migrating `init_schema` are
  genuinely polished by contrast.

#### Portability — BNP-FR only (7/10 for another BNP-FR user, 1/10 otherwise)
Five layers deeply hardcode BNP / FR / EUR:
1. Memo parser is BNP-format only (`src/finance/analysis/memo.py:1-69`).
2. Categorize system prompt names BNP Paribas + French merchants
   (`src/finance/llm/prompts/categorize_system.md:1,9-14`).
3. EUR-only SQL filters silently drop non-EUR rows
   (`src/finance/analysis/io.py:119`, `src/finance/analysis/alerts.py:74`).
4. Advisory prompt assumes FR provider market
   (`src/finance/llm/prompts/advise_integral_system.md:24`).
5. `--country FR` is the only documented example (`src/finance/cli.py:187`,
   `README.md:15`).

Things that ARE parameterized well: ASPSP selection via
`list_aspsps(country)` (`src/finance/eb/flows.py:19-25`);
`FINANCE_CONFIG_DIR` / `FINANCE_DATA_DIR` env overrides
(`src/finance/config.py:22-26`); `taxonomy.yaml` is universal; per-tx
`currency` is stored faithfully (the EUR filter is a downstream choice,
not a storage constraint).

#### Architecture (3/10) — single-user tool, not a platform
- No `BankClient` protocol. `EnableBankingClient` referenced across 5
  files (`sync.py`, `cli.py`, `web/app.py`, `db/store.py`, `eb/flows.py`).
- `db/store.persist_session(conn, SessionResponse)` takes `eb.models.SessionResponse`
  directly (`src/finance/db/store.py:75`) — the DB layer has a
  compile-time dependency on the Enable Banking domain model.
- `LLMClient` / `ClaudeCLIProvider` already duck-type on
  `parse_structured` (good), but `advise.py:26` and
  `advise_dispatch.py:30,256-280` pin the concrete `LLMClient` type — a
  third provider would need type-annotation changes at 4+ sites.
- No repository abstraction; raw `sqlite3.Connection` threaded
  everywhere; `db/store.py` uses SQLite-specific `PRAGMA` calls.

#### Redundancy (7/10) — mostly clean after A-G, a handful of loose ends
- `finance recategorize` still registered as a CLI command
  (`src/finance/cli.py:356-357`) even though it deprecation-warns and
  exits 1 — confusing in `--help`.
- Two enrich paths (`analyze enrich` and `enrich llm-categorize`) with
  no top-level explanation of the split.
- `cache_read_tokens` / `cache_creation_tokens` still written and
  printed (`llm/client.py:142-180`, `llm/categorize.py:224-225`) though
  the cache never fires (established in Tier D).
- **Personal data leaks in test fixtures** — real card-number
  fragments, family / friend names, and an employer string scattered
  across 10 test files and the `analysis/memo.py` comment / docstring.
  (Specifics redacted from this public-mirror copy of the audit.)
- `sandbox/mock_account.json` — reviewed. The placeholder name is
  generic, `BNPAFRPPXXX` is BNP's public BIC; not leakage. Leave as-is.

### Decisions

#### Tier H — personal-data sanitization (immediate, one commit)
1. Replace real card-number fragments across 10 test files +
   `analysis/memo.py` with neutral placeholders (zeros for digits;
   French generic placeholder names like `DUPONT JEAN`, `DURAND ANNE`,
   `ACME CORP` for human/employer strings).
2. Parser behavior is unchanged — `_CARTE_RE = r"\s+CARTE\s+\d{4}X{4,}(?:\d{2,4})?"`
   is already generic on digits, and the personal names appear only
   in fixture memos / assertions, never in parser logic.

#### Tier I — portability adaptation (plan-mode)
Scope: adapt for **general French usage** (not necessarily BNP). Will
require a plan before implementation because it touches:
- Memo parser generalization / provider-aware parsing.
- A `BankClient` (or equivalent) seam and decoupling `db/store` from
  `eb.models.SessionResponse`.
- Categorize / advisory prompts that name BNP directly.
- Setup friction items flagged above (README / EB registration docs /
  `rules_init` deprecation note / `uv` pointer).

#### Tier J — non-FR-non-BNP portability (out of scope for now)
EUR-only SQL filters, FR-scoped advisory prompts, `--country FR` default.
Not a near-term goal per user; noted for completeness.

### Changes
- `ff624c1` — Tier H: sanitized personal data from test fixtures +
  `analysis/memo.py` comment. Card-number fragments → neutral
  `0000XXXXXXXX0000` placeholder; 19-digit COMMISSIONS PAN → all-zeros;
  real family / friend names + an employer string → generic
  placeholders. 10 test files + 1 src file touched, no behavioural
  change, 198 / 198 tests pass.

### Tier I — landed in `99c6550` + `4e0a21a` + `b91a3b2` + `b0ff14f`

Scope: unblock any French retail bank customer (not just BNP) via
Enable Banking. Stays FR-only by design; non-FR users remain out of
scope (Tier J, deferred indefinitely).

- `99c6550` — Tier I.1 docs + cosmetic genericization: README
  headline + `uv` install pointer + new Troubleshooting section
  (EB 403 activation, redirect URI registration, keyring-on-headless
  caveat) + `.env` mention in Paths; `pyproject.toml` description;
  top-level CLI help; `rules init` follow-up now points at
  `analyze enrich --reenrich`; `recategorize` marked `hidden=True`;
  `sandbox/mock_account.json` BIC `BNPAFRPPXXX` → `MOCKFRPPXXX`.
- `4e0a21a` — Tier I.2 prompt cleanup: dropped `"(BNP Paribas)"` from
  `categorize_system.md:1` identity line. French merchant hints
  (Carrefour, Navigo, EDF, etc.) retained — valid for any FR bank.
- `b91a3b2` — Tier I.3 BankProfile abstraction: new
  `analysis/bank_profile.py` with `FR_BNP_PROFILE` /
  `FR_GENERIC_PROFILE` singletons + `from_aspsp_name` dispatcher +
  `get_account_profile(conn, account_uid)` helper.
  `parse_memo(memo, *, creditor_name=None, debtor_name=None,
  profile=FR_BNP_PROFILE)` — keyword-only defaults preserve every
  existing `parse_memo(memo)` call. Under FR_GENERIC, the four
  BNP-proprietary branches (`FACTURE`, `VIREMENT`, `VIR CPTE A CPTE`,
  `REMBOURST`) and the EB Mock ASPSP regex are skipped; `_parse_fallback`
  uses `creditor_name || debtor_name` (populated by EB from ISO-20022
  party fields) as `merchant_raw` so merchant normalization survives
  for non-BNP card debits. `enrich.py` widens its SELECT + caches
  profile per account_uid. 7 new tests in `tests/test_bank_profile.py`;
  198 → 205 passing.
- `b0ff14f` — Tier I.4 keyring headless fix: `config set-llm-key`
  catches `keyring.errors.KeyringError` and emits a concrete recommendation
  to `export ANTHROPIC_API_KEY=...` instead of raw traceback. Deviated
  from the plan's proposed client.py location to keep the library
  layer free of typer.

**Result:** non-BNP FR user portability score 1/10 → ≥ 7/10 (parser
no longer starves merchant table, LLM prompt unbiased, docs cover
setup). 205 / 205 tests pass; mypy / vulture / pip-audit clean.

### Tier I — caveat
End-to-end smoke test against a real non-BNP FR ASPSP (Crédit
Agricole, Société Générale, LCL, Boursorama) was NOT performed at
commit time — requires an actual non-BNP consent flow. The plan's
reasoning relies on (a) EB's ISO-20022 `creditor.name` / `debtor.name`
being populated for card debits at non-BNP banks, and (b) the pan-FR
SEPA prefix set (`PRLV SEPA`, `VIR SEPA`, `FRAIS`, `RETRAIT DAB`)
being emitted verbatim by those banks. Both are grounded in spec and
in per-bank memo sampling from the exploration phase, but neither is
empirically validated on this branch. Re-run the overview dashboard
after the first non-BNP sync on real data before trusting the
generic profile in production.

### Tier J — deferred indefinitely
Non-FR / non-BNP portability. Would require dropping the EUR-only
SQL filters (`analysis/io.py:119`, `analysis/alerts.py:74`),
parameterizing the advisory prompt's FR-provider assumptions
(`advise_integral_system.md:24`), and changing the `--country FR`
default in the CLI. Out of scope for the single-user-self-hosted
design; revisit only if there's a concrete user.

---

## Pass 6 — reusability audit follow-through

Re-ran the friend-clones-the-repo frame against `ccfcdf1` (the
Tier I landing point). Surfaced four small defects, none P1, all
fitting in one tier.

### Findings (baseline: commit `ccfcdf1`)

- **README quick start steers users down the non-default Enable Banking
  path.** `README.md:11-19` walked `finance init` (self-generate
  keypair → upload `public.crt` → `config set-app-id`), but the
  canonical EB flow is in-browser keygen → download `<app_id>.pem` →
  `finance import-key <path>` (which also infers `app_id` from the
  filename, so `set-app-id` becomes redundant). `import-key` was
  documented in `--help` and in the docstring at `cli.py:134-138`
  but never appeared in the README's main walk.
- **`.env.example` still lacks the Tier A "do not commit" warning on
  `FINANCE_KEY_PASSPHRASE`.** Tier A explicitly deferred this as
  user-side because Claude's permissions block `.env*` writes
  (`AUDIT.md:163-173`). Still outstanding.
- **Dead `sign` line at `cli.py:264`.** Both branches of
  `sign = "" if r["amount"] >= 0 else ""` assigned the empty string
  and the variable was unread. The `{amount:>10.2f}` format already
  carries the minus sign. One of the F841s deferred at Pass 1.
- **Two stale "BNP" comments** at `analysis/merchants.py:24` and
  `categorize.py:108-109` — described pan-FR-SEPA logic
  (city/country tokens, `/MOTIF SALAIRE` field) as BNP-specific,
  contradicting Tier I's generalization. Inspecting the regexes
  confirmed they were already bank-agnostic; the wording was just
  stale.

### Decisions

#### Tier K — onboarding polish (one commit)

1. README quick start: lead with `finance import-key`, demote
   `finance init` to a one-paragraph fallback for the upload-cert
   case. Drop the now-redundant `config set-app-id` step from the
   main flow (import-key writes app_id automatically at
   `cli.py:160-162`).
2. Delete the dead `sign` line in `finance list`.
3. Reword the two stale BNP comments — comment-only, no regex or
   behaviour change.

### Changes
- `8040685` — Tier K: README rewrite (`import-key` primary, `init`
  fallback paragraph), `cli.py:264` dead-var deletion, two BNP
  comment rewords. 205 / 205 tests pass; mypy / vulture clean;
  ruff F841 count 2 → 1.

### Tier-K deferred (user-side)
- `.env.example` warning about `FINANCE_KEY_PASSPHRASE`: Claude
  Code's permissions still block writes to `.env*`. Same one-liner
  as the Tier A note (`AUDIT.md:163-173`):
  ```sh
  cat >> .env.example <<'EOF'

  # SECURITY: if set, never commit this file. .gitignore excludes `.env`,
  # but double-check before sharing a snapshot.
  # FINANCE_KEY_PASSPHRASE=
  EOF
  ```

### Tool-state note (unrelated to Tier K)
- `pip-audit` newly reports CVE-2026-3219 on `pip` 26.0.1 with no
  fix version yet published. `pip` is bundled with the venv, not
  pinned in `pyproject.toml`; nothing to do until upstream releases
  a fix. Tier K's verification suite is otherwise clean (pytest,
  ruff non-deferred, mypy, vulture all pass).

---

## Tier L — polymorphic BankProfile (architectural cleanup)

Not a finding from any audit pass — surfaced in conversation when the
question was raised whether the Tier I `BankProfile` abstraction was
maintainable or just an `is_bnp` flag in disguise. It was the latter:
`bank_profile.py` was a one-field dataclass and `memo.py:69-96` had
five `if is_bnp and …` gates threading bank-specific branches through
one shared function. With two profiles it was workable; a third would
have made `parse_memo` genuinely tangled.

### Decision

Push memo-parser branches into per-profile data and rewrite the
dispatcher as a pure loop. After this tier, adding a new bank profile
is one tuple entry in `bank_profile.py` plus (if the bank has a novel
memo prefix) one new `_try_*` function in `memo.py` — no edits to
`parse_memo` itself.

### What changed

- **memo.py.** Each `_parse_*` became `_try_*` — same parsing logic,
  but each function checks its own prefix and returns `None` when the
  memo isn't its concern. The `_parse_fallback(memo, party, *,
  use_mock_aspsp: bool)` shape was split into two distinct fallbacks
  (`_fallback_party_only` and `_fallback_with_mock_aspsp`) so a
  profile picks the appropriate one by reference, not by a runtime
  flag. `parse_memo` collapses to: empty-memo guard → loop
  `profile.branches` → `profile.fallback`. Lazy-imports
  `FR_BNP_PROFILE` for the default to avoid a top-level cycle.
- **bank_profile.py.** `BankProfile` grew from `name: str` to
  `(name, branches: tuple[ParseBranch, ...], fallback: FallbackFn)`.
  `FR_BNP_PROFILE` and `FR_GENERIC_PROFILE` are now non-trivial
  constants assembled from the imported branches. The BNP-only
  branches (`_try_facture`, `_try_transfer`, `_try_virement`,
  `_try_rembourst`) are *literally absent* from
  `FR_GENERIC_PROFILE.branches` — generic dispatch cannot reach them.
  `from_aspsp_name` was rewritten as `_PROFILE_REGISTRY` — an ordered
  tuple of `(matcher, profile)` pairs. A future Crédit Agricole
  profile is one entry, not an `if/elif` edit.
- **tests.** 2 new polymorphism tests in `test_bank_profile.py`
  build ad-hoc `BankProfile` instances with arbitrary branch tuples
  and verify dispatch is genuinely data-driven (not BNP-cased).
  Existing 7 + 22 + orchestrator tests unchanged.

### Verification — BNP-no-regression smoke against the real DB

Pre and post the refactor I hashed the user's `tx_enrichment` table:

```
sha256(SELECT tx_id, txn_type, merchant_id, memo_merchant_raw
       FROM tx_enrichment ORDER BY tx_id)
= 62586cf9dc9878cd668dce614c7ce0967910e6f67b1156259c95de9468132e96
```

Identical pre and post (after `analyze enrich --reenrich`), 669 rows.
The user's BNP enrichment is bit-for-bit unchanged.

### Changes
- `a687879` — `refactor(analysis): polymorphic BankProfile — branches
  per profile, no is_bnp flag`. 207 / 207 tests pass; mypy / vulture
  clean. `git grep "is_bnp\|profile.name" src/` returns a single
  docstring hit ("There is no `is_bnp` flag in this module").

---

## Tier M — cruft removal (5 sub-commits)

Not a finding from any audit pass — surfaced when the user asked
for an honest list of places where the codebase still carried
weight purely to preserve a previous abstraction. Five items, each
independently visible, bundled into one tier with five sub-commits
because each is small enough to land in isolation but they don't
share scope.

### Findings (baseline: commit `f2d6f47`, the Tier L audit doc)

1. **`finance recategorize` chain.** Tier D (`0adb89a`) deprecation-
   warned the CLI command and exited 1, but the entire write path
   was still wired up: hidden CLI command, dead
   `sync.recategorize_all` (zero callers — even the
   `tests/test_sync.py` that Tier D claimed kept it alive had no
   recategorize test), `sync_account` writing to
   `transactions.category` at fetch time, `load_transactions`
   COALESCEing it as a `'legacy'` source. On the user's real DB
   the legacy source was used by zero rows.
2. **`cache_read_tokens` / `cache_creation_tokens` columns +
   writes.** Tier D removed the cache-control marker but kept the
   columns "for forward compatibility". Telemetry showed zero
   cache hits in 94+ `llm_runs` rows; `LLMUsage` fields, `log_run`
   / `finish_run` writes, the `--dry-run` echo, and the dashboard
   settings-page totals were all ceremony.
3. **`environment` config field.** `config.py:14-16` already
   documented that EB endpoints are identical for sandbox and
   production. The `Settings.environment` / `AppConfig.environment`
   fields, the `config show` echo line, and the `config set-env`
   CLI command had zero runtime effect.
4. **`parse_memo(memo)` default-profile lazy-import.** A concession
   to the 22 default-profile callsites in `tests/test_analysis_memo.py`
   that survived Tier L. The `profile=None` default forced a lazy
   `from finance.analysis.bank_profile import FR_BNP_PROFILE` inside
   `parse_memo` to dodge a circular import that existed only because
   of the default.
5. **`_overwritable_sql()` helper** (`enrich.py:273-276`) — built a
   SQL `IN (...)` clause from a Python tuple via runtime f-string
   join. Trusted input only, but exactly the f-string-SQL pattern
   Tier A hardened against in `_ensure_column`.

### Decisions

Five sub-commits, each green in isolation, plus the audit-doc
commit. Same multi-sub-commit pattern as Tier I.

### Changes
- `5850d7d` — `refactor(cli+sync+io): drop deprecated recategorize
  chain`. Hidden CLI command deleted; `sync.recategorize_all`
  removed; `sync_account` no longer writes
  `transactions.category` at fetch time (rules now apply only
  through `enrich_transactions` → `classify_merchant`); legacy
  COALESCE arm + `'legacy'` CASE branch removed from
  `load_transactions`; `transactions.category` column removed from
  schema (existing DBs keep it as inert tombstone). Real-DB
  `load_transactions` hash unchanged
  (`fa66161d5022eefb228759a958efd565e748921847a8f69ee8551c316ee61253`,
  663 rows; zero `source='legacy'` rows existed pre-change).
- `c1221bf` — `refactor(llm): drop dead cache_*_tokens columns +
  writes`. `LLMUsage` fields, INSERT/UPDATE columns, `--dry-run`
  echo, dashboard SELECT, totals CTE, both Jinja templates, and
  fixture `LLMUsage(...)` instantiations updated. `git grep
  "cache_read\|cache_creation"` returns zero hits in `src/` and
  `tests/`.
- `71d947c` — `refactor(config): drop dead environment field`.
  `Environment = Literal[…]` alias, both pydantic fields,
  `config show` echo, and `config set-env` command removed.
  Existing `config.toml` files with `environment = sandbox`
  load fine via pydantic's default `extra='ignore'`.
- `8064dd4` — `refactor(memo): make parse_memo profile= mandatory`.
  Default + lazy import dropped; 22 callsites in
  `tests/test_analysis_memo.py` and 1 in `tests/test_bank_profile.py`
  updated to pass `profile=FR_BNP_PROFILE` explicitly. The
  TYPE_CHECKING import is sufficient because `from __future__
  import annotations` defers all annotation evaluation. Real-DB
  `tx_enrichment` hash unchanged
  (`62586cf9dc9878cd668dce614c7ce0967910e6f67b1156259c95de9468132e96`,
  669 rows — same as the Tier L baseline).
- `4f4c9f8` — `refactor(enrich): _overwritable_sql helper → module
  constant`. Helper deleted; `_OVERWRITABLE_IN_SQL` precomputed
  once at module load; SQL strings use plain concat instead of
  f-string interpolation of a runtime-built fragment.

### Tier-M deferred (user-side) — supersedes Tier A and Tier K deferrals

Three `.env.example` edits accumulated across Tier A, Tier K, and
Tier M; Claude permissions block `.env*` writes. Single combined
script closes all three deferred items at once:

```sh
# 1. Drop the dead FINANCE_ENVIRONMENT line + its lead-in comment.
sed -i '/^# Enable Banking environment/,/^FINANCE_ENVIRONMENT=/d' .env.example

# 2. Append the SECURITY warning that Tier A / Tier K queued.
cat >> .env.example <<'EOF'

# SECURITY: if set, never commit this file. .gitignore excludes `.env`,
# but double-check before sharing a snapshot.
EOF
```

The historical Tier A note (`AUDIT.md:163-173`) and Tier K note
(`AUDIT.md:721-731`) remain unchanged per the append-only
convention. This Tier M note is the live reference.

### Tool-state note

`pip-audit` continues to flag CVE-2026-3219 on `pip` 26.0.1 (no
fix version published yet, same status as Tier K). Otherwise the
verification suite is clean: 207 / 207 tests pass after every
sub-commit; mypy / vulture / ruff non-deferred clean throughout.

---

## Tier N — post-creative-agent fixes

Three Explore agents (UX / fragility / test-coverage) reviewed the
post-Tier-M repo for "what could be better without over-engineering."
Synthesized output: ~21 candidate items. Honest filtering against the
over-engineering smell (defensive code for hypothetical failures,
cosmetic polish, tests for code that's already fine) cut that to 6
items. A spot-verification dropped one further: the agent's "weekly
subscription cost understated 4.3×" was a **false positive** —
`find_subscriptions` (`subscriptions.py:230`) filters to
`classification IN ('monthly', 'quarterly', 'annual')`, so the
fallthrough at `subscriptions.py:180` is unreachable dead code, not
a live bug. Real Tier N is 5 items, three of which are behavioural
fixes and two are doc fixes.

### Findings

1. **`finance sync` exits 0 even when every account errored**
   (`cli.py:225-231`). A user wrapping `finance sync` in cron /
   systemd sees "success" while data has stopped flowing. Errors
   echoed to stderr, but the process never raised.
2. **`enrich_transactions` exception inside `sync_all_accounts`
   escapes unhandled** (`sync.py:151-156`). `sync_account` already
   committed per-account, so the fetched transactions were durable;
   but a crash in the auto-enrich call surfaced as an unhandled
   traceback exit 1, with no `sync_runs` "error" row to indicate
   what went wrong.
3. **Unguarded `fetchone()[0]` in `enrich.py:128-132`.**
   `normalize_merchant` always inserts the row immediately before
   the canonical-name query, so this never returns `None` today —
   but if it ever does, the unguarded `[0]` IndexErrors out of the
   entire `enrich_transactions` call, silently rolling back every
   other tx_enrichment write in the batch. Defensive, theoretical
   today, low cost.
4. **README quick-start omits `analyze enrich`** between `sync`
   and `list` (`README.md:24-31`). First-time user sees an
   unenriched dashboard on first `analyze overview` and thinks the
   tool is broken.
5. **`CLAUDE.md:26` references `finance overview`** which doesn't
   exist. Real command is `finance analyze overview`.

### Decisions

Three commits in canonical, then the same 5 fixes propagate to the
public mirror. Behaviour fixes (1+2+3) bundle into one commit
because they share a verification path; docs (4+5) ship together;
audit doc is its own commit per convention.

### Changes (canonical shas, mirrored 1:1 here)
- Behaviour: `8a7e5e6` — `fix(sync+enrich): exit code on error,
  wrap auto-enrich, defensive guard`. Items 1 + 2 + 3, plus two new
  tests. 203 / 203 tests pass; mypy / vulture / ruff non-deferred
  clean.
- Docs: `27097b0` — `docs(readme+claude): fix overview command +
  add enrich to quick-start`. Items 4 + 5.

### Verified-out (skipped after honest filtering)

- ~~Weekly subscription cost understatement~~ — false positive.
- `import-key --delete/--keep` flip — security-incorrect.
- `enrich_transactions` `conn.commit()` inside outer `with conn:` —
  design-smell, not actively breaking.
- Dashboard active-page nav highlight — pure cosmetic.
- `sessions ls` account-count column — N+1 query, polish.
- `streams.py` malformed-date logging, NaT booking-date `dropna`,
  forecast non-ISO date handling — all defending against
  hypothetical EB output not seen in practice.
- Various agent test-coverage suggestions — would test code paths
  that are fine; only the two tests bundled with items 1 + 2 above
  are warranted.

---

## Tier O — verified audit-report triage

Reviewed the local `audit-reports/*.md` files against the current public
tree instead of treating them as source of truth. Several report claims
were already fixed or superseded by this append-only audit log
(`CLAUDE.md` overview command, `.env.example` passphrase warning,
cache / recategorize cleanup, weekly subscription savings). The live
issues were correctness/security/doc/test-harness items.

### Findings

1. **Overview MTD + monthly chart ignored excluded accounts**
   (`store.month_to_date_totals`, `store.monthly_series`). The header
   totals used spend-only analysis, while dashboard MTD/monthly still
   counted savings / investment accounts.
2. **LLM categorize could overwrite regex-rule categories**
   (`llm/categorize.py`). `category_source='rule'` rows were selected
   for LLM categorization and allowed through the auto-write guard.
3. **JWT bearer assertions lacked `jti`** (`auth/jwt.py`). Tokens were
   reusable for their full TTL if intercepted.
4. **Enable Banking error strings embedded raw response bodies**
   (`eb/client.py`). Long bodies and account identifiers could be
   copied into terminal logs.
5. **Dashboard DB helper did not close connections** and
   `merchants_page` opened three connections for one request.
6. **Dead store helpers referenced removed `transactions.category`**
   (`top_categories`, `recent_transactions`).
7. **README was stale** on web LLM buttons, route table, phase labels,
   subscription category gate wording, and spend-only defaults.
8. **Starlette `TestClient` hung in this environment** on web route
   tests. Route logic itself passed through direct ASGI execution.

### Changes

- `aa610b0` — `fix(audit): address verified correctness and security findings`.
  Added spend-only filtering to dashboard aggregate helpers; preserved
  rule/curated/user category precedence in LLM categorize; added JWT
  `jti`; redacted/truncated EB error bodies; removed the unused
  category-column store helpers; made dashboard DB usage close via
  `store.open_db`; consolidated the merchant-page DB work; cleaned
  live `streams.py` SIM findings; updated README; replaced web tests'
  `TestClient` usage with a small `httpx.ASGITransport` wrapper.

### Verification

- `UV_CACHE_DIR=/tmp/uv-cache uv run pytest -q` — 207 / 207 passing.
- `UV_CACHE_DIR=/tmp/uv-cache uv run ruff check . --select SIM,UP,B,F,I` — clean.
- `UV_CACHE_DIR=/tmp/uv-cache uv run mypy src/finance` — clean.
- `UV_CACHE_DIR=/tmp/uv-cache uv run vulture src/finance --min-confidence 80` — clean.

### Local scratch reports

`audit-reports/` was untracked local scratch output. It was removed
after the verified fixes landed; `AUDIT.md` remains the durable source
of audit history.

---

## Tier P — `web/app.py` follow-through

Surfaced when an external review AI proposed two architectural tweaks:
(1) decouple `db/store.py` from `eb.models.SessionResponse`, and
(2) normalize `web/app.py`'s DB access to match `dashboard.py`. Item 1
was rejected as speculative — single-user single-bank tool, the
`CLAUDE.md` rules explicitly forbid designing for hypothetical future
providers. Item 2 had a stronger rationale than the suggester gave it:
not just consistency, but an actual file-descriptor leak.

### Findings (baseline: commit `5e0f106`, the Tier O AUDIT entry)

1. **`web/app.py` leaked SQLite connections.** The local
   `def db(): return store.connect(state.db_path)` helper returned
   a raw `sqlite3.Connection`. `sqlite3.Connection.__exit__` only
   commits/rollbacks — it does **not** call `close()`. Every
   `with db() as conn:` in `/callback`, `/transactions`, and
   `/accounts`, plus the `with store.connect(...) as conn:` in
   `/sync`, leaked an open file descriptor per request. Tier O fixed
   this exact pattern in `web/dashboard.py` (commit `aa610b0`) but
   missed `web/app.py`.
2. **Duplicated `init_schema(conn)` calls.** Four handlers called
   `store.init_schema(conn)` inline immediately after opening the
   connection. `store.open_db()` already runs init_schema; the calls
   were dead under the new contextmanager.
3. **`accounts_page` re-implemented `store.list_accounts`.** The
   inline SELECT at `app.py:159-169` duplicated the helper's body
   with a slightly narrower column set plus a
   `COALESCE(excluded_from_spend, 0) AS excluded` alias. The
   dashboard's account-toggle endpoint already produced an `excluded`
   field with the same alias; templates (`_account_row.html`)
   consumed it. Schema-drift trap: any change to the accounts
   shape needed to land in three places.

### Decisions

Single fix commit. Drop the `db()` helper; inline
`store.open_db(state.db_path)` at the four call sites
(closure already has `state` in scope, so no wrapper needed —
unlike `dashboard.py`'s `_db(request)` which has to dig the path
out of `request.app.state.finance`). Drop the now-redundant
`init_schema` calls. Widen `store.list_accounts` to alias
`COALESCE(excluded_from_spend, 0) AS excluded` and have
`accounts_page` call it instead of running its own SQL.

The widen is backward-compatible: `tests/test_web_flow.py:126` is
the only external `list_accounts` consumer and it reads existing
keys only. The extra dict entry is harmless to any caller.

Item 1 (decouple `db/store.py` from `eb.models.SessionResponse`)
**not** taken. `persist_session` has one caller (`web/app.py`'s
`/callback`); adding a `SessionRecord` translator is pure ceremony
for a single-bank single-user tool, and `CLAUDE.md` rejects
hypothetical-future abstractions.

### Changes

- `05265da` — `refactor(web): migrate app.py to store.open_db,
  dedup accounts_page SQL`. Local `db()` helper deleted; four
  handlers switched to `store.open_db(state.db_path)`; redundant
  `init_schema(conn)` calls removed; `accounts_page` now calls
  `store.list_accounts(conn)`; `list_accounts` SELECT widened with
  `COALESCE(excluded_from_spend, 0) AS excluded`. Net: +8 / −24.

### Verification

- `UV_CACHE_DIR=/tmp/uv-cache uv run pytest -q` — 207 / 207 passing.
- `UV_CACHE_DIR=/tmp/uv-cache uv run ruff check . --select SIM,UP,B,F,I` — clean.
- `UV_CACHE_DIR=/tmp/uv-cache uv run mypy src/finance` — clean.
- `UV_CACHE_DIR=/tmp/uv-cache uv run vulture src/finance --min-confidence 80` — clean.

### Tier P deferred (out of scope)

- **Decouple `db/store.py` from `finance.eb.models.SessionResponse`.**
  Single-caller, single-provider; speculative. Revisit only if a
  second bank provider lands.
- **`store.connect` vs `store.open_db` ambiguity.** `store.connect`
  still returns a raw `sqlite3.Connection`, which the test fixtures
  use (`tests/test_web_dashboard.py`, `tests/test_sync.py`) for
  one-shot direct-SQL seeding. The leak only matters in long-running
  processes; tests are fine. Mark `connect` as fixture/internal-only
  if a future tier touches it.
- **`cli.py` 1551 LoC, 0% test coverage** (per Pass 1 baseline,
  status unchanged). Largest remaining fragility lever in the repo,
  but a multi-commit project (split into command groups + integration
  tests) — not a small tier. Flagged here as the obvious next thing
  if quality work continues.

---

## Tier Q — security baseline from repo-audit-2026-05-07

The structured contrarian audit in `docs/repo-audit-2026-05-07/`
identified a roadmap of security, operations, maintainability, and
analytics-platform work. Per the plan, Tier Q is the first implementation
tier: fix the immediate security/doc drift items while leaving job safety,
large refactors, and platform contracts for later tiers.

### Findings

1. **Dependency audit failed on current locked packages.** The fresh
   `pip-audit --skip-editable` run found vulnerable runtime
   `python-multipart` plus vulnerable dev/tooling packages
   (`jupyter-server`, `jupyterlab`, `mistune`, `pip`). CI also had
   `continue-on-error: true`, so dependency failures were visible but
   non-blocking.
2. **LLM categorize auto-write threshold was documented as `0.90` but
   implemented as `0.73`.** The code threshold was already used by CLI
   and web flows; docs and module prose were stale.
3. **Account rows rendered full IBANs.** The DB still stores IBANs, but
   the web account table did not need to expose the full identifier.
4. **Dynamic HTML fragments interpolated unescaped provider/local error
   text.** The most relevant paths were LLM provider errors, LLM progress
   labels, regex validation errors, and keyring errors.
5. **Uvicorn access logs could include OAuth callback query strings.**
   The callback carries `code` in the query string.
6. **`scripts/finance-all.sh` hid failures and queried removed LLM cache
   columns.** The script printed "All done" after failed sections and the
   LLM cost summary referenced `cache_read_tokens`.

### Decisions

- Keep the current `AUTO_WRITE_THRESHOLD = 0.73`; update docs/prose to
  match rather than changing behavior.
- Treat `CVE-2026-3219` on `pip` as a no-fixed-version toolchain CVE for
  now: update `pip` to the version that fixes the second pip CVE, and
  make CI run `pip-audit` with only that one explicit ignore.
- Keep the raw IBAN in SQLite for now; this tier only changes display
  behavior. Storage minimization/encryption remains a later security item.
- Escape dynamic direct `HTMLResponse` fragments in place. Broader route
  splitting / Jinja partial conversion is deferred to the maintainability
  tier.
- Disable uvicorn access logs by default for the local dashboard.

### Changes

- `545da6b` — `fix(security): land tier q baseline`.
  Upgraded vulnerable package constraints + lock entries
  (`python-multipart`, `jupyter-server`, `jupyterlab`, `mistune`, `pip`);
  made CI dependency audit blocking except for the no-fixed-version pip
  CVE; aligned the LLM threshold docs to `0.73`; added masked IBAN
  rendering; escaped dynamic dashboard HTML fragments; disabled uvicorn
  access logs; fixed `finance-all.sh` failure propagation and current
  `llm_runs` cost SQL; added regression tests for masked IBAN and escaped
  fragments.

### Verification

- `UV_CACHE_DIR=/tmp/uv-cache uv run ruff check .` — clean.
- `UV_CACHE_DIR=/tmp/uv-cache uv run mypy src/finance` — clean.
- `UV_CACHE_DIR=/tmp/uv-cache uv run vulture src/finance --min-confidence 80` — clean.
- `UV_CACHE_DIR=/tmp/uv-cache uv run pip-audit --skip-editable --ignore-vuln CVE-2026-3219` — no known vulnerabilities found.
- `UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_web_flow.py tests/test_web_dashboard.py tests/test_llm_categorize.py -q` — 54 / 54 passing.
- `UV_CACHE_DIR=/tmp/uv-cache uv run pytest -q` — 209 / 209 passing.

### Closed by the next roadmap pass

- **Tier R/S/T:** landed in `95e7c7a` and recorded below.

---

## Tier R/S/T — operations, migration, and analytics contracts

After Tier Q removed the immediate security/doc drift items, this pass
implemented the remaining roadmap tiers that could be done safely without
turning the app into a different product.

### Findings addressed

1. **Account sync could commit partial transaction rows after a later page
   failure.** `sync_account()` now records the run first, then wraps the
   account's transaction inserts and success update in an explicit
   transaction. On error it rolls back inserted rows and records fetched
   count/date range on the failed `sync_runs` row.
2. **Long jobs had no DB-backed overlap guard.** Added `job_locks` with
   expiry cleanup and wired it into all-account sync, CLI/web reenrich, and
   CLI/web LLM categorization.
3. **SQLite was not concurrency-tuned.** Connections now set foreign keys,
   WAL, `busy_timeout`, connection timeout, and `synchronous=NORMAL`; DB
   directories/files are chmodded best-effort private.
4. **Common operational queries lacked supporting indexes.** Added global
   transaction date, sync-run account/start, lock-expiry, and LLM
   status/start indexes.
5. **Schema evolution was still ad hoc.** Added `schema_migrations`, a
   tracked migration registry for existing additive columns, and old-schema
   migration tests.
6. **Merchant merges could leave stream identity incoherent.** Merges now
   clear affected stream references, delete stale stream rows, recompute
   streams, and tests assert no dangling `tx_enrichment.stream_id`.
7. **Enrichment repeated expensive merchant normalization decisions.** The
   enrichment loop now caches repeated raw merchant resolutions within a run.
8. **Reusable analytics contracts were missing.** Added `finance.core`
   contracts (`CanonicalEvent`, `DatasetAdapter`, `MetricSpec`,
   `MetricRegistry`), finance metric specs, and a non-finance usage-event CSV
   adapter as the second-domain proof.
9. **Enable Banking transient failures were all fatal.** GET/DELETE requests
   now retry bounded 429/5xx/network timeout failures with exponential
   backoff and `Retry-After` support.

### Changes

- `95e7c7a` — `feat(audit): land tiers r s t`.
  Implemented atomic per-account sync, sync/enrich/LLM locks, SQLite WAL and
  indexes, migration tracking, stream cleanup after merchant merge, EB
  retries, intra-run merchant cache, core analytics contracts, finance metric
  specs, and a usage-event adapter proof. Added regression tests for DB
  pragmas/migrations/locks, partial sync rollback, held sync locks, EB retry,
  merge stream integrity, and platform contracts.

### Verification

- `UV_CACHE_DIR=/tmp/uv-cache uv run ruff check src tests` — clean.
- `UV_CACHE_DIR=/tmp/uv-cache uv run mypy src/finance` — clean.
- `UV_CACHE_DIR=/tmp/uv-cache uv run vulture src/finance --min-confidence 80` — clean.
- `UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_db_store.py tests/test_sync.py tests/test_eb_client.py tests/test_analysis_merchants_ops.py tests/test_core_analytics.py -q` — 34 / 34 passing.
- `UV_CACHE_DIR=/tmp/uv-cache uv run pytest -q` — 218 / 218 passing.
- `UV_CACHE_DIR=/tmp/uv-cache uv run pip-audit --skip-editable --ignore-vuln CVE-2026-3219` — no known vulnerabilities found.
- `bash -n scripts/finance-all.sh` — clean.

### Still intentionally deferred

- Tier U landed the local dashboard browser boundary; remaining security work
  is now about privacy minimization, deployment hardening, and data-retention
  policy rather than unauthenticated local browser writes.
- Sync, reenrich, and LLM still run inline in requests/commands; locks prevent
  overlap, but there is not yet a background worker/job queue.
- `cli.py` and `web/dashboard.py` are still large modules. This pass added
  operational guards around them, not a route/command split.
- The analytics core is a contract layer plus proof adapter, not a full plugin
  runtime or non-finance dashboard.

---

## Tier U — local dashboard browser security boundary

After Tier R/S/T left the web boundary as the largest remaining P1 security
gap, this pass hardened the local dashboard without changing the product into
a multi-user or cloud service.

### Findings addressed

1. **The dashboard accepted browser traffic without an app-issued secret.**
   `finance serve` now generates a random dashboard token, prints a startup
   URL containing it, strips the token from the browser URL on first GET, and
   stores it in an HttpOnly SameSite cookie. Programmatic clients can still
   use the same token as a bearer/query token in tests or local automation.
2. **Mutating browser routes had no CSRF boundary.** Unsafe methods now
   require same-origin traffic and the app's CSRF token. The token is exposed
   through a meta tag for local dashboard JS and can also be supplied as
   `_csrf` for regular form posts.
3. **Sensitive pages depended on CDN JavaScript and inline handlers.**
   Tailwind, HTMX, Chart.js, inline scripts, inline styles, and inline click
   handlers were replaced by local `app.css` and `app.js`. The local script
   implements the small HTMX subset and chart rendering the dashboard
   actually uses.
4. **Security headers were absent.** Responses now set `nosniff`,
   `DENY` framing, `no-referrer`, and a self-only CSP with no inline scripts.
5. **Callback/token URL leakage needed another reduction layer.** Tier Q
   disabled uvicorn access logs by default; Tier U additionally removes the
   dashboard token from the browser-visible URL after initial login.

### Changes

- `46412f7` — `fix(web): harden local dashboard boundary`.
  Added token-cookie authentication, CSRF/origin enforcement, security
  headers, local static asset serving, local dashboard CSS/JS, template
  updates, and ASGI client support for cookies/CSRF. Added regressions for
  locked dashboard access, token stripping, CSRF/same-origin rejection, CSP
  headers, and local assets.

### Verification

- `UV_CACHE_DIR=/tmp/uv-cache uv run ruff check src tests` — clean.
- `UV_CACHE_DIR=/tmp/uv-cache uv run mypy src/finance` — clean.
- `UV_CACHE_DIR=/tmp/uv-cache uv run vulture src/finance --min-confidence 80` — clean.
- `UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_web_flow.py tests/test_web_dashboard.py -q` — 41 / 41 passing.
- `UV_CACHE_DIR=/tmp/uv-cache uv run pytest -q` — 221 / 221 passing.
- `UV_CACHE_DIR=/tmp/uv-cache uv run pip-audit --skip-editable --ignore-vuln CVE-2026-3219` — no known vulnerabilities found.
- `bash -n scripts/finance-all.sh` — clean.

### Still intentionally deferred

- This is a local single-user guard, not multi-user auth, TLS deployment, or
  a hardened reverse-proxy/cloud posture.
- LLM prompt minimization, tool-provider opt-in, and redaction-version
  tracking remain open.
- Raw provider payload retention still needs purge/minimize/encryption policy.
- Sync, reenrich, and LLM still run inline; locks prevent overlap, but there
  is not yet a background worker/job queue.
- `cli.py` and `web/dashboard.py` remain large coordination modules.
- The analytics core still needs plugin/runtime registration and a
  non-finance dashboard path.

---

## Tier V — reproducible developer shell

This pass answers the Docker / Nix flake / devenv question from the audit
roadmap. The repo is still primarily a local, stateful SQLite + dashboard
tool, so the default developer environment should optimize for repeatable
local commands, not container deployment.

### Findings addressed

1. **The repo had a locked Python graph but no pinned system-tool shell.**
   `uv.lock` controlled Python packages, while shell tools (`git`, `jq`,
   `sqlite`, `shellcheck`, `openssl`, `uv`) depended on the host.
2. **Docker is not the best default for this workflow.** The app reads local
   config/data dirs, uses OS keyring/browser OAuth flows, and serves a local
   dashboard. A container can still be added for deployment later, but it
   should not be the first developer entry point.
3. **A raw flake would expose lower-level Nix plumbing without adding much.**
   Devenv 2.1 gives the useful pieces directly: pinned inputs, shell
   activation, tasks, processes, git-hooks, clean env handling, and generated
   local files.
4. **Devenv 2.1 does not need `direnv`.** No `.envrc` was added; optional
   activation is through `devenv hook`.
5. **`languages.python.version` pulled `nixpkgs-python` and tried to compile
   Python from source in this environment.** The final config uses
   `pkgs.python311` from the locked nixpkgs input instead; validation showed
   Python coming from the binary cache.

### Changes

- Added `devenv.yaml`, `devenv.nix`, and `devenv.lock`.
- Added a devenv 2.1 shell that keeps `uv.lock` as the Python dependency
  source of truth and runs `uv sync --frozen --all-groups`.
- Added devenv scripts for `finance-test`, `finance-audit`,
  `finance-serve`, and `finance-check`.
- Added a `devenv test` task graph for ruff, mypy, vulture, pytest,
  pip-audit, and shell syntax/shellcheck.
- Added local `git-hooks` generation without committing the generated
  `.pre-commit-config.yaml`.
- Ignored `.devenv/`, local devenv overrides, and generated hook config.
- Documented the environment in `README.md` and
  `docs/development-environment.md`.

### Verification

- `devenv --version` — `devenv 2.1.1+23120f1 (x86_64-linux)`.
- `devenv info` — lock validates and Nix evaluates with inputs
  `nixpkgs` + `git-hooks`.
- `devenv shell python --version` — syncs 174 locked Python packages and
  reports `Python 3.11.15`.
- `devenv tasks list` — `checks:{ruff,mypy,vulture,pytest,pip-audit,shell}`
  are wired under `devenv:enterTest`.
- `devenv test` — full task graph passed, including dashboard process test.
- `devenv tasks run checks` — check namespace passed.

### Still intentionally deferred

- Docker remains a deployment/runtime packaging option, not the default local
  developer workflow.
- CI still runs through explicit `uv` commands; switching CI to `devenv test`
  is possible, but should be a separate change because it alters runner Nix
  setup.
- `uv` emits a warning that the project build backend bound
  (`uv_build>=0.9.26,<0.10.0`) does not include the Nix-provided uv
  `0.11.8`; the build succeeds. Widening that bound is a separate packaging
  decision.

---

## Tier W — package audit cleanup

This follow-up resolves the two package loose ends left by Tier V.

### Findings addressed

1. **The `uv_build` upper bound was stale.** The project required
   `uv_build>=0.9.26,<0.10.0`, while the committed devenv shell provides
   uv `0.11.8`. Official uv docs recommend an upper bound on the
   `uv_build` minor line; the repo now uses `uv_build>=0.11.8,<0.12.0`,
   matching the checked-in devenv uv while still blocking unreviewed
   `0.12.x` build-backend changes.
2. **The `CVE-2026-3219` ignore was no longer sensible.** It was reasonable
   when Tier Q landed because the advisory data then reported no fixed
   version for the pip issue and the repo needed CI to stay blocking for all
   other vulnerabilities. The current lock already has pip `26.1.1`, and a
   fresh `pip-audit --skip-editable` run is clean without any ignore.
3. **The dev dependency floor did not make the clean pip intent explicit.**
   `pip>=26.1` was already outside the affected `<=26.0.1` range, but the
   lock contains `26.1.1`; the floor now says `pip>=26.1.1`.

### Changes

- Changed `[build-system]` to `uv_build>=0.11.8,<0.12.0`.
- Changed the dev dependency floor to `pip>=26.1.1`.
- Refreshed `uv.lock`; only the pip specifier changed.
- Removed `--ignore-vuln CVE-2026-3219` from CI and devenv pip-audit tasks.

### Verification

- `UV_CACHE_DIR=/tmp/uv-cache uv run pip-audit --skip-editable` — no known
  vulnerabilities found.
- `devenv shell python --version` — no `uv_build` compatibility warning;
  reports `Python 3.11.15`.
- `devenv test` — full task graph passed, including pip-audit without an
  ignore and the dashboard process test.

### Still intentionally deferred

- Historical Tier Q/U evidence remains unchanged; those sections describe
  what was true when those tiers landed.

---

## Tier X — Nix tooling triage

This pass reviewed the tools listed in Asaduzzaman Pavel's April 2026
"The NixOS Tools That Actually Make a Difference" article against this repo's
current shape.

### Finding

Most listed tools are not repo-level wins right now:

- `comma`, `nix-index`, `nh`, `nix-direnv`, `hjem`, and NixOS Options Search
  are personal-machine or OS/home-manager conveniences. They do not belong in
  a Python finance app's committed dev environment.
- `nurl` and `nix-init` become useful if this project starts packaging
  upstream sources or publishing a Nix package. The current devenv deliberately
  keeps Python packaging in `uv.lock`, so they would add process without
  removing current work.
- `flake-parts` is useful once a flake has several outputs, packages, systems,
  or CI checks. This repo currently has a focused devenv config, so adopting it
  would be premature.
- `statix` is the one clear fit: the repo now has committed Nix code, and
  `statix check devenv.nix` passed with no findings.

### Change

- Added `pkgs.statix` to the devenv shell.
- Added `checks:nix` to the `devenv test` task graph.
- Documented the Nix lint check in `docs/development-environment.md` and
  `CLAUDE.md`.

### Verification

- `nix run nixpkgs#statix -- check devenv.nix` — no findings.
- `devenv test` — full task graph passed; `checks:nix` ran
  `statix check devenv.nix` alongside ruff, mypy, vulture, pytest,
  pip-audit, and shellcheck.

---

## Tier Y — hotfix: partial commits and sync resilience

### Changes

- Made `release_job_lock` transaction-aware so releasing a lock no longer
  commits unrelated caller work.
- Wrapped sync auto-enrich and web re-enrich paths in explicit transaction
  contexts and added rollback-before-release behavior on failure.
- Added stale `sync_runs.status='running'` recovery and one-shot Enable Banking
  401 retry with a refreshed JWT.

### Verification

- Added regression tests for lock release, enrichment rollback, stale sync
  recovery, and 401 token refresh.

---

## Tier Z — transaction identity, overlap sync, and stream repartitioning

### Changes

- Added local `transactions.tx_uid`, provider/source identity fields, and
  compatibility triggers for existing `transaction_id` fixtures.
- Sync now reconciles by `(account_uid, source_key)`, updates mutable provider
  rows, supports `--overlap-days`, and estimates a data-informed overlap when
  not configured.
- Stream identity now includes merchant, amount bucket, sign, currency, and
  transaction type class. Added `finance analyze recompute-streams
  --report-orphan-overrides`.

### Verification

- Added tests for duplicate provider IDs across accounts, corrected-provider
  updates, overlap lookback, opposite-sign stream splits, and override split
  reporting.

---

## Tier AA — dashboard browser correctness

### Changes

- Removed inline handlers and returned inline scripts; delegated auto-submit,
  reset-on-success, reload, and polling behavior to `app.js`.
- Deleted stale `index.html`, expanded local CSS utility coverage, added focus
  styling, `aria-live` regions, and table header `scope="col"`.
- Added static tests that reject inline browser code and unsupported template
  utility classes.

### Verification

- Dashboard and flow tests pass with the stricter static checks.

---

## Tier AB — sync and onboarding trust

### Changes

- Overview now distinguishes connected-but-unsynced state with a primary sync
  CTA and shows last sync status/timestamps after syncs exist.
- Sync fragments distinguish success, partial failure, and total failure.
- Enable Banking 401/403 hints are shared between CLI and web.
- Added confirmations for account include/exclude, category clear, and broad
  web re-enrich. Added AI categorization disclosure covering provider, data
  shape, WebSearch behavior, and expected cost.

---

## Tier AC — privacy, consent, and encrypted local state

### Changes

- Added `finance backup create --output <path> [--redacted]`,
  `finance privacy purge-raw`, `finance db encrypt`, and
  `finance db decrypt-export --output <path>`.
- `db encrypt` writes a new encrypted file only, requires a prior backup marker,
  prompts for the passphrase with hidden input, and preserves plaintext.
- Added `finance sessions revoke <id>` and `finance sessions rm <id> --revoke`
  with `revoked_at` recording.
- LLM categorization now redacts memo prompt text and exposes
  `--preview-prompt` to show exactly what would be sent without calling an LLM.

### Deferred note

- SQLCipher encryption requires `pysqlcipher3` / SQLCipher in the runtime
  environment; the command fails closed if unavailable and leaves plaintext
  untouched.

---

## Tier AD — analytics policy cleanup

### Changes

- Stream rollups used by totals, recurring, subscriptions, and forecast now
  require EUR streams and at least one included-account EUR transaction.
- Added minor-unit columns (`transactions.amount_minor`,
  `streams.median_amount_minor`) with migration/backfill and sync/stream writes.
- Added central config fields for timezone, sync overlap, and minimal raw-data
  retention; CLI sync honors configured overlap/minimal retention.
- Category writes and YAML rules now validate against the canonical taxonomy.

---

## Tier AE — CLI, CI, devenv, docs polish

### Changes

- Removed fake `--csv` support from nested `advise` commands; advisory output
  remains nested JSON with `--json`.
- `merchant seed-top` now requires `--seed-file`.
- CI now runs `uv sync --frozen --all-groups`, pins Python `3.11.15`, and adds
  `uv run ruff format --check .`.
- README/CLAUDE were refreshed for DB paths, privacy commands, prompt preview,
  merchant seed flags, and current verification workflow.

### Verification

- `uv run pytest -q` — 233 passed.
- `uv run ruff check src tests` — clean.
- `uv run ruff format --check .` — clean.
- `uv run mypy src/finance` — clean.
- `uv run vulture src/finance --min-confidence 80` — clean.
- `uv run pip-audit --skip-editable` — no known vulnerabilities.
- `bash -n scripts/finance-all.sh` — clean.
- `shellcheck` and `statix` were not available in the current shell.
