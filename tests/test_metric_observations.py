"""Self-contained P1.3a facts; no dependency on local incident recordings."""

from datetime import datetime, timedelta, timezone
import json
import math

import pytest
from pydantic import ValidationError

from aegis_rca.models.incident import IncidentInput, TimeWindow
from aegis_rca.models.metric_observation import (
    CategoryPresenceValue, MetricContextIdentity, MetricDescriptor,
    MetricDescriptorBinding, MetricObservationBatch, MetricObservationContext,
    MetricSeriesKey, ObservedSampleExtent, WindowSummaryValue,
)
from aegis_rca.models.telemetry import (
    LogRecord, LogResult, MetricPoint, MetricResult, MetricSeries, SpanRecord, TraceResult,
)
from aegis_rca.observations import observe_metrics, observe_spans
from aegis_rca.observations import metrics as implementation
from aegis_rca.policy import make_task
from aegis_rca.runner import run_investigation
from aegis_rca.runtime import Dependencies
from aegis_rca.state import initial_state
from aegis_rca.tools.normalize import normalize_logs, normalize_spans
from aegis_rca.tools.replay import ReplayBackend
from aegis_rca.tools.snapshot import TelemetrySnapshot, read_snapshot, write_snapshot


START = datetime(2026, 9, 9, 23, 29, 36, tzinfo=timezone.utc)
WINDOW = TimeWindow(start=START, end=START + timedelta(seconds=60))
NAME = "rpc_client_call_duration_seconds_count"
STATUS = "rpc_response_status_code"


def series(samples=((0, 1),), *, name="numeric", labels=None, source="prometheus:/api/v1/query_range"):
    return MetricSeries(name=name, labels=labels or {}, source=source,
                        points=tuple(MetricPoint(timestamp=START + timedelta(seconds=time), value=value)
                                     for time, value in samples))


def payment_result():
    labels = {"__name__": NAME, "service_name": "checkout",
              "rpc_method": "oteldemo.PaymentService/Charge", "job": "otel/checkout"}
    return MetricResult(series=(
        series(tuple((time, 321) for time in (15, 30, 45, 60)), name=NAME,
               labels={**labels, STATUS: "OK", "server_address": "example-endpoint", "server_port": "50051"}),
        series(tuple((time, 6) for time in (15, 30, 45, 60)), name=NAME,
               labels={**labels, STATUS: "UNAVAILABLE"}),
    ))


def observe(result=None, **kwargs):
    return observe_metrics(result if result is not None else payment_result(), window=WINDOW, **kwargs)


def summaries(batch):
    return tuple(record for record in batch.observations if isinstance(record.value, WindowSummaryValue))


def categories(batch):
    return tuple(record for record in batch.observations if isinstance(record.value, CategoryPresenceValue))


def semantic_context(key, version="1", descriptor=None, **kwargs):
    return MetricObservationContext(
        contract=MetricContextIdentity(contract_id="fixture-contract", source="synthetic-instrumentation", version=version),
        descriptors=(MetricDescriptorBinding(series=key, descriptor=descriptor or MetricDescriptor(
            instrument_type="counter", value_semantics="cumulative_counter", unit="observations")),),
        **kwargs,
    )


def test_payment_shape_preserves_values_and_presence_without_business_counts():
    batch = observe(context=MetricObservationContext(category_labels=(STATUS,)))
    by_status = {dict(record.series.labels)[STATUS]: record for record in summaries(batch)}
    for status, number in (("OK", 321), ("UNAVAILABLE", 6)):
        record = by_status[status]
        assert record.descriptor == MetricDescriptor()
        assert record.value.model_dump() == {
            "kind": "window_summary", "usable_sample_count": 4,
            "first": number, "last": number, "min": number, "max": number,
            "sample_median": number, "observed_sample_extent": {
                "first": START + timedelta(seconds=15), "last": WINDOW.end},
        }
        assert record.quality.issues == ("unknown_semantics",)
    assert by_status["OK"].series != by_status["UNAVAILABLE"].series
    presence = categories(batch)[0].value
    assert presence.observed_values == ("OK", "UNAVAILABLE")
    assert {member.value: member.series for member in presence.members} == {
        status: (by_status[status].series,) for status in by_status
    }
    assert set(presence.model_dump()) == {"kind", "scope", "label", "observed_values", "members"}
    assert len(batch.observations) == 3
    serialized = batch.model_dump_json()
    for forbidden in ("request_count", "error_rate", '"rate":', '"delta":', "baseline",
                      "healthy", "abnormal", "root_cause", "supports_hypothesis",
                      "contradicts_hypothesis", "successful requests", "failed requests"):
        assert forbidden not in serialized
    assert batch.provenance.coverage_complete is None
    assert batch.provenance.truncated is False
    assert MetricObservationBatch.model_validate_json(serialized) == batch


