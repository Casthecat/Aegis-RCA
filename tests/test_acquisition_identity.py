from portfolio_helpers import single_phv, subject
"""P1.5 materialization, attempt provenance and no-retry execution tests."""

import asyncio
from datetime import timedelta, timezone
from types import SimpleNamespace

import pytest
from langgraph.runtime import Runtime
from pydantic import TypeAdapter, ValidationError

from aegis_rca import graph
from aegis_rca.fixtures import FixtureBackend, demo_evidence, demo_incident, demo_traces
from aegis_rca.models.acquisition import AttemptIdentity, AttemptRecord, MaterializedTask, TaskSpec
from aegis_rca.models.check import CheckCoverageView
from aegis_rca.models.evidence import ToolCallRecord, ToolResult
from aegis_rca.models.incident import Budget, TimeWindow
from aegis_rca.models.telemetry import MetricResult, TraceResult
from aegis_rca.policy import make_task
from aegis_rca.runner import run_investigation
from aegis_rca.runtime import Dependencies, refresh_connectivity_verification, validate_execution_state
from aegis_rca.settings import BackendSettings
from aegis_rca.verification.persisted import current_connectivity_verification
from aegis_rca.state import IncidentState, initial_state, merge_records
from aegis_rca.task_materialization import materialize_task
from aegis_rca.tools.live import LiveBackend
from aegis_rca.tools.replay import ReplayBackend
from aegis_rca.tools.snapshot import TelemetrySnapshot


def metric_spec(**changes):
    return TaskSpec(**dict(signal="metric", services=("checkout",), window=demo_incident().window,
        query_template_id="prometheus.query_range.v1", query_expression="up", metric_step_seconds=15,
        result_limit=100, **changes))


@pytest.mark.parametrize("change", [
    {"services": ("payment",)}, {"query_expression": "rate(requests[1m])"},
    {"query_template_id": "prometheus.other.v1"}, {"metric_step_seconds": 30}, {"result_limit": 500},
    {"window": TimeWindow(start=demo_incident().window.start+timedelta(seconds=1), end=demo_incident().window.end)},
])
def test_materialized_query_changes_task_identity(change):
    first = metric_spec()
    second = TaskSpec.model_validate({**first.model_dump(), **change})
    assert first.id != second.id
    assert first.id.startswith("T-") and len(first.id) == 66


def test_signal_changes_identity_without_inventing_unused_metric_parameters():
    backend = FixtureBackend()
    assert materialize_task(make_task(demo_incident(), "trace"), backend).id != materialize_task(
        make_task(demo_incident(), "log"), backend).id


def test_task_identity_canonicalizes_services_time_and_ignores_display():
    spec = metric_spec()
    changed = TaskSpec.model_validate({**spec.model_dump(), "alias": "planner wording", "services": ("checkout", "checkout"),
        "window": TimeWindow(start=spec.window.start.astimezone(timezone(timedelta(hours=8))),
                             end=spec.window.end.astimezone(timezone(timedelta(hours=8))))})
    assert spec.id == changed.id
    assert TaskSpec.model_validate_json(spec.model_dump_json()).id == spec.id
    assert metric_spec() == spec
    task = make_task(demo_incident(), "trace")
    changed_intent = task.model_copy(update={"id": "another-alias", "goal": "arbitrary LLM wording"})
    assert materialize_task(task, FixtureBackend()).id == materialize_task(changed_intent, FixtureBackend()).id
    assert "settings" not in task.model_dump()
    for forbidden in ("attempt_ordinal", "execution_id", "check_id", "hypothesis_id", "backend_url", "snapshot_path", "goal"):
        with pytest.raises(ValidationError):
            TaskSpec.model_validate({**spec.model_dump(), forbidden: "not-acquisition-semantics"})


@pytest.fixture
def live():
    backend = LiveBackend(BackendSettings(prometheus_url="http://metrics.test", jaeger_url="http://traces.test",
        opensearch_url="http://logs.test", metric_query="up"), demo_incident())
    yield backend
    backend.close()


@pytest.mark.parametrize("signal,setting,new_value", [
    ("metric", "metric_query", "sum(up)"), ("metric", "metric_step_seconds", 30),
    ("metric", "max_results", 500), ("trace", "max_results", 500), ("log", "max_results", 500),
])
def test_runtime_defaults_are_concrete_before_id(signal, setting, new_value, live):
    task = make_task(demo_incident(), signal)
    original = materialize_task(task, live)
    assert original.result_limit == 100
    live.settings = live.settings.model_copy(update={setting: new_value})
    assert original.id != materialize_task(task, live).id


