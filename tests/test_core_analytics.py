from __future__ import annotations

from finance.analysis.metric_specs import FINANCE_METRIC_REGISTRY
from finance.core.analytics import MetricRegistry, MetricSpec
from finance.core.usage import UsageCsvAdapter


def test_metric_registry_rejects_duplicate_names():
    spec = MetricSpec(
        name="events_by_plan",
        dataset="usage_events",
        description="Count usage events by plan.",
        inputs=("canonical_events",),
        measures=("events",),
        dimensions=("plan",),
    )
    registry = MetricRegistry([spec])
    assert registry.get("events_by_plan") == spec
    assert registry.by_dataset("usage_events") == [spec]

    try:
        registry.register(spec)
    except ValueError as exc:
        assert "already registered" in str(exc)
    else:
        raise AssertionError("duplicate metric registration should fail")


def test_usage_csv_adapter_normalizes_non_finance_events():
    adapter = UsageCsvAdapter()
    events = adapter.normalize(
        [
            {
                "event_id": "evt-1",
                "timestamp": "2026-05-08T12:00:00Z",
                "user_id": "user-1",
                "user_name": "Ada",
                "event_name": "report_exported",
                "value": "1",
                "unit": "action",
                "plan": "pro",
                "workspace": "acme",
            }
        ]
    )

    assert len(events) == 1
    event = events[0]
    assert event.event_id == "evt-1"
    assert event.entity_id == "user-1"
    assert event.entity_name == "Ada"
    assert event.measure == "report_exported"
    assert event.value == 1.0
    assert event.unit == "action"
    assert event.polarity == "positive"
    assert event.dimensions["plan"] == "pro"
    assert event.dimensions["event_name"] == "report_exported"
    assert event.provenance == {"adapter": "usage_events"}


def test_finance_metric_specs_describe_existing_analysis_contracts():
    names = {spec.name for spec in FINANCE_METRIC_REGISTRY.list()}
    assert {"monthly_totals", "subscription_streams", "merchant_outflow"} <= names
    totals = FINANCE_METRIC_REGISTRY.get("monthly_totals")
    assert totals.dataset == "finance.transactions"
    assert "transactions" in totals.inputs
    assert "category" in totals.dimensions
