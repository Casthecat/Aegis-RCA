from portfolio_helpers import single_phv, subject
"""P1.4-2 orchestration regressions; all source telemetry is synthetic."""

from datetime import timedelta
import hashlib
import json

import pytest
from langgraph.runtime import Runtime

from aegis_rca import graph, policy
from aegis_rca.fixtures import FixtureBackend, demo_evidence, demo_incident, demo_traces
from aegis_rca.models.check import CheckCoverageView
from aegis_rca.models.evidence import ToolCallRecord, ToolResult
from aegis_rca.models.hypothesis import Verification
from aegis_rca.models.telemetry import LogRecord, LogResult, MetricResult, TraceResult
from aegis_rca.observations import observe_spans
from aegis_rca.policy import form_hypotheses, make_task
from aegis_rca.report import audit_and_report
from aegis_rca.runner import run_investigation
from aegis_rca.runtime import Dependencies, refresh_connectivity_verification
from aegis_rca.settings import BackendSettings
from aegis_rca.verification.persisted import current_connectivity_verification
from aegis_rca.state import initial_state
from aegis_rca.tools.live import LiveBackend
from aegis_rca.tools.replay import ReplayBackend
from aegis_rca.tools.snapshot import TelemetrySnapshot, write_snapshot
from aegis_rca.verification import connectivity as canonical

from test_workflow import RecordingBackend, run_workflow
import test_metric_observations as metric_fixture
import test_metric_derivation as metric_derivation


def raw_log(peer="payment"):
    return LogRecord(id="synthetic-log", service="checkout", timestamp=demo_incident().window.start,
                     severity="ERROR", body=f"dial {peer}:50051: connection refused",
                     attributes={"peer.service": peer}, trace_id="9" * 32, source="synthetic:logs")


def trace_result(grpc=14, *, peer="payment", duplicate=False, success=False):
    span = demo_traces().spans[0]
    span = span.model_copy(update={"attributes": {"rpc.grpc.status_code": grpc, "peer.service": peer,
                                                   "rpc.service": "oteldemo.PaymentService"}})
    spans = (span, span) if duplicate else (span,)
    if success:
        spans += (span.model_copy(update={"span_id": "3" * 16, "status": "OK",
                                         "attributes": {"rpc.grpc.status_code": 0, "peer.service": peer}}),)
    return TraceResult(spans=spans, truncated=True, coverage_complete=False)


def backend_for(kind, monkeypatch, *, grpc=14, logs=True, peer="payment", duplicate=False, success=False):
    traces = trace_result(grpc, peer=peer, duplicate=duplicate, success=success)
    log_data = LogResult(logs=(raw_log(peer),) if logs else ())
    if kind == "fixture":
        evidence = tuple(e for e in demo_evidence() if logs or e.signal != "log")
        backend = FixtureBackend(evidence, traces=traces)
    elif kind == "replay":
        backend = ReplayBackend(TelemetrySnapshot(incident=demo_incident(), provenance="synthetic_wire_fixture",
                                                  traces=traces, logs=log_data))
    else:
        backend = LiveBackend(BackendSettings(prometheus_url="http://metrics.test",
            jaeger_url="http://traces.test", opensearch_url="http://logs.test"), demo_incident())
        # Existing test_live_replay additionally covers the real HTTP wire adapters.
        monkeypatch.setattr(backend.jaeger, "search", lambda *a, **k: traces)
        monkeypatch.setattr(backend.opensearch, "search", lambda *a, **k: log_data)
    backend.attempted = []
    method = "acquire" if kind == "live" else "query"
    query = getattr(backend, method)

    async def recorded(task):
        backend.attempted.append(task.signal)
        return await query(task)

    monkeypatch.setattr(backend, method, recorded)
    return backend