def test_unused_defaults_and_backend_urls_do_not_enter_identity(live):
    task = make_task(demo_incident(), "trace")
    spec = materialize_task(task, live)
    live.settings = live.settings.model_copy(update={"metric_query": "changed", "metric_step_seconds": 30,
                                                   "jaeger_url": "http://other.test"})
    assert materialize_task(task, live) == spec
    live.settings = live.settings.model_copy(update={"metric_query": None})
    metric = materialize_task(make_task(demo_incident(), "metric"), live)
    assert metric.query_expression is metric.metric_step_seconds is metric.result_limit is None
    assert "http" not in metric.model_dump_json()


def test_actual_log_filter_index_and_server_timeout_are_materialized(live):
    task = make_task(demo_incident(), "log")
    original = materialize_task(task, live)
    live.opensearch.index = "another-index-*"
    assert materialize_task(task, live).id != original.id
    live.opensearch.index = original.log_query.index
    from dataclasses import replace
    live.opensearch.fields = replace(live.opensearch.fields, service_exact="other.service.keyword")
    assert materialize_task(task, live).id != original.id
    assert original.log_query.server_timeout_ms == int(live.opensearch.http.timeout_seconds * 1000)


def admit(backend, tasks, *, execution_id="execution-test", budget=None):
    state = initial_state(demo_incident(), budget, execution_id=execution_id)
    state["pending_tasks"] = list(tasks)
    dependencies = Dependencies(backend)
    runtime = Runtime(context=dependencies)
    state.update(graph.admit_batch(state, runtime))
    return state, dependencies, runtime


async def test_materialized_values_are_the_values_actually_executed(live, monkeypatch):
    task = make_task(demo_incident(), "metric")
    state, dependencies, runtime = admit(live, (task,))
    reserved = next(iter(dependencies.attempts.values()))
    assert reserved.spec.query_expression == "up" and reserved.spec.result_limit == 100
    live.settings = live.settings.model_copy(update={"metric_query": "different", "max_results": 500,
                                                   "metric_step_seconds": 60})
    calls = []

    def query(expression, start, end, **kwargs):
        calls.append((expression, kwargs))
        return MetricResult()

    monkeypatch.setattr(live.prometheus, "query_range", query)
    update = await graph.worker({"attempt_id": reserved.id}, runtime)
    assert calls == [("up", {"step_seconds": 15, "limit": 100})]
    assert update["attempt_records"][0].task_spec_id == reserved.spec.id
    assert update["attempt_records"][0].tool_status == "empty"


async def test_log_query_config_drift_fails_without_a_different_query(live, monkeypatch):
    _, dependencies, runtime = admit(live, (make_task(demo_incident(), "log"),))
    reserved = next(iter(dependencies.attempts.values()))
    live.opensearch.index = "changed-index"
    monkeypatch.setattr(live.opensearch, "search", lambda *a, **k: pytest.fail("must not execute a drifted query"))
    update = await graph.worker({"attempt_id": reserved.id}, runtime)
    assert update["attempt_records"][0].tool_status == "error"
    assert update["attempt_records"][0].task_spec_id == reserved.spec.id


def attempt(**updates):
    values = dict(execution_id="execution-test", task_spec_id=metric_spec().id, ordinal=1,
                  tool_status="empty", backend_kind="fixture", timeout_seconds=5,
                  tool_result_digest="sha256-" + "1"*64)
    return AttemptRecord(**{**values, **updates})


def test_attempt_identity_binds_execution_task_and_ordinal_only():
    first = attempt()
    second = attempt(ordinal=2)
    assert first.tool_result_digest == second.tool_result_digest
    assert first.id != second.id
    assert first.id != attempt(execution_id="another-execution").id
    assert first.id != attempt(task_spec_id=materialize_task(make_task(demo_incident(), "trace"), FixtureBackend()).id).id
    assert first.id == attempt(tool_status="ok", warnings=("arbitrary display",), truncated=True,
                               coverage_complete=True, tool_result_digest="sha256-" + "2"*64).id
    assert len(merge_records([first], [second])) == 2
    assert AttemptRecord.model_validate_json(first.model_dump_json()) == first
    with pytest.raises(ValidationError):
        attempt(ordinal=0)


@pytest.mark.parametrize("changes", [
    {"admitted_observation_ids": ("E-T1",)}, {"admitted_evidence_ids": ("O-trace-native",)},
    {"admitted_observation_ids": ("O-trace-native",), "observation_binding": "unknown"},
    {"observation_binding": "bound"},
])
def test_attempt_namespaces_and_unknown_binding_are_explicit(changes):
    with pytest.raises(ValidationError):
        attempt(**changes)


