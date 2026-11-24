"""P1.4-1 pure check semantics using synthetic P1.1/P1.3 fixture producers."""

from datetime import timedelta, timezone
import hashlib
import socket

import pytest
from pydantic import ValidationError

from aegis_rca.models.check import (
    CategoryPresent, CheckCoverageView, CheckResult, CheckScope, CheckSpec,
    CounterDeltaCompare, IntervalRateCompare, MetricSummaryExists, RpcExists,
)
from aegis_rca.models.incident import TimeWindow
from aegis_rca.models.metric_observation import (
    ClosedSampleInterval, CounterDeltaSpec, IntervalRateSpec, MetricObservationContext,
    MetricSeriesKey,
)
from aegis_rca.models.telemetry import MetricResult
from aegis_rca.observations import derive_metrics, observe_metrics, observe_spans
from aegis_rca.verification.evaluate import evaluate_check

import test_observations as traces
import test_metric_observations as metrics
import test_metric_derivation as derived


TRACE_WINDOW = TimeWindow(start=traces.STAMP, end=traces.STAMP + timedelta(minutes=1))


def rpc_fact(**kwargs):
    return observe_spans((traces.rpc_span(**kwargs),), traces.SERVICE_MAP)[0]


def trace_check(*, query="checkout-rpcs-v1", **matchers):
    return CheckSpec(
        scope=CheckScope(signal="trace", services=("checkout",), window=TRACE_WINDOW,
                         query_identity=query),
        predicate=RpcExists(**{"local_service": "checkout", "span_kind": "CLIENT",
                               "peer_service": "payment", "rpc_status_names": ("UNAVAILABLE",),
                               **matchers}),
    )


def view(spec, records=(), **kwargs):
    return CheckCoverageView(**{
        "signal": spec.scope.signal, "services": spec.scope.services,
        "window": spec.scope.window, "query_identity": spec.scope.query_identity,
        "acquisition_id": "synthetic-acquisition-1", "admitted_ids": tuple(r.id for r in records),
        **kwargs,
    })


def complete(spec, records=(), **kwargs):
    return view(spec, records, **{"query_status": "succeeded", "coverage_complete": True,
                                 "truncated": False, "pre_capture_omissions": "none",
                                 "fact_set_complete": True, **kwargs})


def evaluate(spec, records=(), **coverage_kwargs):
    return evaluate_check(spec, observations=records, coverage=(view(spec, records, **coverage_kwargs),))


@pytest.mark.parametrize("coverage_kwargs", [
    {}, {"truncated": True}, {"coverage_complete": False}, {"query_status": "failed"},
    {"query_status": "not_configured"}, {"pre_capture_omissions": "present"},
])
def test_positive_unavailable_survives_open_or_failed_broader_acquisition(coverage_kwargs):
    fact, spec = rpc_fact(rpc_status=14, otlp_status="ERROR"), trace_check()
    result = evaluate(spec, (fact,), **coverage_kwargs)
    assert result.verdict == "SUPPORT"
    assert result.matched_ids == (fact.id,)
    assert result.incompatible_ids == ()


def test_payment_failure_unknown_is_not_unavailable_but_explicit_generic_failure_matches():
    fact = rpc_fact(rpc_status=2, otlp_status="ERROR")
    assert fact.value.rpc_status_name == "UNKNOWN" and fact.value.outcome == "failure"
    spec = trace_check()
    assert evaluate(spec, (fact,)).verdict == "UNKNOWN"
    assert evaluate_check(spec, observations=(fact,), coverage=(complete(spec, (fact,)),)).verdict == "NEUTRAL"
    generic = trace_check(rpc_status_names=(), outcomes=("failure",))
    assert evaluate(generic, (fact,)).verdict == "SUPPORT"


def test_conflicting_explicit_rpc_status_sources_are_not_resolved_by_field_order():
    fact = rpc_fact(rpc_status=14, response_status="OK", otlp_status="ERROR")
    assert fact.value.outcome == "inconsistent"
    spec = trace_check()
    assert evaluate(spec, (fact,)).verdict == "UNKNOWN"
    assert evaluate_check(spec, observations=(fact,), coverage=(complete(spec, (fact,)),)).verdict == "UNKNOWN"
    explicit = trace_check(rpc_status_names=(), outcomes=("inconsistent",))
    assert evaluate(explicit, (fact,)).verdict == "SUPPORT"


