"""P1.7c bounded multi-HS truth, current-only provenance and honest reporting."""
import pytest
from langgraph.runtime import Runtime
from pydantic import ValidationError, TypeAdapter

from aegis_rca import graph, runtime, trace_acquisition
from aegis_rca.fixtures import FixtureBackend, demo_incident
from aegis_rca.models.portfolio import HypothesisAdmissionPolicy, HypothesisPortfolio
from aegis_rca.models.hypothesis import HypothesisAssessment, ReportClaim
from aegis_rca.models.incident import Budget
from aegis_rca.models.telemetry import TraceResult, LogResult, LogRecord, MetricResult
from aegis_rca.models.acquisition_result import AcquisitionResult
from aegis_rca.models.evidence import ToolResult
from aegis_rca.models.identity import structural_id
from aegis_rca.portfolio import current_hypothesis_portfolio, hypothesis_state_basis
from aegis_rca.assessments import current_assessments
from aegis_rca.verification.persisted import current_hypothesis_verifications, current_connectivity_verification
from aegis_rca.scheduling import required_requests
from aegis_rca.runner import run_investigation
from aegis_rca.state import initial_state, IncidentState
from aegis_rca.policy import make_task
from aegis_rca.report import audit_and_report
from aegis_rca.tools.snapshot_v2 import export_snapshot_v2
from aegis_rca.tools.recorded_replay import RecordedReplayBackend
from test_dependency_hypotheses import span
from test_acquisition_partitions import execute


def scenario(edges=(("frontend", "auth"), ("frontend", "catalog")), *, log_edges=None, seeds=None, records=None):
    seeds = tuple(sorted(set(seeds or (caller for caller, _ in edges))))
    incident = demo_incident().model_copy(update={"seed_services": seeds, "query": "Investigate dependency failures"})
    traces = TraceResult(spans=tuple(records) if records is not None else tuple(
        span(local=caller, peer=callee, key=format(i + 1, "x")) for i, (caller, callee) in enumerate(edges)))
    materials = {("trace", seeds): traces, ("metric", seeds): MetricResult()}
    for caller in seeds:
        logs = tuple(LogRecord(id=f"log-{local}-{peer}", timestamp=incident.window.start,
            service=caller, severity="ERROR", body=f"dial {peer}:50051: connection refused",
            attributes={"peer.service": peer}, source="synthetic:portfolio")
            for local, peer in (edges if log_edges is None else log_edges) if local == caller)
        materials[("log", (caller,))] = LogResult(logs=logs)
    return incident, FixtureBackend(records=(), seed_services=seeds, materials=materials)


async def run_case(*args, budget=None, execution_id=None, **kwargs):
    incident, backend = scenario(*args, **kwargs)
    deps = runtime.Dependencies(backend)
    state, _ = await run_investigation(initial_state(incident, budget, execution_id=execution_id), deps)
    return state, deps


def by_edge(state):
    return {(a.hypothesis_spec.caller, a.hypothesis_spec.callee): a for a in current_assessments(state)}


@pytest.mark.parametrize("logs,expected", [(None, ("SUPPORTED", "SUPPORTED")),
    ((("frontend", "catalog"),), ("UNRESOLVED", "SUPPORTED")), ((), ("UNRESOLVED", "UNRESOLVED"))])
async def test_shared_completed_log_intent_supports_each_edge_independently(logs, expected):
    state, deps = await run_case(log_edges=logs)
    assessments = current_assessments(state)
    assert tuple(a.status for a in assessments) == expected
    assert state["rounds"] == 2 and len(state["tool_calls"]) == 3
    rows = [r for r in state["intent_ledger"] if r.intent.signal == "log"]
    assert len(rows) == 1 and rows[0].status == "completed"
    need, = rows[0].needs
    assert len(need.requested_for_hypothesis_ids) == len(need.requested_for_check_ids) == 2
    assert len([a for a in deps.attempts.values() if a.spec.signal == "log"]) == 1
    assert required_requests(state)[0] == []
    report = state["final_report"]
    assert len(report.claims) == expected.count("SUPPORTED")
    assert report.underlying_cause is None
    assert state["verification"].leading_hypothesis_id is None
    assert "connectivity_verification" not in state
    assert current_connectivity_verification(state) is None
    with pytest.raises(ValueError, match="unambiguous"):
        current_connectivity_verification(state, required=True)
    for bundle in current_hypothesis_verifications(state):
        assert current_connectivity_verification(state, bundle.hypothesis_spec) == bundle.evaluation
    for claim in report.claims:
        bundle = next(b for b in state["hypothesis_verifications"] if b.hypothesis_spec.id == claim.hypothesis_id)
        assert claim.claim_class == "verified_failure_mode"
        assert claim.plan_result_id == bundle.evaluation.plan_result.id
        assert set(claim.check_result_ids) == set(bundle.evaluation.plan_result.check_result_ids)
        assert all(key.startswith("E-") for key in claim.evidence_ids)
    assert report.assessments == assessments


