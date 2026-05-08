# Scalability And Operations

## Summary

The repo is operationally appropriate for one local user and modest bank
history. It is not yet robust under concurrent web/CLI activity, large
transaction histories, background jobs, or long-running LLM/sync work.

The important distinction: SQLite and Pandas are fine choices here, but
the app needs locks, timeouts, job records, indexes, and clearer batch
boundaries to remain reliable.

## Findings

### O1. Account Sync Can Commit Partial Transactions On Failure

Evidence:

- `sync_account()` inserts transactions inside the `try` loop in
  [sync.py](../../src/finance/sync.py#L80).
- The `except` block updates `sync_runs` and commits in
  [sync.py](../../src/finance/sync.py#L123).

Risk:

If an exception occurs after some transaction inserts, the error update can
commit those partial inserts while returning `added=0`. This leaves data in
a confusing state and can mislead automation.

Recommendation:

Wrap each account sync in a clear transaction boundary:

- start run row
- fetch page(s)
- insert rows
- commit success with added/fetched/page count
- rollback inserted tx rows on account failure, or explicitly record
  partial success with counts and date range

### O2. Long-Running Work Blocks Async Web Routes

Evidence:

- `/sync` runs sync inline in [web/app.py](../../src/finance/web/app.py#L122).
- `/merchants/llm-categorize` runs LLM batches inline in
  [dashboard.py](../../src/finance/web/dashboard.py#L220).
- `/rules/reenrich` runs full enrichment inline in
  [dashboard.py](../../src/finance/web/dashboard.py#L700).

Risk:

One request can block the server, hold SQLite writes, and collide with
other requests. This matters even for local use if the browser double-clicks
or an HTMX poll overlaps.

Recommendation:

Move sync, reenrich, and LLM work into a DB-backed job table plus worker
thread/process. Routes should enqueue, then poll job state.

### O3. No Durable Job Lock

Evidence:

- `sync_all_accounts()` runs sequential account syncs in
  [sync.py](../../src/finance/sync.py#L132).
- Nothing prevents concurrent CLI/web syncs, reenrich, and LLM categorization.

Risk:

Overlapping jobs can duplicate API work, read the same `_last_booking_date`,
contend on SQLite, and produce stale progress rows.

Recommendation:

Add a `jobs` or `locks` table with unique active lock keys:

- `sync`
- `enrich`
- `llm_categorize`
- optional account-scoped locks

Use lock expiry/stale cleanup for crashed processes.

### O4. Merchant Enrichment Has Poor Large-Data Scaling

Evidence:

- `normalize_merchant()` loads all canonicals for fuzzy matching in
  [merchants.py](../../src/finance/analysis/merchants.py#L132).
- It is called in the per-transaction loop in
  [enrich.py](../../src/finance/analysis/enrich.py#L108).
- `group_streams()` scans all enriched tx rows in
  [streams.py](../../src/finance/analysis/streams.py#L114).

Risk:

Worst-case enrichment trends toward transactions times merchants, plus
full stream recomputation. This is fine at small scale, but avoidable.

Recommendation:

- preload aliases and canonicals once per enrichment run
- cache fuzzy match decisions for normalized raw strings
- batch `tx_enrichment` updates
- recompute streams only for affected merchants where possible

### O5. Dashboard Analytics Rehydrate Full DataFrames

Evidence:

- `load_transactions()` reads the enriched join into Pandas in
  [io.py](../../src/finance/analysis/io.py#L65).
- `compute_totals()` calls it in
  [totals.py](../../src/finance/analysis/totals.py#L166).
- Trends call it in [trends.py](../../src/finance/analysis/trends.py#L30).
- Overview composes many analyses in
  [overview.py](../../src/finance/analysis/overview.py#L89).

Risk:

Multi-year histories and many accounts will make dashboard requests
progressively heavier.

Recommendation:

Push filters into SQL before DataFrame hydration, especially date windows,
account filters, category filters, and currency filters. Consider materialized
monthly rollups for dashboard cards.

### O6. SQLite Is Correctness-Oriented, Not Concurrency-Tuned

Evidence:

- `connect()` enables foreign keys only in
  [store.py](../../src/finance/db/store.py#L24).
- There is no WAL mode, busy timeout, connection timeout tuning, or migration
  version table.

Risk:

Concurrent reads/writes and long transactions can fail with lock errors or
stall unpredictably.

Recommendation:

Set:

- `PRAGMA journal_mode=WAL`
- `PRAGMA busy_timeout = 5000`
- explicit connection timeout
- schema migration versioning

### O7. Missing Indexes For Common Queries

Evidence:

- Schema has `idx_tx_account_date` but no global transaction date index in
  [schema.sql](../../src/finance/db/schema.sql#L48).
- `/transactions` filters by `booking_date` without account in
  [web/app.py](../../src/finance/web/app.py#L135).
- LLM progress polls by status/time in
  [dashboard.py](../../src/finance/web/dashboard.py#L278).

Recommendation:

Add indexes based on observed query plans:

- `transactions(booking_date DESC)`
- `llm_runs(status, started_at DESC)`
- `sync_runs(account_uid, started_at DESC)`
- possibly `transactions(currency, booking_date)` if reports grow

### O8. External API Reliability Is Thin

Evidence:

- EB client has fixed 30s timeout in
  [eb/client.py](../../src/finance/eb/client.py#L42).
- No retry/backoff around 429/5xx/network timeouts.
- Pagination only guards repeated continuation keys in
  [eb/flows.py](../../src/finance/eb/flows.py#L84).

Risk:

One transient failure can fail an account sync. Automation has limited
visibility into recoverability.

Recommendation:

Add bounded exponential backoff for 429, 5xx, and network timeouts.
Record retry counts and page counts in `sync_runs`.

### O9. `scripts/finance-all.sh` Hid Failures And Had Stale SQL (Fixed In Tier Q)

Evidence:

- `run()` swallows command failures in
  [finance-all.sh](../../scripts/finance-all.sh#L32).
- LLM cost summary references removed `cache_read_tokens` in
  [finance-all.sh](../../scripts/finance-all.sh#L121).

Risk:

Automation can end with "All done" despite failed commands, and the LLM
section can fail due to stale schema assumptions.

Current status:

Tier Q added failure tracking/nonzero exit behavior and updated the LLM
cost SQL to current `llm_runs` columns.

Remaining recommendation:

Decide whether the script is exploratory or automation:

- exploratory: show a final failed command summary
- automation: fail fast or return nonzero if any section fails

Keep new summary queries tied to current schema tests or shell syntax
checks so the script does not drift again.

## Operations Priorities

1. Fix partial sync transaction semantics.
2. Add job locks and move long web work to background jobs.
3. Enable WAL and busy timeouts.
4. Add indexes for date/progress/sync queries.
5. Add EB retry/backoff and richer sync observability.
6. Stop shell automation from hiding failures.
7. Benchmark enrichment with synthetic 10k/100k transaction datasets.