@pytest.mark.parametrize("name", ["requests_total", "duration_seconds", "duration_seconds_count"])
def test_names_do_not_infer_type_or_unit(name):
    record = observe(MetricResult(series=(series(name=name),))).observations[0]
    assert record.descriptor == MetricDescriptor()
    assert record.provenance.context_identity is None


def test_input_series_labels_and_samples_can_all_be_reordered():
    result = payment_result()
    changed = result.model_copy(update={"series": tuple(
        item.model_copy(update={"labels": dict(reversed(list(item.labels.items()))),
                                "points": tuple(reversed(item.points))})
        for item in reversed(result.series))})
    context = MetricObservationContext(category_labels=(STATUS, "job"))
    assert observe(result, context=context) == observe(changed, context=context)


def test_key_preserves_case_empty_values_and_all_dimensions():
    labels = {"job": "A", "instance": "i", "address": "host", "version": "V1", "empty": ""}
    key = MetricSeriesKey(name="Metric", labels=tuple(reversed(list(labels.items()))))
    assert key.labels == tuple(sorted(labels.items()))
    with pytest.raises(ValidationError):
        MetricSeriesKey(name="m", labels=(("job", "A"), ("job", "B")))
    original = observe(MetricResult(series=(series(labels=labels),))).observations[0]
    for label in labels:
        changed = observe(MetricResult(series=(series(labels={**labels, label: "changed"}),))).observations[0]
        assert original.series != changed.series
        assert original.id != changed.id


def test_same_key_records_are_normalized_together_without_arbitrary_winners():
    first, second = series(((0, 1), (10, 2))), series(((10, 3), (20, 4)))
    batch = observe(MetricResult(series=(first, second)))
    assert batch == observe(MetricResult(series=(second, first)))
    assert len(batch.observations) == 1
    assert batch.observations[0].value.usable_sample_count == 2
    assert batch.observations[0].value.sample_median == 2.5
    assert batch.sampling[0].conflicting_timestamps == (START + timedelta(seconds=10),)


def test_exact_duplicates_do_not_change_facts_or_ids():
    original = observe(MetricResult(series=(series(((0, 1), (10, 2))),)))
    duplicated = observe(MetricResult(series=(series(((10, 2), (0, 1), (10, 2), (0, 1))),)))
    assert original.observations == duplicated.observations
    assert duplicated.sampling[0].duplicate_sample_count == 2
    assert duplicated.sampling[0].returned_sample_count == 4
    assert duplicated.sampling[0].unique_timestamp_count == 2


def test_conflicts_and_nonfinite_points_are_unusable_not_zero():
    points = ((0, 1), (10, 2), (10, 3), (20, float("nan")), (30, float("inf")),
              (40, float("-inf")), (50, 8), (50, float("nan")), (60, 9))
    batch = observe(MetricResult(series=(series(points),)))
    assert batch == observe(MetricResult(series=(series(tuple(reversed(points))),)))
    fact = batch.observations[0]
    assert fact.value.usable_sample_count == 2
    assert (fact.value.first, fact.value.last, fact.value.sample_median) == (1, 9, 5)
    assert set(fact.quality.issues) == {"conflicting_samples", "nonfinite_samples", "unknown_semantics"}
    assert len(batch.sampling[0].nonfinite_timestamps) == 4
    assert batch.sampling[0].conflicting_timestamps == (START + timedelta(seconds=10),)
    json.dumps(batch.model_dump(mode="json"), allow_nan=False)


@pytest.mark.parametrize("samples", [(), ((0, float("nan")),), ((0, 1), (0, 2))])
def test_no_usable_samples_create_diagnostics_not_zero_facts(samples):
    batch = observe(MetricResult(series=(series(samples),)))
    assert batch.observations == ()
    assert "no_usable_samples" in {item.code for item in batch.diagnostics}
    assert batch.sampling[0].usable_sample_count == 0