async def test_different_callers_have_distinct_exact_log_intents_in_deterministic_order():
    state, deps = await run_case(edges=(("beta", "storage"), ("alpha", "auth")))
    logs = [r for r in deps.attempts.values() if r.spec.signal == "log"]
    assert [r.spec.services for r in logs] == [("alpha",), ("beta",)]
    assert len(state["tool_calls"]) == 4 and len(state["final_report"].claims) == 2
    assert state["final_report"].diagnosis_level == "failure_mode"
    assert state["final_report"].underlying_cause is None


async def test_supported_hypothesis_cannot_stop_other_actionable_required_work():
    incident, backend = scenario(edges=(("alpha", "auth"), ("beta", "storage")))
    deps = runtime.Dependencies(backend)
    state = await execute(initial_state(incident), deps, [make_task(incident, "trace"),
        make_task(incident, "log").model_copy(update={"services": ("alpha",)})])
    assert [a.status for a in current_assessments(state)] == ["SUPPORTED", "UNRESOLVED"]
    update = graph.verify(state, Runtime(context=deps))
    assert update["verification"].decision == "targeted_followup"
    assert update["verification"].leading_hypothesis_id is None
    assert [r.intent.services for r in update["pending_tasks"]] == [("beta",)]
    state = await graph.investigate_graph().ainvoke({**state, **update}, context=deps)
    assert all(a.status == "SUPPORTED" for a in current_assessments(state))


@pytest.mark.parametrize("kind", ["failed", "unavailable"])
async def test_one_supported_and_one_blocked_has_consumer_scoped_limitations(kind):
    incident, backend = scenario(edges=(("alpha", "auth"), ("beta", "storage")))
    if kind == "unavailable":
        backend.seed_services = ("alpha",)
        del backend.materials[("log", ("beta",))]
    else:
        original = backend.acquire
        async def acquire(task):
            if task.signal == "log" and task.services == ("beta",):
                return AcquisitionResult(signal="log", projection=ToolResult(status="error"))
            return await original(task)
        backend.acquire = acquire
    deps = runtime.Dependencies(backend)
    state, _ = await run_investigation(initial_state(incident), deps)
    alpha, beta = current_assessments(state)
    assert alpha.status == "SUPPORTED" and alpha.limitations == ()
    assert beta.status == "UNRESOLVED"
    assert [l.reason for l in beta.limitations] == [f"required_acquisition_{kind}"]
    assert required_requests(state)[0] == []
    assert len(state["final_report"].claims) == 1 and state["final_report"].underlying_cause is None
    assert state["verification"].leading_hypothesis_id is None


async def test_budget_clip_leaves_other_hs_unresolved_without_unavailable_or_phantom_pending():
    state, _ = await run_case(edges=(("alpha", "auth"), ("beta", "storage")), budget=Budget(max_tool_calls=3))
    alpha, beta = current_assessments(state)
    assert alpha.status == "SUPPORTED" and not alpha.limitations
    assert beta.status == "UNRESOLVED"
    assert [l.reason for l in beta.limitations] == ["budget_prevented_required_acquisition"]
    assert all(r.intent.services != ("beta",) for r in state["intent_ledger"])
    assert all(r.status == "completed" for r in state["intent_ledger"])
    assert state["final_report"].status == "SUPPORTED" and state["verification"].decision == "stop_budget"


async def test_failed_background_metric_is_not_a_hypothesis_diagnostic_limitation():
    incident, backend = scenario()
    original = backend.acquire
    async def acquire(task):
        if task.signal == "metric":
            return AcquisitionResult(signal="metric", projection=ToolResult(status="error"))
        return await original(task)
    backend.acquire = acquire
    state, _ = await run_investigation(initial_state(incident), runtime.Dependencies(backend))
    assert all(a.status == "SUPPORTED" and not a.limitations for a in current_assessments(state))
    assert any("metric acquisition" in text for text in state["final_report"].limitations)


