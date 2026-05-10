# Methodology And Evidence

## Goal

Audit the current repository for cleanliness, security, scalability, and
reuse as a data analytics foundation. Existing `CLAUDE.md` and `AUDIT.md`
were read for context but were not treated as source of truth.

## Contrarian Review Design

Four independent read-only subagents inspected distinct risk areas:

| Agent | Lens | Output Used For |
|---|---|---|
| A | Cleanliness, maintainability, architecture, tests | Maintainability findings and code organization risks. |
| B | Security, privacy, web/LLM/data handling | Security and privacy findings. |
| C | Scalability, operations, performance, deployment | Scalability and operational risks. |
| D | Reusable analytics/data platform potential | Platform contracts and reuse roadmap. |

The parent audit then re-read the code directly, ran checks, and
consolidated overlaps. Agent findings that could not be verified locally
were not elevated.

## Initial Audit Verification

Commands run from repo root:

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run ruff check .
UV_CACHE_DIR=/tmp/uv-cache uv run mypy src/finance
UV_CACHE_DIR=/tmp/uv-cache uv run vulture src/finance --min-confidence 80
UV_CACHE_DIR=/tmp/uv-cache uv run pytest -q
UV_CACHE_DIR=/tmp/uv-cache uv run pytest --cov=finance --cov-report=term-missing:skip-covered -q
UV_CACHE_DIR=/tmp/uv-cache uv run pip-audit --skip-editable
```

Results:

| Check | Result |
|---|---|
| Ruff | Passed. |
| Mypy | Passed: no issues in 42 source files. |
| Vulture | Passed: no output at `--min-confidence 80`. |
| Pytest | Passed: 207 tests. |
| Coverage | Passed: 70% total coverage. |
| pip-audit | Failed: 10 known vulnerabilities in 5 packages. |

## Tier Q Verification

After the Tier Q security baseline:

| Check | Result |
|---|---|
| Ruff | Passed. |
| Mypy | Passed: no issues in 43 source files. |
| Vulture | Passed: no output at `--min-confidence 80`. |
| Focused pytest | Passed: 54 tests across web flow/dashboard/LLM categorization. |
| Full pytest | Passed: 209 tests. |
| pip-audit | Passed with explicit ignore for no-fixed-version `CVE-2026-3219`. |

## Tier R/S/T Verification

After the operations, migration, and analytics-contract pass:

| Check | Result |
|---|---|
| Ruff | Passed for `src tests`. |
| Mypy | Passed: no issues in 47 source files. |
| Vulture | Passed: no output at `--min-confidence 80`. |
| Focused pytest | Passed: 34 tests across DB store, sync, EB client, merchant ops, and core analytics. |
| Full pytest | Passed: 218 tests. |
| pip-audit | Passed with explicit ignore for no-fixed-version `CVE-2026-3219`. |
| Shell syntax | `bash -n scripts/finance-all.sh` passed. |

## Tier U Verification

After the local dashboard browser-boundary pass:

| Check | Result |
|---|---|
| Ruff | Passed for `src tests`. |
| Mypy | Passed: no issues in 47 source files. |
| Vulture | Passed: no output at `--min-confidence 80`. |
| Focused pytest | Passed: 41 tests across web flow and dashboard coverage. |
| Full pytest | Passed: 221 tests. |
| pip-audit | Passed with explicit ignore for no-fixed-version `CVE-2026-3219`. |
| Shell syntax | `bash -n scripts/finance-all.sh` passed. |

The first `uv` attempts failed because the default uv cache under
`/home/genge/.cache/uv` was read-only in this sandbox. Re-running with
`UV_CACHE_DIR=/tmp/uv-cache` resolved that.

## Coverage Snapshot

Coverage is strong in domain modules and weak around user entry points:

| Area | Coverage Signal |
|---|---|
| Total | 70%. |
| `src/finance/cli.py` | 20%, 1574 lines, 806 statements. |
| `src/finance/web/dashboard.py` | 83%, but many LLM/progress/write branches remain uncovered. |
| `src/finance/analysis/reports.py` | 0%. |
| `src/finance/web/tls.py` | 0%. |
| `src/finance/auth/keys.py` | 61%. |
| `src/finance/analysis/recurring.py` | 68%. |

## Severity Model

| Level | Meaning |
|---|---|
| P0 | Active security/correctness risk or stale dependency risk. Should be fixed before sharing or relying on automation. |
| P1 | Important design/operations risk that blocks scale, network exposure, or platform reuse. |
| P2 | Maintainability/reuse improvement with clear payoff but less immediate blast radius. |
| P3 | Polish or future-proofing. |

## Constraints

- The initial audit did not modify application code. Tier Q, Tier R/S/T, and
  Tier U later applied remediation passes and are recorded in `AUDIT.md`.
- It did not attempt live Enable Banking calls.
- It did not inspect private history beyond local files in this public mirror.
- `pip-audit` required network access and was run after approval.
