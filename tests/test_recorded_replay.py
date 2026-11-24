from portfolio_helpers import single_phv, subject
"""Historical A selection is not current execution identity or authorization."""

from datetime import timedelta
import json

import pytest
from langgraph.runtime import Runtime
from pydantic import ValidationError, TypeAdapter

from aegis_rca import graph
from aegis_rca.fixtures import FixtureBackend, demo_incident
from aegis_rca.models.acquisition import AttemptIdentity, TaskSpec
from aegis_rca.models.acquisition_result import AcquisitionResult
from aegis_rca.models.evidence import ToolResult
from aegis_rca.models.incident import Budget, TimeWindow
from aegis_rca.models.recorded import RecordedTaskRequest
from aegis_rca.models.telemetry import TraceResult, MetricResult
from aegis_rca.policy import make_task
from aegis_rca.runner import run_investigation
from aegis_rca.runtime import Dependencies, ReservedAttempt, finalize_acquisition
from aegis_rca.state import initial_state, IncidentState
from aegis_rca.task_materialization import materialize_task, UnauthorizedAcquisition
from aegis_rca.tools.recorded_replay import RecordedReplayBackend, RecordedAcquisitionUnavailable, AmbiguousRecordedAcquisition
from aegis_rca.tools.snapshot_v2 import export_snapshot_v2, SnapshotV2, RecordedAcquisitionV2
from aegis_rca.trace_acquisition import resolve_trace_attempt

from test_snapshot_v2 import history, reseal, replace_row
from test_acquisition_partitions import execute, request, overlap, logs, live


def explicit(task, selector):
    return RecordedTaskRequest(**task.model_dump(), recorded_selection=selector)


def test_task_v1_wire_and_identity_golden_are_unchanged():
    task = materialize_task(make_task(demo_incident(), "trace"), FixtureBackend())
    wire = ('{"schema_version":1,"signal":"trace","services":["checkout"],"window":'
            '{"start":"2026-09-08T14:30:00Z","end":"2026-09-08T14:40:00Z"},'
            '"query_template_id":"fixture.select.v1","query_expression":null,"result_limit":null,'
            '"metric_step_seconds":null,"log_query":null,"alias":null}')
    assert task.model_dump_json() == wire
    assert task.id == "T-1b2766336940c98744c8226297da12c07cf0ac18c42adc0dcdc80807b99a6caa"
    assert "recorded_selection" not in task.model_dump()
    assert TypeAdapter(list[TaskSpec]).dump_json([task]) == ("[" + wire + "]").encode()


async def test_task_v2_requires_selector_and_template_and_v1_forbids_selection(history):
    backend = RecordedReplayBackend(history[2])
    spec = materialize_task(request(), backend)
    assert spec.schema_version == 2 and spec.query_template_id == "replay.select.recorded.v1"
    assert spec.recorded_selection.replay_manifest_id == history[2].replay_manifest_id
    assert TaskSpec.model_validate_json(spec.model_dump_json()).id == spec.id
    for update in ({"schema_version": 1}, {"recorded_selection": None}, {"query_template_id": "replay.select.v1"},
                   {"result_limit": 1}):
        with pytest.raises(ValidationError):
            TaskSpec.model_validate({**spec.model_dump(), **update})