async def test_policy_bound_and_overflow_are_independent_of_tool_budget():
    edges = tuple(("frontend", f"service-{i}") for i in range(6))
    state, deps = await run_case(edges=edges)
    portfolio = current_hypothesis_portfolio(state)
    assert len(portfolio.active) == 4 and portfolio.overflow_count == 2
    assert len(state["hypothesis_verifications"]) == len(state["final_report"].claims) == 4
    assert len(state["tool_calls"]) == 3
    assert "service-4" not in portfolio.model_dump_json() and "service-5" not in portfolio.model_dump_json()
    assert state["final_report"].portfolio_overflow.count == 2
    smaller = runtime.refresh_acquisition_state(state, deps, hypothesis_policy=HypothesisAdmissionPolicy(max_active_hypotheses=2))
    assert len(smaller["hypothesis_verifications"]) == 2 and smaller["hypothesis_portfolio"].overflow_count == 4
    state.update(smaller)
    assert len(current_hypothesis_verifications(state)) == 2
    assert portfolio.policy.id != state["hypothesis_portfolio"].policy.id
    assert len(state["intent_ledger"]) == 3


async def test_HPS_stable_with_equivalent_new_generation_but_HSB_HRB_change():
    first, _ = await run_case(execution_id="generation-one")
    second, _ = await run_case(execution_id="generation-two")
    a, b = first["hypothesis_portfolio"], second["hypothesis_portfolio"]
    assert a.id == b.id and a.active == b.active
    assert a.state_basis.id != b.state_basis.id and a.resolution_basis.id != b.resolution_basis.id
    assert a.id == structural_id("HPS-", {"policy_id": a.policy.id, "active_hypothesis_ids": [h.id for h in a.active]})


async def test_accessors_do_not_observe_derive_evaluate_or_read_backend(monkeypatch):
    state, deps = await run_case()
    expected = current_hypothesis_portfolio(state)
    def forbidden(*args, **kwargs):
        pytest.fail("state-only consumer performed refresh work")
    import aegis_rca.hypotheses as candidates
    import aegis_rca.verification.evaluate as evaluate
    import aegis_rca.verification.connectivity as connectivity
    monkeypatch.setattr(trace_acquisition, "observe_spans", forbidden)
    monkeypatch.setattr(runtime, "resolve_trace_attempt", forbidden)
    monkeypatch.setattr(candidates, "derive_dependency_candidates", forbidden)
    monkeypatch.setattr(evaluate, "evaluate_check", forbidden)
    monkeypatch.setattr(connectivity, "evaluate_check", forbidden)
    monkeypatch.setattr(deps.backend, "acquire", forbidden)
    deps.backend.materials.clear()  # Source changes do not alter authoritative state freshness.
    assert current_hypothesis_portfolio(state) == expected
    assert len(current_hypothesis_verifications(state)) == 2
    assert len(current_assessments(state)) == 2
    assert graph.verify(state, Runtime(context=deps))["verification"].decision == "finalize"
    assert audit_and_report(state) == state["final_report"]


async def test_shared_trace_is_resolved_once_per_refresh_with_multiple_hypotheses(monkeypatch):
    state, deps = await run_case()
    calls = []
    original = runtime.resolve_trace_attempt
    def resolve(attempt, spec, source):
        calls.append(attempt.id)
        return original(attempt, spec, source)
    monkeypatch.setattr(runtime, "resolve_trace_attempt", resolve)
    update = runtime.refresh_acquisition_state(state, deps)
    assert len(calls) == len(set(calls)) == 1
    assert update["hypothesis_verifications"] == state["hypothesis_verifications"]
    assert update["hypothesis_portfolio"] == state["hypothesis_portfolio"]


