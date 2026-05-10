# Repository Audit - 2026-05-07

Fresh contrarian audit of the current public `finance` repository state.
This audit used `README.md`, `CLAUDE.md`, and `AUDIT.md` only as context,
not as authority. Findings below are based on direct source inspection,
four independent contrarian subagent passes, and local verification commands.

## Reading Order

1. [00 Executive Summary](00-executive-summary.md)
2. [01 Methodology And Evidence](01-methodology-and-evidence.md)
3. [02 Cleanliness And Maintainability](02-cleanliness-and-maintainability.md)
4. [03 Security And Privacy](03-security-and-privacy.md)
5. [04 Scalability And Operations](04-scalability-and-operations.md)
6. [05 Reusable Analytics Platform](05-reusable-analytics-platform.md)
7. [06 Remediation Roadmap](06-remediation-roadmap.md)
8. [07 Evidence Appendix](07-evidence-appendix.md)

## Short Verdict

The repo is clean and coherent for a single-user, local personal finance
tool. It has strong domain tests, clear SQLite/Pandas analysis paths,
age-encrypted Enable Banking key handling, and useful LLM isolation.

Tier U makes the local browser dashboard much safer against accidental
exposure and cross-site localhost writes, but this is still not a multi-user
or cloud-hardened service. It is also still not a full reusable analytics
platform. The largest remaining gaps are data/LLM privacy boundaries,
background job execution, route/CLI decomposition, and plugin/runtime
integration.

## Roadmap Status

- **Tier Q landed:** security baseline, dependency audit fixes, threshold
  docs drift, masked IBAN display, escaped dynamic HTML fragments, callback
  access-log reduction, and `finance-all.sh` hardening.
- **Tier R landed:** atomic per-account sync, DB-backed locks,
  SQLite WAL/busy timeout/indexes, and EB retries.
- **Tier S landed:** migration tracking/tests, stream-integrity cleanup
  after merchant merges, and an enrichment cache.
- **Tier T landed:** `finance.core` analytics contracts, finance metric
  specs, and a usage-event CSV adapter proof.
- **Tier U landed:** local dashboard token-cookie auth, CSRF/origin checks,
  local JS/CSS assets, self-only CSP, and browser security headers.

## Current Verification Snapshot

- `UV_CACHE_DIR=/tmp/uv-cache uv run ruff check src tests` passed.
- `UV_CACHE_DIR=/tmp/uv-cache uv run mypy src/finance` passed.
- `UV_CACHE_DIR=/tmp/uv-cache uv run vulture src/finance --min-confidence 80` passed.
- `UV_CACHE_DIR=/tmp/uv-cache uv run pytest -q` passed: 221 tests.
- `UV_CACHE_DIR=/tmp/uv-cache uv run pip-audit --skip-editable --ignore-vuln CVE-2026-3219` passed.
