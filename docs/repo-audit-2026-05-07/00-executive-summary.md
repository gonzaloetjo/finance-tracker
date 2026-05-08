# Executive Summary

Date: 2026-05-07

Scope: current repo state at `/home/genge/dev-ash/foundry-nodevenv/gonzalo/finance-public`.

## Verdict

This is a solid single-user finance product, not a general analytics
platform yet. The codebase is meaningfully cleaner than a prototype:
domain logic is mostly separated from web rendering, tests cover many
finance behaviors, static checks pass, and the existing audit trail shows
serious prior cleanup work.

The contrarian answer is that cleanliness is concentrated in the core
finance algorithms. Tier R/S/T added operational locks, migration tracking,
and analytics contracts, but exposing the dashboard, moving long work to
true background jobs, growing to larger datasets, or plugging in another
analytics domain still needs deliberate product work.

## Quality Ratings

| Area | Rating | Reason |
|---|---:|---|
| Local single-user functionality | B+ | Strong tests, clear domain modules, passing ruff/mypy/vulture/pytest. |
| Maintainability | B- | Good module split, but `cli.py` and `web/dashboard.py` are large coordination blobs. |
| Security for localhost-only use | B- | Key handling is good, but browser/web and LLM privacy boundaries are weak. |
| Security if exposed on a network | D | No app auth, no CSRF, CDN scripts on sensitive pages, configurable host. |
| Scalability/operations | B- | Tier R fixed partial syncs, added locks, WAL/timeouts/indexes, and EB retries; long jobs are still inline. |
| Reuse for other analytics domains | C+ | Tier T added contracts and one non-finance adapter proof; no plugin runtime/dashboard yet. |

## Strongest Parts

- Domain modules are reasonably separated: `analysis`, `llm`, `web`, `eb`, `db`.
- Core analysis tests are substantial; 218 tests pass after Tier R/S/T
  (207 at the initial audit snapshot).
- Static quality gates are currently clean for ruff, mypy, and vulture.
- Enable Banking private keys are age-encrypted and chmodded `0600`.
- SQL is parameterized in normal data paths, with explicit foreign keys enabled.
- A canonical transaction DataFrame boundary exists in
  [analysis/io.py](../../src/finance/analysis/io.py).
- LLM calls are behind structured wrappers, usage logging, and key redaction.

## Highest-Risk Findings

1. The dashboard has no authentication or CSRF protection. Default loopback
   helps, but `finance serve --host 0.0.0.0` would expose personal financial
   data and write actions.
2. The web UI loads third-party scripts from CDNs on pages that render bank
   data, without vendoring, SRI, or CSP.
3. LLM categorization sends merchant names and raw bank memo examples to
   external or tool-enabled providers, including Claude CLI prompts that
   explicitly permit WebSearch.
4. Dependency audit failed in the initial audit, including runtime
   `python-multipart 0.0.26`; Tier Q upgraded the vulnerable locked
   packages and made CI audit blocking except for no-fixed-version
   `CVE-2026-3219`.
5. Sync/enrich/LLM jobs now have DB-backed overlap locks, but still run
   inline instead of through a background worker.
6. Tier R fixed partial account-sync commits; failed pages roll back inserted
   rows and record failed-run counts.
7. Enrichment and dashboard analytics still rely on full scans/DataFrames and
   per-transaction fuzzy matching that will degrade with larger histories.
8. The initial audit found LLM auto-write threshold docs drift; Tier Q
   aligned docs/prose to the implemented `0.73` threshold.
9. Tier T added domain-neutral `CanonicalEvent`, `DatasetAdapter`, and
   `MetricSpec` contracts, but the repo is still finance-shaped at the UI,
   schema, and plugin-runtime layers.

## Best Next Moves

Treat the next work as three tracks:

- Security and privacy baseline: local auth/CSRF, vendor scripts/CSP,
  define LLM redaction/minimization, harden raw data storage, and retain
  the Tier Q dependency/UI-fragment protections.
- Operational reliability: move locked inline jobs to background execution
  with persistent job state and progress.
- Analytics platform readiness: turn the new contracts into plugin/runtime
  boundaries and add a non-finance dashboard path.