async def test_dedupe_is_by_task_spec_not_alias_goal_or_check_identity():
    task = make_task(demo_incident(), "trace")
    alternative = task.model_copy(update={"id": "another-check-consumer", "goal": "different goal"})
    state, dependencies, runtime = admit(FixtureBackend(), (task, alternative))
    assert len(state["pending_tasks"]) == len(dependencies.attempts) == 1
    reserved = next(iter(dependencies.attempts.values()))
    assert reserved.identity.ordinal == 1
    update = await graph.worker({"attempt_id": reserved.id}, runtime)
    assert update["tool_calls"][0].id == "query-trace"
    assert update["tool_calls"][0].attempt_id == reserved.id
    assert update["tool_calls"][0].task_spec_id == reserved.spec.id
    with pytest.raises(ValueError, match="execute twice"):
        await graph.worker({"attempt_id": reserved.id}, runtime)
    state["pending_tasks"] = [alternative]
    assert graph.admit_batch(state, runtime)["pending_tasks"] == []


async def test_different_specs_with_same_display_alias_are_not_deduplicated():
    task = make_task(demo_incident(), "trace")
    second = task.model_copy(update={"window": TimeWindow(start=task.window.start+timedelta(seconds=1), end=task.window.end)})
    _, dependencies, runtime = admit(FixtureBackend(), (task, second))
    assert len(dependencies.attempts) == 2
    updates = [await graph.worker({"attempt_id": key}, runtime) for key in dependencies.attempts]
    calls = [update["tool_calls"][0] for update in updates]
    assert len({call.id for call in calls}) == len({call.task_spec_id for call in calls}) == 2
    assert all(call.task.id == "query-trace" for call in calls)


async def test_canonical_scope_is_executed_but_original_intent_stays_in_projection(live, monkeypatch):
    task = make_task(demo_incident(), "trace").model_copy(update={"services": ("checkout", "checkout")})
    _, dependencies, runtime = admit(live, (task,))
    calls = []
    monkeypatch.setattr(live.jaeger, "search", lambda service, *a, **k: calls.append(service) or TraceResult())
    reserved = next(iter(dependencies.attempts.values()))
    update = await graph.worker({"attempt_id": reserved.id}, runtime)
    assert calls == ["checkout"]
    assert update["tool_calls"][0].task.services == ("checkout", "checkout")
    assert reserved.spec.services == ("checkout",)


def test_central_reservations_cannot_overbook_before_workers_start():
    task = make_task(demo_incident(), "trace")
    state, dependencies, runtime = admit(FixtureBackend(), (task,), budget=Budget(max_tool_calls=1))
    state["pending_tasks"] = [make_task(demo_incident(), "log")]
    assert graph.admit_batch(state, runtime)["pending_tasks"] == []
    assert len(dependencies.attempts) == 1 and not dependencies.started_attempts


async def test_one_execution_namespace_and_compatibility_projection_are_authoritative():
    state, _ = await run_investigation(initial_state(demo_incident(), execution_id="single-run"), Dependencies(FixtureBackend()))
    assert state["final_report"].status == "SUPPORTED"
    assert state["final_report"].rounds == 2 and state["final_report"].tool_calls == 3
    assert set(state["final_report"].claims[0].evidence_ids) == {"E-T1", "E-L1"}
    assert len(state["attempt_records"]) == len(state["task_specs"]) == len(state["tool_calls"]) == 3
    assert {item.execution_id for item in state["attempt_records"]} == {"single-run"}
    attempts = {item.id: item for item in state["attempt_records"]}
    for call in state["tool_calls"]:
        assert call.id == f"query-{call.task.signal}"
        assert call.status == attempts[call.attempt_id].tool_status
        assert call.task_spec_id == attempts[call.attempt_id].task_spec_id
        assert set(call.evidence_ids) == set(attempts[call.attempt_id].admitted_evidence_ids)
        assert "task_spec" not in call.task.model_dump()
    validate_execution_state(state)
    tampered = {**state, "tool_calls": [state["tool_calls"][0].model_copy(update={"status": "error"}), *state["tool_calls"][1:]]}
    with pytest.raises(ValueError, match="authoritative"):
        validate_execution_state(tampered)
    with pytest.raises(ValueError, match="this execution"):
        validate_execution_state({**state, "execution_id": "different"})
    altered = state["tool_calls"][0].model_copy(update={
        "task": state["tool_calls"][0].task.model_copy(update={"services": ("shipping",)})})
    with pytest.raises(ValueError, match="materialized TaskSpec"):
        validate_execution_state({**state, "tool_calls": [altered, *state["tool_calls"][1:]]})