def test_single_point_and_negative_gauge_values_are_valid_without_counter_logic():
    batch = observe(MetricResult(series=(series(((0, -3),)),)))
    value = batch.observations[0].value
    assert value.usable_sample_count == 1
    assert value.first == value.last == value.min == value.max == value.sample_median == -3
    assert value.observed_sample_extent == ObservedSampleExtent(first=START, last=START)


@pytest.mark.parametrize("values,expected", [
    ((1e308, 1e308), 1e308), ((-1e308, 1e308), 0),
    ((-1e308, -1e308), -1e308), ((5e-324, 5e-324), 5e-324),
    ((5e-324, 1e-323), 1e-323),
    ((3, 1, 2), 2), ((4, 1, 2, 3), 2.5),
])
def test_sample_median_is_finite_and_not_time_weighted(values, expected):
    value = observe(MetricResult(series=(series(tuple(enumerate(values))),))).observations[0].value
    assert value.sample_median == expected
    assert math.isfinite(value.sample_median)


def test_frozen_samples_are_not_refiltered_and_requested_window_is_batch_only():
    result = MetricResult(series=(series(((-1, 1), (0, 2), (60, 3), (61, 4))),))
    first = observe(result)
    second = observe_metrics(result, window=TimeWindow(start=START + timedelta(seconds=10), end=WINDOW.end))
    assert first.observations == second.observations
    assert first.provenance.requested_window != second.provenance.requested_window
    assert first.observations[0].value.usable_sample_count == 4
    assert first.observations[0].value.observed_sample_extent.first < WINDOW.start
    assert first.observations[0].value.observed_sample_extent.last > WINDOW.end


def test_utc_and_negative_zero_have_canonical_identity():
    result = MetricResult(series=(series(((0, -0.0), (10, 2))),))
    record = result.series[0]
    shifted = record.model_copy(update={"points": tuple(
        point.model_copy(update={"timestamp": point.timestamp.astimezone(timezone(timedelta(hours=8))),
                                 "value": 0.0 if point.value == 0 else point.value}) for point in record.points)})
    assert observe(result) == observe(MetricResult(series=(shifted,)))
    assert math.copysign(1, observe(result).observations[0].value.first) == 1


def test_ids_bind_used_samples_even_when_statistics_are_unchanged():
    first = observe(MetricResult(series=(series(((0, 1), (10, 2), (20, 3))),))).observations[0]
    second = observe(MetricResult(series=(series(((0, 1), (11, 2), (20, 3))),))).observations[0]
    assert first.value == second.value
    assert first.id != second.id


def test_changing_used_values_changes_identity_even_with_identical_summary():
    first = observe(MetricResult(series=(series(tuple(enumerate((1, 2, 4, 5, 6)))),))).observations[0]
    second = observe(MetricResult(series=(series(tuple(enumerate((1, 3, 4, 5, 6)))),))).observations[0]
    assert first.value == second.value
    assert first.id != second.id


def test_display_prose_query_metadata_and_filesystem_locators_do_not_change_fact_ids(monkeypatch):
    result = payment_result()
    before = observe(result)
    monkeypatch.setattr(implementation, "_summary_text", lambda _: "Different display text")
    for coverage in (True, False, None):
        changed = result.model_copy(update={"coverage_complete": coverage, "truncated": True,
            "warnings": ("new wording",), "series": tuple(item.model_copy(update={
                "source": "C:/different-candidate/snapshot.json"}) for item in result.series)})
        after = observe(changed, context=MetricObservationContext(query="changed query", backend="test"))
        assert [record.id for record in before.observations] == [record.id for record in after.observations]
        assert after.provenance.coverage_complete is coverage
        assert after.provenance.truncated is True
        assert all(record.summary == "Different display text" for record in after.observations)


