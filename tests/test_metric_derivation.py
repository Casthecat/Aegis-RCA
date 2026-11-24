"""P1.3b-1 arithmetic and immutable P1.3a wire regressions; synthetic inputs only."""

from datetime import timedelta, timezone
import hashlib
import json

import pytest
from pydantic import ValidationError

from aegis_rca.models.incident import IncidentInput, TimeWindow
from aegis_rca.models.metric_observation import (
    ClosedSampleInterval, CounterDeltaSpec, CounterDeltaValue, IntervalRateSpec,
    IntervalRateValue, MetricContextIdentity, MetricDescriptor, MetricDescriptorBinding,
    MetricDerivationDiagnostic, MetricDerivedFactProvenance, MetricObservationBatch,
    MetricObservationContext, MetricSeriesKey,
)
from aegis_rca.models.telemetry import MetricResult
from aegis_rca.observations import derive_metrics, observe_metrics
from aegis_rca.observations import metric_derivation as implementation
from aegis_rca.policy import make_task
from aegis_rca.runner import run_investigation
from aegis_rca.runtime import Dependencies
from aegis_rca.state import initial_state
from aegis_rca.tools.replay import ReplayBackend
from aegis_rca.tools.snapshot import TelemetrySnapshot, read_snapshot, write_snapshot

import test_metric_observations as base


COUNTER = MetricDescriptor(instrument_type="counter", value_semantics="cumulative_counter", unit="observations")
SPEC_TYPES = (CounterDeltaSpec, IntervalRateSpec)


def key(item):
    return MetricSeriesKey(name=item.name, labels=tuple(item.labels.items()))


def context_for(result, descriptor=COUNTER, version="1", basis="unknown", **kwargs):
    keys = sorted({key(item) for item in result.series}, key=lambda item: (item.name, item.labels))
    return MetricObservationContext(
        contract=MetricContextIdentity(contract_id="counter-fixture", source="synthetic-contract", version=version),
        descriptors=tuple(MetricDescriptorBinding(series=series_key, descriptor=descriptor) for series_key in keys),
        sample_time_basis=basis, **kwargs,
    )


def run(samples, spec_type=CounterDeltaSpec, *, descriptor=COUNTER, **spec_kwargs):
    result = MetricResult(series=(base.series(samples),))
    return derive_metrics(result, window=base.WINDOW, context=context_for(result, descriptor),
                          specs=(spec_type(series=key(result.series[0]), **spec_kwargs),))


def facts(batch):
    return tuple(record for record in batch.observations if isinstance(record.value, (CounterDeltaValue, IntervalRateValue)))


def intervals(batch):
    return sorted(((record.value.interval.start - base.START).total_seconds(),
                   (record.value.interval.end - base.START).total_seconds()) for record in facts(batch))


def diagnostics(batch):
    return tuple(item for item in batch.diagnostics if isinstance(item, MetricDerivationDiagnostic))


def reasons(batch):
    return {item.reason for diagnostic in diagnostics(batch) for item in diagnostic.exclusions}


def p13a_cases():
    typed_key = base.observe().observations[0].series
    return {
        "payment": (base.payment_result(), None),
        "category": (base.payment_result(), MetricObservationContext(category_labels=(base.STATUS,))),
        "context": (base.payment_result(), base.semantic_context(typed_key)),
        "invalid": (MetricResult(series=(base.series(((0,1),(10,2),(10,3),(20,float('nan')),
                      (30,float('inf')),(40,-1),(60,9))),)), None),
        "duplicate": (MetricResult(series=(base.series(((0,1),(10,2))),base.series(((0,1),(10,3),(20,4))))), None),
        "empty": (MetricResult(), None),
        "no_usable": (MetricResult(series=(base.series(()),)), None),
    }


