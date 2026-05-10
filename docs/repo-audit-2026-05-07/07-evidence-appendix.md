# Evidence Appendix

## Repository Shape

Key source files by line count:

| File | Lines |
|---|---:|
| `src/finance/cli.py` | 1574 |
| `src/finance/web/dashboard.py` | 871 |
| `src/finance/analysis/merchants.py` | 462 |
| `src/finance/llm/categorize.py` | 313 |
| `src/finance/analysis/streams.py` | 311 |
| `src/finance/web/app.py` | 302 |
| `src/finance/analysis/subscriptions.py` | 279 |
| `src/finance/llm/client.py` | 256 |
| `src/finance/analysis/totals.py` | 251 |

Test suite:

- 26 `tests/test_*.py` files.
- 221 tests passing after Tier U.
- 70% total coverage.

## Verification Results

```text
ruff check src tests                              passed
mypy src/finance                                  passed
vulture src/finance --min-confidence 80           passed
pytest -q                                         221 passed
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
| Dashboard startup token | [cli.py](../../src/finance/cli.py#L304) |
| Local auth/CSRF/security-header middleware | [web/app.py](../../src/finance/web/app.py#L155) |
| POST `/sync` inline work | [web/app.py](../../src/finance/web/app.py#L268) |
| LLM web categorization inline | [dashboard.py](../../src/finance/web/dashboard.py#L220) |
| Rules reenrich inline | [dashboard.py](../../src/finance/web/dashboard.py#L700) |
| Local dashboard JS | [app.js](../../src/finance/web/static/app.js#L1) |
| Local dashboard CSS | [app.css](../../src/finance/web/static/app.css#L1) |
| CSRF token meta tag | [base.html](../../src/finance/web/templates/base.html#L6) |
| Masked IBAN display | [_account_row.html](../../src/finance/web/templates/_account_row.html#L8) |
| LLM memo prompt construction | [categorize.py](../../src/finance/llm/categorize.py#L131) |
| Claude CLI WebSearch instruction | [providers.py](../../src/finance/llm/providers.py#L83) |
| Raw transaction JSON persistence | [sync.py](../../src/finance/sync.py#L109) |
| DB private chmod | [store.py](../../src/finance/db/store.py#L27) |
| Atomic sync failure handling | [sync.py](../../src/finance/sync.py#L77), [sync.py](../../src/finance/sync.py#L134) |
| Fuzzy match per unknown merchant | [merchants.py](../../src/finance/analysis/merchants.py#L132) |
| Full stream scan | [streams.py](../../src/finance/analysis/streams.py#L114) |
| DataFrame hydration | [io.py](../../src/finance/analysis/io.py#L65) |
| WAL/busy timeout | [store.py](../../src/finance/db/store.py#L38) |
| Operational indexes | [schema.sql](../../src/finance/db/schema.sql#L50) |
| Shell script failure tracking | [finance-all.sh](../../scripts/finance-all.sh#L32) |
| Shell script current LLM cost SQL | [finance-all.sh](../../scripts/finance-all.sh#L121) |
| Threshold constant | [categorize.py](../../src/finance/llm/categorize.py#L35) |
| Schema migrations | [store.py](../../src/finance/db/store.py#L88) |
| Direct finance schema | [schema.sql](../../src/finance/db/schema.sql#L34) |
| Hardcoded finance nav | [base.html](../../src/finance/web/templates/base.html#L45) |
| CI blocks pip-audit except no-fixed-version pip CVE | [ci.yml](../../.github/workflows/ci.yml#L29) |
| DB-backed locks | [store.py](../../src/finance/db/store.py#L126) |
| Analytics contracts | [analytics.py](../../src/finance/core/analytics.py#L11) |
| Usage adapter proof | [usage.py](../../src/finance/core/usage.py#L19) |
| Finance metric specs | [metric_specs.py](../../src/finance/analysis/metric_specs.py#L5) |

## Contrarian Agent Consensus

Areas where independent agents agreed:

- Current repo is clean for a local single-user finance tool.
- Tier U closed the unauthenticated local browser-write cliff, but broader
  web exposure still needs a deliberate deployment/auth model.
- LLM privacy needs a formal boundary.
- CLI and dashboard router are the two largest maintainability surfaces.
- SQLite/Pandas are acceptable now, but jobs/locks/indexes are needed before
  scaling the workflow.
- Reusable analytics requires explicit contracts, not just cleaner modules.

Areas requiring judgement:

- Multi-tenant auth and cloud deployment are not needed for the stated product,
  but the Tier U local auth/CSRF boundary should remain in place even for
  localhost-only use.
- Generalizing too early would damage the finance product. The right move is
  a small platform vocabulary plus one second-domain proof, not a broad rewrite.
- Some security issues are acceptable only if the app remains strictly
  loopback and trusted-user. Tier U improves the browser boundary, but does
  not turn the app into a shared service.
