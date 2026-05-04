# finance

Personal finance tracker for **French retail bank accounts** via the **Enable Banking** Open Banking API.

CLI + local FastAPI web dashboard, SQLite storage, single-user self-hosted.

## Quick start (sandbox)

Install [`uv`](https://docs.astral.sh/uv/getting-started/installation/) first if you don't already have it, then:

```bash
uv sync
# At https://enablebanking.com/, sign in, create an application, and
# choose the default in-browser keypair option. Download the private
# key as `<app_id>.pem`, then:
uv run finance import-key path/to/<app_id>.pem
uv run finance aspsps --country FR   # smoke test: must list your bank
uv run finance serve                 # open http://localhost:8000 to connect
```

> If your EB application instead asks you to upload your own public
> certificate, run `uv run finance init` first (generates a 4096-bit
> keypair, prints a `public.crt` to upload), then
> `uv run finance config set-app-id <APP_ID>` after EB returns one.

After the consent flow, sync transactions:

```bash
uv run finance sync
uv run finance analyze enrich            # build merchants + categories from the synced txs
uv run finance list --since 2026-01-01
```

## Paths

- Config: `~/.config/finance/config.toml`
- Keys: `~/.config/finance/keys/{private.key.age, public.crt}`
- DB: `~/.local/share/finance/finance.db`

Override with `FINANCE_CONFIG_DIR` / `FINANCE_DATA_DIR`. You can also
set any `FINANCE_*` variable or `ANTHROPIC_API_KEY` in a `.env` file at
the project root (see `.env.example`) — `pydantic-settings` loads it
automatically.

## One-shot script

Run every structural analysis in one go:

```bash
scripts/finance-all.sh                    # read-only, free
scripts/finance-all.sh --sync             # + pull fresh transactions first
scripts/finance-all.sh --llm              # + LLM categorize + 3 advisory calls (cents)
scripts/finance-all.sh --serve            # + launch web dashboard at the end
scripts/finance-all.sh --sync --llm --serve
```

Use this when you want to eyeball the full picture without remembering every subcommand. Each section is clearly labeled — scroll through the output top to bottom.

## Web dashboard

Start it:

```bash
uv run finance serve                                     # http://localhost:8000
uv run finance serve --port 8001                         # alternate port
```

Routes — everything structural is navigable:

| URL | What it shows |
|---|---|
| `/`                         | Overview dashboard: accounts, MTD cards, MoM trends, top merchants, active subs, forecast, alerts. Toggle `spend-only` + window from the header. |
| `/merchants`                | Top-N merchant table. Filters: `top`, `uncategorized`, `spend-only`, `since`. Each row has an inline **category picker** — change it, it's persisted with `source='user'`. |
| `/merchants/<canonical>`    | Deep-dive: summary, aliases, full tx history. Category picker. |
| `/recurring`                | Recurring streams (PRLV mandates + regular-enough FACTURE streams). |
| `/subscriptions`            | Active subscriptions + domain overlaps. |
| `/forecast`                 | Upcoming expected charges over a horizon (7 / 14 / 30 / 60 / 90 days). |
| `/alerts`                   | New-large merchants + PRLV from new merchants + recently-stopped subscriptions. |
| `/advice`                   | Persisted LLM advice cards (generate via `finance advise ...`). Dismiss with the × button. |
| `/accounts`                 | Account list with a toggle to flag `excluded_from_spend`. |
| `/transactions`             | Full transaction list with a date filter. |
| `/rules`                    | Manage regex categorization rules, then re-enrich. |
| `/settings`                 | Anthropic API key status + LLM usage totals. Store an API key here without the CLI. |
| `/connect` / `/callback`    | Enable Banking consent flow. |

LLM categorization can be triggered from the **Uncategorized** merchant page
(`/merchants?uncategorized=true`) as well as from the CLI. `finance advise ...`
advisory runs are CLI-only.

## Accounts + sessions

```bash
uv run finance accounts ls                             # list all accounts + flags
uv run finance accounts exclude <account_uid>          # drop from --spend-only analyses
uv run finance accounts include <account_uid>          # clear the flag
uv run finance sessions ls
uv run finance sessions rm <session_id> [--force]      # cascade delete (tx/balances/accounts)
```

Flag a joint savings / investment account with `accounts exclude` so it
doesn't pollute spending totals. The web overview and `analyze totals` use
spend-only mode by default; pass `--include-all` to totals or clear the web
toggle to include every account. For `analyze overview`, `analyze trends`, and
`analyze merchants`, pass `--spend-only` explicitly.

## Enrichment & analyses

After transactions are synced, persist a merchant/stream/category layer on top:

```bash
uv run finance analyze enrich              # incremental; run after every sync
uv run finance analyze enrich --reenrich   # full reprocess (preserves user overrides)
```

Read-only analyses (all accept `--csv` / `--json` for piping, unless noted):

```bash
uv run finance analyze overview [--months 3] [--top 15] [--spend-only]   # full dashboard; default off
uv run finance analyze totals [--months 3] [--spend-only/--include-all]  # headline rollups; default on
uv run finance analyze merchants [--top 30] [--uncategorized] [--spend-only]
uv run finance analyze recurring [--active-only]
uv run finance analyze subscriptions [--overlaps]
uv run finance analyze trends [--months 6] [--growth] [--spend-only]     # default off
uv run finance analyze forecast [--days 30]
uv run finance analyze alerts [--threshold 500] [--stopped]
uv run finance analyze merchant <canonical_or_alias>                     # zoom-in on one merchant
```

**Special categories**:
- `Income` — auto-assigned to merchants with a recurring monthly VIR-in stream (source=`rule-stream`). Kept separate from spend totals.
- `Transfer` — auto-assigned to any merchant with a `VIR CPTE A CPTE` transaction (BNP's explicit account-to-account tag). **Dropped from `--spend-only` analyses.**
- `Investment` — manual / LLM-assigned for crypto / broker / retirement-account deposits (Kraken, Boursorama, etc.). **Dropped from `--spend-only` analyses** — repositions wealth, doesn't consume.
- `Loan` — loan repayments (mortgage, personal, rent-to-own). **Counted as spend** — it's a real cash outflow.

Interactively categorize the long tail:

```bash
uv run finance merchant review [--limit 20] [--include-rule]   # wizard, writes source='user'
uv run finance enrich llm-categorize                           # auto via Anthropic
```

**Subscription detection** is structural (recurring + monthly cadence + stable amount) but category-gated: merchants tagged `Dining`, `Groceries`, `Income`, `Transfer`, or `Investment` are NEVER flagged as subscriptions, even when the structural rule matches (prevents false positives like weekly food orders).

**`overview`** composes all of the above into one page — start here when you want a general picture. The individual commands remain for piping / scripting. **`merchants`** (plural) is the cross-merchant ranked table; **`merchant`** (singular) is the zoom-in.

Merchant bookkeeping (writes DB):

```bash
uv run finance label <tx_id> --category <cat>          # tx-level override
uv run finance merchant set-category <name> <cat>      # merchant-level (source='user')
uv run finance merchant rename <old> <new>
uv run finance merchant merge <src> <into>
uv run finance merchant recluster [--apply] [--threshold 90]
uv run finance merchant apply-merges [--dry-run]       # curated src/finance/data/merchant_merges.yaml
uv run finance merchant seed-top [--limit 20]          # interactive curation of top uncategorized
```

Category precedence (highest wins): `tx_overrides` → `merchants.category` with `source='user'` → curated seed YAML → regex rules → LLM → NULL.

## LLM categorization + advisory

One-time: give it an API key:

```bash
export ANTHROPIC_API_KEY=sk-ant-...           # session-only
# or, persisted in OS keyring (preferred — no shell history):
uv run finance config set-llm-key
uv run finance config clear-llm-key           # remove from keyring
```

Categorize the uncategorized long tail (default model: `claude-haiku-4-5`):

```bash
uv run finance enrich llm-categorize --dry-run         # preview proposals + token cost
uv run finance enrich llm-categorize                   # auto-write ≥0.90 confidence
uv run finance enrich llm-categorize --limit 50 --model claude-sonnet-4-6
```

Re-running is a no-op once everything has a final category; user-set merchants are never re-queried.

Advisory (default model: `claude-sonnet-4-6`; cached by `input_hash` so identical economics → no LLM call):

```bash
uv run finance advise subscriptions [--refresh]
uv run finance advise cutbacks [--months 6] [--refresh]
uv run finance advise integral-offers [--refresh]
uv run finance advise ls [--all]                        # list persisted advice
uv run finance advise dismiss <id>                      # stop cache-hitting a stale row
```

All `advise` commands accept `--model` and `--csv` / `--json`.

## Cost observability

The `llm_runs` table logs every LLM call (tokens in/out, status, duration). Quick look:

```bash
uv run python -c "
import sqlite3, os
c = sqlite3.connect(os.path.expanduser('~/.local/share/finance/finance.db'))
for r in c.execute('SELECT kind, COUNT(*), SUM(input_tokens), SUM(output_tokens) FROM llm_runs WHERE status = \"ok\" GROUP BY 1'): print(r)
"
```

Prompt caching is not used: this repo's system prompts are below Anthropic's
per-block cache minimum (~1 024 tokens on Haiku, ~2 048 on Sonnet), so the
API would silently ignore any `cache_control: ephemeral` marker. The marker
and the previously-tracked `cache_read_tokens` / `cache_creation_tokens`
schema columns were removed once telemetry across ~100 LLM runs confirmed
zero cache hits.

## Troubleshooting

- **Enable Banking returns HTTP 403 on the first connect.** Go to
  [enablebanking.com](https://enablebanking.com/) → Control Panel →
  your application, and complete the activation / self-whitelisting
  step (they'll ask for your IBAN). Newly registered apps are inactive
  by default and produce a terse 403 with no hint.
- **"Invalid redirect URI" during the consent flow.** The default
  callback is `http://localhost:8000/callback` (see `config.py`) —
  register that exact URL under your Enable Banking application's
  allowed redirect URIs before running `finance serve`. Override with
  `finance config set-callback-url <url>` if you need to.
- **`finance config set-llm-key` fails or hangs on a headless Linux
  server.** `keyring` needs a running D-Bus secret service (e.g.
  `gnome-keyring` or `keepassxc`). Headless users should skip keyring
  and `export ANTHROPIC_API_KEY=sk-ant-...` in their shell /
  `~/.bashrc` / systemd unit instead — the runtime checks env first,
  then keyring.

## Testing

```bash
uv run pytest tests/                 # full suite (no network: LLM is mocked)
```