# Captured from the unmodified P1.3a implementation BEFORE this build. Hashes
# include the complete JSON bytes (including O-metric IDs), not recalculated expectations.
P13A_WIRE_HASHES = {
    "payment": "c86f077cde7744a60497d36be133ae07da489eef61e7034651c848a92d926e9a",
    "category": "da22077ace0afea1524ea543e2d4dd27119d4b4ecf5ce4653cf0b0fe506ccd9a",
    "context": "8ff11a8949c7f1892f3e0af095240df393064458cfebcda6b8c30c84e5d8fa91",
    "invalid": "3d6449e020e1219e9940dba95fb88c945264b8dc7258afdee47f410d6b28b0d8",
    "duplicate": "7e71bf455625ebc6dcc564dc954aebca250f12a986ab432123a14e879f4958aa",
    "empty": "2cd9cc2aa9bf957bfe29fb41ad517d1b56b45be5a9bc6d63686373dbe7728452",
    "no_usable": "d8d94353f430c8a546ac4afdc62b65b549c194e71b1dec50880c623830aede33",
}


@pytest.mark.parametrize("case", P13A_WIRE_HASHES)
def test_p13a_complete_wire_and_ids_unchanged_without_specs(case):
    result, context = p13a_cases()[case]
    observed = observe_metrics(result, window=base.WINDOW, context=context)
    assert hashlib.sha256(observed.model_dump_json().encode()).hexdigest() == P13A_WIRE_HASHES[case]
    assert derive_metrics(result, window=base.WINDOW, context=context, specs=()) == observed
    assert derive_metrics(result, window=base.WINDOW, context=context).model_dump_json() == observed.model_dump_json()


@pytest.mark.parametrize("scope,expected", [
    ("returned_usable", [(15,30),(30,45),(45,60)]),
    ("requested_window", [(15,30),(30,45)]),
])
def test_payment_321_6_adjacent_deltas_and_rates_are_only_visible_endpoint_arithmetic(scope, expected):
    result = base.payment_result()
    context = context_for(result, category_labels=(base.STATUS,))
    specs = tuple(spec_type(series=key(item), sample_scope=scope) for item in result.series for spec_type in SPEC_TYPES)
    batch = derive_metrics(result, window=base.WINDOW, context=context, specs=specs)
    originals = observe_metrics(result, window=base.WINDOW, context=context)
    assert tuple(record for record in batch.observations if record not in facts(batch)) == originals.observations
    for item in result.series:
        for kind in ("counter_delta", "interval_rate"):
            selected = [record for record in facts(batch) if record.series == key(item) and record.value.kind == kind]
            assert sorted(((record.value.interval.start-base.START).total_seconds(),
                           (record.value.interval.end-base.START).total_seconds()) for record in selected) == expected
            for record in selected:
                assert record.value.delta == 0
                assert record.provenance.reset_visibility == "visible_decreases_only"
                assert record.provenance.gap_policy == "unspecified"
                if kind == "interval_rate":
                    assert record.value.rate == 0
                    assert record.value.elapsed_seconds == 15
                    assert record.value.used_sample_count == 2
                    assert record.value.rate_kind == "local_adjacent_endpoint"
    assert len(facts(batch)) == len(expected) * 4
    serialized = batch.model_dump_json()
    for forbidden in ('"request_count"', '"total":327', '"error_rate"', "no traffic", "healthy", "anomaly", "root cause"):
        assert forbidden not in serialized
    assert MetricObservationBatch.model_validate_json(serialized) == batch


@pytest.mark.parametrize("suffix", ["_total", "_count", "_seconds", "_bucket", "_sum"])
@pytest.mark.parametrize("spec_type", SPEC_TYPES)
def test_names_and_query_text_never_grant_counter_authority(suffix, spec_type):
    result = MetricResult(series=(base.series(((0,1),(10,2)), name="requests"+suffix),))
    context = MetricObservationContext(query="rate(requests_total[5m])")
    batch = derive_metrics(result, window=base.WINDOW, context=context, specs=(spec_type(series=key(result.series[0])),))
    assert facts(batch) == ()
    assert diagnostics(batch)[0].code == "missing_counter_authority"


