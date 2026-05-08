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

It is not currently safe to expose beyond localhost, not operationally
robust under overlapping jobs, and not yet a reusable analytics platform.
The largest gaps are explicit web hardening, data/LLM privacy boundaries,
sync/job concurrency, schema/migration discipline, and domain-neutral
analytics contracts.

## Roadmap Status

- **Tier Q landed:** security baseline, dependency audit fixes, threshold
  docs drift, masked IBAN display, escaped dynamic HTML fragments, callback
  access-log reduction, and `finance-all.sh` hardening.
- **Tier R planned:** job safety, partial-sync semantics, SQLite tuning,
  indexes, and job locks.
- **Tier S planned:** maintainability refactor prep for CLI/web and
  migration discipline.
- **Tier T planned:** reusable analytics contracts and a second-domain
  platform spike.

## Current Verification Snapshot

- `UV_CACHE_DIR=/tmp/uv-cache uv run ruff check .` passed.
- `UV_CACHE_DIR=/tmp/uv-cache uv run mypy src/finance` passed.
- `UV_CACHE_DIR=/tmp/uv-cache uv run vulture src/finance --min-confidence 80` passed.
- `UV_CACHE_DIR=/tmp/uv-cache uv run pytest -q` passed: 209 tests.
- `UV_CACHE_DIR=/tmp/uv-cache uv run pip-audit --skip-editable --ignore-vuln CVE-2026-3219` passed.
