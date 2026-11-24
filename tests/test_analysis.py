from datetime import datetime, timedelta, timezone
import math

import pytest

from aegis_rca.analysis import build_timeline, compare_metric, counter_to_rate, infer_topology
from aegis_rca.models.incident import TimeWindow
from aegis_rca.models.telemetry import LogRecord, MetricPoint, MetricSeries, SpanRecord


ORIGIN = datetime(2026, 9, 8, tzinfo=timezone.utc)
BASELINE = TimeWindow(start=ORIGIN, end=ORIGIN + timedelta(seconds=30))
INCIDENT = TimeWindow(start=BASELINE.end, end=ORIGIN + timedelta(seconds=60))


def metric(samples, name="latency_ms"):
    return MetricSeries(
        name=name,
        labels={"service_name": "checkout"},
        points=tuple(MetricPoint(timestamp=ORIGIN + timedelta(seconds=time), value=value) for time, value in samples),
        source="test:metric",
    )


def span(trace="trace-1", id="span-1", parent=None, service="checkout", kind="INTERNAL", status="UNSET", attrs=None, seconds=0):
    return SpanRecord(
        trace_id=trace, span_id=id, parent_span_id=parent, service=service, name="operation",
        kind=kind, start=ORIGIN + timedelta(seconds=seconds), duration_ms=20,
        status=status, attributes=attrs or {}, source="test:trace",
    )


def test_metric_comparison_sorts_input_and_uses_half_open_explicit_windows():
    result = compare_metric(
        metric([(50, 21), (0, 10), (60, 900), (30, 20), (10, 11), (40, 19), (20, 9), (-1, 999)]),
        BASELINE, INCIDENT, min_absolute_shift=2,
    )
    assert result.baseline_count == result.incident_count == 3
    assert result.baseline_median == 10
    assert result.incident_median == 20
    assert result.baseline_mad == 1
    assert result.robust_score == pytest.approx(10 / 1.4826)
    assert result.anomalous is True
    assert result.direction == "increase"


@pytest.mark.parametrize("shift, expected", [(0, False), (0.1, False), (2, True), (-2, True)])
def test_zero_mad_never_produces_fabricated_score(shift, expected):
    samples = [(i, 10) for i in (0, 10, 20)] + [(i, 10 + shift) for i in (30, 40, 50)]
    result = compare_metric(metric(samples), BASELINE, INCIDENT, min_absolute_shift=1)
    assert result.robust_score is None
    assert result.baseline_mad == 0
    assert result.anomalous is expected
    assert any("MAD is zero" in warning for warning in result.warnings)


def test_absolute_floor_suppresses_tiny_effect_even_with_large_score():
    result = compare_metric(
        metric([(0, 1), (10, 1.000001), (20, 0.999999), (30, 1.1), (40, 1.1), (50, 1.1)]),
        BASELINE, INCIDENT, min_absolute_shift=1,
    )
    assert result.anomalous is False
    assert result.score_capped
    assert result.robust_score == 100


def test_missing_nonfinite_and_conflicting_samples_do_not_count_as_normal():
    result = compare_metric(
        metric([(0, float("nan")), (10, float("inf")), (20, float("-inf")), (30, 1), (40, 2), (40, 3)]),
        BASELINE, INCIDENT, min_absolute_shift=1,
    )
    assert result.baseline_count == 0
    assert result.incident_count == 1
    assert result.anomalous is None
    assert result.direction == "unknown"
    assert result.baseline_median is None


def test_identical_duplicate_samples_do_not_manufacture_sample_size():
    result = compare_metric(
        metric([(0, 1)] * 10 + [(30, 2)] * 10), BASELINE, INCIDENT, min_absolute_shift=1,
    )
    assert result.baseline_count == result.incident_count == 1
    assert result.anomalous is None


def test_empty_metrics_are_unknown():
    result = compare_metric(metric([]), BASELINE, INCIDENT, min_absolute_shift=1)
    assert result.anomalous is None
    assert result.robust_score is None


def test_large_finite_values_have_finite_medians_and_no_infinite_shift():
    result = compare_metric(
        metric([(0, -1e308), (10, -1e308), (30, 1e308), (40, 1e308)]),
        BASELINE, INCIDENT, min_absolute_shift=1, min_samples=2,
    )
    assert math.isfinite(result.baseline_median)
    assert math.isfinite(result.incident_median)
    assert result.absolute_shift is None
    assert result.anomalous is None
    assert any("Numeric range" in warning for warning in result.warnings)


@pytest.mark.parametrize("floor", [0, -1, float("nan"), float("inf")])
def test_requires_meaningful_finite_absolute_floor(floor):
    with pytest.raises(ValueError, match="min_absolute_shift"):
        compare_metric(metric([]), BASELINE, INCIDENT, min_absolute_shift=floor)


def test_rejects_overlapping_baseline_and_incident():
    with pytest.raises(ValueError, match="baseline"):
        compare_metric(metric([]), BASELINE, BASELINE, min_absolute_shift=1)