@pytest.mark.parametrize("descriptor", [
    MetricDescriptor(), MetricDescriptor(instrument_type="gauge", value_semantics="gauge_value"),
    MetricDescriptor(instrument_type="counter", value_semantics="rate"),
    MetricDescriptor(instrument_type="histogram", value_semantics="cumulative_counter"),
    MetricDescriptor(instrument_type="counter"),
])
@pytest.mark.parametrize("spec_type", SPEC_TYPES)
def test_unknown_gauge_and_already_rate_inputs_are_inapplicable(descriptor, spec_type):
    batch = run(((0,1),(10,3)), spec_type, descriptor=descriptor)
    assert not facts(batch)
    assert diagnostics(batch)[0].code == "incompatible_descriptor"
    assert len(batch.observations) == 1  # Generic P1.3a summary survives.


def test_specs_select_exact_series_and_descriptor_binding():
    result = base.payment_result()
    spec = CounterDeltaSpec(series=key(result.series[1]))
    context = context_for(MetricResult(series=(result.series[0],)))
    batch = derive_metrics(result, window=base.WINDOW, context=context, specs=(spec,))
    assert diagnostics(batch)[0].code == "missing_counter_authority"
    missing = CounterDeltaSpec(series=MetricSeriesKey(name="absent"))
    batch = derive_metrics(result, window=base.WINDOW, specs=(missing,))
    assert diagnostics(batch)[0].code == "series_not_returned"


def test_adjacent_nonzero_delta_and_rate_are_exact():
    batch = run(((0,100),(10,120),(30,130)), IntervalRateSpec)
    values = sorted((record.value for record in facts(batch)), key=lambda value: value.interval.start)
    assert [(value.delta, value.elapsed_seconds, value.rate) for value in values] == [(20,10,2),(10,20,.5)]
    assert all(value.used_sample_count == 2 for value in values)
    assert (0,30) not in intervals(batch)
    assert [record.value.delta for record in sorted(facts(run(((0,100),(10,120),(30,130)))),
                                                   key=lambda record: record.value.interval.start)] == [20,10]


@pytest.mark.parametrize("spec_type", SPEC_TYPES)
@pytest.mark.parametrize("after_reset", [0, 2])
def test_visible_reset_and_reset_to_zero_never_repaired_or_crossed(spec_type, after_reset):
    batch = run(((0,100),(10,120),(20,after_reset),(30,8)), spec_type)
    assert intervals(batch) == [(0,10),(20,30)]
    assert reasons(batch) == {"visible_reset"}
    for record in facts(batch):
        assert record.value.delta >= 0
        assert record.provenance.exclusions_ref == diagnostics(batch)[0].id
        assert record.provenance.reset_visibility == "visible_decreases_only"


@pytest.mark.parametrize("spec_type", SPEC_TYPES)
@pytest.mark.parametrize("invalid,reason", [
    (((10,float("nan")),), "nonfinite_timestamp"),
    (((10,float("inf")),), "nonfinite_timestamp"),
    (((10,float("-inf")),), "nonfinite_timestamp"),
    (((10,2),(10,3)), "conflicting_timestamp"),
    (((10,-1),), "negative_counter"),
    (((10,2),(10,float("nan"))), "nonfinite_timestamp"),
])
def test_valid_invalid_valid_never_bridges_the_barrier(spec_type, invalid, reason):
    batch = run(((0,1), *invalid, (20,4)), spec_type)
    assert facts(batch) == ()
    assert reason in reasons(batch)
    assert diagnostics(batch)[0].code == "no_legal_intervals"
    # Resume at the next ORIGINAL adjacent pair, not by bridging 0 -> 20.
    resumed = run(((0,1), *invalid, (20,4), (30,7)), spec_type)
    assert intervals(resumed) == [(20,30)]


def test_negative_numbers_stay_valid_generic_p13a_samples():
    batch = run(((0,1),(10,-2),(20,3)))
    summary = next(record for record in batch.observations if record.value.kind == "window_summary")
    assert summary.value.usable_sample_count == 3
    assert summary.value.min == -2
    assert "negative_counter" in reasons(batch)
    assert facts(batch) == ()


