from portfolio_helpers import single_phv, subject
"""P1.7a: diagnostic eligibility, generic plans and one persisted subject."""
from dataclasses import replace
from datetime import timedelta, timezone

import pytest
from pydantic import ValidationError

from aegis_rca import graph
from aegis_rca.fixtures import FixtureBackend, demo_incident, demo_traces
from aegis_rca.hypotheses import dependency_candidates
from aegis_rca.models.hypothesis import HypothesisSpec, Hypothesis
from aegis_rca.models.identity import structural_id
from aegis_rca.models.incident import TimeWindow
from aegis_rca.models.telemetry import TraceResult, LogResult, LogRecord, MetricResult
from aegis_rca.policy import connectivity_citations, form_hypotheses
from aegis_rca.report import audit_and_report
from aegis_rca.runner import run_investigation
from aegis_rca.runtime import Dependencies
from aegis_rca.service_scope import MAX_SEED_SERVICES, MAX_SERVICE_NAME_LENGTH
from aegis_rca.state import initial_state
from aegis_rca.trace_acquisition import resolve_trace_attempt
from aegis_rca.verification.aggregate import aggregate_plan
from aegis_rca.verification.catalog import connectivity_plan, dependency_connectivity_plan
from aegis_rca.verification.evaluate import evaluate_check
from aegis_rca.verification.persisted import current_connectivity_verification, PersistedConnectivityVerification
from aegis_rca.tools.recorded_replay import RecordedReplayBackend
from aegis_rca.tools.snapshot_v2 import export_snapshot_v2


def incident(seeds=("frontend",)):
    return demo_incident().model_copy(update={"query": "Investigate dependency availability", "seed_services": seeds})


def span(local="frontend", peer="catalog", code=14, kind="CLIENT", key="2", **updates):
    return demo_traces().spans[0].model_copy(update={
        "service": local, "name": "GetItem", "kind": kind, "span_id": key * 16,
        "attributes": {"peer.service": peer, "rpc.grpc.status_code": code}, **updates})


def backend(seeds=("frontend",), spans=None, caller="frontend", callee="catalog"):
    services = tuple(sorted(set(seeds)))
    traces = TraceResult(spans=(span(),) if spans is None else spans)
    logs = LogResult(logs=(LogRecord(id="generic-log", service=caller, timestamp=incident().window.start,
        severity="ERROR", body=f"dial {callee}:50051: connection refused",
        attributes={"peer.service": callee}, source="synthetic:generic"),))
    return FixtureBackend(records=(), seed_services=services, materials={
        ("trace", services): traces, ("log", services): logs, ("log", (caller,)): logs,
        ("metric", services): MetricResult()})


async def run(seeds=("frontend",), spans=None, **kwargs):
    deps = Dependencies(backend(seeds, spans, **kwargs))
    state, _ = await run_investigation(initial_state(incident(seeds)), deps)
    return state, deps


def resolved(state, deps):
    specs = {s.id: s for s in state["task_specs"]}
    return {a.id: resolve_trace_attempt(a, specs[a.task_spec_id], deps)
            for a in state["attempt_records"] if specs[a.task_spec_id].signal == "trace"}


async def test_frontend_catalog_supported_parameterized_report_and_caller_only_queries():
    state, deps = await run()
    bundle = single_phv(state)
    subject = HypothesisSpec(caller="frontend", callee="catalog", window=incident().window)
    assert bundle.hypothesis_spec == subject
    assert bundle.state_basis.plan_id == dependency_connectivity_plan("frontend", "catalog", subject.window).id
    assert bundle.evaluation.trace_check.verdict == bundle.evaluation.log_check.verdict == "SUPPORT"
    assert state["hypotheses"][0].id == subject.id
    report = state["final_report"]
    assert report.status == "SUPPORTED" and report.diagnosis_level == "failure_mode"
    assert report.underlying_cause is None
    assert report.summary == "frontend could not establish its catalog dependency."
    assert any("unresolved" in text for text in report.next_checks)
    assert "checkout" not in report.model_dump_json() and "payment" not in report.model_dump_json()
    assert state["rounds"] == 2 and len(state["tool_calls"]) == 3
    assert {t.services for t in state["task_specs"]} == {("frontend",)}
    assert all(a.ordinal == 1 for a in state["attempt_records"])
    assert state["investigation_scope"].admitted_services == ("catalog", "frontend")
    assert dependency_candidates(state, resolved(state, deps)) == (subject,)
    assert PersistedConnectivityVerification.model_validate_json(bundle.model_dump_json()) == bundle
    for result in (bundle.evaluation.trace_check, bundle.evaluation.log_check, bundle.evaluation.plan_result):
        assert type(result).model_validate_json(result.model_dump_json()) == result