@pytest.mark.parametrize("actual,span_kwargs", [
    ("unknown", {"otlp_status": "UNSET"}),
    ("inconsistent", {"rpc_status": 0, "otlp_status": "ERROR"}),
])
@pytest.mark.parametrize("requested", ["success", "failure"])
def test_epistemic_outcome_remains_unresolved_under_complete_acquisition(actual, span_kwargs, requested):
    fact = rpc_fact(**span_kwargs)
    assert fact.value.outcome == actual
    spec = trace_check(rpc_status_names=(), outcomes=(requested,))
    for requirement in ("required", "optional"):
        check = spec.model_copy(update={"requirement": requirement})
        result = evaluate_check(check, observations=(fact,), coverage=(complete(check, (fact,)),))
        assert result.verdict == "UNKNOWN"
        assert result.reason == "unresolved_rpc_fields"
        assert result.matched_ids == result.incompatible_ids == ()


@pytest.mark.parametrize("actual,span_kwargs", [
    ("unknown", {"otlp_status": "UNSET"}),
    ("inconsistent", {"rpc_status": 0, "otlp_status": "ERROR"}),
])
@pytest.mark.parametrize("include_concrete_outcome", [False, True])
def test_explicit_epistemic_outcome_request_supports(actual, span_kwargs, include_concrete_outcome):
    fact = rpc_fact(**span_kwargs)
    requested = (actual, "failure") if include_concrete_outcome else (actual,)
    spec = trace_check(rpc_status_names=(), outcomes=requested)
    for coverage_view in (view(spec, (fact,), truncated=True), complete(spec, (fact,))):
        result = evaluate_check(spec, observations=(fact,), coverage=(coverage_view,))
        assert result.verdict == "SUPPORT"
        assert result.matched_ids == (fact.id,)


@pytest.mark.parametrize("requested", ["OK", "ERROR", "UNSET"])
def test_normalized_otlp_unknown_cannot_be_disproved_by_complete_acquisition(requested):
    fact = rpc_fact(rpc_status=14, otlp_status="unrecognized_otlp_status")
    assert fact.value.otlp_status == "UNKNOWN"
    spec = trace_check(otlp_status=requested)
    result = evaluate_check(spec, observations=(fact,), coverage=(complete(spec, (fact,)),))
    assert result.verdict == "UNKNOWN"
    assert result.reason == "unresolved_rpc_fields"


def test_explicit_otlp_unknown_request_supports():
    fact = rpc_fact(rpc_status=14, otlp_status="unrecognized_otlp_status")
    spec = trace_check(otlp_status="UNKNOWN")
    result = evaluate_check(spec, observations=(fact,), coverage=(complete(spec, (fact,)),))
    assert result.verdict == "SUPPORT"
    assert result.matched_ids == (fact.id,)


@pytest.mark.parametrize("span_kwargs,matchers", [
    ({"otlp_status": "UNSET"}, {"rpc_status_names": (), "outcomes": ("failure",)}),
    ({"rpc_status": 0, "otlp_status": "ERROR"}, {"rpc_status_names": (), "outcomes": ("success",)}),
    ({"rpc_status": 14, "otlp_status": "unrecognized_otlp_status"}, {"otlp_status": "ERROR"}),
])
def test_independent_known_peer_mismatch_still_makes_unresolved_record_irrelevant(span_kwargs, matchers):
    fact = rpc_fact(**span_kwargs)
    spec = trace_check(peer_service="shipping", **matchers)
    assert evaluate(spec, (fact,)).verdict == "UNKNOWN"
    result = evaluate_check(spec, observations=(fact,), coverage=(complete(spec, (fact,)),))
    assert result.verdict == "NEUTRAL"
    assert result.reason == "complete_no_match"


def test_concrete_grpc_mismatch_can_disprove_record_with_unresolved_outcome():
    fact = rpc_fact(rpc_status=0, otlp_status="ERROR")
    assert fact.value.outcome == "inconsistent"
    spec = trace_check(outcomes=("failure",))
    result = evaluate_check(spec, observations=(fact,), coverage=(complete(spec, (fact,)),))
    assert result.verdict == "NEUTRAL"
    assert result.reason == "complete_no_match"