def test_context_is_explicit_exact_and_versioned_but_never_enables_advanced_derivations():
    result = MetricResult(series=(series(((0, 100), (10, 2), (20, 4))),))
    unknown = observe(result)
    key = unknown.observations[0].series
    first = observe(result, context=semantic_context(key))
    second = observe(result, context=semantic_context(key, version="2"))
    assert unknown.observations[0].id != first.observations[0].id != second.observations[0].id
    assert first.observations[0].descriptor.instrument_type == "counter"
    assert first.observations[0].value == unknown.observations[0].value
    assert len(first.observations) == 1
    assert first.observations[0].quality.issues == ()  # No reset/anomaly interpretation.
    with pytest.raises(ValidationError, match="versioned"):
        MetricObservationContext(descriptors=semantic_context(key).descriptors)
    with pytest.raises(ValidationError, match="only one"):
        MetricObservationContext(**{**semantic_context(key).model_dump(),
            "descriptors": semantic_context(key).descriptors * 2})
    with pytest.raises(ValueError, match="does not match"):
        observe(result, context=semantic_context(MetricSeriesKey(name="other")))


def test_context_only_affects_the_facts_that_use_it():
    original = observe(context=MetricObservationContext(category_labels=(STATUS,)))
    key = summaries(original)[0].series
    contextual = observe(context=semantic_context(key, category_labels=(STATUS,)))
    by_key = {record.series: record for record in summaries(contextual)}
    for record in summaries(original):
        assert (record.id != by_key[record.series].id) is (record.series == key)
    assert categories(original) == categories(contextual)
    unused = MetricObservationContext(contract=semantic_context(key).contract, category_labels=(STATUS,))
    assert observe(context=unused).observations == original.observations


def test_context_bindings_are_canonical_and_identity_cannot_be_blank():
    keys = [record.series for record in observe().observations]
    first = semantic_context(keys[0])
    bindings = first.descriptors + semantic_context(keys[1]).descriptors
    left = MetricObservationContext(contract=first.contract, descriptors=bindings)
    right = MetricObservationContext(contract=first.contract, descriptors=tuple(reversed(bindings)))
    assert observe(context=left) == observe(context=right)
    with pytest.raises(ValidationError, match="blank"):
        MetricContextIdentity(contract_id="test", source=" ", version="1")


def test_time_semantics_need_contract_and_bind_summary_identity():
    with pytest.raises(ValidationError, match="versioned"):
        MetricObservationContext(sample_time_basis="source_sample")
    contract = MetricContextIdentity(contract_id="capture", source="fixture", version="1")
    result = payment_result()
    unknown = observe(result)
    known = observe(result, context=MetricObservationContext(contract=contract, sample_time_basis="query_evaluation"))
    assert {record.id for record in unknown.observations}.isdisjoint(record.id for record in known.observations)


@pytest.mark.parametrize("status,code", [
    ("unknown", "query_status_unknown"), ("failed", "query_failed"),
    ("not_configured", "query_not_configured"), ("succeeded", "zero_returned_series"),
])
def test_empty_query_status_is_not_business_absence(status, code):
    batch = observe(MetricResult(), context=MetricObservationContext(query_status=status))
    assert batch.observations == ()
    assert code in {item.code for item in batch.diagnostics}
    assert batch.provenance.query_status == status
    assert batch.provenance.coverage_complete is None


def test_warning_prose_is_not_parsed_into_query_status():
    batch = observe(MetricResult(warnings=("No catalog-verified metric query is configured.",)))
    assert batch.provenance.query_status == "unknown"
    assert batch.provenance.warnings == ("No catalog-verified metric query is configured.",)


def test_category_presence_keeps_empty_series_empty_label_values_and_full_members():
    result = MetricResult(series=(series((), labels={"status": ""}), series((), labels={"status": "OK"}), series(())))
    batch = observe(result, context=MetricObservationContext(category_labels=("status", "absent", "status")))
    assert summaries(batch) == ()
    assert categories(batch)[0].value.observed_values == ("", "OK")
    assert len(categories(batch)[0].value.members) == 2
    assert "category_label_absent" in {item.code for item in batch.diagnostics}


def test_category_fact_depends_on_membership_not_sample_values_or_coverage():
    context = MetricObservationContext(category_labels=(STATUS,))
    original = observe(context=context)
    result = payment_result().model_copy(update={"series": tuple(
        item.model_copy(update={"points": ()}) for item in payment_result().series), "truncated": True})
    assert categories(original) == categories(observe(result, context=context))
    changed = result.model_copy(update={"series": (result.series[0],)})
    assert categories(original)[0].id != categories(observe(changed, context=context))[0].id