@pytest.mark.parametrize("spec_type", SPEC_TYPES)
def test_explicit_gap_threshold_is_strictly_greater_than(spec_type):
    batch = run(((0,1),(10,2),(21,3),(31,4)), spec_type, max_gap_seconds=10)
    assert intervals(batch) == [(0,10),(21,31)]
    assert reasons(batch) == {"max_gap_exceeded"}
    assert all(record.provenance.gap_policy == "maximum_gap" for record in facts(batch))
    assert all(record.provenance.max_gap_seconds == 10 for record in facts(batch))


@pytest.mark.parametrize("spec_type", SPEC_TYPES)
def test_absent_gap_policy_allows_only_pair_arithmetic_without_trusting_continuity(spec_type):
    batch = run(((0,1),(10000,3)), spec_type)
    assert intervals(batch) == [(0,10000)]
    assert facts(batch)[0].provenance.gap_policy == "unspecified"
    assert facts(batch)[0].provenance.max_gap_seconds is None


def test_segment_mode_requires_explicit_gap_and_emits_independent_regions():
    samples = ((0,100),(10,120),(20,130),(30,0),(40,4),(50,9),(100,10),(110,12))
    missing = run(samples, mode="single_monotonic_segment")
    assert facts(missing) == ()
    assert diagnostics(missing)[0].code == "segment_gap_required"
    batch = run(samples, mode="single_monotonic_segment", max_gap_seconds=10)
    assert intervals(batch) == [(0,20),(30,50),(100,110)]
    values = sorted((record.value for record in facts(batch)), key=lambda value: value.interval.start)
    assert [(value.delta,value.used_sample_count) for value in values] == [(30,3),(9,3),(2,2)]
    assert reasons(batch) == {"visible_reset","max_gap_exceeded"}


@pytest.mark.parametrize("invalid", [((20,float("nan")),), ((20,1),(20,2)), ((20,-1),)])
def test_segment_mode_does_not_cross_invalid_timestamps(invalid):
    batch = run(((0,1),(10,3),*invalid,(30,4),(40,9)), mode="single_monotonic_segment", max_gap_seconds=20)
    assert intervals(batch) == [(0,10),(30,40)]


@pytest.mark.parametrize("value", [0,-1,float("inf"),float("nan"),True])
@pytest.mark.parametrize("spec_type", SPEC_TYPES)
def test_gap_must_be_positive_finite_and_not_boolean(value, spec_type):
    with pytest.raises(ValidationError):
        spec_type(series=MetricSeriesKey(name="m"), max_gap_seconds=value)


def test_rate_spec_and_value_cannot_describe_segment_endpoint_rate():
    with pytest.raises(ValidationError):
        IntervalRateSpec(series=MetricSeriesKey(name="m"), mode="single_monotonic_segment")
    value = facts(run(((0,1),(10,3)), IntervalRateSpec))[0].value
    with pytest.raises(ValidationError):
        IntervalRateValue(**{**value.model_dump(), "used_sample_count": 3})
    with pytest.raises(ValidationError):
        IntervalRateValue(**{**value.model_dump(), "rate_kind": "segment_endpoint_rate"})
    with pytest.raises(ValidationError):
        ClosedSampleInterval(start=base.START, end=base.START)


def test_derived_wire_contract_rejects_mismatched_specs_intervals_and_authority():
    record = facts(run(((0,1),(10,3)), IntervalRateSpec))[0]
    payload = record.model_dump()
    invalid = {**payload, "provenance": {**payload["provenance"],
               "spec": CounterDeltaSpec(series=record.series).model_dump()}}
    with pytest.raises(ValidationError):
        type(record).model_validate(invalid)
    invalid = {**payload, "descriptor": MetricDescriptor().model_dump()}
    with pytest.raises(ValidationError):
        type(record).model_validate(invalid)
    invalid = {**payload, "provenance": {**payload["provenance"], "gap_policy": "maximum_gap"}}
    with pytest.raises(ValidationError):
        type(record).model_validate(invalid)
    invalid = {**payload, "provenance": {**payload["provenance"], "used_interval": {
        "start":base.START, "end":base.START+timedelta(seconds=20)}}}
    with pytest.raises(ValidationError):
        type(record).model_validate(invalid)