async def test_completed_shared_intent_gains_new_consumers_without_reexecution():
    incident, backend = scenario()
    deps = runtime.Dependencies(backend)
    state = initial_state(incident)
    state.update(runtime.refresh_acquisition_state(state, deps,
        hypothesis_policy=HypothesisAdmissionPolicy(max_active_hypotheses=1)))
    state, _ = await run_investigation(state, deps)
    original, = [r for r in state["intent_ledger"] if r.intent.signal == "log"]
    assert len(original.needs[0].requested_for_hypothesis_ids) == 1
    calls = tuple(state["tool_calls"])
    state.update(runtime.refresh_acquisition_state(state, deps,
        hypothesis_policy=HypothesisAdmissionPolicy(max_active_hypotheses=2)))
    state.update(graph.verify(state, Runtime(context=deps)))
    shared, = [r for r in state["intent_ledger"] if r.intent.signal == "log"]
    assert shared.id == original.id and shared.committed_attempt_id == original.committed_attempt_id
    assert shared.status == "completed" and len(shared.needs[0].requested_for_hypothesis_ids) == 2
    assert state["pending_tasks"] == [] and tuple(state["tool_calls"]) == calls
    assert all(a.status == "SUPPORTED" for a in current_assessments(state))


async def test_global_deadline_rebuilds_current_multi_hs_generation_after_terminal_merge():
    import asyncio
    incident, backend = scenario()
    original = backend.acquire
    entered = asyncio.Event()
    async def acquire(task):
        if task.signal == "log":
            entered.set()
            await asyncio.Event().wait()
        return await original(task)
    backend.acquire = acquire
    deps = runtime.Dependencies(backend)
    state, events = await run_investigation(initial_state(incident), deps, deadline_seconds=1)
    assert entered.is_set() and events[-1]["nodes"] == ["deadline_stop"]
    assert len(current_hypothesis_portfolio(state).active) == len(current_hypothesis_verifications(state)) == 2
    log, = [a for a in state["attempt_records"] if a.error_class == "global_deadline"]
    row, = [r for r in state["intent_ledger"] if r.intent.signal == "log"]
    assert row.status == "failed" and row.committed_attempt_id == log.id
    for bundle, assessment in zip(current_hypothesis_verifications(state), current_assessments(state)):
        assert log.id in {r.coverage.acquisition_id for r in bundle.evaluation_basis.resolutions}
        assert assessment.status == "UNRESOLVED"
        assert [l.reason for l in assessment.limitations] == ["required_acquisition_failed"]
    assert state["final_report"].assessments == current_assessments(state)
    assert state["final_report"].underlying_cause is None


async def test_different_operations_collapse_to_one_edge_with_distinct_witness_basis():
    edges = (("frontend", "catalog"),)
    a, _ = await run_case(edges=edges, records=(span(name="OperationA"),))
    b, _ = await run_case(edges=edges, records=(span(name="OperationA"), span(name="OperationB", key="3")))
    assert a["hypothesis_portfolio"].id == b["hypothesis_portfolio"].id
    assert len(b["hypothesis_portfolio"].active) == 1 and b["hypothesis_portfolio"].active[0].operation is None
    assert a["hypothesis_portfolio"].resolution_basis.candidate_support_digest != b["hypothesis_portfolio"].resolution_basis.candidate_support_digest


@pytest.mark.parametrize("record", [span(kind="SERVER"), span(code=0), span(code=2)])
async def test_topology_only_facts_do_not_enter_portfolio(record):
    state, _ = await run_case(edges=(("frontend", "catalog"),), records=(record,))
    assert state["hypothesis_portfolio"].active == () and state["hypothesis_verifications"] == ()
    assert state["final_report"].status == "INCONCLUSIVE" and len(state["tool_calls"]) == 2
    assert state["rounds"] == 1


async def test_unrelated_neighbor_trace_does_not_stale_phv_or_create_hop_two():
    records = (span(), span(peer="shipping", code=0, key="4"))
    state, deps = await run_case(edges=(("frontend", "catalog"),), records=records)
    portfolio, bundles = state["hypothesis_portfolio"], state["hypothesis_verifications"]
    deps.backend.materials[("trace", ("shipping",))] = TraceResult(spans=(span(local="shipping", peer="database"),))
    state = await execute(state, deps, [make_task(state["incident"], "trace").model_copy(update={"services": ("shipping",)})])
    assert state["hypothesis_portfolio"] == portfolio and state["hypothesis_verifications"] == bundles
    assert all(h.caller == "frontend" and h.callee != "database" for h in portfolio.active)


