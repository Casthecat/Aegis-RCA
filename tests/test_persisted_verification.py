from portfolio_helpers import single_phv, subject
"""P1.4-3 acquisition binding, freshness and single-owner persistence regressions."""

import asyncio
from datetime import timedelta

import pytest
from langgraph.runtime import Runtime
from pydantic import TypeAdapter, ValidationError

from aegis_rca import graph, runtime as execution, runner, trace_acquisition as native
from aegis_rca.fixtures import FixtureBackend, demo_evidence, demo_incident, demo_traces
from aegis_rca.models.acquisition import AttemptIdentity, AttemptRecord, TaskSpec
from aegis_rca.models.evidence import Observation, ToolResult
from aegis_rca.models.hypothesis import Verification
from aegis_rca.models.incident import Budget, TimeWindow
from aegis_rca.models.telemetry import TraceResult
from aegis_rca.policy import form_hypotheses, make_task
from aegis_rca.report import audit_and_report
from aegis_rca.state import IncidentState, initial_state, merge_records
from aegis_rca.verification import connectivity as pure
from aegis_rca.verification.catalog import connectivity_plan
from aegis_rca.verification.evaluate import evaluate_check
from aegis_rca.verification.persisted import (
    AttemptBindingResolution, PersistedConnectivityVerification, attempt_coverage,
    current_connectivity_verification, relevant_attempts, state_verification_basis,
)


@pytest.fixture
async def completed():
    backend = FixtureBackend()
    state, _ = await runner.run_investigation(initial_state(demo_incident(), execution_id="persisted-test"),
                                             execution.Dependencies(backend))
    return state, backend


def trace_attempt(state):
    return next((a, t) for a, t in relevant_attempts(state, subject(state)) if t.signal == "trace")


def replace_attempt(state, attempt, **updates):
    changed = AttemptRecord.model_validate({**attempt.model_dump(), **updates})
    return {**state, "attempt_records": [changed if a.id == attempt.id else a for a in state["attempt_records"]]}


def resolution(bundle, signal="trace"):
    return next(item for item in bundle.evaluation_basis.resolutions if item.coverage.signal == signal)


async def test_two_rounds_replace_one_bundle_and_only_refresh_node_evaluates(monkeypatch):
    captured = []
    original = graph.refresh_acquisition_state

    def refresh(state, backend):
        result = original(state, backend)
        captured.append((state["rounds"], single_phv(state), single_phv(result)))
        return result

    monkeypatch.setattr(graph, "refresh_acquisition_state", refresh)
    state, _ = await runner.run_investigation(initial_state(demo_incident()), execution.Dependencies(FixtureBackend()))
    assert len(captured) == 2
    first, second = captured[0][2], captured[1][2]
    assert captured[0][:2] == (1, None)
    assert captured[1][0] == 2 and captured[1][1] == first
    assert first.evaluation.trace_check.verdict == "SUPPORT"
    assert first.evaluation.log_check.verdict == "UNKNOWN"
    assert first.evaluation.plan_result.status == "UNCERTAIN"
    assert second.evaluation.plan_result.status == "SUPPORTED"
    assert single_phv(state) == second
    assert first.state_basis.id != second.state_basis.id
    assert set(second.evaluation.plan_result.check_result_ids) == {
        second.evaluation.trace_check.id, second.evaluation.log_check.id}
    assert "check_results" not in state and "plan_results" not in state
    assert all(a.ordinal == 1 for a in state["attempt_records"])
    assert (state["rounds"], len(state["tool_calls"])) == (2, 3)
    assert state["final_report"].claims[0].evidence_ids == ("E-T1", "E-L1")


