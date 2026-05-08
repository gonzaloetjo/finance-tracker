# Evidence Appendix

## Repository Shape

Key source files by line count:

| File | Lines |
|---|---:|
| `src/finance/cli.py` | 1551 |
| `src/finance/web/dashboard.py` | 835 |
| `src/finance/analysis/merchants.py` | 454 |
| `src/finance/llm/categorize.py` | 312 |
| `src/finance/analysis/streams.py` | 301 |
| `src/finance/analysis/subscriptions.py` | 279 |
| `src/finance/llm/client.py` | 256 |
| `src/finance/analysis/totals.py` | 251 |

Test suite:

- 24 `tests/test_*.py` files.
- 209 tests passing after Tier Q.
- 70% total coverage.

## Verification Results

```text
ruff check .                                      passed
mypy src/finance                                  passed
vulture src/finance --min-confidence 80           passed
pytest -q                                         209 passed
pytest --cov=finance ...                          initial audit: 207 passed, 70% coverage
pip-audit --skip-editable --ignore-vuln CVE-2026-3219
                                                  no known vulnerabilities
```

## Initial Vulnerability Findings

From `UV_CACHE_DIR=/tmp/uv-cache uv run pip-audit --skip-editable`:

```text
Found 10 known vulnerabilities in 5 packages
jupyter-server   2.17.0  CVE-2025-61669  fix 2.18.0
jupyter-server   2.17.0  CVE-2026-40110  fix 2.18.0
jupyter-server   2.17.0  CVE-2026-35397  fix 2.18.0
jupyter-server   2.17.0  CVE-2026-40934  fix 2.18.0
jupyterlab       4.5.6   CVE-2026-42266  fix 4.5.7
jupyterlab       4.5.6   CVE-2026-42557  fix 4.5.7
mistune          3.2.0   CVE-2026-33079  fix 3.2.1
pip              26.0.1  CVE-2026-3219
pip              26.0.1  CVE-2026-6357   fix 26.1
python-multipart 0.0.26  CVE-2026-42561  fix 0.0.27
```

## Tier Q Dependency Result

Tier Q upgraded:

- `python-multipart` 0.0.26 -> 0.0.27
- `jupyter-server` 2.17.0 -> 2.18.2
- `jupyterlab` 4.5.6 -> 4.5.7
- `mistune` 3.2.0 -> 3.2.1
- `pip` 26.0.1 -> 26.1.1

`UV_CACHE_DIR=/tmp/uv-cache uv run pip-audit --skip-editable --ignore-vuln CVE-2026-3219`
now reports no known vulnerabilities. The ignore is retained because
`CVE-2026-3219` had no fixed pip version available during this pass.

## Evidence Map

| Concern | Evidence |
|---|---|
| Host can be exposed | [cli.py](../../src/finance/cli.py#L271) |
| App mounted without auth | [web/app.py](../../src/finance/web/app.py#L57) |
| POST `/sync` inline work | [web/app.py](../../src/finance/web/app.py#L122) |
| LLM web categorization inline | [dashboard.py](../../src/finance/web/dashboard.py#L220) |
| Rules reenrich inline | [dashboard.py](../../src/finance/web/dashboard.py#L700) |
| CDN scripts | [base.html](../../src/finance/web/templates/base.html#L7) |
| Masked IBAN display | [_account_row.html](../../src/finance/web/templates/_account_row.html#L8) |
| LLM memo prompt construction | [categorize.py](../../src/finance/llm/categorize.py#L131) |
| Claude CLI WebSearch instruction | [providers.py](../../src/finance/llm/providers.py#L83) |
| Raw transaction JSON persistence | [sync.py](../../src/finance/sync.py#L109) |
| DB creation lacks chmod | [store.py](../../src/finance/db/store.py#L24) |
| Partial sync risk | [sync.py](../../src/finance/sync.py#L80), [sync.py](../../src/finance/sync.py#L123) |
| Fuzzy match per unknown merchant | [merchants.py](../../src/finance/analysis/merchants.py#L132) |
| Full stream scan | [streams.py](../../src/finance/analysis/streams.py#L114) |
| DataFrame hydration | [io.py](../../src/finance/analysis/io.py#L65) |
| No WAL/busy timeout | [store.py](../../src/finance/db/store.py#L24) |
| Limited indexes | [schema.sql](../../src/finance/db/schema.sql#L48) |
| Shell script failure tracking | [finance-all.sh](../../scripts/finance-all.sh#L32) |
| Shell script current LLM cost SQL | [finance-all.sh](../../scripts/finance-all.sh#L121) |
| Threshold constant | [categorize.py](../../src/finance/llm/categorize.py#L35) |
| Ad hoc migrations | [store.py](../../src/finance/db/store.py#L32) |
| Direct finance schema | [schema.sql](../../src/finance/db/schema.sql#L34) |
| Hardcoded finance nav | [base.html](../../src/finance/web/templates/base.html#L45) |
| CI blocks pip-audit except no-fixed-version pip CVE | [ci.yml](../../.github/workflows/ci.yml#L29) |

## Contrarian Agent Consensus

Areas where independent agents agreed:

- Current repo is clean for a local single-user finance tool.
- Web exposure is the major security cliff.
- LLM privacy needs a formal boundary.
- CLI and dashboard router are the two largest maintainability surfaces.
- SQLite/Pandas are acceptable now, but jobs/locks/indexes are needed before
  scaling the workflow.
- Reusable analytics requires explicit contracts, not just cleaner modules.

Areas requiring judgement:

- Multi-tenant auth and cloud deployment are not needed for the stated product,
  but basic local auth/CSRF becomes important even before multi-tenancy.
- Generalizing too early would damage the finance product. The right move is
  a small platform vocabulary plus one second-domain proof, not a broad rewrite.
- Some security issues are acceptable only if the app remains strictly
  loopback and trusted-user. The current code does not enforce that boundary.
