from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from finance.taxonomy import validate_category


@dataclass(frozen=True)
class Rule:
    match: re.Pattern[str]
    category: str


def load_rules(path: Path) -> list[Rule]:
    """Load categorization rules from YAML. Returns empty list if file missing."""
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text()) or []
    rules: list[Rule] = []
    for entry in data:
        pat = entry.get("match")
        cat = entry.get("category")
        if not pat or not cat:
            continue
        rules.append(Rule(match=re.compile(pat), category=validate_category(cat, source=str(path))))
    return rules


def save_rules(path: Path, rules: list[Rule]) -> None:
    """Persist rules back to YAML. Preserves the source pattern (.pattern) of
    each compiled regex so the file stays human-editable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    for rule in rules:
        validate_category(rule.category, source=str(path))
    data = [{"match": r.match.pattern, "category": r.category} for r in rules]
    # Dump with explicit flow style: keys first-match-wins ordering preserved.
    yaml_str = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    path.write_text(yaml_str)


DEFAULT_RULES_YAML = """\
# Map merchant/party names to categories. First matching rule wins.
# Patterns are Python regex, case-insensitive via (?i).

- match: "(?i)carrefour|leclerc|auchan|monoprix|franprix|lidl|intermarche|picard|naturalia"
  category: "Groceries"

- match: "(?i)edf|engie|total.energies|totalenergies|veolia|suez"
  category: "Utilities"

# Housing — syndicates, building management, rent paid to syndic.
- match: "(?i)syndicat|syndic\\\\s|copropriete|copropri.t.|residence"
  category: "Housing"

# Telecom — ISP + mobile. 'free' is ambiguous (Free Mobile vs "free"), so
# anchor to brand-likely spellings.
- match: "(?i)orange|free\\\\s*mobile|free\\\\s*telecom|bouygues|sfr|sosh"
  category: "Telecom"

- match: "(?i)sncf|ratp|uber|lyft|bolt|navigo|velib|blablacar|lime|tier|voi\\\\s"
  category: "Transport"

- match: "(?i)amazon|fnac|darty|boulanger|decathlon|zara|uniqlo|hm\\\\s"
  category: "Shopping"

# Streaming services → Entertainment. 'canal' covers 'canal plus' / 'canal+'.
- match: "(?i)netflix|spotify|youtube|disney|hbo|canal(\\\\s*plus|\\\\s*\\\\+)?|dazn|deezer|tidal|paramount|prime\\\\s*video|apple\\\\s*tv"
  category: "Entertainment"

# AI tools → AI.
- match: "(?i)openai|chatgpt|anthropic|claude\\\\.|mistral|cursor|github\\\\s*copilot|perplexity|replicate|midjourney|eleven\\\\s*labs|elevenlabs|runway\\\\s*ml|\\\\bgoogle\\\\b|gemini"
  category: "AI"

# Productivity / dev SaaS (not AI, not entertainment) → SaaS
- match: "(?i)notion|figma|linear\\\\s|todoist|1password|adobe|framer|airtable|zapier|vercel|grammarly"
  category: "SaaS"

# Apple.com/bill is a catch-all for iCloud / App Store — keep as generic Subscriptions
- match: "(?i)apple\\\\.com/bill"
  category: "Subscriptions"

- match: "(?i)deliveroo|uber\\\\s*eats|frichti|mcdo|burger\\\\s*king|starbucks|kfc|five\\\\s*guys|restaurant|brasserie|bistro"
  category: "Dining"

# Insurance — common French insurers.
- match: "(?i)\\\\baxa\\\\b|\\\\bmaif\\\\b|\\\\bmacif\\\\b|\\\\bmaaf\\\\b|matmut|cardif|allianz|groupama|\\\\bgmf\\\\b|harmonie|\\\\bluko\\\\b|\\\\balan\\\\b|\\\\bmutuelle\\\\b|assurance"
  category: "Insurance"

# Health — pharmacies, doctors, and common FR gym brands.
- match: "(?i)pharmacie|medecin|m.decin|dentiste|hopital|h.pital|doctolib|basic.fit|classpass|neoness|fitness\\\\s*park|keepcool|on.air|crossfit"
  category: "Health"

# Income — match word-boundaried "salaire"/"salary"/"payroll" plus the
# SEPA `/MOTIF SALAIRE` field that French banks emit on credit transfers.
# The bare token "paie" was removed because it substring-matched inside
# "PAIEMENT" (outgoing payment) and flipped it to Income. The
# stream-based classifier (classify_from_streams) is the primary income
# signal now; this rule is a text-level fallback for one-off memos.
- match: "(?i)\\\\bsalaire\\\\b|\\\\bsalary\\\\b|\\\\bpayroll\\\\b|/motif\\\\s+salaire"
  category: "Income"
"""