def test_unresolved_record_does_not_erase_another_observed_positive():
    unknown = rpc_fact(otlp_status="UNSET", span_id="a"*16)
    failure = rpc_fact(rpc_status=2, otlp_status="ERROR", span_id="b"*16)
    spec = trace_check(rpc_status_names=(), outcomes=("failure",))
    result = evaluate(spec, (unknown, failure), truncated=True)
    assert result.verdict == "SUPPORT"
    assert result.matched_ids == (failure.id,)


@pytest.mark.parametrize("quality_update,value_update", [
    ({"ambiguous": True}, {}), ({"peer_resolution_method": "unresolved"}, {}),
    ({}, {"peer_service": None}),
])
def test_exact_peer_requires_resolved_unambiguous_fact_even_with_complete_scope(quality_update, value_update):
    fact = rpc_fact(rpc_status=14)
    fact = fact.model_copy(update={"quality": fact.quality.model_copy(update=quality_update),
                                   "value": fact.value.model_copy(update=value_update)})
    spec = trace_check()
    result = evaluate_check(spec, observations=(fact,), coverage=(complete(spec, (fact,)),))
    assert result.verdict == "UNKNOWN"
    assert result.reason == "unresolved_rpc_fields"


@pytest.mark.parametrize("matcher", [
    {"peer_service": "shipping"}, {"span_kind": "SERVER"}, {"operation": "different"},
    {"rpc_system": "http"}, {"outcomes": ("success",)}, {"otlp_status": "OK"},
])
def test_exact_rpc_fields_never_fuzzy_match(matcher):
    fact = rpc_fact(rpc_status=14, otlp_status="ERROR", attributes={"rpc.system": "grpc"})
    spec = trace_check(**matcher)
    assert evaluate(spec, (fact,)).verdict == "UNKNOWN"
    assert evaluate_check(spec, observations=(fact,), coverage=(complete(spec, (fact,)),)).verdict == "NEUTRAL"


@pytest.mark.parametrize("coverage_kwargs", [
    {"services": ("payment",)}, {"signal": "metric"}, {"query_identity": "unrelated-filter"},
    {"window": TimeWindow(start=TRACE_WINDOW.end, end=TRACE_WINDOW.end+timedelta(minutes=1))},
    {"window": TimeWindow(start=TRACE_WINDOW.start+timedelta(seconds=1), end=TRACE_WINDOW.end)},
    {"query_status": "failed"}, {"query_status": "unknown"}, {"query_status": "not_configured"},
    {"truncated": True}, {"coverage_complete": None}, {"coverage_complete": False},
    {"pre_capture_omissions": "unknown"}, {"pre_capture_omissions": "present"},
    {"fact_set_complete": False}, {"admitted_ids": ("O-trace-unsupplied",)},
    {"input_kind": "legacy_evidence"},
])
def test_complete_alone_never_closes_unbound_or_incomplete_scope(coverage_kwargs):
    spec = trace_check()
    result = evaluate_check(spec, coverage=(complete(spec, **coverage_kwargs),))
    assert result.verdict == "UNKNOWN"


def test_empty_exact_acquisition_can_evaluate_positive_existence_false():
    spec = trace_check()
    result = evaluate_check(spec, coverage=(complete(spec),))
    assert result.verdict == "NEUTRAL" and result.incompatible_ids == ()
    # The predicate is still false under a service/window superset for trace.
    broad = complete(spec, services=("checkout", "payment"), window=TimeWindow(
        start=TRACE_WINDOW.start-timedelta(seconds=1), end=TRACE_WINDOW.end+timedelta(seconds=1)))
    assert evaluate_check(spec, coverage=(broad,)).verdict == "NEUTRAL"


def test_claimed_admitted_id_must_have_correct_signal_service_and_acquisition_time():
    spec, fact = trace_check(), rpc_fact(rpc_status=2)
    wrong_records = (fact.model_copy(update={"service": "shipping"}),
                     fact.model_copy(update={"timestamp": TRACE_WINDOW.end}),
                     numeric_facts()[0])
    for wrong in wrong_records:
        result = evaluate_check(spec, observations=(wrong,), coverage=(complete(spec, (wrong,)),))
        assert result.verdict == "UNKNOWN"