@pytest.mark.parametrize("kind", ["fixture", "replay", "live"])
@pytest.mark.parametrize("grpc,expected", [(14, "SUPPORTED"), (2, "INCONCLUSIVE")])
async def test_all_backends_use_real_span_observations_and_the_same_aggregate(kind, grpc, expected, monkeypatch):
    backend = backend_for(kind, monkeypatch, grpc=grpc)
    aggregate_calls = []
    original = canonical.aggregate_plan

    def aggregate(plan, *, results):
        aggregate_calls.append((plan, results))
        assert {r.input_kind for r in results} == {"observation", "legacy_evidence"}
        assert all(view.acquisition_id.startswith("A-") and view.query_identity.startswith("T-")
                   for r in results for view in r.coverage_used)
        assert all(c.scope.acquisition_id is None and c.scope.query_identity is None for c in plan.checks)
        return original(plan, results=results)

    monkeypatch.setattr(canonical, "aggregate_plan", aggregate)
    try:
        state, decisions, _ = await run_workflow(backend)
        assert state["final_report"].status == expected
        assert len(backend.attempted) == len(set(backend.attempted)) == (3 if grpc == 14 else 2)
        actual = current_connectivity_verification(state)
        if grpc == 2:
            assert actual is None and not aggregate_calls and state["hypotheses"] == []
            return
        assert aggregate_calls
        source = backend.trace_source()
        facts = observe_spans(source.traces.spans, source.rpc_service_map,
                              backend=source.backend, synthetic=source.synthetic)
        assert actual.trace_check.relevant_ids == tuple(sorted({fact.id for fact in facts}))
        assert all(identifier.startswith("O-trace-") for identifier in actual.trace_check.relevant_ids)
        assert all(call.id == f"query-{call.task.signal}" for call in state["tool_calls"])
        assert not {"check_results", "traces", "backend", "plan_result"} & state.keys()
        if grpc == 14:
            assert actual.trace_check.verdict == "SUPPORT"
            assert "targeted_followup" in decisions
            assert all(identifier.startswith("E-") for claim in state["final_report"].claims
                       for identifier in claim.evidence_ids)
        else:
            assert actual.trace_check.verdict == "UNKNOWN"
            assert "targeted_followup" not in decisions
            if kind != "fixture":
                trace_call = next(c for c in state["tool_calls"] if c.task.signal == "trace")
                assert trace_call.status == "empty" and trace_call.evidence_ids == ()
                assert len(facts) == 1 and facts[0].value.rpc_status_name == "UNKNOWN"
    finally:
        if isinstance(backend, LiveBackend):
            backend.close()


@pytest.mark.parametrize("kind", ["fixture", "replay", "live"])
async def test_missing_log_followup_once_uses_rpc_result(kind, monkeypatch):
    backend = backend_for(kind, monkeypatch, logs=False)
    try:
        state, decisions, _ = await run_workflow(backend)
        assert decisions.count("targeted_followup") >= 1
        assert backend.attempted.count("log") == 1
        assert state["final_report"].status == "INCONCLUSIVE"
        result = current_connectivity_verification(state)
        assert result.trace_check.verdict == "SUPPORT" and result.log_check.verdict == "UNKNOWN"
    finally:
        if isinstance(backend, LiveBackend):
            backend.close()


@pytest.mark.parametrize("options,expected", [
    ({"peer": "shipping"}, "INCONCLUSIVE"),
    ({"duplicate": True}, "SUPPORTED"),
    ({"success": True}, "SUPPORTED"),
])
async def test_wrong_peer_duplicates_and_success_on_same_edge(options, expected, monkeypatch):
    backend = backend_for("replay", monkeypatch, **options)
    state, _, _ = await run_workflow(backend)
    result = current_connectivity_verification(state)
    assert state["final_report"].status == expected
    if expected == "SUPPORTED":
        assert result.trace_check.incompatible_ids == ()
        assert len(result.trace_check.matched_ids) == 1
    else:
        assert result is None  # Explicit shipping conflicts with mapped RPC payment peer.


