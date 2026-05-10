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
and analytics contracts; Tier U added a local browser security boundary.
Moving long work to true background jobs, growing to larger datasets, and
plugging in another analytics domain still need deliberate product work.

## Quality Ratings

| Area | Rating | Reason |
|---|---:|---|
| Local single-user functionality | B+ | Strong tests, clear domain modules, passing ruff/mypy/vulture/pytest. |
| Maintainability | B- | Good module split, but `cli.py` and `web/dashboard.py` are large coordination blobs. |
| Security for localhost-only use | B | Tier U added token-cookie auth, CSRF/origin checks, local assets, and CSP; LLM/data privacy boundaries remain weak. |
| Security if exposed on a network | C- | Better than unauthenticated localhost, but still no multi-user auth model, deployment/TLS posture, or privacy hardening. |
| Scalability/operations | B- | Tier R fixed partial syncs, added locks, WAL/timeouts/indexes, and EB retries; long jobs are still inline. |
| Reuse for other analytics domains | C+ | Tier T added contracts and one non-finance adapter proof; no plugin runtime/dashboard yet. |

## Strongest Parts

- Domain modules are reasonably separated: `analysis`, `llm`, `web`, `eb`, `db`.
- Core analysis tests are substantial; 221 tests pass after Tier U
  (207 at the initial audit snapshot).
- Static quality gates are currently clean for ruff, mypy, and vulture.
- Enable Banking private keys are age-encrypted and chmodded `0600`.
- SQL is parameterized in normal data paths, with explicit foreign keys enabled.
- A canonical transaction DataFrame boundary exists in
  [analysis/io.py](../../src/finance/analysis/io.py).
- LLM calls are behind structured wrappers, usage logging, and key redaction.

## Highest-Risk Findings

1. Tier U added local dashboard token-cookie auth, CSRF/origin checks,
   local assets, CSP, and security headers. This substantially improves
   localhost use, but it is not a full multi-user or cloud deployment model.
2. LLM categorization sends merchant names and raw bank memo examples to
   external or tool-enabled providers, including Claude CLI prompts that
   explicitly permit WebSearch.
3. Dependency audit failed in the initial audit, including runtime
   `python-multipart 0.0.26`; Tier Q upgraded the vulnerable locked
   packages and made CI audit blocking except for no-fixed-version
   `CVE-2026-3219`.
4. Sync/enrich/LLM jobs now have DB-backed overlap locks, but still run
   inline instead of through a background worker.
5. Tier R fixed partial account-sync commits; failed pages roll back inserted
   rows and record failed-run counts.
6. Enrichment and dashboard analytics still rely on full scans/DataFrames and
   per-transaction fuzzy matching that will degrade with larger histories.
7. The initial audit found LLM auto-write threshold docs drift; Tier Q
   aligned docs/prose to the implemented `0.73` threshold.
8. Tier T added domain-neutral `CanonicalEvent`, `DatasetAdapter`, and
   `MetricSpec` contracts, but the repo is still finance-shaped at the UI,
   schema, and plugin-runtime layers.

## Best Next Moves

Treat the next work as three tracks:

- Security and privacy baseline: build on Tier U's local auth/CSRF/CSP by
  defining LLM redaction/minimization, hardening raw data storage, and
  keeping the Tier Q dependency/UI-fragment protections active.
- Operational reliability: move locked inline jobs to background execution
  with persistent job state and progress.
- Analytics platform readiness: turn the new contracts into plugin/runtime
  boundaries and add a non-finance dashboard path.