def test_closure_helper_itself_rejects_unrelated_complete_scope():
    from aegis_rca.verification._shared import closes
    spec = trace_check()
    assert not closes(spec.scope, (complete(spec, services=("shipping",)),), ())


def test_query_identity_required_for_closure_but_not_intrinsic_positive():
    spec, fact = trace_check(query=None), rpc_fact(rpc_status=14)
    assert evaluate_check(spec, observations=(fact,)).verdict == "SUPPORT"
    assert evaluate_check(spec, coverage=(complete(spec),)).verdict == "UNKNOWN"
    scoped = trace_check()
    assert evaluate_check(scoped, observations=(fact,)).verdict == "UNKNOWN"
    assert evaluate_check(scoped, observations=(fact,), coverage=(view(scoped),)).verdict == "UNKNOWN"


def test_explicit_acquisition_identity_is_binding():
    spec = trace_check()
    spec = spec.model_copy(update={"scope": spec.scope.model_copy(update={"acquisition_id": "capture-a"})})
    fact = rpc_fact(rpc_status=14)
    assert evaluate(spec, (fact,)).verdict == "UNKNOWN"
    assert evaluate(spec, (fact,), acquisition_id="capture-a").verdict == "SUPPORT"


def test_rpc_time_window_is_half_open():
    fact, spec = rpc_fact(rpc_status=14), trace_check(query=None)
    for stamp in (TRACE_WINDOW.start-timedelta(microseconds=1), TRACE_WINDOW.end):
        assert evaluate_check(spec, observations=(fact.model_copy(update={"timestamp": stamp}),)).verdict == "UNKNOWN"


def metric_check(predicate, *, window=metrics.WINDOW):
    return CheckSpec(scope=CheckScope(signal="metric", services=("checkout",), window=window,
                                     query_identity="synthetic-metrics-v1"), predicate=predicate)


def numeric_facts(samples=((0, 1), (10, 4)), spec_type=CounterDeltaSpec):
    return derived.facts(derived.run(samples, spec_type))


def test_summary_exists_with_unknown_descriptor_and_exact_lossless_series_key():
    records = observe_metrics(MetricResult(series=(metrics.series(((0, 1), (10, 4)),
        labels={"__name__": "numeric", "empty": ""}),)), window=metrics.WINDOW).observations
    fact = records[0]
    assert fact.descriptor.value_semantics == "unknown"
    spec = metric_check(MetricSummaryExists(series=fact.series))
    assert evaluate(spec, records, truncated=True).verdict == "SUPPORT"
    other = metric_check(MetricSummaryExists(series=MetricSeriesKey(name="numeric")))
    assert evaluate(other, records).verdict == "UNKNOWN"
    assert evaluate_check(spec, observations=records).verdict == "UNKNOWN"


@pytest.mark.parametrize("operator,threshold,verdict", [
    ("eq", 3, "SUPPORT"), ("ne", 3, "NEUTRAL"), ("lt", 3, "NEUTRAL"),
    ("lte", 3, "SUPPORT"), ("gt", 3, "NEUTRAL"), ("gte", 3, "SUPPORT"),
    ("gt", 2.9999999999999996, "SUPPORT"), ("lt", 3.0000000000000004, "SUPPORT"),
])
def test_exact_delta_comparators_use_no_tolerance(operator, threshold, verdict):
    records = numeric_facts()
    spec = metric_check(CounterDeltaCompare(series=records[0].series, operator=operator, threshold=threshold))
    result = evaluate(spec, records)
    assert result.verdict == verdict and not result.incompatible_ids
    optional = spec.model_copy(update={"requirement": "optional"})
    assert evaluate(optional, records).verdict == verdict


def test_zero_delta_false_is_neutral_not_health_or_no_traffic():
    records = numeric_facts(((0, 6), (10, 6)))
    spec = metric_check(CounterDeltaCompare(series=records[0].series, operator="gt", threshold=0))
    result = evaluate(spec, records)
    assert result.verdict == "NEUTRAL" and result.reason == "comparison_false"
    assert all(term not in result.model_dump_json() for term in ("healthy", "no traffic", "root cause"))
    assert evaluate(spec).verdict == "UNKNOWN"