@pytest.mark.parametrize("samples", [(), ((0,1),), ((0,float("nan")),)])
@pytest.mark.parametrize("spec_type", SPEC_TYPES)
def test_insufficient_samples_do_not_manufacture_zero_facts(samples, spec_type):
    batch = run(samples, spec_type)
    assert not facts(batch)
    assert diagnostics(batch)[0].code == "insufficient_samples"


def test_explicit_window_is_independent_of_requested_window_and_keeps_barriers():
    explicit = TimeWindow(start=base.START+timedelta(seconds=60), end=base.START+timedelta(seconds=100))
    batch = run(((0,1),(60,2),(70,float("nan")),(80,3),(90,4),(100,5)),
                sample_scope="explicit_window", scope_window=explicit)
    assert intervals(batch) == [(80,90)]
    assert reasons(batch) == {"outside_scope","nonfinite_timestamp"}
    assert facts(batch)[0].provenance.scope_window == explicit
    assert facts(batch)[0].provenance.used_interval.end != explicit.end


def test_scope_window_is_required_only_for_explicit_scope():
    with pytest.raises(ValidationError):
        CounterDeltaSpec(series=MetricSeriesKey(name="m"), sample_scope="explicit_window")
    with pytest.raises(ValidationError):
        CounterDeltaSpec(series=MetricSeriesKey(name="m"), scope_window=base.WINDOW)


@pytest.mark.parametrize("basis,expected", [
    ("unknown", ("unknown_time_basis",)),
    ("query_evaluation", ("evaluation_timestamps_not_scrape_timestamps",)),
    ("source_sample", ()),
])
def test_time_basis_is_structured_and_does_not_change_arithmetic(basis, expected):
    result = MetricResult(series=(base.series(((0,1),(10,3))),))
    batch = derive_metrics(result, window=base.WINDOW, context=context_for(result,basis=basis),
                          specs=(IntervalRateSpec(series=key(result.series[0])),))
    record = facts(batch)[0]
    assert record.quality.issues == expected
    assert record.provenance.sample_time_basis == basis
    assert record.value.rate == .2


def test_rate_overflow_is_a_typed_exclusion_not_an_infinite_result():
    batch = run(((0,0),(.000001,1e308),(.000002,1e308)), IntervalRateSpec)
    assert intervals(batch) == [(.000001,.000002)]
    assert reasons(batch) == {"numeric_overflow"}
    assert facts(batch)[0].value.rate == 0
    json.dumps(batch.model_dump(mode="json"),allow_nan=False)


def test_derived_identity_binds_context_spec_and_used_intermediate_samples():
    result = MetricResult(series=(base.series(((0,1),(10,2),(20,5))),))
    spec = CounterDeltaSpec(series=key(result.series[0]),mode="single_monotonic_segment",max_gap_seconds=15)
    def derived_id(result=result, context=None, spec=spec):
        return facts(derive_metrics(result,window=base.WINDOW,context=context or context_for(result),specs=(spec,)))[0].id
    original = derived_id()
    assert derived_id(context=context_for(result,version="2")) != original
    assert derived_id(spec=spec.model_copy(update={"max_gap_seconds":20.0})) != original
    modified = MetricResult(series=(base.series(((0,1),(10,3),(20,5))),))
    assert derived_id(result=modified) != original  # Same endpoint delta, different used input.
    shifted = MetricResult(series=(base.series(((0,1),(11,2),(20,5))),))
    assert derived_id(result=shifted) != original