@pytest.mark.parametrize("kind", ["fixture", "live"])
async def test_exact_trace_replay_preserves_O_and_raw_but_uses_new_T_A_namespace(kind, live):
    backend = live if kind == "live" else FixtureBackend()
    deps = Dependencies(backend)
    state = await execute(initial_state(demo_incident(), execution_id="real-history"), deps, [request()])
    snapshot = export_snapshot_v2(state, deps)
    old, = snapshot.recorded_acquisitions
    current_deps = Dependencies(RecordedReplayBackend(snapshot))
    current = await execute(initial_state(demo_incident(), execution_id="fresh-replay"), current_deps, [request()])
    new, = current_deps.acquisitions.values()
    assert new.attempt.id != old.attempt.id and new.attempt.task_spec_id != old.attempt.task_spec_id
    assert new.attempt.execution_id == "fresh-replay"
    assert new.attempt.admitted_observation_ids == old.attempt.admitted_observation_ids
    assert new.attempt.source_material_digest == old.attempt.source_material_digest
    assert new.partition.material == old.partition.material and new.partition.source == old.partition.source
    assert new.partition.content_digest == old.partition_content_digest
    assert new.partition.source.backend != "replay"
    assert new.recorded_from.recorded_attempt_id == old.attempt.id
    assert new.recorded_from.recorded_task_spec_id == old.attempt.task_spec_id
    assert new.recorded_from.recorded_content_digest == new.partition.content_digest
    assert "recorded_from" not in new.partition.model_dump()
    assert "path" not in new.recorded_from.model_dump()
    assert old.attempt.id not in {a.id for a in current["attempt_records"]}
    assert resolve_trace_attempt(new.attempt, current["task_specs"][0], current_deps).outcome == "verified"
    rearchived = export_snapshot_v2(current, current_deps)
    assert rearchived.recorded_acquisitions[0].recorded_from == new.recorded_from
    adapter = TypeAdapter(IncidentState)
    restored = adapter.validate_json(adapter.dump_json(current))
    assert restored["task_specs"][0].schema_version == 2
    assert restored["task_specs"][0].id == new.attempt.task_spec_id


async def test_reusing_historical_namespace_fails_before_reservation(history):
    snapshot = history[2]
    deps = Dependencies(RecordedReplayBackend(snapshot))
    with pytest.raises(ValueError, match="new execution namespace"):
        await run_investigation(initial_state(snapshot.incident, execution_id=snapshot.historical_execution_id), deps)
    assert not deps.attempts and not deps.acquisitions.ids()


async def test_current_authorization_is_rederived_before_payment_replay():
    deps = Dependencies(FixtureBackend(materials={
        ("trace", ("payment",)): overlap(), ("log", ("payment",)): logs("payment")}))
    state, _ = await run_investigation(initial_state(demo_incident(), execution_id="expanded-history"), deps)
    state = await graph.investigate_graph().ainvoke({**state, "pending_tasks": [request(service="payment"),
        request("log", "payment")]}, context=deps)
    snapshot = export_snapshot_v2(state, deps)
    assert snapshot.investigation_scope.admitted_services == ("checkout", "payment")
    replay_deps = Dependencies(RecordedReplayBackend(snapshot))
    current = initial_state(snapshot.incident, Budget(max_tool_calls=10), execution_id="current-replay")
    assert current["investigation_scope"] is None
    with pytest.raises(UnauthorizedAcquisition):
        await execute(current, replay_deps, [request(service="payment")])
    assert not replay_deps.attempts
    current = await execute(current, replay_deps, [request()])
    assert current["investigation_scope"].admitted_services == ("checkout", "payment")
    before = single_phv(current)
    current = await execute(current, replay_deps, [request(service="payment"), request("log", "payment")])
    assert single_phv(current) == before
    assert "accounting" not in current["investigation_scope"].admitted_services
    assert current["investigation_scope"].basis.id != snapshot.investigation_scope.basis.id
    assert {a.id for a in current["attempt_records"]}.isdisjoint(r.attempt.id for r in snapshot.recorded_acquisitions)


async def test_unique_candidate_explicit_A_and_reference_validation(history):
    snapshot = history[2]
    backend = RecordedReplayBackend(snapshot)
    selected = backend.select(request())
    assert selected == backend.selector_for_task(selected.recorded_task_spec_id)
    assert selected == backend.selector_for_attempt(selected.recorded_attempt_id)
    assert backend.materialize(request()).id == backend.materialize(explicit(request(), selected)).id
    for change in ({"replay_manifest_id": "RM-" + "0" * 64}, {"recorded_attempt_id": "A-" + "0" * 64},
                   {"recorded_task_spec_id": "T-" + "0" * 64}):
        with pytest.raises(RecordedAcquisitionUnavailable):
            backend.materialize(explicit(request(), selected.model_copy(update=change)))
    with pytest.raises(RecordedAcquisitionUnavailable):
        backend.selector_for_task("T-" + "0" * 64)


