# Reusable Analytics Platform

## Verdict

The repo has good reusable patterns, but it is not yet a reusable analytics
platform. The blocker is not general code quality. The blocker is that the
domain contract is currently "whatever finance tables and Pandas functions
expect."

Finance should remain the first product domain. A platform layer should be
introduced beside it, then proven with a second small domain before broad
refactors.

## Useful Domain Coupling

Some finance coupling is product value, not mess:

- Enable Banking adapter code is isolated under `eb` and `sync`.
- Bank profile memo parsing is a useful finance extension pattern:
  [bank_profile.py](../../src/finance/analysis/bank_profile.py).
- Finance concepts like `Transfer`, `Investment`, `Income`, subscriptions,
  and spend-only filters are real product semantics:
  [io.py](../../src/finance/analysis/io.py#L19),
  [streams.py](../../src/finance/analysis/streams.py#L21),
  [totals.py](../../src/finance/analysis/totals.py#L18).

Keep these in a finance layer. Do not erase them in the name of abstraction.

## Harmful Coupling For Reuse

### R1. Core Schema Is Finance-Shaped

Evidence:

- `transactions` bakes in accounts, amounts, currency, creditor/debtor, and
  remittance fields in [schema.sql](../../src/finance/db/schema.sql#L34).
- Enrichment bakes in `merchants`, `streams`, and `txn_type` in
  [schema.sql](../../src/finance/db/schema.sql#L61).

Why it matters:

Other analytics domains may have events, entities, measures, dimensions,
observations, or records rather than bank transactions and merchants.

### R2. IO Boundary Is Not Actually Domain-Neutral

Evidence:

- Canonical finance DataFrame exists in
  [analysis/io.py](../../src/finance/analysis/io.py).
- But many metrics read finance tables directly:
  [merchants.py](../../src/finance/analysis/merchants.py#L213),
  [recurring.py](../../src/finance/analysis/recurring.py),
  [subscriptions.py](../../src/finance/analysis/subscriptions.py#L74),
  [forecast.py](../../src/finance/analysis/forecast.py#L41).

Why it matters:

Reusable metrics need declared inputs and stable contracts, not implicit
table knowledge scattered through functions.

### R3. Taxonomy Is Flat But Behavior Is Scattered

Evidence:

- Category names live in
  [taxonomy.yaml](../../src/finance/llm/prompts/taxonomy.yaml).
- Non-spend behavior lives in
  [io.py](../../src/finance/analysis/io.py#L22).
- Non-subscription behavior lives in
  [streams.py](../../src/finance/analysis/streams.py#L34).
- Essential/optional behavior lives in
  [totals.py](../../src/finance/analysis/totals.py#L21).

Why it matters:

A reusable analytics product needs category metadata, not just names.

### R4. Web Navigation And Pages Are Finance Product-Specific

Evidence:

- App mounts one dashboard router in
  [web/app.py](../../src/finance/web/app.py#L63).
- Base nav is hardcoded to merchants/subscriptions/forecast/alerts in
  [base.html](../../src/finance/web/templates/base.html#L45).

Why it matters:

Plugins need to contribute nav items, pages, widgets, and metric views.

## Reusable Patterns Already Present

| Pattern | Current Implementation | Reuse Potential |
|---|---|---|
| Canonical frame | `load_transactions()` in `analysis/io.py` | Basis for `load_events()`. |
| Entity resolution | merchant normalization and aliases | Reusable as `EntityResolver`. |
| Recurring detection | streams from entity + amount bucket + cadence | Reusable time-series pattern. |
| Metric composition | `build_overview()` | Basis for dashboard registry. |
| LLM structured output | `LLMClient.parse_structured()` | Reusable across domains. |
| Advisory registry | `ADVISORY_KINDS` in `advise_dispatch.py` | Basis for metric/advisory plugins. |
| Generic table partial | `_table.html` | Reusable UI component. |

## Platform Contracts

### DatasetAdapter (Introduced In Tier R/S/T)

Needed contract:

```python
class DatasetAdapter(Protocol):
    name: str
    def sync(self, store: Store, *, since: str | None = None) -> SyncSummary: ...
    def normalize(self, raw: object) -> list[CanonicalEvent]: ...
```

Current status:

Tier R/S/T introduced a `DatasetAdapter` protocol in
[core/analytics.py](../../src/finance/core/analytics.py), plus a
non-finance `UsageCsvAdapter` proof in
[core/usage.py](../../src/finance/core/usage.py). `sync_account()` remains
finance-specific imperative code.

### CanonicalEvent (Introduced In Tier R/S/T)

Needed fields:

- `event_id`
- `timestamp`
- `entity_id`
- `entity_name`
- `measure`
- `value`
- `unit`
- `polarity`
- `dimensions`
- `source`
- `raw_json`
- `provenance`

Finance mapping:

Bank transaction amount becomes `value`, merchant becomes `entity`,
currency becomes `unit`, account/session becomes dimensions.

### MetricSpec (Introduced In Tier R/S/T)

Needed fields:

- metric id and title
- input contract
- SQL/DataFrame dependencies
- parameters
- output schema
- freshness/caching policy
- visualization hints
- privacy level

Current status:

Tier R/S/T added `MetricSpec` and `MetricRegistry` in
[core/analytics.py](../../src/finance/core/analytics.py), and finance metric
specs for monthly totals, subscription streams, and merchant outflow in
[metric_specs.py](../../src/finance/analysis/metric_specs.py). Metrics are
still normal functions taking `sqlite3.Connection`.

### Taxonomy Metadata

Current taxonomy is a flat list. A platform-ready taxonomy should carry:

```yaml
- name: Transfer
  rollup: non_spend
  polarity: neutral
  subscription_excluded: true
  prompt_hint: Internal transfers and account-to-account moves.
```

### Plugin Registration

Needed registration points:

- migrations
- dataset adapters
- taxonomies
- entity resolvers
- metrics
- LLM advisories
- web routes/nav/widgets
- fixtures and contract tests

## Staged Platform Roadmap

1. Done in Tier R/S/T: keep finance behavior intact and introduce platform
   vocabulary beside it with `CanonicalEvent`, `DatasetAdapter`, and
   `MetricSpec`.
2. Done in Tier R/S/T: formalize several existing finance outputs with
   metric specs without rewriting algorithms.
3. Done in Tier R/S/T: prove a second non-finance adapter with usage-event
   CSV rows.
4. Next: move category behavior into richer taxonomy metadata.
5. Next: put a real data-access boundary under Stage C: either metrics consume
   `load_transactions/load_events`, or each metric declares direct SQL
   dependencies.
6. Extract finance plugin pieces: EB adapter, bank profiles, memo parser,
   merchant resolver, finance taxonomy, subscription metrics, finance pages.
7. Build a real non-finance dashboard path before broad refactors.