@pytest.mark.parametrize("healthy", [False, True])
async def test_empty_or_healthy_replay_does_not_prove_absence(healthy):
    traces = trace_result(grpc=0) if healthy else TraceResult(coverage_complete=True)
    snapshot = TelemetrySnapshot(incident=demo_incident(), provenance="synthetic_wire_fixture", traces=traces)
    backend = ReplayBackend(snapshot)
    state, _ = await run_investigation(initial_state(demo_incident()), Dependencies(backend))
    result = current_connectivity_verification(state)
    assert state["final_report"].status == "INCONCLUSIVE"
    assert result is None and state["hypotheses"] == []
    assert all(a.pre_capture_omissions == "unknown" for a in state["attempt_records"])


async def test_fixture_source_exists_without_any_legacy_trace_citation():
    records = tuple(e for e in demo_evidence() if e.signal != "trace")
    backend = FixtureBackend(records, traces=demo_traces())
    state, _ = await run_investigation(initial_state(demo_incident()), Dependencies(backend))
    trace_call = next(c for c in state["tool_calls"] if c.task.signal == "trace")
    assert trace_call.status == "empty" and trace_call.evidence_ids == ()
    assert state["final_report"].status == "SUPPORTED"
    assert state["final_report"].claims[0].evidence_ids == ("E-L1",)
    result = current_connectivity_verification(state)
    assert result.trace_check.verdict == "SUPPORT"
    assert result.trace_check.matched_ids[0].startswith("O-trace-")


async def test_missing_log_followup_is_targeted_even_without_e_t1():
    backend = RecordingBackend((demo_evidence()[0],), traces=demo_traces())
    state, decisions, backend = await run_workflow(backend)
    assert "targeted_followup" in decisions and "replan" not in decisions
    assert backend.attempted == ["metric", "trace", "log"]
    assert {e.id for e in state["evidence"]} == {"E-M1"}
    assert state["final_report"].status == "INCONCLUSIVE"
    assert current_connectivity_verification(state).trace_check.verdict == "SUPPORT"


async def test_legacy_trace_evidence_alone_cannot_override_missing_source():
    backend = FixtureBackend(demo_evidence(), traces=TraceResult())
    state, decisions, _ = await run_workflow(backend)
    assert {e.id for e in state["evidence"]} == {"E-M1", "E-T1"}
    assert state["final_report"].status == "INCONCLUSIVE"
    assert "targeted_followup" not in decisions
    assert state["hypotheses"] == []
    assert not hasattr(policy, "connectivity_ready")


async def test_citation_selector_cannot_control_verification_hypothesis_or_report(monkeypatch):
    backend = FixtureBackend()
    monkeypatch.setattr(policy, "connectivity_citations", lambda evidence, subject: ())
    monkeypatch.setattr("aegis_rca.report.connectivity_citations", lambda evidence, subject: ())
    state, _, _ = await run_workflow(backend)
    assert state["final_report"].status == "SUPPORTED"
    assert state["hypotheses"][0].status == "supported"
    assert state["final_report"].claims[0].evidence_ids == ()


@pytest.mark.parametrize("kind", ["fixture", "replay", "live"])
async def test_verify_and_report_share_persisted_pr_without_backend_or_evaluation(kind, monkeypatch):
    backend = backend_for(kind, monkeypatch)
    try:
        state, _, _ = await run_workflow(backend)
        first = current_connectivity_verification(state)
        calls = tuple(backend.attempted)

        def forbidden(*args, **kwargs):
            raise AssertionError("Consumer must not query or evaluate")

        monkeypatch.setattr(backend, "query", forbidden)
        monkeypatch.setattr(backend, "trace_source", forbidden)
        monkeypatch.setattr(canonical, "evaluate_check", forbidden)
        monkeypatch.setattr(canonical, "evaluate_legacy_check", forbidden)
        monkeypatch.setattr(canonical, "aggregate_plan", forbidden)
        runtime = Runtime(context=Dependencies(backend))
        assert graph.verify(state, runtime)["verification"].decision == "finalize"
        assert graph.finalize(state, runtime)["final_report"] == state["final_report"]
        assert current_connectivity_verification(state).plan_result.id == first.plan_result.id
        assert tuple(backend.attempted) == calls
    finally:
        if isinstance(backend, LiveBackend):
            backend.close()


