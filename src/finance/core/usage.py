from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from finance.core.analytics import CanonicalEvent


def _parse_timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        raise ValueError("usage event timestamp is required")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@dataclass(frozen=True)
class UsageCsvAdapter:
    """Small non-finance adapter for SaaS/product usage event rows."""

    name: str = "usage_events"
    source: str = "usage_csv"

    def normalize(self, raw: Iterable[Mapping[str, Any]]) -> list[CanonicalEvent]:
        events: list[CanonicalEvent] = []
        for row in raw:
            event_name = str(row.get("event_name") or row.get("event") or "").strip()
            user_id = str(row.get("user_id") or row.get("entity_id") or "").strip()
            if not event_name:
                raise ValueError("usage event_name is required")
            if not user_id:
                raise ValueError("usage user_id is required")

            event_id = str(row.get("event_id") or f"{user_id}:{event_name}:{len(events)}")
            value = float(row.get("value", 1.0) or 0.0)
            unit = str(row.get("unit") or "event")
            dimensions = {
                key: str(row[key])
                for key in ("plan", "workspace", "region")
                if row.get(key) not in (None, "")
            }
            dimensions["event_name"] = event_name
            events.append(
                CanonicalEvent(
                    event_id=event_id,
                    timestamp=_parse_timestamp(row.get("timestamp")),
                    entity_id=user_id,
                    entity_name=str(row.get("user_name") or user_id),
                    measure=event_name,
                    value=value,
                    unit=unit,
                    polarity="positive",
                    dimensions=dimensions,
                    source=self.source,
                    raw_json=dict(row),
                    provenance={"adapter": self.name},
                )
            )
        return events
