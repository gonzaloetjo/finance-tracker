# Remediation Roadmap

## Prioritized Backlog

| Priority | Work | Why | Main Files |
|---|---|---|---|
| Done Q | Fix dependency audit findings, at least runtime `python-multipart`. | Tier Q upgraded vulnerable locked packages and made CI audit blocking except no-fixed-version `CVE-2026-3219`. | `pyproject.toml`, `uv.lock`, `.github/workflows/ci.yml` |
| Done Q | Fix LLM threshold docs/code drift. | Tier Q aligned docs/prose to implemented `0.73`. | `llm/categorize.py`, `README.md` |
| Done Q | Mask IBANs in web UI. | Tier Q masks account display. | `_account_row.html`, tests |
| Done Q | Escape direct HTML fragments. | Tier Q escapes provider/local error fragments. | `web/dashboard.py` |
| P1 | Add local auth and CSRF/origin checks. | Required before any non-loopback exposure and useful even on localhost. | `web/app.py`, templates |
| P1 | Vendor JS assets and add CSP. | CDN script compromise can read bank data. | `web/templates/base.html`, static assets |
| P1 | Define LLM privacy/minimization boundary. | Raw memos and merchant context leave the machine. | `llm/categorize.py`, `llm/providers.py`, web settings |
| P1 | Fix partial sync transaction semantics. | Failed account sync can commit partial rows. | `sync.py`, tests |
| P1 | Add job locks/background jobs for sync/enrich/LLM. | Prevent overlapping long operations and blocked web requests. | `db/schema.sql`, `sync.py`, `web/*`, `llm/*` |
| P1 | Enable SQLite WAL/busy timeout and add hot indexes. | Improve local concurrency and larger histories. | `db/store.py`, `db/schema.sql` |
| P2 | Add migration version table/runner. | Current additive migration approach will not scale. | `db/store.py`, `db/migrations/*` |
| P2 | Split `cli.py` by command group through service functions. | User entry points are large and under-tested. | `cli.py`, new `cli/*` or `services/*` |
| P2 | Split `web/dashboard.py` by route family. | Reduce mixed route/SQL/HTML/LLM surface. | `web/dashboard.py`, new route modules |
| P2 | Batch merchant normalization and stream recomputation. | Avoid O(transactions x merchants) enrichment growth. | `analysis/merchants.py`, `analysis/enrich.py`, `analysis/streams.py` |
| P2 | Formalize analytics contracts. | Required for reuse beyond finance. | new `core/*`, `analysis/*` |

## 30-Day Plan

1. Done in Tier Q: update vulnerable dependencies and make dependency audit blocking.
2. Done in Tier Q: align threshold docs/prose to `0.73`.
3. Done in Tier Q: mask IBANs in account UI and tests.
4. Done in Tier Q: escape direct dynamic `HTMLResponse` fragments.
5. Done in Tier Q: disable callback access logs by default.
6. Done in Tier Q: fix `scripts/finance-all.sh` failure handling and stale
   `cache_read_tokens` query.
7. Next for Tier R: add tests for partial account sync failure.

## 60-Day Plan

1. Add auth and CSRF/origin checks.
2. Vendor Tailwind/HTMX/Chart.js or replace the CDN strategy.
3. Add CSP/security headers.
4. Add SQLite WAL, busy timeout, and hot indexes.
5. Add job/lock table and prevent overlapping sync/enrich/LLM jobs.
6. Add EB retry/backoff with retry counts in `sync_runs`.
7. Add old-schema migration tests.

## 90-Day Plan

1. Move long web tasks to background jobs.
2. Split CLI command groups after extracting service functions.
3. Split dashboard route families.
4. Batch merchant normalization and stream updates.
5. Introduce `CanonicalEvent`, `DatasetAdapter`, `MetricSpec`, and richer
   taxonomy metadata.
6. Build one non-finance proof-of-concept dataset with two metrics and one
   dashboard page.

## Suggested Issue Batches

### Batch A: Security Baseline (Done In Tier Q)

- Upgraded vulnerable locked dependencies.
- Made `pip-audit` blocking in CI with one explicit no-fixed-version ignore.
- Masked IBANs.
- Escaped direct dynamic HTML fragments found in this pass.
- Disabled callback access logs by default.

### Batch B: Job Safety

- Add job locks.
- Fix partial sync commits.
- Add SQLite WAL/busy timeout.
- Add EB retry/backoff.
- Add tests for overlapping and failed syncs.

### Batch C: Maintainability

- Extract CLI service functions.
- Add CLI command-contract tests.
- Split dashboard route modules.
- Add migration runner and migration tests.

### Batch D: Analytics Platform Spike

- Define `CanonicalEvent` and `MetricSpec`.
- Add taxonomy metadata.
- Convert one existing finance metric to declare a metric spec.
- Add a second small dataset as proof.