def test_multiple_intervals_need_exact_binding_and_never_pick_first():
    records = numeric_facts(((0, 1), (10, 4), (20, 4)))
    spec = metric_check(CounterDeltaCompare(series=records[0].series, operator="gt", threshold=0))
    assert evaluate(spec, records).verdict == "UNKNOWN"
    for fact in records:
        bound = spec.model_copy(update={"predicate": spec.predicate.model_copy(update={"interval": fact.value.interval})})
        result = evaluate(bound, tuple(reversed(records)))
        assert result.verdict == ("SUPPORT" if fact.value.delta > 0 else "NEUTRAL")
        assert result == evaluate(bound, records)
    missing = ClosedSampleInterval(start=metrics.START+timedelta(seconds=30), end=metrics.START+timedelta(seconds=40))
    absent = spec.model_copy(update={"predicate": spec.predicate.model_copy(update={"interval": missing})})
    assert evaluate(absent, records).reason == "missing_metric_fact"


def test_same_interval_different_context_is_still_ambiguous():
    raw = MetricResult(series=(metrics.series(((0, 1), (10, 4))),))
    spec = CounterDeltaSpec(series=derived.key(raw.series[0]))
    records = tuple(fact for version in ("1", "2") for fact in derived.facts(derive_metrics(
        raw, window=metrics.WINDOW, context=derived.context_for(raw, version=version), specs=(spec,))))
    assert len({record.id for record in records}) == 2
    check = metric_check(CounterDeltaCompare(series=spec.series, operator="gt", threshold=0,
                                            interval=records[0].value.interval))
    assert evaluate(check, records).reason == "ambiguous_metric_fact"
    assert evaluate(check, (records[0], records[0])).verdict == "SUPPORT"


def test_interval_rate_consumes_rate_fact_only():
    records = numeric_facts(spec_type=IntervalRateSpec)
    spec = metric_check(IntervalRateCompare(series=records[0].series, operator="eq", threshold=0.3))
    assert evaluate(spec, records).verdict == "SUPPORT"
    false = spec.model_copy(update={"predicate": spec.predicate.model_copy(update={"threshold": 0.4})})
    assert evaluate(false, records).verdict == "NEUTRAL"
    assert evaluate(spec, numeric_facts()).verdict == "UNKNOWN"


@pytest.mark.parametrize("changes", [
    {"services": ("payment",)}, {"services": ("checkout", "payment")},
    {"signal": "trace"}, {"query_identity": "other"}, {"admitted_ids": ()},
])
def test_metric_positive_must_be_explicitly_bound_to_its_service_acquisition(changes):
    records = numeric_facts()
    spec = metric_check(CounterDeltaCompare(series=records[0].series, operator="gt", threshold=0))
    assert evaluate(spec, records, **changes).verdict == "UNKNOWN"


def test_metric_whole_extent_must_fit_scope_without_clipping_or_recomputation():
    records = numeric_facts()
    for window in (TimeWindow(start=metrics.START, end=metrics.START+timedelta(seconds=10)),
                   TimeWindow(start=metrics.START+timedelta(seconds=1), end=metrics.WINDOW.end)):
        spec = metric_check(CounterDeltaCompare(series=records[0].series, operator="gt", threshold=0), window=window)
        assert evaluate(spec, records).verdict == "UNKNOWN"
    spec = metric_check(CounterDeltaCompare(series=records[0].series, operator="gt", threshold=0))
    broader = TimeWindow(start=metrics.START-timedelta(seconds=10), end=metrics.WINDOW.end+timedelta(seconds=10))
    assert evaluate(spec, records, window=broader).verdict == "SUPPORT"


def category_facts():
    return metrics.categories(metrics.observe(context=MetricObservationContext(category_labels=(metrics.STATUS,))))