@pytest.mark.parametrize("code", [0, 2])
async def test_success_and_grpc_unknown_authorize_topology_but_not_failure(code):
    state, deps = await run(spans=(span(code=code),))
    assert state["investigation_scope"].admitted_services == ("catalog", "frontend")
    assert dependency_candidates(state, resolved(state, deps)) == ()
    assert single_phv(state) is None and state["hypotheses"] == []
    assert state["final_report"].status == "INCONCLUSIVE"


@pytest.mark.parametrize("seeds,record", [
    (("frontend",), span(kind="SERVER")),
    (("frontend", "catalog"), span(local="catalog", peer="frontend", kind="SERVER")),
])
async def test_verified_server_only_topology_is_not_diagnostic(seeds, record):
    state, deps = await run(seeds=seeds, spans=(record,))
    generation = resolved(state, deps)
    assert all(r.outcome == "verified" and r.observations for r in generation.values())
    assert state["investigation_scope"].admitted_services == ("catalog", "frontend")
    assert dependency_candidates(state, generation) == ()
    assert single_phv(state) is None


async def test_two_original_seeds_can_form_failure_without_scope_expansion():
    state, deps = await run(seeds=("frontend", "checkout"), spans=(span(peer="checkout"),), callee="checkout")
    assert state["investigation_scope"].expansions == ()
    assert single_phv(state).hypothesis_spec == HypothesisSpec(
        caller="frontend", callee="checkout", window=incident().window)
    assert len(dependency_candidates(state, resolved(state, deps))) == 1
    assert state["final_report"].status == "SUPPORTED"


async def test_hop_two_caller_and_raw_peer_cannot_authorize_new_hypotheses():
    records = (span(code=0), span(local="catalog", peer="database", key="3"))
    state, deps = await run(spans=records)
    assert state["investigation_scope"].admitted_services == ("catalog", "frontend")
    assert dependency_candidates(state, resolved(state, deps)) == ()
    assert "database" not in state["investigation_scope"].admitted_services
    # Even a separately authorized neighbor acquisition cannot become an original-seed caller.
    from test_acquisition_partitions import execute
    from aegis_rca.policy import make_task
    deps.backend.materials[("trace", ("catalog",))] = TraceResult(spans=records)
    state = await execute(state, deps, [make_task(state["incident"], "trace").model_copy(update={"services": ("catalog",)})])
    assert any(r.spec.services == ("catalog",) and r.observations for r in resolved(state, deps).values())
    assert dependency_candidates(state, resolved(state, deps)) == ()


async def test_scope_freshness_absence_and_verified_binding_are_required():
    state, deps = await run()
    generation = resolved(state, deps)
    assert dependency_candidates({**state, "investigation_scope": None}, generation) == ()
    scope = state["investigation_scope"]
    stale = {**state, "investigation_scope": scope.model_copy(update={"seed_services": ("other",)})}
    with pytest.raises(ValueError):
        dependency_candidates(stale, generation)
    unverified = {key: replace(value, outcome="source_unavailable", observations=()) for key, value in generation.items()}
    assert dependency_candidates(state, unverified) == ()
    with pytest.raises(ValueError, match="another execution|authoritative"):
        dependency_candidates({**state, "execution_id": "other-execution"}, generation)
    both, deps = await run(seeds=("frontend", "catalog"))
    assert len(dependency_candidates({**both, "investigation_scope": None}, resolved(both, deps))) == 1


@pytest.mark.parametrize("record", [
    span(attributes={"peer.service": "catalog", "rpc.service": "oteldemo.PaymentService", "rpc.grpc.status_code": 14}),
    span(start=incident().window.end),
    span(peer="frontend"),
    span(attributes={"rpc.grpc.status_code": 14}),
])
async def test_ambiguous_outside_window_self_or_unresolved_edges_do_not_create_subject(record):
    state, deps = await run(spans=(record,))
    assert dependency_candidates(state, resolved(state, deps)) == ()
    assert single_phv(state) is None


