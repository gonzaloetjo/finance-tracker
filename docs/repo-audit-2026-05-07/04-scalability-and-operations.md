# Scalability And Operations

## Summary

The repo is operationally appropriate for one local user and modest bank
history. Tier R/S/T fixed the immediate overlap/SQLite/sync-retry risks, but
it is not yet a background-job system and still needs larger-data work.

The important distinction: SQLite and Pandas are fine choices here, but
the app now has locks, timeouts, migration tracking, and indexes; the next
step is moving long work out of request/command execution.

## Findings

### O1. Account Sync Could Commit Partial Transactions On Failure (Fixed In Tier R/S/T)

Evidence:

- `sync_account()` inserts transactions inside the `try` loop in
  [sync.py](../../src/finance/sync.py#L80).
- The `except` block updates `sync_runs` and commits in
  [sync.py](../../src/finance/sync.py#L123).

Risk:

If an exception occurs after some transaction inserts, the error update can
commit those partial inserts while returning `added=0`. This leaves data in
a confusing state and can mislead automation.

Current status:

Tier R/S/T records the run first, wraps transaction inserts and success
updates in an explicit account-level transaction, rolls inserted rows back on
failure, and records fetched count/date range on `sync_runs`.

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

### O3. No Durable Job Lock (Fixed For Current Long Jobs In Tier R/S/T)

Evidence:

- `sync_all_accounts()` runs sequential account syncs in
  [sync.py](../../src/finance/sync.py#L132).
- Nothing prevents concurrent CLI/web syncs, reenrich, and LLM categorization.

Risk:

Overlapping jobs can duplicate API work, read the same `_last_booking_date`,
contend on SQLite, and produce stale progress rows.

Current status:

Tier R/S/T added `job_locks` with expiry cleanup and uses it for all-account
sync, reenrich, and LLM categorization. A true job queue/progress table is
still deferred.

### O4. Merchant Enrichment Has Poor Large-Data Scaling (Partly Improved In Tier R/S/T)

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
- batch `tx_enrichment` updates
- recompute streams only for affected merchants where possible

Current status:

Tier R/S/T caches repeated raw merchant resolutions within one enrichment
run and prunes stale stream rows. Full alias/canonical preloading and
affected-merchant-only stream recomputation remain.

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

### O6. SQLite Was Not Concurrency-Tuned (Fixed In Tier R/S/T)

Evidence:

- `connect()` enables foreign keys only in
  [store.py](../../src/finance/db/store.py#L24).
- There is no WAL mode, busy timeout, connection timeout tuning, or migration
  version table.

Risk:

Concurrent reads/writes and long transactions can fail with lock errors or
stall unpredictably.

Current status:

Tier R/S/T sets WAL, `busy_timeout`, connection timeout, `synchronous=NORMAL`,
and schema migration tracking in `store.connect()` / `init_schema()`.

### O7. Missing Indexes For Common Queries (Partly Fixed In Tier R/S/T)

Evidence:

- Schema has `idx_tx_account_date` but no global transaction date index in
  [schema.sql](../../src/finance/db/schema.sql#L48).
- `/transactions` filters by `booking_date` without account in
  [web/app.py](../../src/finance/web/app.py#L135).
- LLM progress polls by status/time in
  [dashboard.py](../../src/finance/web/dashboard.py#L278).

Current status:

Tier R/S/T added `transactions(booking_date DESC)`,
`llm_runs(status, started_at DESC)`, `sync_runs(account_uid, started_at DESC)`,
and lock-expiry indexes. Add report-specific composite indexes only after
query-plan evidence.

### O8. External API Reliability Was Thin (Partly Fixed In Tier R/S/T)

Evidence:

- EB client has fixed 30s timeout in
  [eb/client.py](../../src/finance/eb/client.py#L42).
- No retry/backoff around 429/5xx/network timeouts.
- Pagination only guards repeated continuation keys in
  [eb/flows.py](../../src/finance/eb/flows.py#L84).

Risk:

One transient failure can fail an account sync. Automation has limited
visibility into recoverability.

Current status:

Tier R/S/T added bounded retry/backoff for idempotent EB GET/DELETE 429,
5xx, network timeout failures, with `Retry-After` support. Retry/page counts
are not yet persisted in `sync_runs`.

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

1. Move sync/reenrich/LLM from inline requests to background jobs.
2. Add richer sync observability such as retry/page counts.
3. Benchmark enrichment with synthetic 10k/100k transaction datasets.
4. Add query-plan-driven composite indexes and/or materialized rollups.