def test_tool_call_structural_references_must_be_paired():
    task = make_task(demo_incident(), "trace")
    with pytest.raises(ValidationError, match="supplied together"):
        ToolCallRecord(id=task.id, task=task, status="empty", evidence_ids=(), attempt_id=attempt().id)


async def test_worker_timeout_and_invalid_result_classifications_are_structured():
    class Malformed:
        async def query(self, task):
            return {"status": "not-a-tool-status"}

    class Hanging:
        async def query(self, task):
            await asyncio.Event().wait()

    class Failing:
        async def query(self, task):
            raise RuntimeError("Backend failed")

    for backend, expected, status in ((Malformed(), "invalid_result", "error"),
                                      (Hanging(), "tool_timeout", "timeout"),
                                      (Failing(), "backend_error", "error")):
        _, dependencies, runtime = admit(backend, (make_task(demo_incident(), "trace"),),
                                         budget=Budget(tool_timeout_seconds=0.01))
        update = await graph.worker({"attempt_id": next(iter(dependencies.attempts))}, runtime)
        record = update["attempt_records"][0]
        assert record.error_class == expected
        assert record.tool_status == status
        assert AttemptRecord.model_validate_json(record.model_dump_json()) == record
        assert record.tool_result_digest is None and record.observation_binding == "unknown"


async def test_empty_projection_binds_native_trace_without_claiming_completeness():
    span = demo_traces().spans[0].model_copy(update={"attributes": {"rpc.grpc.status_code": 2,
                                                                   "rpc.service": "oteldemo.PaymentService"}})
    snapshot = TelemetrySnapshot(incident=demo_incident(), provenance="synthetic_wire_fixture", traces=TraceResult(spans=(span,)))
    backend = ReplayBackend(snapshot)
    dependencies = Dependencies(backend)
    state, _ = await run_investigation(initial_state(demo_incident()), dependencies)
    call = next(call for call in state["tool_calls"] if call.task.signal == "trace")
    bound = next(item for item in state["attempt_records"] if item.id == call.attempt_id)
    assert call.status == bound.tool_status == "empty"
    assert bound.admitted_evidence_ids == () and len(bound.admitted_observation_ids) == 1
    assert bound.observation_binding == bound.evidence_binding == "bound"
    assert bound.source_material_digest is not None and bound.tool_result_digest is not None
    assert not bound.truncated and bound.coverage_complete is None and bound.pre_capture_omissions == "unknown"
    assert current_connectivity_verification(state) is None  # gRPC UNKNOWN is not UNAVAILABLE.
    assert state["investigation_scope"].admitted_services == ("checkout", "payment")
    assert state["final_report"].status == "INCONCLUSIVE"
    assert all(key.startswith("A-") for key in dependencies.attempts)
    assert set(dependencies.completions) == set(dependencies.attempts) == dependencies.started_attempts
    assert all(item.observation_binding == "unknown" for item in state["attempt_records"] if item.id != bound.id)


async def test_projection_digest_does_not_claim_complete_raw_acquisition():
    snapshots = [TelemetrySnapshot(incident=demo_incident(), provenance="synthetic_wire_fixture",
        traces=TraceResult(spans=(demo_traces().spans[0].model_copy(update={"attributes": {
            "rpc.grpc.status_code": code, "rpc.service": "oteldemo.PaymentService"}}),))) for code in (2, 13)]
    records = []
    for snapshot in snapshots:
        state, _ = await run_investigation(initial_state(demo_incident()), Dependencies(ReplayBackend(snapshot)))
        call = next(call for call in state["tool_calls"] if call.task.signal == "trace")
        records.append(next(item for item in state["attempt_records"] if item.id == call.attempt_id))
    a, b = records
    assert a.tool_result_digest == b.tool_result_digest and a.task_spec_id == b.task_spec_id
    assert a.source_material_digest != b.source_material_digest
    assert a.admitted_observation_ids != b.admitted_observation_ids and a.id != b.id