async def test_consumers_and_restored_accessor_never_resolve_or_evaluate(completed, monkeypatch):
    state, backend = completed
    adapter = TypeAdapter(IncidentState)
    restored = adapter.validate_json(adapter.dump_json(state))

    def forbidden(*args, **kwargs):
        raise AssertionError("consumer attempted semantic evaluation or backend access")

    for target, name in ((execution, "evaluate_connectivity"), (native, "observe_spans"),
                         (native, "trace_source_material"), (graph, "refresh_acquisition_state"),
                         (runner, "refresh_acquisition_state"), (pure, "evaluate_check"),
                         (pure, "evaluate_legacy_check"), (pure, "aggregate_plan"),
                         (backend, "query"), (backend, "trace_source")):
        monkeypatch.setattr(target, name, forbidden)
    context = Runtime(context=execution.Dependencies(backend))
    assert await graph.hypothesize(restored, context) == {"hypotheses": state["hypotheses"], "provisional_hypotheses": []}
    assert graph.verify(restored, context)["verification"] == state["verification"]
    assert graph.finalize(restored, context)["final_report"] == state["final_report"]
    assert audit_and_report(restored) == state["final_report"]
    assert form_hypotheses(restored) == state["hypotheses"]
    execution.validate_execution_state(restored)
    assert current_connectivity_verification(restored) == single_phv(state).evaluation


@pytest.mark.parametrize("updates", [
    {"ordinal": 2}, {"tool_status": "empty"}, {"truncated": True}, {"coverage_complete": True},
    {"source_material_digest": "sha256-" + "1" * 64}, {"tool_result_digest": "sha256-" + "2" * 64},
    {"admitted_observation_ids": ()}, {"admitted_evidence_ids": ()},
    {"observation_binding": "unknown", "source_material_digest": None, "admitted_observation_ids": ()},
    {"evidence_binding": "unknown", "admitted_evidence_ids": ()},
])
async def test_relevant_attempt_semantics_invalidate_state_basis(completed, updates):
    state, _ = completed
    attempt, _ = trace_attempt(state)
    changed = replace_attempt(state, attempt, **updates)
    assert state_verification_basis(changed, subject(changed)).id != state_verification_basis(state, subject(state)).id
    with pytest.raises(ValueError, match="stale"):
        current_connectivity_verification(changed)


async def test_task_identity_window_and_attempt_membership_invalidate_basis(completed):
    state, _ = completed
    attempt, spec = trace_attempt(state)
    changed_spec = spec.model_copy(update={"query_template_id": "another-template"})
    changed = replace_attempt(state, attempt, task_spec_id=changed_spec.id)
    changed["task_specs"] = [changed_spec if t.id == spec.id else t for t in state["task_specs"]]
    assert state_verification_basis(changed, subject(changed)).id != state_verification_basis(state, subject(state)).id
    changed = {**state, "incident": state["incident"].model_copy(update={"window": TimeWindow(
        start=spec.window.start + timedelta(seconds=1), end=spec.window.end)})}
    assert state_verification_basis(changed, subject(changed)).id != state_verification_basis(state, subject(state)).id
    changed = {**state, "attempt_records": [a for a in state["attempt_records"] if a.id != attempt.id]}
    assert state_verification_basis(changed, subject(changed)).id != state_verification_basis(state, subject(state)).id


async def test_display_order_and_unrelated_metric_changes_do_not_invalidate(completed):
    state, _ = completed
    changed = {**state,
        "task_specs": [t.model_copy(update={"alias": "display"}) for t in reversed(state["task_specs"])],
        "attempt_records": [a.model_copy(update={"warnings": ("new warning",)}) for a in reversed(state["attempt_records"])],
        "evidence": [e.model_copy(update={"excerpt": "display", "source": e.source.model_copy(
            update={"locator": "arbitrary/path"})}) for e in reversed(state["evidence"])],
        "hypotheses": [h.model_copy(update={"claim": "display"}) for h in state["hypotheses"]]}
    metric = next(a for a in state["attempt_records"] if a.id not in {x.id for x, _ in relevant_attempts(state, subject(state))})
    changed = replace_attempt(changed, metric, truncated=True, tool_result_digest="sha256-" + "9" * 64)
    metric_spec = next(t for t in state["task_specs"] if t.id == metric.task_spec_id)
    new_spec = metric_spec.model_copy(update={"query_template_id": "unrelated-metric-change"})
    changed = replace_attempt(changed, metric, task_spec_id=new_spec.id)
    changed["task_specs"] = [new_spec if t.id == metric_spec.id else t for t in changed["task_specs"]]
    assert state_verification_basis(changed, subject(changed)).id == state_verification_basis(state, subject(state)).id
    assert current_connectivity_verification(changed) == current_connectivity_verification(state)


