from portfolio_helpers import single_phv, subject
"""P1.6b explicit acquisition, immutable ownership and terminal-commit regressions."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import threading

import pytest
from langgraph.runtime import Runtime
from pydantic import ValidationError

from aegis_rca import graph
from aegis_rca.acquisition_store import AcquisitionStore, AlreadyCommitted
from aegis_rca.fixtures import FixtureBackend, demo_incident, demo_traces
from aegis_rca.models.acquisition_result import AcquisitionResult, CommittedAcquisition, SourceMetadata, TelemetryPartition
from aegis_rca.models.evidence import ToolResult
from aegis_rca.models.incident import Budget, TimeWindow
from aegis_rca.models.telemetry import LogRecord, LogResult, MetricResult, TraceResult
from aegis_rca.policy import make_task
from aegis_rca.runner import run_investigation
from aegis_rca.runtime import Dependencies, finalize_acquisition, refresh_acquisition_state
from aegis_rca.settings import BackendSettings
from aegis_rca.state import initial_state, merge_records
from aegis_rca.task_materialization import UnsupportedAcquisition, UnauthorizedAcquisition
from aegis_rca.tools.live import LiveBackend
from aegis_rca.tools.projection import project_material
from aegis_rca.tools.replay import ReplayBackend
from aegis_rca.tools.snapshot import TelemetrySnapshot
from aegis_rca.trace_acquisition import resolve_trace_attempt


def request(signal="trace", service="checkout", **updates):
    return make_task(demo_incident(), signal).model_copy(update={"services": (service,), **updates})


def overlap():
    client = demo_traces().spans[0]
    server = client.model_copy(update={"span_id": "3" * 16, "service": "payment", "kind": "SERVER",
        "attributes": {"peer.service": "checkout", "rpc.grpc.status_code": 14}})
    hop2 = client.model_copy(update={"span_id": "4" * 16, "service": "payment",
        "attributes": {"peer.service": "accounting", "rpc.grpc.status_code": 14}})
    return TraceResult(spans=(client, server, hop2), coverage_complete=True)


def logs(service):
    return LogResult(logs=(LogRecord(id=f"raw-{service}", timestamp=demo_incident().window.start,
        service=service, severity="ERROR", body="dial accounting:50051: connection refused",
        attributes={"peer.service": "accounting"}, source=f"synthetic:{service}"),))


def reserve(state, deps, tasks):
    state = {**state, "pending_tasks": tasks}
    update = graph.admit_batch(state, Runtime(context=deps))
    return {**state, **update, "task_specs": merge_records(state["task_specs"], update["task_specs"])}


async def execute(state, deps, tasks):
    state = reserve(state, deps, tasks)
    pending = [key for key in deps.attempts if key not in deps.started_attempts]
    updates = await asyncio.gather(*(graph.worker({"attempt_id": key}, Runtime(context=deps)) for key in pending))
    for update in updates:
        for field, values in update.items():
            state[field] = merge_records(state[field], values)
    state.update(refresh_acquisition_state(state, deps))
    return state


@pytest.fixture
def live(monkeypatch):
    backend = LiveBackend(BackendSettings(prometheus_url="http://metrics.test", jaeger_url="http://traces.test",
        opensearch_url="http://logs.test", metric_query='sum(rate(requests{service_name="checkout"}[1m]))'), demo_incident())
    monkeypatch.setattr(backend.jaeger, "search", lambda *a, **kw: overlap())
    monkeypatch.setattr(backend.opensearch, "search", lambda *a, service, **kw: logs(service))
    monkeypatch.setattr(backend.prometheus, "query_range", lambda *a, **kw: MetricResult())
    yield backend
    backend.close()


@pytest.mark.parametrize("kind", ["fixture", "live"])
async def test_explicit_payment_has_own_partitions_without_changing_seed_verification(kind, live):
    backend = live if kind == "live" else FixtureBackend(traces=overlap(), materials={
        ("trace", ("payment",)): overlap(), ("log", ("payment",)): logs("payment")})
    deps = Dependencies(backend)
    state = await execute(initial_state(demo_incident(), Budget(max_tool_calls=10)), deps,
                          [request(), request("log"), request("metric")])
    assert state["investigation_scope"].admitted_services == ("checkout", "payment")
    original = {c.attempt.id: c.partition.model_dump_json() for c in deps.acquisitions.values()}
    original_digests = {c.attempt.id: c.partition.content_digest for c in deps.acquisitions.values()}
    verification, scope = single_phv(state), state["investigation_scope"]
    seed = next(c for c in deps.acquisitions.values() if c.partition.signal == "trace")
    state = await execute(state, deps, [request(service="payment"), request("log", "payment")])
    payment = [c for c in deps.acquisitions.values() if c.tool_call.task.services == ("payment",)]
    assert len(payment) == 2 and len(deps.acquisitions.ids()) == 5
    assert {c.partition.signal for c in payment} == {"trace", "log"}
    assert all(c.attempt.ordinal == 1 and c.attempt.task_spec_id != seed.attempt.task_spec_id for c in payment)
    trace = next(c for c in payment if c.partition.signal == "trace")
    assert trace.partition.material == seed.partition.material  # Full overlapping document, no dedup.
    assert trace.attempt.id != seed.attempt.id
    for key, raw in original.items():
        assert deps.partition_for(key).model_dump_json() == raw
        assert deps.partition_for(key).content_digest == original_digests[key]
    backend.traces = TraceResult()  # Neither query nor mutable compatibility buffer is a resolver.
    for completion in (seed, trace):
        spec = next(t for t in state["task_specs"] if t.id == completion.attempt.task_spec_id)
        resolved = resolve_trace_attempt(completion.attempt, spec, deps)
        assert resolved.outcome == "verified" and resolved.observations
        assert {o.service for o in resolved.observations} == set(spec.services)
    assert single_phv(state) == verification
    assert state["investigation_scope"] == scope
    assert "accounting" not in scope.admitted_services
    assert not {"partitions", "traces", "logs", "metrics", "acquisitions"} & state.keys()
    if kind == "live":
        with pytest.raises(ValueError, match="Snapshot V1"):
            live.snapshot()


async def test_live_concurrent_projection_uses_each_local_fetch(live, monkeypatch):
    barrier = threading.Barrier(2)
    material = {"checkout": demo_traces(), "payment": overlap()}
    def fetch(service, *args, **kwargs):
        barrier.wait(timeout=5)
        return material[service]
    monkeypatch.setattr(live.jaeger, "search", fetch)
    monkeypatch.setattr(live, "snapshot", lambda: pytest.fail("projection read global snapshot"))
    results = await asyncio.gather(live.acquire(request()), live.acquire(request(service="payment")))
    for service, result in zip(("checkout", "payment"), results):
        assert result.material == material[service]
        assert result.projection == project_material(request(service=service), material[service], result.source)
        assert all(e.service == service for e in result.projection.evidence)


async def test_explicit_payment_batch_through_compiled_investigation_graph():
    deps = Dependencies(FixtureBackend(materials={
        ("trace", ("payment",)): overlap(), ("log", ("payment",)): logs("payment")}))
    state, _ = await run_investigation(initial_state(demo_incident()), deps)
    before = single_phv(state)
    state = await graph.investigate_graph().ainvoke({**state,
        "pending_tasks": [request(service="payment"), request("log", "payment")]}, context=deps)
    assert state["rounds"] == 3 and len(state["attempt_records"]) == 5
    assert single_phv(state) == before
    assert state["investigation_scope"].admitted_services == ("checkout", "payment")
    assert len({c.id for c in state["tool_calls"]}) == 5
    for call in state["tool_calls"]:
        if call.task.services == ("payment",):
            assert call.id == call.attempt_id  # Reused legacy query alias cannot collide.
            assert deps.partition_for(call.attempt_id).task_spec_id == call.task_spec_id


async def authorized_state():
    state, _ = await run_investigation(initial_state(demo_incident()), Dependencies(FixtureBackend()))
    return state


@pytest.mark.parametrize("kind,signal,services", [
    ("live", "metric", ("payment",)), ("live", "trace", ("checkout", "payment")),
    ("live", "log", ("checkout", "payment")), ("replay", "trace", ("payment",)),
    ("replay", "log", ("payment",)), ("fixture", "trace", ("payment",)),
])
async def test_capability_rejection_precedes_materialization_and_budget(kind, signal, services, live, monkeypatch):
    state = await authorized_state()
    backend = {"live": live, "replay": ReplayBackend(TelemetrySnapshot(incident=demo_incident(),
        provenance="synthetic_wire_fixture", traces=overlap())), "fixture": FixtureBackend()}[kind]
    deps = Dependencies(backend)
    calls, attempts, specs = list(state["tool_calls"]), list(state["attempt_records"]), list(state["task_specs"])
    monkeypatch.setattr(graph, "materialize_task", lambda *a: pytest.fail("capability must precede T creation"))
    updated = reserve(state, deps, [request(signal, services=services)])
    assert any(r.status == "unavailable" and r.blocking_reason == "unsupported_capability" for r in updated["intent_ledger"])
    assert not deps.attempts and not deps.started_attempts and not deps.acquisitions.ids()
    assert state["tool_calls"] == calls and state["attempt_records"] == attempts and state["task_specs"] == specs


def test_absent_scope_allows_only_original_seed(live):
    state, deps = initial_state(demo_incident()), Dependencies(live)
    with pytest.raises(UnauthorizedAcquisition):
        reserve(state, deps, [request(service="payment")])
    assert not deps.attempts
    state = reserve(state, deps, [request()])
    assert len(state["task_specs"]) == len(deps.attempts) == 1


async def test_stale_scope_fails_before_materialization(live, monkeypatch):
    state = await authorized_state()
    state = {**state, "attempt_records": [a for a in state["attempt_records"] if a.admitted_observation_ids == ()]}
    monkeypatch.setattr(graph, "materialize_task", lambda *a: pytest.fail("stale scope must fail first"))
    with pytest.raises(ValueError, match="stale"):
        reserve(state, Dependencies(live), [request(service="payment")])


@pytest.mark.parametrize("signal,material", [("trace", TraceResult()), ("log", LogResult()), ("metric", MetricResult())])
async def test_successful_empty_material_is_explicit_partition(signal, material):
    backend = FixtureBackend(records=(), materials={(signal, ("checkout",)): material})
    deps = Dependencies(backend)
    state = await execute(initial_state(demo_incident()), deps, [request(signal)])
    completion, = deps.acquisitions.values()
    assert completion.attempt.tool_status == "empty" and completion.partition.material == material
    assert completion.attempt.error_class == "none"
    assert len(state["attempt_records"]) == 1


async def test_empty_projection_can_retain_real_trace_material():
    material = TraceResult(spans=(demo_traces().spans[0].model_copy(update={"status": "OK",
        "attributes": {"rpc.grpc.status_code": 0, "peer.service": "payment"}}),))
    deps = Dependencies(FixtureBackend(materials={("trace", ("checkout",)): material}))
    state = await execute(initial_state(demo_incident()), deps, [request()])
    completion, = deps.acquisitions.values()
    assert completion.attempt.tool_status == "empty"
    assert completion.partition.material.spans and completion.attempt.admitted_observation_ids
    assert not state["evidence"]


@pytest.mark.parametrize("mode,error", [("error", "backend_error"), ("invalid", "invalid_result"),
                                      ("incoherent", "invalid_result"), ("timeout", "tool_timeout")])
async def test_failed_acquisition_has_no_partition(mode, error):
    class Backend(FixtureBackend):
        async def acquire(self, task):
            if mode == "error":
                raise RuntimeError("synthetic")
            if mode == "invalid":
                return {"signal": "trace", "projection": {"status": "empty"}}
            if mode == "timeout":
                return AcquisitionResult(signal="trace", projection=ToolResult(status="timeout"))
            return AcquisitionResult(signal="trace", material=TraceResult(), source=SourceMetadata(),
                projection=ToolResult(status="ok", evidence=FixtureBackend().records[1:2]))
    deps = Dependencies(Backend())
    await execute(initial_state(demo_incident()), deps, [request()])
    completion, = deps.acquisitions.values()
    assert completion.attempt.error_class == error and completion.partition is None


async def test_worker_rejects_result_after_suppressed_timeout_cancellation():
    class Backend(FixtureBackend):
        async def acquire(self, task):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return await super().acquire(task)
    deps = Dependencies(Backend())
    await execute(initial_state(demo_incident(), Budget(tool_timeout_seconds=0.02)), deps, [request()])
    completion, = deps.acquisitions.values()
    assert completion.attempt.error_class == "tool_timeout" and completion.partition is None


async def test_late_live_thread_cannot_publish_after_worker_timeout(live, monkeypatch):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    def fetch(*a, **kw):
        entered.set()
        assert release.wait(timeout=5)
        finished.set()
        return demo_traces()
    monkeypatch.setattr(live.jaeger, "search", fetch)
    deps = Dependencies(live)
    try:
        await execute(initial_state(demo_incident(), Budget(tool_timeout_seconds=0.1)), deps, [request()])
        assert entered.is_set()
        completion, = deps.acquisitions.values()
        assert completion.attempt.error_class == "tool_timeout" and completion.partition is None
        release.set()
        assert await asyncio.to_thread(finished.wait, 5)
        await asyncio.sleep(0)
        assert deps.acquisitions.values() == (completion,)
        assert live.traces == TraceResult()
        reserved = deps.attempts[completion.attempt.id]
        with pytest.raises(AlreadyCommitted):
            deps.acquisitions.commit(finalize_acquisition(reserved, live, AcquisitionResult(signal="trace",
                material=demo_traces(), source=SourceMetadata(),
                projection=project_material(request(), demo_traces(), SourceMetadata()))))
        assert deps.partition_for(completion.attempt.id) is None
    finally:
        release.set()


async def test_global_deadline_preserves_committed_partition_and_finalizes_only_started():
    class Backend(FixtureBackend):
        async def query(self, task):
            if task.signal == "log":
                await asyncio.Event().wait()
            return await super().query(task)
    deps = Dependencies(Backend())
    state, _ = await run_investigation(initial_state(demo_incident(), Budget(tool_timeout_seconds=5)),
                                        deps, deadline_seconds=0.15)
    assert len(deps.acquisitions.ids()) == len(state["attempt_records"]) == 3
    assert single_phv(state).evaluation.trace_check.verdict == "SUPPORT"
    assert state["investigation_scope"].admitted_services == ("checkout", "payment")
    for c in deps.acquisitions.values():
        if c.tool_call.task.signal == "log":
            assert c.attempt.error_class == "global_deadline" and c.partition is None
        else:
            assert c.attempt.error_class == "none" and c.partition is not None
    assert state["final_report"].termination_reason == "deadline_exhausted"


async def test_single_write_terminal_commit_and_reader_mutation_isolation():
    deps = Dependencies(FixtureBackend())
    state = await execute(initial_state(demo_incident()), deps, [request()])
    successful, = deps.acquisitions.values()
    reserved = deps.attempts[successful.attempt.id]
    timeout = finalize_acquisition(reserved, deps.backend,
        AcquisitionResult(signal="trace", projection=ToolResult(status="timeout")),
        error_class="global_deadline", original_result=False)
    class Writes(dict):
        count = 0
        def __setitem__(self, key, value):
            self.count += 1
            super().__setitem__(key, value)
    store = AcquisitionStore()
    store._terminal = Writes()
    barrier = threading.Barrier(2)
    def commit(value):
        barrier.wait(timeout=5)
        try:
            return store.commit(value)
        except AlreadyCommitted:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(commit, c) for c in (successful, timeout)]
        results = [f.result(timeout=5) for f in futures]
    assert sum(r is not None for r in results) == store._terminal.count == 1
    only, = store.values()
    assert (only.partition is not None) == (only.attempt.tool_status == "ok")
    assert not hasattr(store, "put")
    raw = deps.partition_for(successful.attempt.id).model_dump_json()
    successful.partition.material.spans[0].attributes.clear()
    deps.partition_for(successful.attempt.id).source.rpc_service_map.clear()
    deps.completions.clear()  # Derived compatibility view is not authoritative.
    assert deps.partition_for(successful.attempt.id).model_dump_json() == raw
    assert len(deps.acquisitions.ids()) == 1
    assert "partition" not in successful.state_update()
    spec = state["task_specs"][0]
    assert resolve_trace_attempt(deps.acquisitions.get(reserved.id).attempt, spec, deps).outcome == "verified"


async def test_missing_or_foreign_partition_never_falls_back_to_backend(monkeypatch):
    deps = Dependencies(FixtureBackend())
    state = await execute(initial_state(demo_incident()), deps, [request()])
    c, = deps.acquisitions.values()
    spec = state["task_specs"][0]
    # Even a backend with identical raw material but no reader is unavailable.
    assert resolve_trace_attempt(c.attempt, spec, FixtureBackend()).outcome == "source_unavailable"
    monkeypatch.setattr(deps.backend, "trace_source", lambda: pytest.fail("global fallback"))
    monkeypatch.setattr(deps.acquisitions, "partition_for", lambda _: None)
    assert resolve_trace_attempt(c.attempt, spec, deps).outcome == "source_unavailable"
    monkeypatch.setattr(deps.acquisitions, "partition_for",
        lambda _: c.partition.model_copy(update={"attempt_id": "A-" + "0" * 64}))
    assert resolve_trace_attempt(c.attempt, spec, deps).outcome == "source_digest_mismatch"


def test_partition_content_identity_excludes_execution_but_includes_material_and_source():
    p = TelemetryPartition(attempt_id="A-" + "1" * 64, task_spec_id="T-" + "2" * 64,
        signal="trace", material=demo_traces(), source=SourceMetadata(rpc_service_map={"b": "p", "a": "c"}))
    other = p.model_copy(update={"attempt_id": "A-" + "3" * 64, "task_spec_id": "T-" + "4" * 64,
        "source": SourceMetadata(rpc_service_map={"a": "c", "b": "p"})})
    assert p.content_digest == other.content_digest
    assert p.content_digest == TelemetryPartition.model_validate_json(p.model_dump_json()).content_digest
    assert p.content_digest != p.model_copy(update={"material": TraceResult()}).content_digest
    assert p.content_digest != p.model_copy(update={"source": SourceMetadata()}).content_digest


@pytest.mark.parametrize("change", [
    {"projection": ToolResult(status="timeout")}, {"projection": ToolResult(status="error")},
    {"material": LogResult()}, {"source": None}, {"material": None},
    {"projection": ToolResult(status="empty", coverage_complete=True)},
    {"projection": ToolResult(status="ok")},
])
def test_acquisition_contract_rejects_incoherent_products(change):
    with pytest.raises(ValidationError):
        AcquisitionResult.model_validate({"signal": "trace", "material": TraceResult(), "source": SourceMetadata(),
            "projection": ToolResult(status="empty"), **change})


async def test_bound_completion_cannot_drop_its_partition():
    deps = Dependencies(FixtureBackend())
    await execute(initial_state(demo_incident()), deps, [request()])
    c, = deps.acquisitions.values()
    with pytest.raises(ValidationError, match="requires its raw partition"):
        CommittedAcquisition.model_validate({**c.model_dump(), "partition": None})


async def test_v1_snapshot_rejects_even_two_seed_trace_acquisitions(live):
    deps = Dependencies(live)
    state = await execute(initial_state(demo_incident()), deps, [request()])
    assert live.snapshot().traces == overlap()
    smaller = TimeWindow(start=demo_incident().window.start + timedelta(seconds=1), end=demo_incident().window.end)
    await execute(state, deps, [request(window=smaller)])
    with pytest.raises(ValueError, match="multiple committed"):
        live.snapshot()


async def test_default_fixture_runtime_scope_and_verification_goldens():
    deps = Dependencies(FixtureBackend())
    state, _ = await run_investigation(initial_state(demo_incident(), execution_id="p16a-golden"), deps)
    assert (state["rounds"], len(state["tool_calls"]), state["final_report"].status) == (2, 3, "SUPPORTED")
    assert state["final_report"].claims[0].evidence_ids == ("E-T1", "E-L1")
    assert all(t.services == ("checkout",) for t in state["task_specs"])
    assert all(c.partition is not None for c in deps.acquisitions.values())
    scope, verification = state["investigation_scope"], single_phv(state)
    assert scope.id == "SIS-f3310713d2679b587989121d5c558cdcad6beb6a9cc7037b193e54f7404483b2"
    assert scope.basis.id == "SB-5b3664c9665c1f70ef798895765f61b6283a72b401064fd04f4b4622c291b7c5"
    assert verification.state_basis.id == "VBS-d7dd283b800bd0b4a4c6f504310b36860e28db8ccc7f5c52fe3c86618078dfdc"
    assert verification.evaluation_basis.id == "VBE-b01c93039a4ad22ae42e9d6aaa35ed83481107af290d21fc0e30e59bfe7b3de4"