@pytest.mark.parametrize("status", ["ok", "empty", "error", "timeout"])
async def test_status_verbatim_never_retries_and_never_upgrades_coverage(status):
    class Backend:
        def __init__(self):
            self.calls = []

        async def query(self, task):
            self.calls.append(task)
            assert isinstance(task, MaterializedTask)
            assert not hasattr(task, "attempt_id")
            return ToolResult(status=status, truncated=False, coverage_complete=None)

    backend = Backend()
    state, _ = await run_investigation(initial_state(demo_incident()), Dependencies(backend))
    assert len(backend.calls) == 2 and len({task.signal for task in backend.calls}) == 2
    for record in state["attempt_records"]:
        assert record.tool_status == status and record.ordinal == 1
        assert record.error_class == {"ok": "none", "empty": "none", "error": "backend_error",
                                      "timeout": "tool_timeout"}[status]
        assert AttemptRecord.model_validate_json(record.model_dump_json()) == record
        assert record.coverage_complete is None and record.observation_binding == "unknown"
        assert not record.admitted_observation_ids


async def test_global_deadline_finalizes_started_attempts_and_refs_once():
    class Backend:
        async def query(self, task):
            if task.signal == "metric":
                return ToolResult(status="ok", evidence=(demo_evidence()[0],))
            await asyncio.Event().wait()

    dependencies = Dependencies(Backend())
    state, _ = await run_investigation(initial_state(demo_incident(), Budget(tool_timeout_seconds=10)),
                                     dependencies, deadline_seconds=0.05)
    validate_execution_state(state)
    attempts = {item.id: item for item in state["attempt_records"]}
    assert len(attempts) == len(state["tool_calls"]) == 2
    call = next(call for call in state["tool_calls"] if call.task.signal == "trace")
    assert attempts[call.attempt_id].error_class == "global_deadline"
    assert attempts[call.attempt_id].tool_status == "timeout"
    assert AttemptRecord.model_validate_json(attempts[call.attempt_id].model_dump_json()) == attempts[call.attempt_id]
    assert attempts[call.attempt_id].tool_result_digest is None
    assert state["final_report"].termination_reason == "deadline_exhausted"


async def test_saved_state_preserves_execution_namespace_without_enabling_resume_retries():
    state, _ = await run_investigation(initial_state(demo_incident(), execution_id="saved-execution"), Dependencies(FixtureBackend()))
    adapter = TypeAdapter(IncidentState)
    restored = adapter.validate_json(adapter.dump_json(state))
    validate_execution_state(restored)
    assert restored["execution_id"] == "saved-execution"
    assert [item.id for item in restored["attempt_records"]] == [item.id for item in state["attempt_records"]]
    dependencies = Dependencies(FixtureBackend())
    again, _ = await run_investigation(restored, dependencies)
    assert again["execution_id"] == "saved-execution" and not dependencies.attempts
    assert again["final_report"] == state["final_report"]
    assert initial_state(demo_incident())["execution_id"] != initial_state(demo_incident())["execution_id"]


async def test_source_change_cannot_silently_rebind_finished_attempt():
    backend = FixtureBackend()
    state, _ = await run_investigation(initial_state(demo_incident()), Dependencies(backend))
    first = current_connectivity_verification(state)
    backend.traces = TraceResult()
    second = current_connectivity_verification(state)
    assert first == second and second.plan_result.status == "SUPPORTED"
    refreshed = refresh_connectivity_verification(state, backend, subject=subject(state))
    assert refreshed.evaluation.trace_check.verdict == "SUPPORT"
    assert refreshed.state_basis.id == single_phv(state).state_basis.id
    assert refreshed.evaluation_basis.id == single_phv(state).evaluation_basis.id
    assert any(item.admitted_observation_ids for item in state["attempt_records"])


async def test_cli_reuses_one_execution_identity_for_output_and_attempts(tmp_path, monkeypatch):
    import json
    import aegis_rca.cli as cli

    count = []
    monkeypatch.setattr(cli, "uuid4", lambda: count.append(1) or "cli-execution")
    args = SimpleNamespace(output=tmp_path / "custom-output", mode="fixture", incident=None, scenario="connectivity",
                           llm=False, max_tool_calls=6, max_rounds=3, tool_timeout=5, deadline=120)
    await cli.run(args)
    from aegis_rca.verification.persisted import PersistedConnectivityVerification
    persisted = PersistedConnectivityVerification.model_validate_json(
        (args.output / "connectivity_verification.json").read_text())
    assert persisted.evaluation.plan_result.status == "SUPPORTED"
    from aegis_rca.models.scope import InvestigationScope
    scope = InvestigationScope.model_validate_json((args.output / "investigation_scope.json").read_text())
    assert scope.admitted_services == ("checkout", "payment")
    assert count == [1]
    assert json.loads((args.output / "execution.json").read_text())["execution_id"] == "cli-execution"
    attempts = json.loads((args.output / "attempts.json").read_text())
    assert {item["execution_id"] for item in attempts} == {"cli-execution"}