@pytest.mark.parametrize("change", [
    {"observation": Observation(code="application_error", peer_service="payment")},
    {"service": "shipping"}, {"signal": "trace"},
    {"timestamp": demo_incident().window.end},
])
async def test_legacy_same_id_semantic_changes_invalidate(completed, change):
    state, _ = completed
    changed = {**state, "evidence": [e.model_copy(update=change) if e.id == "E-L1" else e for e in state["evidence"]]}
    assert {e.id for e in changed["evidence"]} == {e.id for e in state["evidence"]}
    assert state_verification_basis(changed, subject(changed)).id != state_verification_basis(state, subject(state)).id
    with pytest.raises(ValueError, match="stale"):
        current_connectivity_verification(changed)


@pytest.mark.parametrize("mode,expected", [
    ("digest", "source_digest_mismatch"), ("unavailable", "source_unavailable"),
    ("subset", "observation_id_mismatch"), ("superset", "observation_id_mismatch"),
])
async def test_resolution_failures_change_vbe_not_vbs_or_historical_attempt(completed, monkeypatch, mode, expected):
    state, backend = completed
    old = single_phv(state)
    historical = tuple(a.model_dump_json() for a in state["attempt_records"])
    original = native.observe_spans
    if mode == "digest":
        part = backend._partition_reader.partition_for(trace_attempt(state)[0].id)
        monkeypatch.setattr(backend._partition_reader, "partition_for",
                            lambda _: part.model_copy(update={"material": TraceResult()}))
        monkeypatch.setattr(native, "observe_spans", lambda *a, **kw: pytest.fail("digest must be checked first"))
    elif mode == "unavailable":
        monkeypatch.setattr(backend._partition_reader, "partition_for", lambda _: None)
    elif mode == "subset":
        monkeypatch.setattr(native, "observe_spans", lambda *a, **kw: ())
    else:
        def superset(*args, **kwargs):
            facts = original(*args, **kwargs)
            return (*facts, facts[0].model_copy(update={"id": "O-trace-new-producer-fact"}))
        monkeypatch.setattr(native, "observe_spans", superset)
    assert current_connectivity_verification(state) == old.evaluation
    new = execution.refresh_connectivity_verification(state, backend, subject=subject(state))
    assert new.state_basis.id == old.state_basis.id
    assert new.evaluation_basis.id != old.evaluation_basis.id
    assert resolution(new).outcome == expected
    assert resolution(new).coverage.fact_set_complete is False
    assert new.evaluation_basis.observation_ids == ()
    assert new.evaluation.trace_check.verdict == "UNKNOWN"
    assert tuple(a.model_dump_json() for a in state["attempt_records"]) == historical


@pytest.mark.parametrize("status,error,query", [
    ("ok", "none", "succeeded"), ("empty", "none", "succeeded"),
    ("error", "backend_error", "failed"), ("timeout", "tool_timeout", "failed"),
])
def test_exact_coverage_mapping_never_parses_warnings(status, error, query):
    spec = TaskSpec(signal="trace", services=("checkout",), window=demo_incident().window,
                    query_template_id="test")
    attempt = AttemptRecord(execution_id="test", task_spec_id=spec.id, ordinal=1, tool_status=status,
        error_class=error, backend_kind="fixture", timeout_seconds=1, truncated=True,
        coverage_complete=None, warnings=("not_configured coverage complete",))
    view = attempt_coverage(attempt, spec)
    assert view.acquisition_id == attempt.id and view.query_identity == spec.id
    assert (view.signal, view.services, view.window) == (spec.signal, spec.services, spec.window)
    assert view.query_status == query
    assert view.truncated and view.coverage_complete is None
    assert view.pre_capture_omissions == "unknown" and not view.fact_set_complete
    assert view.warnings == ()


async def test_empty_bound_projection_is_complete_binding_without_absence():
    state, _ = await runner.run_investigation(initial_state(demo_incident()),
        execution.Dependencies(FixtureBackend(records=(), traces=TraceResult(coverage_complete=True))))
    bundle = single_phv(state)
    assert bundle is None  # No positive native failure candidate, hence no HS/plan.
    assert all(a.admitted_observation_ids == () for a in state["attempt_records"])
    assert state["final_report"].status == "INCONCLUSIVE"