def test_category_membership_is_only_returned_series_membership():
    records = category_facts()
    spec = metric_check(CategoryPresent(label=metrics.STATUS, value="UNAVAILABLE"))
    assert evaluate(spec, records, truncated=True).verdict == "SUPPORT"
    absent = metric_check(CategoryPresent(label=metrics.STATUS, value="UNKNOWN"))
    result = evaluate(absent, records, truncated=True)
    assert result.verdict == "NEUTRAL" and result.incompatible_ids == ()
    assert result.reason == "returned_category_not_present"
    assert evaluate(absent).verdict == "UNKNOWN"
    assert evaluate(metric_check(CategoryPresent(label="not-returned", value="UNKNOWN")), records).verdict == "UNKNOWN"


def test_category_has_no_member_time_so_broader_window_cannot_localize_it():
    records = category_facts()
    spec = metric_check(CategoryPresent(label=metrics.STATUS, value="UNAVAILABLE"))
    broader = TimeWindow(start=metrics.START-timedelta(seconds=1), end=metrics.WINDOW.end)
    assert evaluate(spec, records, window=broader).verdict == "UNKNOWN"


def test_reordering_duplicates_and_shared_correlation_do_not_vote():
    records = (rpc_fact(rpc_status=14, span_id="a"*16), rpc_fact(rpc_status=14, span_id="b"*16))
    assert records[0].correlation.trace_id == records[1].correlation.trace_id
    spec = trace_check()
    result = evaluate(spec, records)
    assert result == evaluate(spec, tuple(reversed(records))) == evaluate(spec, records*2)
    assert result.id == evaluate(spec, records*2).id
    assert result.matched_ids == tuple(sorted(record.id for record in records))
    assert result.verdict == evaluate(spec, records[:1]).verdict == "SUPPORT"
    assert "confidence" not in result.model_dump()


def test_conflicting_same_id_records_do_not_depend_on_order():
    fact = rpc_fact(rpc_status=14)
    conflict = fact.model_copy(update={"value": fact.value.model_copy(update={"peer_service": "shipping"})})
    spec = trace_check()
    assert evaluate(spec, (fact, conflict)) == evaluate(spec, (conflict, fact))
    assert evaluate(spec, (fact, conflict)).reason == "conflicting_fact_identity"


@pytest.mark.parametrize("changes", [
    {"semantic_version": "2"}, {"requirement": "optional"}, {"relation": "contradicts_hypothesis"},
    {"predicate": RpcExists(local_service="checkout", rpc_status_names=("UNKNOWN",))},
    {"scope": CheckScope(signal="trace", services=("checkout",), window=TRACE_WINDOW, query_identity="other")},
])
def test_changed_semantics_change_structural_check_id(changes):
    spec = trace_check()
    assert spec.id.startswith("C-") and len(spec.id) == 66
    assert spec.model_copy(update=changes).id != spec.id


def test_numeric_identity_binds_operator_threshold_interval_and_full_labels():
    record = numeric_facts()[0]
    spec = metric_check(CounterDeltaCompare(series=record.series, operator="gt", threshold=0))
    for changes in ({"operator": "gte"}, {"threshold": 1.0}, {"interval": record.value.interval},
                    {"series": MetricSeriesKey(name=record.series.name, labels=(("empty", ""),))}):
        assert spec.model_copy(update={"predicate": spec.predicate.model_copy(update=changes)}).id != spec.id


def test_display_warning_and_physical_locator_do_not_change_identity():
    spec, fact = trace_check(), rpc_fact(rpc_status=14)
    display = spec.model_copy(update={"alias": "human-key", "description": "Different prose"})
    assert display.id == spec.id
    original = evaluate(spec, (fact,))
    moved = fact.model_copy(update={"summary": "Different fact prose", "provenance": fact.provenance.model_copy(
        update={"locator": "C:/private/new-path"}), "quality": fact.quality.model_copy(update={"warnings": ("wording",)})})
    assert evaluate(display, (moved,), warnings=("warning prose",)) == original
    assert original.model_copy(update={"summary": "display"}).id == original.id
    assert evaluate(spec, (fact,), truncated=True).id != original.id
    assert CheckSpec.model_validate_json(spec.model_dump_json()).id == spec.id
    assert CheckResult.model_validate_json(original.model_dump_json()).id == original.id
    with pytest.raises(ValidationError):
        CheckSpec(**spec.model_dump(), id="caller-chosen-id")