def test_hypothesis_semantic_identity_omits_unconstrained_operation_and_prose():
    h = HypothesisSpec(caller="frontend", callee="catalog", window=incident().window)
    assert h.id == "HS-21db5fdd7eba7296f2a3966bcc82beedb3e2a7730c042fbfaee193e33974ca2e"
    assert h.id == structural_id("HS-", h.model_dump(exclude={"operation"}))
    assert h == HypothesisSpec(caller="frontend", callee="catalog", window=incident().window, operation=None)
    assert h.id != HypothesisSpec(caller="checkout", callee="payment", window=h.window).id
    assert h.id != HypothesisSpec(caller=h.caller, callee=h.callee, window=h.window, operation="GetItem").id
    shifted = TimeWindow(start=h.window.start.astimezone(timezone(timedelta(hours=5))),
                         end=h.window.end.astimezone(timezone(timedelta(hours=5))))
    assert h.id == HypothesisSpec(caller=h.caller, callee=h.callee, window=shifted).id
    projection = Hypothesis(id=h.id, claim="display one", affected_service=h.caller, failure_mode=h.failure_class)
    assert projection.model_copy(update={"claim": "display two"}).id == h.id
    assert HypothesisSpec.model_validate_json(h.model_dump_json()) == h
    with pytest.raises(ValidationError):
        h.caller = "other"
    for field in ("claim", "confidence", "evidence_ids", "attempt_id", "check_result_id", "status"):
        with pytest.raises(ValidationError):
            HypothesisSpec.model_validate({**h.model_dump(), field: "untrusted prose"})


@pytest.mark.parametrize("updates", [
    {"caller": " "}, {"callee": "x" * (MAX_SERVICE_NAME_LENGTH + 1)}, {"callee": "frontend"},
    {"schema_version": 2}, {"semantic_version": "2"}, {"kind": "ultimate_cause"},
    {"failure_class": "process_crash"}, {"operation": " "},
])
def test_hypothesis_rejects_invalid_or_out_of_scope_contracts(updates):
    with pytest.raises(ValidationError):
        HypothesisSpec(**{ "caller": "frontend", "callee": "catalog", "window": incident().window, **updates})


async def test_generic_factory_operation_restricts_trace_and_demo_is_same_factory():
    window = incident().window
    assert connectivity_plan(window) == dependency_connectivity_plan("checkout", "payment", window)
    state, deps = await run()
    facts = tuple(o for r in resolved(state, deps).values() for o in r.observations)
    for operation, verdict in ((None, "SUPPORT"), ("GetItem", "SUPPORT"), ("Other", "UNKNOWN")):
        plan = dependency_connectivity_plan("frontend", "catalog", window, operation)
        trace = next(c for c in plan.checks if c.scope.signal == "trace")
        log = next(c for c in plan.checks if c.scope.signal == "log")
        assert trace.scope.services == log.scope.services == ("frontend",)
        assert log.predicate.peer_service == trace.predicate.peer_service == "catalog"
        assert trace.predicate.span_kind == "CLIENT" and trace.predicate.rpc_status_names == ("UNAVAILABLE",)
        assert log.predicate.codes == ("connection_refused", "dns_resolution_failed")
        assert evaluate_check(trace, observations=facts).verdict == verdict


async def test_legacy_names_and_projection_prose_do_not_control_truth():
    state, _ = await run()
    before = current_connectivity_verification(state)
    changed = {**state, "hypotheses": [Hypothesis(id="H-connectivity", claim="callee crashed",
        affected_service="unrelated", failure_mode="payment_connection_failure", status="supported")]}
    assert current_connectivity_verification(changed) == before
    assert form_hypotheses(changed) == form_hypotheses(state)
    report = audit_and_report({**changed, "hypotheses": form_hypotheses(changed)})
    assert report.summary == state["final_report"].summary and report.underlying_cause is None
    plan = dependency_connectivity_plan("frontend", "catalog", incident().window)
    old = plan.model_copy(update={"failure_mode": "payment_connection_failure"})
    assert aggregate_plan(old, results=(before.trace_check, before.log_check)).status == "UNCERTAIN"


async def test_citations_filter_service_peer_code_and_window():
    state, _ = await run()
    h = single_phv(state).hypothesis_spec
    evidence = state["evidence"]
    expected = connectivity_citations(evidence, h)
    assert expected and all(key.startswith("E-") for key in expected)
    log = next(e for e in evidence if e.signal == "log")
    decoys = [log.model_copy(update={"id": "E-wrong-service", "service": "unrelated"}),
              log.model_copy(update={"id": "E-wrong-peer", "observation": log.observation.model_copy(update={"peer_service": "unrelated"})}),
              log.model_copy(update={"id": "E-outside", "timestamp": h.window.end}),
              log.model_copy(update={"id": "E-code", "observation": log.observation.model_copy(update={"code": "rpc_unavailable"})})]
    assert connectivity_citations([*evidence, *decoys], h) == expected