async def test_positive_support_survives_truncation_and_unknown_coverage():
    class Partial(FixtureBackend):
        async def query(self, task):
            value = await super().query(task)
            return value.model_copy(update={"truncated": True, "coverage_complete": None})
    state, _ = await runner.run_investigation(initial_state(demo_incident()), execution.Dependencies(Partial()))
    assert state["final_report"].status == "SUPPORTED"
    assert all(v.coverage.fact_set_complete and v.coverage.truncated and v.coverage.coverage_complete is None
               for v in single_phv(state).evaluation_basis.resolutions)


async def test_unbound_log_and_missing_attempt_evidence_cannot_support(completed):
    state, backend = completed
    log_attempt, _ = next((a, t) for a, t in relevant_attempts(state, subject(state)) if t.signal == "log")
    changed = {**state, "evidence": [e for e in state["evidence"] if e.id != "E-L1"]}
    # A different E-* with identical content cannot replace the historically bound ID.
    changed["evidence"].append(demo_evidence()[2].model_copy(update={"id": "E-unrelated"}))
    bundle = execution.refresh_connectivity_verification(changed, backend, subject=subject(changed))
    assert resolution(bundle, "log").outcome == "evidence_missing"
    assert bundle.evaluation.log_check.verdict == "UNKNOWN"
    assert not bundle.evaluation_basis.log_evidence
    # Removing the log attempt leaves a real but unbound log in authoritative state.
    changed = {**state, "attempt_records": [a for a in state["attempt_records"] if a.id != log_attempt.id],
               "tool_calls": [c for c in state["tool_calls"] if c.attempt_id != log_attempt.id]}
    assert execution.refresh_connectivity_verification(changed, backend, subject=subject(changed)).evaluation.log_check.verdict == "UNKNOWN"


async def test_relevant_selection_uses_scopes_not_alias(completed):
    state, _ = completed
    attempt, spec = trace_attempt(state)
    variants = [spec.model_copy(update={"services": ("shipping",)}),
                spec.model_copy(update={"window": TimeWindow(start=spec.window.end,
                    end=spec.window.end + timedelta(minutes=1))})]
    for other in variants:
        changed = {**state, "task_specs": [*state["task_specs"], other],
                   "attempt_records": [*state["attempt_records"], attempt.model_copy(update={"task_spec_id": other.id})]}
        assert relevant_attempts(changed, subject(changed)) == relevant_attempts(state, subject(state))
        assert state_verification_basis(changed, subject(changed)).id == state_verification_basis(state, subject(state)).id
    partial = spec.model_copy(update={"window": TimeWindow(start=spec.window.start,
                                                          end=spec.window.end - timedelta(seconds=1))})
    changed = {**state, "task_specs": [*state["task_specs"], partial],
               "attempt_records": [*state["attempt_records"], attempt.model_copy(update={"task_spec_id": partial.id})]}
    assert len(relevant_attempts(changed, subject(changed))) == len(relevant_attempts(state, subject(state))) + 1


async def test_deadline_finalizes_attempts_then_refreshes_then_reports(monkeypatch):
    order = []
    class Backend(FixtureBackend):
        async def query(self, task):
            if task.signal == "trace":
                await asyncio.Event().wait()
            return await super().query(task)
    original_refresh, original_report = runner.refresh_acquisition_state, runner.audit_and_report
    def refresh(state, backend):
        assert any(a.error_class == "global_deadline" for a in state["attempt_records"])
        assert any(a.tool_status == "ok" for a in state["attempt_records"])
        assert len(state["attempt_records"]) == len(state["tool_calls"]) == 2
        order.append("refresh")
        return original_refresh(state, backend)
    def report(state):
        assert single_phv(state) is None
        assert current_connectivity_verification(state) is None
        order.append("report")
        return original_report(state)
    monkeypatch.setattr(runner, "refresh_acquisition_state", refresh)
    monkeypatch.setattr(runner, "audit_and_report", report)
    state, _ = await runner.run_investigation(initial_state(demo_incident(), Budget(tool_timeout_seconds=10)),
        execution.Dependencies(Backend()), deadline_seconds=0.05)
    assert order == ["refresh", "report"]
    assert state["final_report"].termination_reason == "deadline_exhausted"
    assert all(a.ordinal == 1 for a in state["attempt_records"])


