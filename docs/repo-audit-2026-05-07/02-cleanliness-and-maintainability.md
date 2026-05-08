# Cleanliness And Maintainability

## Summary

The repo is cleaner than its size suggests. Core finance behavior is
mostly isolated in `analysis`, data access is centralized enough for a
single-user app, and tests cover many real domain cases. The main
maintainability risks are large coordination modules, muddy transaction
ownership, ad hoc migrations, and some documentation drift.

## Strengths

- Clear broad package split: `analysis`, `auth`, `db`, `eb`, `llm`, `web`.
- Canonical transaction columns are centralized in
  [analysis/io.py](../../src/finance/analysis/io.py).
- Taxonomy drift guards exist in
  [taxonomy.py](../../src/finance/taxonomy.py).
- Most SQL uses placeholders and explicit SQLite connections.
- Tests are behavior-heavy rather than only smoke tests.
- Static quality is currently clean: ruff, mypy, and vulture all pass.

## Findings

### M1. `cli.py` Is The Largest Maintainability Hotspot

Evidence:

- [cli.py](../../src/finance/cli.py) is 1551 lines and 806 statements.
- Coverage is 20%.
- It mixes Typer command declarations, user prompts, direct SQL, file
  writes, output formatting, LLM setup, interactive workflows, and web
  server startup.

Risk:

Small changes to CLI behavior are hard to review because command wiring
and business behavior are interleaved.

Recommendation:

Split by command group after introducing service functions:

- `cli/config.py`
- `cli/sync.py`
- `cli/analyze.py`
- `cli/merchant.py`
- `cli/llm.py`
- a thin root `cli.py` Typer assembly

Do not start with file movement alone. First move behavior behind
testable service functions, then move command groups.

### M2. `web/dashboard.py` Is A Route/SQL/HTML Coordination Blob

Evidence:

- [web/dashboard.py](../../src/finance/web/dashboard.py) is 835 lines.
- It contains routes, direct SQL, analysis orchestration, keyring actions,
  LLM calls, stream overrides, rules management, and raw HTML fragments.
- Direct string HTML appears around
  [dashboard.py](../../src/finance/web/dashboard.py#L232).

Risk:

Security fixes, UI fixes, and analysis changes are tangled in one router.
It is easy to miss escaping, transaction, or DB lifecycle issues.

Recommendation:

Split into focused routers/modules:

- `web/routes/merchants.py`
- `web/routes/subscriptions.py`
- `web/routes/rules.py`
- `web/routes/settings.py`
- `web/routes/accounts.py`
- `web/routes/llm.py`

Move raw HTMX response strings into Jinja partials or a fragment helper
that escapes dynamic text.

### M3. Initial Docs Drift On LLM Auto-Write Threshold (Fixed In Tier Q)

Initial evidence:

- `AUTO_WRITE_THRESHOLD = 0.73` in
  [llm/categorize.py](../../src/finance/llm/categorize.py#L35).
- The module docstring and README described auto-write at `>=0.90`.

Current status:

Tier Q aligned the module prose and README to the implemented `0.73`
threshold.

Risk:

Users could overtrust LLM categorization if threshold prose drifts from
the implementation again.

Recommendation:

Keep the single threshold constant as the source of truth and update docs
in the same pass as any future threshold behavior change.

### M4. Migrations Are Still Ad Hoc

Evidence:

- Schema lives in [db/schema.sql](../../src/finance/db/schema.sql).
- Additive migrations are hardcoded in
  [store.py](../../src/finance/db/store.py#L32).
- There is no schema version table or migration history.

Risk:

The current approach works for additive columns. It will not age well for
data backfills, table splits, index changes, destructive changes, or
plugin-owned migrations.

Recommendation:

Before the next schema change, add:

- `schema_migrations(version, applied_at)`
- ordered migration files
- tests that create old schemas and verify upgrade paths

### M5. Transaction Ownership Is Muddy

Evidence:

- Helpers commit internally, for example
  [merchants.py](../../src/finance/analysis/merchants.py#L358),
  [merchants.py](../../src/finance/analysis/merchants.py#L388), and
  [advise.py](../../src/finance/llm/advise.py#L82).
- Callers often also wrap writes in `with conn:`.

Risk:

Nested transaction expectations become misleading. It is unclear which
function owns commit/rollback, which complicates retries, background jobs,
and multi-step operations.

Recommendation:

Adopt one convention:

- Low-level functions never commit.
- Top-level command/job/route handlers own transactions.
- Any helper that commits must be named/documented as a top-level write.

### M6. Stream Identity Can Become Incoherent After Merchant Merge

Evidence:

- Stream IDs hash `merchant_id` plus an amount bucket in
  [streams.py](../../src/finance/analysis/streams.py#L75).
- `merge_merchants()` repoints `streams.merchant_id` without recomputing
  stream IDs in [merchants.py](../../src/finance/analysis/merchants.py#L377).

Risk:

Stream rows can remain keyed by an old merchant ID-derived hash until a
reenrich/regroup. This is likely recoverable, but it is a data-integrity
trap and currently lacks a regression test.

Recommendation:

After merge, either delete/recompute affected streams or explicitly mark
the operation as requiring immediate stream regroup. Add a test for merge
plus `group_streams()`.

### M7. The Stated IO Boundary Is Aspirational

Evidence:

- [analysis/io.py](../../src/finance/analysis/io.py#L1) says it is the
  only SQLite reader for analysis.
- Multiple analysis modules query SQLite directly, including
  [streams.py](../../src/finance/analysis/streams.py#L114),
  [subscriptions.py](../../src/finance/analysis/subscriptions.py#L74),
  [forecast.py](../../src/finance/analysis/forecast.py#L41), and
  [alerts.py](../../src/finance/analysis/alerts.py).

Risk:

Schema assumptions are spread across modules. This blocks reusable
analytics contracts and makes future schema changes harder.

Recommendation:

Either enforce a real IO boundary or document metric-level SQL ownership.
For platform reuse, prefer explicit `MetricSpec` dependencies.

### M8. Hardcoded Current Time Reduces Determinism

Evidence:

- `date.today()` in [streams.py](../../src/finance/analysis/streams.py#L222).
- `pd.Timestamp.today()` in
  [totals.py](../../src/finance/analysis/totals.py#L171).
- `date.today()` in [store.py](../../src/finance/db/store.py#L147).

Risk:

Time-window behavior is harder to test and reproduce. Historical backtests
or multi-domain analytics will need an `as_of` concept.

Recommendation:

Add optional `as_of` parameters to analysis functions that use current
time, defaulting to today for CLI/web.

## Maintainability Priorities

1. Fix threshold docs/code drift.
2. Add migration versioning.
3. Make transaction ownership explicit.
4. Split service functions out of `cli.py`.
5. Split `web/dashboard.py` by route family.
6. Add tests for stream integrity after merge and old-schema upgrades.
