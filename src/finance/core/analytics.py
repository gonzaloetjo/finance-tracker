from __future__ import annotations

import builtins
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol

Polarity = Literal["positive", "negative", "neutral"]


@dataclass(frozen=True)
class CanonicalEvent:
    """Domain-neutral event record for analytics adapters."""

    event_id: str
    timestamp: datetime
    entity_id: str
    entity_name: str
    measure: str
    value: float
    unit: str
    polarity: Polarity = "neutral"
    dimensions: Mapping[str, str] = field(default_factory=dict)
    source: str = ""
    raw_json: Mapping[str, Any] = field(default_factory=dict)
    provenance: Mapping[str, Any] = field(default_factory=dict)


class DatasetAdapter(Protocol):
    """Normalize source-specific records into canonical analytics events."""

    name: str

    def normalize(self, raw: Iterable[Mapping[str, Any]]) -> Sequence[CanonicalEvent]:
        """Return canonical events for a batch of raw source records."""


@dataclass(frozen=True)
class MetricSpec:
    """Declarative metric contract independent of a concrete implementation."""

    name: str
    dataset: str
    description: str
    inputs: tuple[str, ...]
    measures: tuple[str, ...] = ()
    dimensions: tuple[str, ...] = ()
    owner: str = "finance"
    freshness: str | None = None


class MetricRegistry:
    def __init__(self, specs: Iterable[MetricSpec] = ()) -> None:
        self._specs: dict[str, MetricSpec] = {}
        for spec in specs:
            self.register(spec)

    def register(self, spec: MetricSpec) -> None:
        if spec.name in self._specs:
            raise ValueError(f"metric already registered: {spec.name}")
        self._specs[spec.name] = spec

    def get(self, name: str) -> MetricSpec:
        try:
            return self._specs[name]
        except KeyError as exc:
            raise KeyError(f"unknown metric: {name}") from exc

    def list(self) -> builtins.list[MetricSpec]:
        return sorted(self._specs.values(), key=lambda spec: spec.name)

    def by_dataset(self, dataset: str) -> builtins.list[MetricSpec]:
        return [spec for spec in self.list() if spec.dataset == dataset]