def test_no_bundle_report_is_conservative_without_evaluation(monkeypatch):
    state = initial_state(demo_incident())
    state["evidence"] = list(demo_evidence())
    state["verification"] = Verification(decision="finalize", reason="planner_claim")
    monkeypatch.setattr(pure, "evaluate_connectivity", lambda *a, **kw: pytest.fail("no fallback evaluation"))
    assert audit_and_report(state).status == "INCONCLUSIVE"
    assert current_connectivity_verification(state) is None
    assert graph.verify(state, Runtime(context=execution.Dependencies(FixtureBackend())))["verification"].decision == "inconclusive"


async def test_runtime_ids_stable_with_provenance_and_distinct_from_pure(completed):
    state, backend = completed
    before = single_phv(state)
    again = execution.refresh_connectivity_verification(state, backend, subject=subject(state))
    assert before == again
    assert before.state_basis.id == again.state_basis.id
    assert before.evaluation_basis.id == again.evaluation_basis.id
    facts, _ = execution.trace_binding(trace_attempt(state)[1], backend)
    no_coverage = pure.evaluate_connectivity(state["incident"].window, trace_observations=facts,
                                             legacy_log_evidence=(demo_evidence()[2],))
    actual = again.evaluation
    assert actual.trace_check.check_id == no_coverage.trace_check.check_id
    assert actual.plan_result.plan_id == no_coverage.plan_result.plan_id
    assert actual.trace_check.id != no_coverage.trace_check.id
    assert actual.log_check.id != no_coverage.log_check.id
    assert actual.plan_result.id != no_coverage.plan_result.id
    assert PersistedConnectivityVerification.model_validate_json(before.model_dump_json()) == before


def test_complete_empty_binding_still_cannot_close_even_an_exact_query():
    check = next(c for c in connectivity_plan(demo_incident().window).checks if c.scope.signal == "trace")
    spec = TaskSpec(signal="trace", services=check.scope.services, window=check.scope.window, query_template_id="test")
    attempt = AttemptRecord(execution_id="test", task_spec_id=spec.id, ordinal=1, tool_status="empty",
        backend_kind="fixture", timeout_seconds=1, coverage_complete=True,
        observation_binding="bound", source_material_digest="sha256-" + "1" * 64)
    check = check.model_copy(update={"scope": check.scope.model_copy(update={"query_identity": spec.id})})
    view = attempt_coverage(attempt, spec, verified=True)
    assert view.coverage_complete and view.fact_set_complete and not view.truncated
    assert evaluate_check(check, coverage=(view,)).verdict == "UNKNOWN"


async def test_one_unavailable_attempt_does_not_hide_another_bound_positive(completed):
    state, backend = completed
    old, spec = trace_attempt(state)
    alternate = spec.model_copy(update={"query_template_id": "second-acquisition"})
    task = make_task(state["incident"], "trace")
    identity = AttemptIdentity(execution_id=state["execution_id"], task_spec_id=alternate.id, ordinal=1)
    reserved = execution.ReservedAttempt(identity, task, alternate, 1, identity.id)
    from aegis_rca.acquisition_store import AcquisitionStore, PartitionReader
    store = AcquisitionStore()
    for completion in backend._partition_reader.values():
        store.commit(completion)
    completion = store.commit(execution.finalize_acquisition(reserved, backend, await backend.acquire(task)))
    backend._partition_reader = PartitionReader(store)
    attempt, call = completion.attempt, completion.tool_call
    # First attempt's historical digest is unavailable; second one is honestly bound.
    changed = replace_attempt(state, old, source_material_digest="sha256-" + "1" * 64)
    changed["task_specs"] = [*state["task_specs"], alternate]
    changed["attempt_records"] = [*changed["attempt_records"], attempt]
    changed["tool_calls"] = [*state["tool_calls"], call]
    bundle = single_phv(execution.refresh_acquisition_state(changed, backend))
    assert bundle.evaluation.trace_check.verdict == "SUPPORT"
    outcomes = {r.coverage.acquisition_id: r.outcome for r in bundle.evaluation_basis.resolutions}
    assert outcomes[old.id] == "source_digest_mismatch" and outcomes[attempt.id] == "verified"
    assert {v.acquisition_id for v in bundle.evaluation.trace_check.coverage_used} == {attempt.id}