@pytest.mark.parametrize("name,labels,issue", [
    ("expression", {}, "expression_placeholder"),
    ("recorded_name", {"__name__": "different_name"}, "metric_name_conflict"),
])
def test_name_ambiguity_is_preserved(name, labels, issue):
    record = observe(MetricResult(series=(series(name=name, labels=labels),))).observations[0]
    assert issue in record.quality.issues
    assert record.series.name == name
    assert dict(record.series.labels) == labels
    assert record.descriptor == MetricDescriptor()


def test_legitimate_expression_metric_with_explicit_name_is_not_a_placeholder():
    record = observe(MetricResult(series=(series(name="expression", labels={"__name__": "expression"}),))).observations[0]
    assert "expression_placeholder" not in record.quality.issues


def test_contracts_are_frozen_extra_forbidden_and_finite():
    record = observe().observations[0]
    with pytest.raises(ValidationError):
        record.summary = "mutated"
    with pytest.raises(ValidationError):
        record.value.first = 0
    with pytest.raises(ValidationError):
        MetricDescriptor(confidence=0.9)
    with pytest.raises(ValidationError):
        WindowSummaryValue(**{**record.value.model_dump(), "first": float("inf")})
    with pytest.raises(ValidationError):
        ObservedSampleExtent(first=WINDOW.end, last=WINDOW.start)


def test_long_category_values_keep_full_fact_and_bounded_display():
    value = "x" * 3000
    batch = observe(MetricResult(series=(series(labels={"category": value}),)),
                    context=MetricObservationContext(category_labels=("category",)))
    record = categories(batch)[0]
    assert record.value.observed_values == (value,)
    assert len(record.summary) == 2000


async def test_observation_coexists_with_legacy_replay_rca_and_immutable_snapshot(tmp_path):
    incident = IncidentInput(query="Investigate checkout errors.", seed_services=("checkout",), window=WINDOW)
    span = SpanRecord(trace_id="1" * 32, span_id="2" * 16, service="checkout",
        name="oteldemo.PaymentService/Charge", kind="CLIENT", start=START, duration_ms=1,
        status="ERROR", attributes={"rpc.grpc.status_code": 14, "rpc.service": "oteldemo.PaymentService"}, source="trace")
    log = LogRecord(id="log", timestamp=START, service="checkout", severity="ERROR",
                    body="dial payment:50051: connection refused", trace_id=span.trace_id, source="logs")
    snapshot = TelemetrySnapshot(incident=incident, provenance="synthetic_wire_fixture", metrics=payment_result(),
                                traces=TraceResult(spans=(span,)), logs=LogResult(logs=(log,)))
    path = tmp_path / "snapshot.json"
    write_snapshot(snapshot, path)
    before_bytes = path.read_bytes()
    backend = ReplayBackend(snapshot)
    metric_task = make_task(incident, "metric")
    before_metric = await backend.query(metric_task)
    before_trace = observe_spans(snapshot.traces.spans, snapshot.rpc_service_map)
    before_evidence = normalize_spans(snapshot.traces.spans, snapshot.rpc_service_map)
    before_logs = normalize_logs(snapshot.logs.logs, snapshot.traces.spans, snapshot.rpc_service_map)
    before_state, _ = await run_investigation(initial_state(incident, execution_id="metric-coexistence"), Dependencies(backend))
    facts = observe_metrics(read_snapshot(path).metrics, window=WINDOW)
    after_state, _ = await run_investigation(initial_state(incident, execution_id="metric-coexistence"), Dependencies(backend))
    assert len(facts.observations) == 2
    assert before_metric == await backend.query(metric_task)
    assert before_metric.status == "empty" and before_metric.evidence == ()
    assert before_state["final_report"] == after_state["final_report"]
    assert before_state["final_report"].status == "SUPPORTED"
    assert before_state["evidence"] == after_state["evidence"]
    assert before_trace == observe_spans(snapshot.traces.spans, snapshot.rpc_service_map)
    assert before_evidence == normalize_spans(snapshot.traces.spans, snapshot.rpc_service_map)
    assert before_logs == normalize_logs(snapshot.logs.logs, snapshot.traces.spans, snapshot.rpc_service_map)
    unrelated = snapshot.model_copy(update={"traces": TraceResult(), "logs": LogResult(),
        "incident": incident.model_copy(update={"query": "another task/hypothesis"})})
    assert facts == observe_metrics(unrelated.metrics, window=WINDOW)
    assert before_bytes == path.read_bytes()
    assert snapshot == read_snapshot(path)