def test_canonical_sets_timezone_and_numeric_zero():
    spec = trace_check(outcomes=("failure", "success", "failure"), rpc_status_names=("UNKNOWN", "UNAVAILABLE"))
    canonical = trace_check(outcomes=("success", "failure"), rpc_status_names=("UNAVAILABLE", "UNKNOWN"))
    assert spec.id == canonical.id
    shifted = TimeWindow(start=TRACE_WINDOW.start.astimezone(timezone(timedelta(hours=8))),
                         end=TRACE_WINDOW.end.astimezone(timezone(timedelta(hours=8))))
    assert spec.model_copy(update={"scope": spec.scope.model_copy(update={"window": shifted})}).id == spec.id
    key = MetricSeriesKey(name="numeric")
    assert metric_check(CounterDeltaCompare(series=key, operator="eq", threshold=-0.0)).id == metric_check(
        CounterDeltaCompare(series=key, operator="eq", threshold=0)).id


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), True, "1"])
def test_invalid_threshold_is_rejected(value):
    with pytest.raises(ValidationError):
        CounterDeltaCompare(series=MetricSeriesKey(name="numeric"), operator="gt", threshold=value)


def test_requirement_never_changes_truth_or_missing_fact_evaluability():
    fact, spec = rpc_fact(rpc_status=2), trace_check()
    optional = spec.model_copy(update={"requirement": "optional"})
    for records in ((), (fact,)):
        assert evaluate(spec, records).verdict == evaluate(optional, records).verdict == "UNKNOWN"
        assert evaluate_check(spec, observations=records, coverage=(complete(spec, records),)).verdict == evaluate_check(
            optional, observations=records, coverage=(complete(optional, records),)).verdict == "NEUTRAL"


def test_reserved_contradiction_relation_does_not_invert_positive_match():
    fact, spec = rpc_fact(rpc_status=14), trace_check()
    reserved = spec.model_copy(update={"relation": "contradicts_hypothesis"})
    assert evaluate(reserved, (fact,)).verdict == "UNKNOWN"


def test_future_check_semantics_are_not_silently_run_as_version_one():
    spec, fact = trace_check(), rpc_fact(rpc_status=14)
    future = spec.model_copy(update={"semantic_version": "2"})
    assert evaluate(future, (fact,)).reason == "unsupported_check_version"


def test_conflicting_acquisition_metadata_rejected():
    spec = trace_check()
    with pytest.raises(ValueError, match="conflicting coverage"):
        evaluate_check(spec, coverage=(complete(spec), complete(spec, truncated=True)))


def test_evaluator_has_no_io_or_metric_recomputation(monkeypatch):
    trace, trace_spec = rpc_fact(rpc_status=14), trace_check()
    metric_records = numeric_facts()
    metric_spec = metric_check(CounterDeltaCompare(series=metric_records[0].series, operator="gt", threshold=0))
    from aegis_rca.observations import metric_derivation, metrics as metric_implementation
    import builtins

    def forbidden(*args, **kwargs):
        raise AssertionError("verification must consume supplied facts without IO or derivation")

    with monkeypatch.context() as patch:
        patch.setattr(builtins, "open", forbidden)
        patch.setattr(socket, "socket", forbidden)
        patch.setattr(metric_derivation, "derive_metrics", forbidden)
        patch.setattr(metric_implementation, "observe_metrics", forbidden)
        assert evaluate(trace_spec, (trace,)).verdict == "SUPPORT"
        assert evaluate(metric_spec, metric_records).verdict == "SUPPORT"
    with pytest.raises(TypeError, match="typed observation"):
        evaluate_check(trace_spec, observations=({"id": "fake"},))


@pytest.mark.parametrize("spec_type,expected_hash", [
    (CounterDeltaSpec, "347d8e4d1371b2ae0b6a45071b636bad5c83494bfe1b58169322050140421f0e"),
    (IntervalRateSpec, "8ec765034175b03f2960e70bfb6e5cee08af3339dc7885dedca01c938db9d487"),
])
def test_existing_p13b1_complete_derived_wire_and_ids_unchanged(spec_type, expected_hash):
    # Captured from untouched P1.3b-1 producers before implementing these tests.
    records = numeric_facts(((0, 1), (10, 4), (20, 4)), spec_type)
    assert hashlib.sha256("".join(record.model_dump_json() for record in records).encode()).hexdigest() == expected_hash