async def test_log_bindings_do_not_borrow_another_attempts_evidence(completed):
    state, backend = completed
    old, spec = next((a, t) for a, t in relevant_attempts(state, subject(state)) if t.signal == "log")
    alternate = spec.model_copy(update={"query_template_id": "second-log-acquisition"})
    identity = AttemptIdentity(execution_id=state["execution_id"], task_spec_id=alternate.id, ordinal=1)
    reserved = execution.ReservedAttempt(identity, make_task(state["incident"], "log"), alternate, 1, identity.id)
    missing = demo_evidence()[2].model_copy(update={"id": "E-not-present"})
    attempt, call = execution.finish_attempt(reserved, backend, ToolResult(status="ok", evidence=(missing,)),
                                             admitted_evidence=(missing,))
    changed = {**state, "task_specs": [*state["task_specs"], alternate],
        "attempt_records": [*state["attempt_records"], attempt], "tool_calls": [*state["tool_calls"], call]}
    bundle = execution.refresh_connectivity_verification(changed, backend, subject=subject(changed))
    views = {r.coverage.acquisition_id: r for r in bundle.evaluation_basis.resolutions}
    assert views[old.id].coverage.admitted_ids == ("E-L1",)
    assert views[attempt.id].outcome == "evidence_missing"
    assert views[attempt.id].coverage.admitted_ids == ()
    assert bundle.evaluation.log_check.verdict == "SUPPORT"
    assert {v.acquisition_id for v in bundle.evaluation.log_check.coverage_used} == {old.id}


@pytest.mark.parametrize("part", ["plan", "check", "origin", "result_refs", "input_refs", "coverage", "status"])
async def test_intrinsic_bundle_rejects_inconsistent_generations(completed, part):
    state, _ = completed
    payload = single_phv(state).model_dump()
    evaluation = payload["evaluation"]
    if part == "plan":
        evaluation["plan_result"]["plan_id"] = "P-other"
    elif part == "check":
        evaluation["trace_check"]["check_id"] = evaluation["log_check"]["check_id"]
    elif part == "origin":
        evaluation["trace_check"]["input_kind"] = "legacy_evidence"
    elif part == "result_refs":
        evaluation["plan_result"]["check_result_ids"] = ()
    elif part == "input_refs":
        payload["evaluation_basis"]["observation_ids"] = ()
    elif part == "coverage":
        payload["evaluation_basis"]["resolutions"][0]["coverage"]["truncated"] = True
    else:
        evaluation["plan_result"]["status"] = "UNCERTAIN"
    with pytest.raises(ValidationError):
        PersistedConnectivityVerification.model_validate(payload)


async def test_state_relative_validation_rejects_foreign_attempt_without_model_guessing(completed):
    state, _ = completed
    bundle = single_phv(state)
    # A stale model_copy must still be checked at the accessor trust boundary.
    damaged = bundle.model_copy(update={"evaluation": bundle.evaluation.model_copy(update={
        "plan_result": bundle.evaluation.plan_result.model_copy(update={"check_result_ids": ()})})})
    with pytest.raises(ValidationError):
        current_connectivity_verification({**state, "hypothesis_verifications": (damaged,)})
    altered = {**state, "execution_id": "foreign-execution"}
    with pytest.raises(ValueError, match="another execution"):
        current_connectivity_verification(altered)


def test_binding_resolution_rejects_diagnostic_prose():
    spec = TaskSpec(signal="trace", services=("checkout",), window=demo_incident().window, query_template_id="test")
    attempt = AttemptRecord(execution_id="test", task_spec_id=spec.id, ordinal=1, tool_status="empty",
                            backend_kind="fixture", timeout_seconds=1)
    with pytest.raises(ValidationError):
        AttemptBindingResolution(outcome="the backend probably failed", coverage=attempt_coverage(attempt, spec))
