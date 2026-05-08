#!/usr/bin/env bash
# Run every finance read-only analysis in one go.
#
# Usage:
#   scripts/finance-all.sh               # read-only tour (free)
#   scripts/finance-all.sh --sync        # + pull new transactions first
#   scripts/finance-all.sh --llm         # + LLM categorize + advisory (costs cents)
#   scripts/finance-all.sh --serve       # + launch web dashboard at the end
#   scripts/finance-all.sh --sync --llm --serve   # everything

set -euo pipefail

DO_SYNC=0
DO_LLM=0
DO_SERVE=0
FAILURES=0
for arg in "$@"; do
  case "$arg" in
    --sync)  DO_SYNC=1 ;;
    --llm)   DO_LLM=1 ;;
    --serve) DO_SERVE=1 ;;
    -h|--help)
      grep '^#' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "unknown flag: $arg" >&2; exit 1 ;;
  esac
done

section() {
  printf '\n\033[1;36m▶ %s\033[0m\n' "$1"
}

run() {
  printf '  \033[2m$ %s\033[0m\n' "$*"
  if ! "$@"; then
    printf '  \033[0;31m(command failed)\033[0m\n'
    FAILURES=$((FAILURES + 1))
  fi
}

cd "$(dirname "$0")/.."

# ─────────────────────────────────────────────────────────────────────────────
# 0. Optional: pull fresh transactions
# ─────────────────────────────────────────────────────────────────────────────
if [[ $DO_SYNC -eq 1 ]]; then
  section "Sync transactions (BNP via Enable Banking)"
  run uv run finance sync
fi

# ─────────────────────────────────────────────────────────────────────────────
# 1. Who / what is connected
# ─────────────────────────────────────────────────────────────────────────────
section "Accounts & sessions"
run uv run finance accounts ls
run uv run finance sessions ls

# ─────────────────────────────────────────────────────────────────────────────
# 2. One-page structural overview (the main event)
# ─────────────────────────────────────────────────────────────────────────────
section "Overview (spend-only, 3 months)"
run uv run finance analyze overview --spend-only --months 3 --top 15

# ─────────────────────────────────────────────────────────────────────────────
# 3. Drill-downs — in case you want more detail than overview shows
# ─────────────────────────────────────────────────────────────────────────────
section "Top 30 merchants by outflow (spend-only)"
run uv run finance analyze merchants --top 30 --spend-only

section "Top 20 uncategorized merchants (targets for categorization)"
run uv run finance analyze merchants --top 20 --uncategorized --spend-only

section "MoM trends — 6 month window"
run uv run finance analyze trends --months 6 --spend-only

section "Category growth — 6 month window"
run uv run finance analyze trends --months 6 --growth --spend-only

section "Active recurring streams"
run uv run finance analyze recurring --active-only

section "Active subscriptions"
run uv run finance analyze subscriptions

section "Subscription overlaps"
run uv run finance analyze subscriptions --overlaps

section "Forecast — next 30 days"
run uv run finance analyze forecast --days 30

section "Alerts — new large merchants & PRLVs"
run uv run finance analyze alerts

section "Alerts — recently stopped subscriptions"
run uv run finance analyze alerts --stopped

# ─────────────────────────────────────────────────────────────────────────────
# 4. Optional LLM pass (costs real tokens)
# ─────────────────────────────────────────────────────────────────────────────
if [[ $DO_LLM -eq 1 ]]; then
  section "LLM categorization — dry-run preview"
  run uv run finance enrich llm-categorize --dry-run

  printf '\nApply categorization? [y/N] '
  read -r confirm
  if [[ "$confirm" == "y" || "$confirm" == "Y" ]]; then
    section "LLM categorization — apply"
    run uv run finance enrich llm-categorize
    section "Overview (after categorization)"
    run uv run finance analyze overview --spend-only --months 3 --top 15
  fi

  section "Advisory — subscriptions (cached by input_hash)"
  run uv run finance advise subscriptions

  section "Advisory — cutbacks"
  run uv run finance advise cutbacks --months 6

  section "Advisory — integral offers"
  run uv run finance advise integral-offers

  section "Persisted advice list"
  run uv run finance advise ls

  section "Cost summary (llm_runs)"
  run uv run python -c "
import sqlite3, os
c = sqlite3.connect(os.path.expanduser('~/.local/share/finance/finance.db'))
print(f'  {\"kind\":<24} {\"calls\":>6} {\"in\":>8} {\"out\":>8}')
for kind, n, i, o in c.execute(
    'SELECT kind, COUNT(*), COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0) '
    'FROM llm_runs GROUP BY 1 ORDER BY 1'):
    print(f'  {kind:<24} {n:>6} {i:>8} {o:>8}')
"
fi

# ─────────────────────────────────────────────────────────────────────────────
# 5. Optional: launch the web dashboard
# ─────────────────────────────────────────────────────────────────────────────
if [[ $DO_SERVE -eq 1 ]]; then
  section "Launching web dashboard — Ctrl-C to stop"
  printf '  Open \033[1;34mhttp://localhost:8000\033[0m in your browser.\n'
  run uv run finance serve
fi

if [[ $FAILURES -gt 0 ]]; then
  printf '\n\033[0;31m%d command(s) failed.\033[0m\n' "$FAILURES"
  exit 1
fi

printf '\n\033[1;32mAll done.\033[0m\n'