def with_second_attempt(snapshot, row):
    identity = AttemptIdentity(execution_id=snapshot.historical_execution_id, task_spec_id=row.attempt.task_spec_id, ordinal=2)
    second = row.model_copy(update={"attempt": row.attempt.model_copy(update={"ordinal": 2}),
        "tool_call": row.tool_call.model_copy(update={"id": identity.id, "attempt_id": identity.id}),
        "partition": row.partition.model_copy(update={"attempt_id": identity.id})})
    return reseal(snapshot, recorded_acquisitions=(*snapshot.recorded_acquisitions, second), investigation_scope=None), second


async def test_two_A_for_one_T_require_explicit_A_and_bind_distinct_replay_T(history):
    snapshot = history[2]
    row = next(r for r in snapshot.recorded_acquisitions if r.partition.signal == "trace")
    snapshot, second = with_second_attempt(snapshot, row)
    backend = RecordedReplayBackend(snapshot)
    with pytest.raises(AmbiguousRecordedAcquisition):
        backend.materialize(request())
    with pytest.raises(AmbiguousRecordedAcquisition):
        backend.selector_for_task(row.attempt.task_spec_id)
    a = backend.materialize(explicit(request(), backend.selector_for_attempt(row.attempt.id)))
    b = backend.materialize(explicit(request(), backend.selector_for_attempt(second.attempt.id)))
    assert a.id != b.id
    assert a.recorded_selection.recorded_task_spec_id == b.recorded_selection.recorded_task_spec_id
    assert a.id != materialize_task(request(), RecordedReplayBackend(history[2])).id  # RM binds all candidates.


async def test_ambiguous_logical_trace_operation_parameters_are_not_ignored(history):
    snapshot = history[2]
    row = next(r for r in snapshot.recorded_acquisitions if r.partition.signal == "trace")
    spec = next(t for t in snapshot.task_specs if t.id == row.attempt.task_spec_id)
    different = spec.model_copy(update={"query_template_id": "jaeger.other-operation.v1", "result_limit": 5})
    identity = AttemptIdentity(execution_id=snapshot.historical_execution_id, task_spec_id=different.id, ordinal=1)
    other = row.model_copy(update={"attempt": row.attempt.model_copy(update={"task_spec_id": different.id}),
        "tool_call": row.tool_call.model_copy(update={"id": identity.id, "attempt_id": identity.id, "task_spec_id": different.id}),
        "partition": row.partition.model_copy(update={"attempt_id": identity.id, "task_spec_id": different.id})})
    snapshot = reseal(snapshot, task_specs=(*snapshot.task_specs, different),
                     recorded_acquisitions=(*snapshot.recorded_acquisitions, other), investigation_scope=None)
    with pytest.raises(AmbiguousRecordedAcquisition):
        RecordedReplayBackend(snapshot).materialize(request())


@pytest.mark.parametrize("signal", ["trace", "log", "metric"])
async def test_exact_scope_required_and_no_narrow_slicing(history, signal):
    backend = RecordedReplayBackend(history[2])
    selection = backend.select(request(signal))
    task = request(signal)
    for change in ({"services": ("payment",)}, {"signal": "knowledge"}, {"window": TimeWindow(
        start=task.window.start + timedelta(seconds=1), end=task.window.end)}):
        with pytest.raises(RecordedAcquisitionUnavailable, match="exact"):
            backend.materialize(explicit(task.model_copy(update=change), selection))


async def test_multiple_metric_promql_candidates_require_explicit_A(history):
    snapshot = history[2]
    row = next(r for r in snapshot.recorded_acquisitions if r.partition.signal == "metric")
    specs, rows = [], []
    for query in ("sum(up)", "sum(rate(errors[1m]))"):
        spec = TaskSpec(signal="metric", services=("checkout",), window=demo_incident().window,
                        query_template_id="prometheus.query_range.v1", query_expression=query)
        identity = AttemptIdentity(execution_id=snapshot.historical_execution_id, task_spec_id=spec.id, ordinal=1)
        specs.append(spec)
        rows.append(row.model_copy(update={"attempt": row.attempt.model_copy(update={"task_spec_id": spec.id}),
            "tool_call": row.tool_call.model_copy(update={"id": identity.id, "attempt_id": identity.id, "task_spec_id": spec.id}),
            "partition": row.partition.model_copy(update={"attempt_id": identity.id, "task_spec_id": spec.id})}))
    changed = reseal(snapshot, task_specs=specs, recorded_acquisitions=rows, investigation_scope=None)
    backend = RecordedReplayBackend(changed)
    with pytest.raises(AmbiguousRecordedAcquisition):
        materialize_task(request("metric"), backend)
    chosen = explicit(request("metric"), backend.selector_for_attempt(rows[1].attempt.id))
    assert backend.materialize(chosen).recorded_selection.recorded_task_spec_id == specs[1].id


