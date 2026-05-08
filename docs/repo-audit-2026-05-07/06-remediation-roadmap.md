# Remediation Roadmap

## Prioritized Backlog

| Priority | Work | Why | Main Files |
|---|---|---|---|
| Done Q | Fix dependency audit findings, at least runtime `python-multipart`. | Tier Q upgraded vulnerable locked packages and made CI audit blocking except no-fixed-version `CVE-2026-3219`. | `pyproject.toml`, `uv.lock`, `.github/workflows/ci.yml` |
| Done Q | Fix LLM threshold docs/code drift. | Tier Q aligned docs/prose to implemented `0.73`. | `llm/categorize.py`, `README.md` |
| Done Q | Mask IBANs in web UI. | Tier Q masks account display. | `_account_row.html`, tests |
| Done Q | Escape direct HTML fragments. | Tier Q escapes provider/local error fragments. | `web/dashboard.py` |
| Done R | Fix partial sync transaction semantics. | Tier R made account sync inserts atomic and added rollback tests. | `sync.py`, tests |
| Done R | Add job locks for sync/enrich/LLM. | Tier R added DB-backed expiry locks for current long jobs. | `db/store.py`, `sync.py`, `web/dashboard.py`, `cli.py` |
| Done R | Enable SQLite WAL/busy timeout and add hot indexes. | Tier R added connection pragmas and date/progress/sync indexes. | `db/store.py`, `db/schema.sql` |
| Done R | Add EB retry/backoff. | Tier R retries idempotent transient EB calls. | `eb/client.py`, tests |
| Done S | Add migration version table/runner. | Tier S added `schema_migrations`, migration registry, and old-schema tests. | `db/store.py`, tests |
| Done S | Fix stream integrity after merchant merges. | Tier S recomputes affected streams and tests dangling refs. | `analysis/merchants.py`, `analysis/streams.py`, tests |
| Done T | Formalize analytics contracts. | Tier T added `CanonicalEvent`, `DatasetAdapter`, `MetricSpec`, finance metric specs, and a usage CSV proof. | `core/*`, `analysis/metric_specs.py`, tests |
| P1 | Add local auth and CSRF/origin checks. | Required before any non-loopback exposure and useful even on localhost. | `web/app.py`, templates |
| P1 | Vendor JS assets and add CSP. | CDN script compromise can read bank data. | `web/templates/base.html`, static assets |
| P1 | Define LLM privacy/minimization boundary. | Raw memos and merchant context leave the machine. | `llm/categorize.py`, `llm/providers.py`, web settings |
| P1 | Move locked long jobs to background execution. | Locks prevent overlap, but requests still block until work finishes. | `web/*`, new job worker |
| P2 | Split `cli.py` by command group through service functions. | User entry points are large and under-tested. | `cli.py`, new `cli/*` or `services/*` |
| P2 | Split `web/dashboard.py` by route family. | Reduce mixed route/SQL/HTML/LLM surface. | `web/dashboard.py`, new route modules |
| P2 | Batch merchant normalization and stream recomputation further. | Tier S added a raw merchant cache; full preloading/incremental streams remain. | `analysis/merchants.py`, `analysis/enrich.py`, `analysis/streams.py` |
| P2 | Build plugin/runtime layer on analytics contracts. | Tier T added contracts, not a full plugin/dashboard runtime. | `core/*`, `web/*` |

## 30-Day Plan

1. Done in Tier Q: update vulnerable dependencies and make dependency audit blocking.
2. Done in Tier Q: align threshold docs/prose to `0.73`.
3. Done in Tier Q: mask IBANs in account UI and tests.
4. Done in Tier Q: escape direct dynamic `HTMLResponse` fragments.
5. Done in Tier Q: disable callback access logs by default.
6. Done in Tier Q: fix `scripts/finance-all.sh` failure handling and stale
   `cache_read_tokens` query.
7. Done in Tier R/S/T: partial sync rollback, DB locks, WAL/indexes,
   migrations, stream-integrity tests, EB retries, and analytics contracts.

## 60-Day Plan

1. Add auth and CSRF/origin checks.
2. Vendor Tailwind/HTMX/Chart.js or replace the CDN strategy.
3. Add CSP/security headers.
4. Move sync/reenrich/LLM jobs to background execution and persistent job
   progress.
5. Add retry/page counts to `sync_runs`.
6. Add old-schema migration tests for the next non-additive migration.

## 90-Day Plan

1. Move long web tasks to background jobs.
2. Split CLI command groups after extracting service functions.
3. Split dashboard route families.
4. Batch merchant normalization and stream updates.
5. Extend the new `CanonicalEvent`, `DatasetAdapter`, and `MetricSpec`
   contracts into a plugin/runtime layer.
6. Build one non-finance dashboard page on top of the usage-event adapter.

## Suggested Issue Batches

### Batch A: Security Baseline (Done In Tier Q)

- Upgraded vulnerable locked dependencies.
- Made `pip-audit` blocking in CI with one explicit no-fixed-version ignore.
- Masked IBANs.
- Escaped direct dynamic HTML fragments found in this pass.
- Disabled callback access logs by default.

### Batch B: Job Safety (Tier R Done, Worker Still Open)

- Done: job locks, partial sync rollback, WAL/busy timeout, EB retry/backoff,
  and tests for held locks/failed syncs.
- Open: background worker/job queue and richer persisted progress.

### Batch C: Maintainability (Tier S Partly Done)

- Done: migration tracking/tests and stream-integrity cleanup.
- Extract CLI service functions.
- Add CLI command-contract tests.
- Split dashboard route modules.

### Batch D: Analytics Platform Spike (Tier T Partly Done)

- Done: define `CanonicalEvent`, `DatasetAdapter`, `MetricSpec`, and
  finance metric specs.
- Done: add usage-event CSV adapter proof.
- Add taxonomy metadata.
- Add plugin/runtime registration and a second-domain dashboard page.
