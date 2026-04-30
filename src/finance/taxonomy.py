"""Canonical category taxonomy — single source of truth.

The category list lives in `finance/llm/prompts/taxonomy.yaml` (that's where
the LLM prompt template reads it from). This module exposes it to non-LLM
modules that hardcode category subsets, so a rename in the YAML surfaces as
an import-time AssertionError instead of a silent miss at runtime.

Typical use from a module that hardcodes a subset:

    from finance.taxonomy import assert_subset_of_taxonomy
    MY_SUBSET = frozenset({"Utilities", "Housing"})
    assert_subset_of_taxonomy(MY_SUBSET, source="analysis/totals.py")
"""

from __future__ import annotations

from functools import lru_cache
from importlib.resources import files

import yaml


@lru_cache(maxsize=1)
def load_taxonomy_set() -> frozenset[str]:
    """Return the canonical set of category names, cached for the process."""
    raw = files("finance.llm.prompts").joinpath("taxonomy.yaml").read_text()
    data = yaml.safe_load(raw) or []
    return frozenset(str(c).strip() for c in data if c)


def assert_subset_of_taxonomy(
    names: frozenset[str] | set[str],
    *,
    source: str,
) -> None:
    """Raise AssertionError if any name is absent from the canonical taxonomy.

    `source` is a short label (filename) surfaced in the error so the drifting
    caller is obvious. Designed to be called at module import, so the check
    runs once per process and the failure is impossible to miss.
    """
    extras = set(names) - load_taxonomy_set()
    if extras:
        raise AssertionError(
            f"{source} references categories absent from taxonomy.yaml: "
            f"{sorted(extras)}. Rename in taxonomy.yaml or remove from {source}."
        )