@pytest.mark.parametrize("status", ["ok", "empty", "error", "timeout"])
def test_legacy_call_alone_cannot_claim_attempt_bound_native_facts(status):
    state = initial_state(demo_incident())
    task = make_task(state["incident"], "trace")
    state["tool_calls"] = [ToolCallRecord(id=task.id, task=task, status=status, evidence_ids=())]
    assert current_connectivity_verification(state) is None
    assert refresh_connectivity_verification(state, FixtureBackend(), subject=subject(state)) is None


def test_unrelated_legacy_call_scope_cannot_claim_native_facts():
    state = initial_state(demo_incident())
    task = make_task(state["incident"], "trace").model_copy(update={"services": ("shipping",)})
    state["tool_calls"] = [ToolCallRecord(id=task.id, task=task, status="empty", evidence_ids=())]
    assert refresh_connectivity_verification(state, FixtureBackend(), subject=subject(state)) is None


async def test_zero_delta_and_321_6_metrics_do_not_enter_connectivity_or_change_snapshot(tmp_path, monkeypatch):
    metric_data = metric_fixture.payment_result()
    interval_start = metric_fixture.START
    incident = demo_incident().model_copy(update={"window": metric_fixture.WINDOW})
    span = demo_traces().spans[0].model_copy(update={"start": interval_start})
    log = raw_log().model_copy(update={"timestamp": interval_start})
    snapshot = TelemetrySnapshot(incident=incident, provenance="synthetic_wire_fixture", metrics=metric_data,
                                traces=TraceResult(spans=(span,)), logs=LogResult(logs=(log,)))
    path = tmp_path / "snapshot.json"
    write_snapshot(snapshot, path)
    original_bytes = path.read_bytes()
    batch = metric_derivation.derive_metrics(metric_data, window=incident.window,
        context=metric_derivation.context_for(metric_data),
        specs=tuple(metric_derivation.CounterDeltaSpec(series=metric_derivation.key(item)) for item in metric_data.series))
    assert all(fact.value.delta == 0 for fact in metric_derivation.facts(batch))

    def forbidden(*args, **kwargs):
        raise AssertionError("Connectivity must not derive metrics")

    monkeypatch.setattr("aegis_rca.observations.metric_derivation.derive_metrics", forbidden)
    backend = ReplayBackend(snapshot)
    state, _ = await run_investigation(initial_state(incident), Dependencies(backend))
    assert state["final_report"].status == "SUPPORTED"
    before = current_connectivity_verification(state)
    backend.snapshot = snapshot.model_copy(update={"metrics": MetricResult()})
    assert current_connectivity_verification(state) == before
    assert path.read_bytes() == original_bytes


@pytest.mark.parametrize("scenario,expected_hash", [
    ("connectivity", "bb17b176a5d0e9e6cbd448e839f5b4f631e498e2bbc0d7c25a7d89e118544c3d"),
    ("empty", "cb316cd270281448ef74e8b7ed3ca26025d06852a40df3f2257fb4055a3171ae"),
    ("missing_logs", "d6541e4acb8ffb5e89cb1ad2ed6bad8f120e973fbfd50e45a143905e06f4354a"),
])
async def test_p17_report_control_and_citation_goldens(scenario, expected_hash):
    # P1.7c reports include execution-bound CR/PR provenance; fix the execution namespace.
    if scenario == "empty":
        backend = RecordingBackend(records=())
    elif scenario == "missing_logs":
        backend = RecordingBackend(tuple(e for e in demo_evidence() if e.signal != "log"), traces=demo_traces())
    else:
        backend = RecordingBackend()
    state, decisions, backend = await run_workflow(backend, execution_id="p17c-report-golden")
    value = {"report": state["final_report"].model_dump(mode="json"),
             "decisions": decisions, "calls": backend.attempted}
    assert hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest() == expected_hash