async def test_portfolio_and_PHV_contract_reject_stale_or_incomplete_generations():
    state, _ = await run_case()
    p = state["hypothesis_portfolio"]
    for updates in ({"overflow_count": 1}, {"active": tuple(reversed(p.active))}, {"active": (p.active[0], p.active[0])}):
        with pytest.raises(ValidationError):
            HypothesisPortfolio.model_validate({**p.model_dump(), **updates})
    for rows in (state["hypothesis_verifications"][:1], tuple(reversed(state["hypothesis_verifications"])),
                 (*state["hypothesis_verifications"], state["hypothesis_verifications"][0])):
        with pytest.raises(ValueError, match="set/order"):
            current_hypothesis_verifications({**state, "hypothesis_verifications": rows})
    changed = {**state, "hypothesis_portfolio": p.model_copy(update={"state_basis": p.state_basis.model_copy(update={"scope_id": "stale"})})}
    with pytest.raises(ValueError, match="stale"):
        current_hypothesis_portfolio(changed)
    with pytest.raises(ValidationError):
        HypothesisAdmissionPolicy(max_active_hypotheses=0)
    with pytest.raises(ValidationError):
        HypothesisAdmissionPolicy(max_active_hypotheses=17)


async def test_LLM_provisional_cannot_replace_or_truncate_four_deterministic_assessments():
    from types import SimpleNamespace
    from aegis_rca.models.hypothesis import Hypothesis
    edges = tuple(("frontend", f"service-{i}") for i in range(4))
    state, deps = await run_case(edges=edges)
    active = state["hypothesis_portfolio"].active
    class Reasoner:
        async def propose_hypotheses(self, *args, **kwargs):
            return SimpleNamespace(hypotheses=(Hypothesis(id=active[0].id, claim="collision", affected_service="frontend",
                failure_mode="invented", status="supported"), Hypothesis(id="LLM-only", claim="unverified prose",
                affected_service="frontend", failure_mode="invented", status="supported")))
    deps = runtime.Dependencies(deps.backend, reasoner=Reasoner())
    updated = await graph.hypothesize(state, Runtime(context=deps))
    assert len(updated["hypotheses"]) == 4
    assert [h.id for h in updated["provisional_hypotheses"]] == ["LLM-only"]
    report = audit_and_report({**state, **updated})
    assert len(report.claims) == len(report.assessments) == 4
    assert report.provisional_hypotheses[0].status == "uncertain"


async def test_replay_recomputes_current_portfolio_and_PHVs_and_preserves_semantics():
    history, deps = await run_case(execution_id="portfolio-history")
    archive = export_snapshot_v2(history, deps)
    assert not {"hypothesis_portfolio", "hypothesis_verifications", "assessments"} & archive.model_dump().keys()
    replay, _ = await run_investigation(initial_state(history["incident"], execution_id="portfolio-replay"),
        runtime.Dependencies(RecordedReplayBackend(archive)))
    assert replay["hypothesis_portfolio"].id == history["hypothesis_portfolio"].id
    assert replay["hypothesis_portfolio"].state_basis.id != history["hypothesis_portfolio"].state_basis.id
    assert replay["hypothesis_portfolio"].resolution_basis.id != history["hypothesis_portfolio"].resolution_basis.id
    assert {r.id for r in replay["intent_ledger"]} == {r.id for r in history["intent_ledger"]}
    assert {t.id for t in replay["task_specs"]}.isdisjoint(t.id for t in history["task_specs"])
    assert len(replay["final_report"].claims) == 2 and replay["final_report"].underlying_cause is None
    assert [b.hypothesis_spec.id for b in replay["hypothesis_verifications"]] == [h.id for h in replay["hypothesis_portfolio"].active]
    restored = TypeAdapter(IncidentState).validate_json(TypeAdapter(IncidentState).dump_json(replay))
    assert current_hypothesis_verifications(restored) == replay["hypothesis_verifications"]


async def test_assessment_status_is_closed_and_claim_provenance_is_required():
    state, _ = await run_case(log_edges=())
    assessment = current_assessments(state)[0]
    assert assessment.status == "UNRESOLVED" and not assessment.limitations
    for status in ("unsupported", "failed", "unavailable", "NEUTRAL"):
        with pytest.raises(ValidationError):
            HypothesisAssessment.model_validate({**assessment.model_dump(), "status": status})
    with pytest.raises(ValidationError):
        ReportClaim(text="not proven", evidence_ids=(), claim_class="verified_failure_mode")