@pytest.mark.parametrize("scope,binds_request", [("returned_usable",False),("requested_window",True),("explicit_window",False)])
def test_only_requested_scope_binds_requested_window_in_fact_id(scope,binds_request):
    result = MetricResult(series=(base.series(((15,1),(30,3))),))
    spec = CounterDeltaSpec(series=key(result.series[0]),sample_scope=scope,
                            scope_window=base.WINDOW if scope=="explicit_window" else None)
    context = context_for(result)
    first = derive_metrics(result,window=base.WINDOW,context=context,specs=(spec,))
    changed_window = TimeWindow(start=base.START,end=base.WINDOW.end+timedelta(seconds=10))
    second = derive_metrics(result,window=changed_window,context=context,specs=(spec,))
    assert (facts(first)[0].id != facts(second)[0].id) is binds_request
    if scope=="explicit_window":
        changed = derive_metrics(result,window=base.WINDOW,context=context,
                                 specs=(spec.model_copy(update={"scope_window":changed_window}),))
        assert facts(changed)[0].id != facts(first)[0].id


def test_order_duplicates_timezone_and_metadata_do_not_change_derived_ids(monkeypatch):
    result = base.payment_result()
    specs = tuple(spec_type(series=key(item)) for item in result.series for spec_type in SPEC_TYPES)
    context = context_for(result)
    first = derive_metrics(result,window=base.WINDOW,context=context,specs=specs)
    reordered = result.model_copy(update={"series":tuple(item.model_copy(update={
        "labels":dict(reversed(list(item.labels.items()))),
        "points":tuple(point.model_copy(update={"timestamp":point.timestamp.astimezone(timezone(timedelta(hours=8)))})
                       for point in reversed(item.points))*2}) for item in reversed(result.series))})
    second = derive_metrics(reordered,window=base.WINDOW,context=context,specs=tuple(reversed(specs))*2)
    assert first.observations == second.observations
    monkeypatch.setattr(implementation,"_derived_summary",lambda _: "Different prose")
    changed = result.model_copy(update={"series":tuple(item.model_copy(update={"source":"C:/candidate-uuid/snapshot.json"})
                                                          for item in result.series),
                                        "coverage_complete":False,"truncated":True,"warnings":("new warning",)})
    third = derive_metrics(changed,window=base.WINDOW,context=context.model_copy(update={
        "query":"different query", "query_status":"failed"}),specs=specs)
    assert [record.id for record in facts(first)] == [record.id for record in facts(third)]
    assert all(record.summary=="Different prose" for record in facts(third))


def test_unrelated_exclusions_are_audit_links_not_inputs_to_unchanged_pair_identity():
    initial = run(((0,1),(10,3)))
    changed = run(((0,1),(10,3),(20,float("nan"))))
    assert facts(initial)[0].id == facts(changed)[0].id
    assert facts(initial)[0].provenance.exclusions_ref is None
    diagnostic = diagnostics(changed)[0]
    assert facts(changed)[0].provenance.exclusions_ref == diagnostic.id
    assert diagnostic.exclusions[0].timestamp == base.START+timedelta(seconds=20)


async def test_derived_facts_do_not_change_legacy_replay_rca_or_snapshot_bytes(tmp_path):
    incident = IncidentInput(query="Investigate checkout errors.",seed_services=("checkout",),window=base.WINDOW)
    snapshot = TelemetrySnapshot(incident=incident,provenance="synthetic_wire_fixture",metrics=base.payment_result())
    path = tmp_path/"snapshot.json"
    write_snapshot(snapshot,path)
    original_bytes = path.read_bytes()
    backend = ReplayBackend(snapshot)
    task = make_task(incident,"metric")
    tool_before = await backend.query(task)
    before,_ = await run_investigation(initial_state(incident),Dependencies(backend))
    result = read_snapshot(path).metrics
    derived = derive_metrics(result,window=base.WINDOW,context=context_for(result),
                             specs=tuple(CounterDeltaSpec(series=key(item)) for item in result.series))
    after,_ = await run_investigation(initial_state(incident),Dependencies(backend))
    assert len(facts(derived)) == 6
    assert await backend.query(task) == tool_before
    assert tool_before.status=="empty" and tool_before.evidence==()
    assert before["final_report"] == after["final_report"]
    assert before["evidence"] == after["evidence"] == []
    assert after["final_report"].status=="INCONCLUSIVE"
    assert path.read_bytes()==original_bytes
    assert read_snapshot(path)==snapshot