async def test_default_fixture_uses_generic_subject_without_changing_rounds_calls_citations():
    state, _ = await run_investigation(initial_state(demo_incident()), Dependencies(FixtureBackend()))
    h = single_phv(state).hypothesis_spec
    assert h == HypothesisSpec(caller="checkout", callee="payment", window=demo_incident().window)
    assert h.id == "HS-b97a36c2415b4dfb801632ecd40ca61a7e8e67242adb432f4b9c2c6d158fbfe6"
    report = state["final_report"]
    assert (report.rounds, report.tool_calls, report.status) == (2, 3, "SUPPORTED")
    assert report.claims[0].evidence_ids == ("E-T1", "E-L1")
    assert report.diagnosis_level == "failure_mode" and report.underlying_cause is None
    assert {s.services for s in state["task_specs"]} == {("checkout",)}


def test_demo_check_goldens_unchanged_and_plan_migration_is_semantic():
    plan = connectivity_plan(demo_incident().window)
    assert [c.id for c in plan.checks] == [
        "C-34f3b28fee067136807c099221e6ea5689865b5589b7d0eb5685c123f99d678d",
        "C-9ccc9797df659e559be397588ed8a5fe6a0a7ddec6f9d74e7a106b1ab6a2ef4a"]
    assert plan.id == "P-b8779552044abd90c1fcadc81aa14f098c382efa5839e52d8db48f9b473e3410"
    # The historical payload still hashes identically; it is no longer executable policy.
    assert plan.model_copy(update={"failure_mode": "payment_connection_failure"}).id == (
        "P-12a468e597d8558cd6dfd9ed088efc87611d6712082ab15ac8443ddd1ca6410c")


async def test_multiple_native_candidates_have_independent_current_verifications():
    records = (span(peer="catalog"), span(peer="auth", key="3"))
    first, deps = await run(spans=records)
    second, _ = await run(spans=tuple(reversed(records)))
    candidates = dependency_candidates(first, resolved(first, deps))
    assert [(h.caller, h.callee) for h in candidates] == [("frontend", "auth"), ("frontend", "catalog")]
    assert first["hypothesis_portfolio"].active == second["hypothesis_portfolio"].active == candidates
    assert len(first["hypotheses"]) == len(first["hypothesis_verifications"]) == 2
    assert current_connectivity_verification(first) is None  # No hidden winner.
    assert first["final_report"].status == "SUPPORTED"
    assert len(first["final_report"].claims) == 1  # Catalog has a log; auth remains unresolved.
    assert {t.services for t in first["task_specs"]} == {("frontend",)}


def test_triage_canonicalizes_generic_seeds_without_backend_assumptions():
    update = graph.triage(initial_state(incident(("frontend", "catalog", "frontend"))))
    assert update["scope"] is not None and update["incident"].seed_services == ("catalog", "frontend")
    assert graph.triage(initial_state(incident(tuple(f"s{i}" for i in range(MAX_SEED_SERVICES)))))["scope"]
    assert graph.triage(initial_state(incident(("x" * MAX_SERVICE_NAME_LENGTH,))))["scope"]


@pytest.mark.parametrize("seeds", [(), ("",), (" \t",), ("x" * (MAX_SERVICE_NAME_LENGTH + 1),),
                                  tuple(f"s{i}" for i in range(MAX_SEED_SERVICES + 1))])
async def test_invalid_seeds_remain_zero_query_invalid_scope(seeds):
    state, _ = await run_investigation(initial_state(incident(seeds)), Dependencies(FixtureBackend()))
    assert state["scope"] is None and state["tool_calls"] == []
    assert state["final_report"].termination_reason == "invalid_scope"


async def test_invalid_incident_window_remains_zero_query():
    value = incident()
    value = value.model_copy(update={"window": value.window.model_copy(update={"end": value.window.start})})
    state, _ = await run_investigation(initial_state(value), Dependencies(FixtureBackend()))
    assert state["tool_calls"] == [] and state["final_report"].termination_reason == "invalid_scope"


async def test_generic_snapshot_v2_replay_preserves_semantics_not_acquisition_identity():
    history, deps = await run()
    archive = export_snapshot_v2(history, deps)
    replay, _ = await run_investigation(initial_state(history["incident"], execution_id="generic-replay"),
                                       Dependencies(RecordedReplayBackend(archive)))
    assert replay["final_report"].status == "SUPPORTED"
    assert single_phv(replay).hypothesis_spec == single_phv(history).hypothesis_spec
    assert single_phv(replay).state_basis.plan_id == single_phv(history).state_basis.plan_id
    assert {t.id for t in replay["task_specs"]}.isdisjoint(t.id for t in history["task_specs"])
    assert {a.id for a in replay["attempt_records"]}.isdisjoint(a.id for a in history["attempt_records"])
    assert single_phv(replay).evaluation_basis.observation_ids == single_phv(history).evaluation_basis.observation_ids
    assert {t.services for t in replay["task_specs"]} == {("frontend",)}
