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
finance algorithms, while operational and platform boundaries are still
thin. Exposing the dashboard, overlapping sync/enrich/LLM jobs, growing
to larger datasets, or plugging in another analytics domain would reveal
the main weaknesses quickly.

## Quality Ratings

| Area | Rating | Reason |
|---|---:|---|
| Local single-user functionality | B+ | Strong tests, clear domain modules, passing ruff/mypy/vulture/pytest. |
| Maintainability | B- | Good module split, but `cli.py` and `web/dashboard.py` are large coordination blobs. |
| Security for localhost-only use | B- | Key handling is good, but browser/web and LLM privacy boundaries are weak. |
| Security if exposed on a network | D | No app auth, no CSRF, CDN scripts on sensitive pages, configurable host. |
| Scalability/operations | C | Fine for personal scale, weak for concurrent jobs, larger datasets, and retries. |
| Reuse for other analytics domains | C | Several reusable patterns exist, but no formal dataset/metric/plugin contracts. |

## Strongest Parts

- Domain modules are reasonably separated: `analysis`, `llm`, `web`, `eb`, `db`.
- Core analysis tests are substantial; 209 tests pass after Tier Q
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
5. Sync/enrich/LLM jobs have no durable job lock or scheduler boundary.
   Inline web requests can block, overlap, and contend on SQLite.
6. `sync_account()` can commit partial inserted transactions on later
   account-level failure while returning `added=0`.
7. Enrichment and dashboard analytics rely on full scans/DataFrames and
   per-transaction fuzzy matching that will degrade with larger histories.
8. The initial audit found LLM auto-write threshold docs drift; Tier Q
   aligned docs/prose to the implemented `0.73` threshold.
9. The repo is finance-shaped end to end. It lacks domain-neutral
   `CanonicalEvent`, `DatasetAdapter`, `MetricSpec`, plugin, and migration
   contracts needed for reusable analytics.

## Best Next Moves

Treat the next work as three tracks:

- Security and privacy baseline: local auth/CSRF, vendor scripts/CSP,
  define LLM redaction/minimization, harden raw data storage, and retain
  the Tier Q dependency/UI-fragment protections.
- Operational reliability: add SQLite WAL/busy timeout, job locks, background
  job records, retry/backoff for EB, and fix partial sync transaction semantics.
- Analytics platform readiness: formalize canonical data/metric contracts
  without rewriting finance behavior, then prove the abstraction with one
  small non-finance domain.