@pytest.mark.parametrize("status,error", [("error", "backend_error"), ("error", "invalid_result"),
                                        ("timeout", "tool_timeout"), ("timeout", "global_deadline")])
async def test_historical_failure_archived_but_unavailable_before_current_T_A(status, error):
    backend = FixtureBackend()
    task = request()
    spec = materialize_task(task, backend)
    identity = AttemptIdentity(execution_id="failure-history", task_spec_id=spec.id, ordinal=1)
    reserved = ReservedAttempt(identity, task, spec, 1, task.id)
    deps = Dependencies(backend)
    committed = deps.acquisitions.commit(finalize_acquisition(reserved, backend,
        AcquisitionResult(signal="trace", projection=ToolResult(status=status)), error_class=error, original_result=False))
    state = initial_state(demo_incident(), execution_id="failure-history")
    state.update(task_specs=[spec], **committed.state_update())
    snapshot = export_snapshot_v2(state, deps)
    replay = Dependencies(RecordedReplayBackend(snapshot))
    assert snapshot.recorded_acquisitions[0].partition is None
    with pytest.raises(RecordedAcquisitionUnavailable):
        replay.backend.selector_for_attempt(identity.id)
    current = await execute(initial_state(demo_incident()), replay, [task])
    assert current["intent_ledger"][0].status == "unavailable"
    assert current["intent_ledger"][0].blocking_reason == "replay_unavailable"
    assert not replay.attempts and not replay.acquisitions.ids()


async def test_successful_empty_historical_partition_remains_replayable():
    deps = Dependencies(FixtureBackend(records=(), traces=TraceResult()))
    state = await execute(initial_state(demo_incident(), execution_id="empty-history"), deps, [request()])
    snapshot = export_snapshot_v2(state, deps)
    replay = Dependencies(RecordedReplayBackend(snapshot))
    current = await execute(initial_state(demo_incident()), replay, [request()])
    c, = replay.acquisitions.values()
    assert c.attempt.tool_status == "empty" and c.partition.material == TraceResult()
    assert c.recorded_from and c.partition.content_digest == snapshot.recorded_acquisitions[0].partition_content_digest


async def test_current_observation_drift_is_not_archive_corruption(history, monkeypatch):
    import aegis_rca.trace_acquisition as native
    snapshot = SnapshotV2.model_validate_json(history[2].model_dump_json())
    monkeypatch.setattr(native, "observe_spans", lambda *a, **kw: ())
    assert SnapshotV2.model_validate_json(snapshot.model_dump_json()) == snapshot
    deps = Dependencies(RecordedReplayBackend(snapshot))
    await execute(initial_state(demo_incident()), deps, [request()])
    c, = deps.acquisitions.values()
    old = next(r for r in snapshot.recorded_acquisitions if r.partition.signal == "trace")
    assert old.attempt.admitted_observation_ids and c.attempt.admitted_observation_ids == ()
    assert c.recorded_from.recorded_attempt_id == old.attempt.id


@pytest.mark.parametrize("damage", ["missing", "foreign_selection", "content_digest"])
async def test_invalid_replay_lineage_becomes_invalid_result_without_partition(history, damage):
    class Invalid(RecordedReplayBackend):
        async def acquire(self, task):
            result = await super().acquire(task)
            reference = result.recorded_from
            if damage == "missing":
                reference = None
            elif damage == "foreign_selection":
                reference = reference.model_copy(update={"recorded_attempt_id": "A-" + "0" * 64})
            else:
                reference = reference.model_copy(update={"recorded_content_digest": "sha256-" + "0" * 64})
            return result.model_copy(update={"recorded_from": reference})
    deps = Dependencies(Invalid(history[2]))
    await execute(initial_state(demo_incident()), deps, [request()])
    c, = deps.acquisitions.values()
    assert c.attempt.error_class == "invalid_result" and c.partition is None