def test_counter_rates_sort_samples_and_resume_after_reset():
    result = counter_to_rate(metric([(30, 8), (0, 100), (20, 2), (10, 120)], name="requests_total"))
    assert result.resets == 1
    assert result.skipped_intervals == 1
    assert [point.value for point in result.series.points] == [2, 0.6]
    assert [point.timestamp for point in result.series.points] == [ORIGIN + timedelta(seconds=x) for x in (10, 30)]
    assert result.series.name == "requests_total_per_second"
    assert any("reset" in warning for warning in result.warnings)


def test_counter_invalid_samples_break_continuity_and_gap_limit_is_explicit():
    result = counter_to_rate(
        metric([(0, 10), (10, float("nan")), (20, 30), (30, 40), (300, 60)]), max_gap_seconds=20,
    )
    assert [point.value for point in result.series.points] == [1]
    assert result.skipped_intervals == 3


def test_counter_conflicting_duplicate_is_unknown_without_zero_division():
    result = counter_to_rate(metric([(0, 10), (10, 20), (10, 25), (20, 30)]))
    assert result.series.points == ()
    assert result.skipped_intervals == 2


def test_counter_low_flow_rates_remain_absolute_rates():
    result = counter_to_rate(metric([(0, 0), (100, 1), (200, 1)]))
    assert [point.value for point in result.series.points] == [0.01, 0]


def test_topology_merges_parent_and_peer_evidence_and_counts_explicit_errors():
    records = [
        span(id="client", kind="CLIENT", attrs={"peer.service": "payment"}),
        span(id="server", parent="client", service="payment", kind="SERVER", status="ERROR"),
        span(trace="trace-2", id="client", kind="CLIENT", attrs={"peer.service": "payment"}),
    ]
    topology = infer_topology(records + [records[0]])
    assert len(topology.edges) == 1
    edge = topology.edges[0]
    assert (edge.caller, edge.callee) == ("checkout", "payment")
    assert edge.trace_ids == ("trace-1", "trace-2")
    assert edge.observation_count == 2
    assert edge.error_count == 1
    assert edge.support == ("client_peer", "parent_relationship")
    assert topology.complete is False


def test_topology_never_matches_reused_span_ids_across_traces():
    topology = infer_topology([
        span(trace="one", id="parent", service="checkout"),
        span(trace="two", id="child", parent="parent", service="payment", kind="SERVER"),
    ])
    assert topology.edges == ()
    assert any("unavailable parents" in warning for warning in topology.warnings)


def test_topology_same_span_ids_in_two_complete_traces_form_separate_observations():
    topology = infer_topology([
        span(trace=trace, id=id, parent=parent, service=service)
        for trace in ("one", "two")
        for id, parent, service in (("parent", None, "checkout"), ("child", "parent", "payment"))
    ])
    assert topology.edges[0].observation_count == 2
    assert topology.edges[0].trace_ids == ("one", "two")


@pytest.mark.parametrize("attributes", [{}, {"server.address": "payment"}, {"peer.service": "unknown"}, {"peer.service": "unknown_service:python"}, {"peer.service": 17}])
def test_topology_requires_explicit_known_peer(attributes):
    assert infer_topology([span(kind="CLIENT", attrs=attributes)]).edges == ()


def test_server_metadata_alone_does_not_establish_caller_direction():
    assert infer_topology([span(kind="SERVER", attrs={"peer.service": "payment"})]).edges == ()


def test_missing_server_span_does_not_prevent_explicit_client_peer_edge():
    topology = infer_topology([span(kind="CLIENT", status="ERROR", attrs={"peer.service": "payment"})])
    assert topology.edges[0].support == ("client_peer",)
    assert topology.edges[0].error_count == 1
    assert topology.complete is False


def test_conflicting_span_identities_are_excluded_independent_of_input_order():
    records = [span(id="parent", service="checkout"), span(id="parent", service="cart"), span(id="child", parent="parent", service="payment")]
    forward = infer_topology(records)
    backward = infer_topology(reversed(records))
    assert forward == backward
    assert forward.edges == ()
    assert any("conflicting" in warning for warning in forward.warnings)


def test_timeline_orders_recorded_evidence_and_marks_aggregate_window():
    comparison = compare_metric(metric([(0, 1), (10, 1), (20, 1), (30, 10), (40, 10), (50, 10)]), BASELINE, INCIDENT, min_absolute_shift=1)
    log = LogRecord(id="error", timestamp=ORIGIN + timedelta(seconds=35), service="checkout", severity="ERROR", body="upstream connection refused", source="test:log", trace_id="trace-1")
    records = [span(seconds=40), span(id="earlier", seconds=5)]
    timeline = build_timeline(spans=records, logs=[log], metric_comparisons=[comparison])
    assert [event.kind for event in timeline.events] == ["span", "metric_window", "log", "span"]
    metric_event = timeline.events[1]
    assert metric_event.timestamp == INCIDENT.start
    assert metric_event.window_end == INCIDENT.end
    assert "not anomaly onset" in metric_event.summary
    assert "causality" in timeline.warnings[0]
    assert timeline.events[2].trace_id == "trace-1"
    assert build_timeline(spans=reversed(records), logs=[log, log], metric_comparisons=[comparison]) == timeline
