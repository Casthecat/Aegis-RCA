from portfolio_helpers import single_phv, subject
"""Synthetic P1.6a permissions: bounded one hop, no diagnostic or query expansion."""

import pytest
from pydantic import TypeAdapter, ValidationError

from aegis_rca import graph, runtime as execution, runner, trace_acquisition as native
from aegis_rca.fixtures import FixtureBackend, demo_evidence, demo_incident, demo_traces
from aegis_rca.investigation_scope import (
    DEFAULT_SCOPE_POLICY, current_investigation_scope, derive_investigation_scope,
    scope_acquisition_digest, seed_trace_attempts,
)
from aegis_rca.models.acquisition import AttemptIdentity, TaskSpec
from aegis_rca.models.acquisition_result import AcquisitionResult, SourceMetadata
from aegis_rca.models.evidence import ToolResult
from aegis_rca.models.hypothesis import Hypothesis
from aegis_rca.models.scope import InvestigationScope, ScopeExpansionPolicy
from aegis_rca.models.telemetry import TraceResult
from aegis_rca.policy import make_task
from aegis_rca.state import IncidentState, initial_state
from aegis_rca.verification.persisted import current_connectivity_verification, state_verification_basis


def span(peer="payment", *, service="checkout", kind="CLIENT", code=14, index=1, attributes=None):
    values = {"rpc.grpc.status_code": code}
    if peer is not None:
        values["peer.service"] = peer
    if attributes:
        values.update(attributes)
    return demo_traces().spans[0].model_copy(update={"span_id": f"{index:016x}", "name": "rpc/Call",
        "service": service, "kind": kind, "status": "OK" if code == 0 else "ERROR", "attributes": values})


def acquisition(spans, *, seeds=("checkout",), services=None, execution_id="scope-test"):
    incident = demo_incident().model_copy(update={"seed_services": seeds})
    state = initial_state(incident, execution_id=execution_id)
    backend = FixtureBackend(records=(), traces=TraceResult(spans=tuple(spans)))
    task = make_task(incident, "trace").model_copy(update={"services": services or seeds})
    spec = TaskSpec(signal="trace", services=task.services, window=task.window, query_template_id="fixture.select.v1")
    identity = AttemptIdentity(execution_id=execution_id, task_spec_id=spec.id, ordinal=1)
    reserved = execution.ReservedAttempt(identity, task, spec, 1, task.id)
    deps = execution.Dependencies(backend)
    completion = deps.acquisitions.commit(execution.finalize_acquisition(reserved, backend,
        AcquisitionResult(signal="trace", projection=ToolResult(status="empty"), material=backend.traces,
            source=SourceMetadata(rpc_service_map={"oteldemo.PaymentService": "payment"},
                                  backend="fixture", synthetic=True), projection_origin="fixture")))
    attempt, call = completion.attempt, completion.tool_call
    state.update(task_specs=[spec], attempt_records=[attempt], tool_calls=[call])
    return state, backend


def persisted(spans, *, policy=None, **kwargs):
    state, backend = acquisition(spans, **kwargs)
    state.update(execution.refresh_acquisition_state(state, backend, scope_policy=policy))
    return state, backend


@pytest.mark.parametrize("code", [0, 2, 14])
def test_resolved_neighbor_admission_does_not_require_a_failure(code):
    state, _ = persisted((span(code=code),))
    scope = current_investigation_scope(state)
    assert state["incident"].seed_services == ("checkout",)
    assert scope.seed_services == ("checkout",)
    assert scope.admitted_services == ("checkout", "payment")
    edge = scope.expansions[0]
    assert (edge.caller, edge.callee, edge.from_seed_service, edge.hop) == ("checkout", "payment", "checkout", 1)
    assert edge.canonical_witness_attempt_id == state["attempt_records"][0].id
    assert edge.canonical_witness_observation_id in state["attempt_records"][0].admitted_observation_ids
    if code != 14:
        assert current_connectivity_verification(state) is None


@pytest.mark.parametrize("kind,service,peer,seeds,services,expected", [
    ("CLIENT", "checkout", "payment", ("checkout",), ("checkout",), ("checkout", "payment")),
    ("SERVER", "checkout", "payment", ("checkout",), ("checkout",), ("payment", "checkout")),
    ("SERVER", "payment", "checkout", ("checkout",), ("checkout", "payment"), ("checkout", "payment")),
])
def test_existing_client_server_orientation_is_reused(kind, service, peer, seeds, services, expected):
    state, _ = persisted((span(peer, service=service, kind=kind),), seeds=seeds, services=services)
    record = state["investigation_scope"].expansions[0]
    assert (record.caller, record.callee) == expected
    assert record.discovered_service == "payment"


def test_verified_multiservice_client_server_representations_share_semantic_expansion(monkeypatch):
    scopes, edge_keys = [], []
    for label, record in (
        ("client", span("payment", service="checkout", kind="CLIENT")),
        ("server", span("checkout", service="payment", kind="SERVER")),
    ):
        state, backend = acquisition((record,), services=("checkout", "payment"), execution_id=label)
        attempt, spec = seed_trace_attempts(state)[0]
        resolved = native.resolve_trace_attempt(attempt, spec, backend)
        assert resolved.outcome == "verified"
        assert {o.id for o in resolved.observations} == set(attempt.admitted_observation_ids)
        edge_keys.append(resolved.observations[0].correlation.rpc_edge_key)
        before = (tuple(state["task_specs"]), tuple(state["attempt_records"]), tuple(state["tool_calls"]))
        monkeypatch.setattr(backend, "query", lambda *a, **kw: pytest.fail("scope admission must not query"))
        state.update(execution.refresh_acquisition_state(state, backend))
        assert before == (tuple(state["task_specs"]), tuple(state["attempt_records"]), tuple(state["tool_calls"]))
        scope = current_investigation_scope(state)
        assert scope.seed_services == ("checkout",)
        assert scope.admitted_services == ("checkout", "payment")
        expansion = scope.expansions[0]
        assert (expansion.caller, expansion.callee, expansion.from_seed_service, expansion.hop) == (
            "checkout", "payment", "checkout", 1)
        scopes.append(scope)
    client, server = scopes
    assert edge_keys[0] == edge_keys[1]
    assert client.expansions[0].id == server.expansions[0].id
    assert client.id == server.id
    assert client.basis.id != server.basis.id
    assert client.expansions[0].canonical_witness_observation_id != server.expansions[0].canonical_witness_observation_id
    assert client.expansions[0].canonical_witness_attempt_id != server.expansions[0].canonical_witness_attempt_id


@pytest.mark.parametrize("service,peer,kind", [
    ("payment", "accounting", "CLIENT"),
    ("payment", "accounting", "SERVER"),
    ("accounting", "payment", "SERVER"),
])
def test_verified_multiservice_nonseed_endpoints_never_admit_hop_two(service, peer, kind, monkeypatch):
    state, backend = acquisition((span(), span(peer, service=service, kind=kind, index=2)),
                                 services=("checkout", "payment", "accounting"))
    attempt, spec = seed_trace_attempts(state)[0]
    resolved = native.resolve_trace_attempt(attempt, spec, backend)
    assert resolved.outcome == "verified" and len(resolved.observations) == 2
    assert {o.id for o in resolved.observations} == set(attempt.admitted_observation_ids)
    monkeypatch.setattr(backend, "query", lambda *a, **kw: pytest.fail("no neighbor query or retry"))
    for _ in range(2):
        state.update(execution.refresh_acquisition_state(state, backend))
        scope = current_investigation_scope(state)
        assert scope.admitted_services == ("checkout", "payment")
        assert scope.discovered_neighbor_count == 1 and scope.overflow_count == 0
        assert len(state["task_specs"]) == len(state["attempt_records"]) == len(state["tool_calls"]) == 1
        assert state["attempt_records"][0].ordinal == 1


@pytest.mark.parametrize("record", [
    span(None), span(None, kind="SERVER", attributes={"rpc.service": "oteldemo.PaymentService"}),
    span("payment", attributes={"service.peer.name": "shipping"}), span("checkout"),
    span("payment", kind="INTERNAL"),
])
def test_unresolved_ambiguous_self_loop_and_non_rpc_do_not_expand(record):
    state, _ = persisted((record,))
    assert state["investigation_scope"].admitted_services == ("checkout",)
    assert state["investigation_scope"].expansions == ()


def test_second_hop_never_uses_mutable_admitted_scope():
    state, backend = persisted((span(), span("accounting", service="payment", index=2)),
                               services=("checkout", "payment"))
    assert state["investigation_scope"].admitted_services == ("checkout", "payment")
    # Refresh over the same now-expanded permission scope must not use payment as origin.
    state.update(execution.refresh_acquisition_state(state, backend))
    assert state["investigation_scope"].admitted_services == ("checkout", "payment")
    assert "accounting" not in state["investigation_scope"].admitted_services


def test_scope_is_acquisition_native_for_non_connectivity_seed():
    state, _ = persisted((span("inventory", service="shipping"),), seeds=("shipping",))
    assert state["investigation_scope"].admitted_services == ("inventory", "shipping")
    assert single_phv(state).hypothesis_spec.caller == "shipping"
    assert single_phv(state).evaluation.trace_check.verdict == "SUPPORT"
    assert single_phv(state).evaluation.plan_result.status == "UNCERTAIN"
    assert seed_trace_attempts(state)


def test_multiple_original_seeds_have_one_hop_each_without_internal_expansion():
    state, _ = persisted((span("payment"), span("inventory", service="shipping", index=2),
                          span("shipping", index=3)), seeds=("shipping", "checkout"))
    scope = state["investigation_scope"]
    assert scope.seed_services == ("checkout", "shipping")
    assert [e.discovered_service for e in scope.expansions] == ["inventory", "payment"]
    assert {e.from_seed_service for e in scope.expansions} == {"checkout", "shipping"}
    assert scope.discovered_neighbor_count == 2


def test_deterministic_bounded_order_not_voting_or_error_ranking():
    records = [span("zebra", index=i) for i in range(1, 21)] + [span("alpha", code=0, index=21), span("payment", index=22)]
    policy = ScopeExpansionPolicy(max_admitted_neighbors=2)
    first, _ = persisted(records, policy=policy)
    second, _ = persisted(tuple(reversed(records)), policy=policy)
    a, b = first["investigation_scope"], second["investigation_scope"]
    assert a.admitted_services == ("alpha", "checkout", "payment")
    assert a.id == b.id and [e.id for e in a.expansions] == [e.id for e in b.expansions]
    assert a.overflow_count == 1 and a.overflow_digest == b.overflow_digest
    assert a.expansions == b.expansions


def test_large_peer_fanout_and_witness_multiplicity_have_bounded_persisted_size():
    policy = ScopeExpansionPolicy(max_admitted_neighbors=2)
    small, _ = persisted([span(f"peer{i:04d}", index=i + 1) for i in range(100)], policy=policy)
    large, _ = persisted([span(f"peer{i:04d}", index=i + 1) for i in range(2000)], policy=policy)
    first, second = small["investigation_scope"], large["investigation_scope"]
    assert len(second.expansions) == 2 and second.overflow_count == 1998
    assert second.id == first.id  # overflow is provenance, not an admitted permission change
    assert second.basis.id != first.basis.id
    assert abs(len(second.model_dump_json()) - len(first.model_dump_json())) < 10
    assert len(second.model_dump_json()) < 3000
    encoded = second.model_dump_json()
    assert "peer1999" not in encoded and "overflow_services" not in encoded
    repeated, _ = persisted([span(index=i + 1) for i in range(2000)], policy=policy)
    scope = repeated["investigation_scope"]
    assert len(scope.expansions) == 1 and scope.discovered_neighbor_count == 1
    assert len(scope.model_dump_json()) < 2000


def test_duplicate_witnesses_preserve_semantic_scope_but_new_generation_changes_basis():
    first, _ = persisted((span(),))
    second, _ = persisted((span(), span(index=2)))
    third, _ = persisted((span(),), execution_id="another-execution")
    scopes = [state["investigation_scope"] for state in (first, second, third)]
    assert len({s.id for s in scopes}) == 1 and len({s.expansions[0].id for s in scopes}) == 1
    assert len({s.basis.id for s in scopes}) == 3
    assert scopes[0].expansions[0].support_set_digest != scopes[1].expansions[0].support_set_digest


def test_duplicate_and_reordered_resolved_facts_are_idempotent():
    state, backend = acquisition((span(), span(index=2)))
    attempt, spec = seed_trace_attempts(state)[0]
    resolved = native.resolve_trace_attempt(attempt, spec, backend)
    reordered = native.ResolvedTraceAttempt(attempt, spec, "verified", tuple(reversed(resolved.observations)) * 2)
    assert derive_investigation_scope(state, {attempt.id: resolved}) == derive_investigation_scope(state, {attempt.id: reordered})


def test_semantic_edge_selection_is_independent_of_witness_order():
    records = (span(), span("payment", kind="SERVER", index=2))
    state, _ = persisted(records)
    expanded, _ = persisted((*records, span("payment", kind="SERVER", index=3)))
    first, second = state["investigation_scope"], expanded["investigation_scope"]
    assert first.id == second.id
    assert (first.expansions[0].caller, first.expansions[0].callee) == ("checkout", "payment")


def test_policy_identity_binds_parameters_and_is_separate_from_task_identity():
    state, backend = persisted((span(),))
    old = state["investigation_scope"]
    policy = ScopeExpansionPolicy(max_admitted_neighbors=1)
    updated = execution.refresh_acquisition_state(state, backend, scope_policy=policy)
    new = updated["investigation_scope"]
    assert new.admitted_services == old.admitted_services
    assert old.policy.id != policy.id and old.basis.id != new.basis.id
    assert old.expansions[0].id == new.expansions[0].id
    assert single_phv(updated) == single_phv(state)
    assert new.policy.id != ScopeExpansionPolicy(max_admitted_neighbors=1, max_service_name_length=128).id


def test_zero_neighbor_budget_and_identifier_bounds_are_explicit():
    state, _ = persisted((span(),), policy=ScopeExpansionPolicy(max_admitted_neighbors=0))
    assert state["investigation_scope"].expansions == () and state["investigation_scope"].overflow_count == 1
    state, _ = persisted((span("x" * 300),))
    scope = state["investigation_scope"]
    assert scope.expansions == () and scope.excluded_identifier_count == 1
    assert "x" * 300 not in scope.model_dump_json()
    with pytest.raises(ValidationError):
        ScopeExpansionPolicy(max_admitted_neighbors=65)


@pytest.mark.parametrize("mode", ["digest", "subset", "superset", "unavailable"])
def test_failed_exact_native_binding_produces_no_expansion(mode, monkeypatch):
    state, backend = persisted((span(),))
    old = state["investigation_scope"]
    original = native.observe_spans
    if mode == "digest":
        part = backend._partition_reader.partition_for(state["attempt_records"][0].id)
        monkeypatch.setattr(backend._partition_reader, "partition_for",
                            lambda _: part.model_copy(update={"material": TraceResult()}))
    elif mode == "subset":
        monkeypatch.setattr(native, "observe_spans", lambda *a, **kw: ())
    elif mode == "superset":
        monkeypatch.setattr(native, "observe_spans", lambda *a, **kw: original(*a, **kw) +
                            (original(*a, **kw)[0].model_copy(update={"id": "O-trace-" + "a" * 20}),))
    else:
        monkeypatch.setattr(backend._partition_reader, "partition_for", lambda _: None)
    historical = state["attempt_records"][0].model_dump_json()
    assert current_investigation_scope(state) == old  # backend change is not a live scope update
    update = execution.refresh_acquisition_state(state, backend)
    assert update["investigation_scope"].expansions == ()
    assert update["investigation_scope"].basis.id != old.basis.id
    assert state["attempt_records"][0].model_dump_json() == historical


def test_legacy_metric_and_hypothesis_prose_cannot_expand_or_stale_scope():
    state, backend = persisted(())
    scope = state["investigation_scope"]
    state["evidence"] = list(demo_evidence())
    state["reasoning_notes"] = ["LLM says investigate accounting, shipping and payment"]
    state["hypotheses"] = [Hypothesis(id="H-suggestion", affected_service="accounting",
        failure_mode="suggested", claim="LLM says accounting caused the incident")]
    state["incident"] = state["incident"].model_copy(update={"query": "payment is surely faulty"})
    assert current_investigation_scope(state) == scope
    assert execution.refresh_acquisition_state(state, backend)["investigation_scope"] == scope
    # A metric acquisition doesn't supply native trace topology, even when its scope is seed-side.
    metric = TaskSpec(signal="metric", services=("checkout",), window=state["incident"].window, query_template_id="metric")
    task = make_task(state["incident"], "metric")
    identity = AttemptIdentity(execution_id=state["execution_id"], task_spec_id=metric.id, ordinal=1)
    attempt, call = execution.finish_attempt(execution.ReservedAttempt(identity, task, metric, 1, task.id),
                                             backend, ToolResult(status="empty"))
    state["task_specs"].append(metric); state["attempt_records"].append(attempt); state["tool_calls"].append(call)
    assert current_investigation_scope(state) == scope


def test_scope_derivation_itself_never_creates_tasks_attempts_or_queries(monkeypatch):
    state, backend = acquisition((span(),))
    before = ([t.model_dump() for t in state["task_specs"]], [a.model_dump() for a in state["attempt_records"]])
    monkeypatch.setattr(backend, "query", lambda *a, **kw: pytest.fail("scope must not query"))
    result = execution.refresh_acquisition_state(state, backend)
    assert result["investigation_scope"].admitted_services == ("checkout", "payment")
    assert before == ([t.model_dump() for t in state["task_specs"]], [a.model_dump() for a in state["attempt_records"]])


def test_persistence_boundary_resolves_native_source_only_once_for_both_consumers(monkeypatch):
    state, backend = acquisition((span(),))
    calls, productions = [], []
    original_source, original_observe = backend._partition_reader.partition_for, native.observe_spans
    def source(attempt_id):
        calls.append(1)
        return original_source(attempt_id)
    def observe(*args, **kwargs):
        productions.append(1)
        return original_observe(*args, **kwargs)
    monkeypatch.setattr(backend._partition_reader, "partition_for", source)
    monkeypatch.setattr(native, "observe_spans", observe)
    result = execution.refresh_acquisition_state(state, backend)
    assert calls == productions == [1]
    assert single_phv(result).evaluation.trace_check.verdict == "SUPPORT"
    assert result["investigation_scope"].expansions


def test_state_freshness_and_roundtrip_without_backend_or_topology(monkeypatch):
    state, backend = persisted((span(),))
    scope = state["investigation_scope"]
    adapter = TypeAdapter(IncidentState)
    restored = adapter.validate_json(adapter.dump_json(state))
    import aegis_rca.investigation_scope as scope_module
    def forbidden(*args, **kwargs):
        raise AssertionError("scope read must not resolve or extract topology")
    monkeypatch.setattr(native, "resolve_trace_attempt", forbidden)
    monkeypatch.setattr(backend, "query", forbidden)
    monkeypatch.setattr(backend, "trace_source", forbidden)
    monkeypatch.setattr(scope_module, "rpc_edge_endpoints", forbidden)
    monkeypatch.setattr(scope_module, "derive_investigation_scope", forbidden)
    assert current_investigation_scope(restored) == scope
    execution.validate_execution_state(restored)
    assert InvestigationScope.model_validate_json(scope.model_dump_json()).id == scope.id
    old = state["attempt_records"][0]
    changed = {**state, "attempt_records": [old.model_copy(update={"source_material_digest": "sha256-" + "1" * 64})]}
    with pytest.raises(ValueError, match="stale investigation scope"):
        current_investigation_scope(changed)
    changed = {**state, "incident": state["incident"].model_copy(update={"seed_services": ("shipping",)})}
    with pytest.raises(ValueError, match="stale investigation scope"):
        current_investigation_scope(changed)
    changed = {**state, "attempt_records": [old.model_copy(update={"warnings": ("display",)})]}
    assert current_investigation_scope(changed) == scope
    assert current_investigation_scope(initial_state(demo_incident())) is None


def test_scope_change_is_independent_of_connectivity_vbs_vbe_pr():
    state, backend = persisted((span(),))
    basis = state_verification_basis(state, subject(state)).id
    before = single_phv(state)
    update = execution.refresh_acquisition_state(state, backend,
        scope_policy=ScopeExpansionPolicy(max_admitted_neighbors=0))
    state["investigation_scope"] = update["investigation_scope"]
    assert state_verification_basis(state, subject(state)).id == basis
    with pytest.raises(ValueError, match="stale hypothesis portfolio"):
        current_connectivity_verification(state)
    state.update(update)
    assert current_connectivity_verification(state) is None
    assert before is not None  # Revocation clears the selected facade, not its historical value.


@pytest.mark.parametrize("code,status", [(14, "SUPPORTED"), (2, "INCONCLUSIVE"), (0, "INCONCLUSIVE")])
async def test_workflow_topology_permission_is_not_diagnosis_or_expanded_query(code, status):
    backend = FixtureBackend(traces=TraceResult(spans=(span(code=code),)))
    state, _ = await runner.run_investigation(initial_state(demo_incident()), execution.Dependencies(backend))
    assert state["final_report"].status == status
    assert current_investigation_scope(state).admitted_services == ("checkout", "payment")
    assert all(t.services == ("checkout",) for t in state["task_specs"])
    assert len(state["tool_calls"]) == (3 if code == 14 else 2) and all(a.ordinal == 1 for a in state["attempt_records"])
    if code == 14:
        assert state["rounds"] == 2
        assert state["final_report"].claims[0].evidence_ids == ("E-T1", "E-L1")


async def test_deadline_persists_scope_after_attempt_merges(monkeypatch):
    import asyncio
    class Backend(FixtureBackend):
        async def query(self, task):
            if task.signal == "trace":
                await asyncio.Event().wait()
            return await super().query(task)
    original = runner.audit_and_report
    def report(state):
        assert any(a.error_class == "global_deadline" for a in state["attempt_records"])
        assert current_investigation_scope(state).admitted_services == ("checkout",)
        return original(state)
    monkeypatch.setattr(runner, "audit_and_report", report)
    state, _ = await runner.run_investigation(initial_state(demo_incident()), execution.Dependencies(Backend()), deadline_seconds=0.05)
    assert state["investigation_scope"] is not None


async def test_runtime_identity_goldens_with_p17_plan_migration():
    state, _ = await runner.run_investigation(initial_state(demo_incident(), execution_id="p16a-golden"),
                                             execution.Dependencies(FixtureBackend()))
    bundle = single_phv(state)
    assert bundle.state_basis.id == "VBS-d7dd283b800bd0b4a4c6f504310b36860e28db8ccc7f5c52fe3c86618078dfdc"
    assert bundle.evaluation_basis.id == "VBE-b01c93039a4ad22ae42e9d6aaa35ed83481107af290d21fc0e30e59bfe7b3de4"
    assert bundle.evaluation.trace_check.id == "CR-45ee7a18b4a6d372c9336405e76d37dc261a03e10c052ad584f54954abdf53f4"
    assert bundle.evaluation.log_check.id == "CR-7efd90190f5b3be3a53382287042f30df71c1d956652f9f8a22a0faababe03ba"
    assert bundle.evaluation.plan_result.id == "PR-b2544dbaa447410f81c9e39fa5ff464120a6ccbaf36b593b08f36cd436d75f0f"
    assert sorted(t.id for t in state["task_specs"]) == [
        "T-1b2766336940c98744c8226297da12c07cf0ac18c42adc0dcdc80807b99a6caa",
        "T-b26df642516c0a47ba96502f7d8b5cb3a15d22eb596794600cb560389a982970",
        "T-ec204b2730d15ff0c918a8d0575d108bde4003f26563a4e2b3d625a13fbe5258"]
    assert sorted(a.id for a in state["attempt_records"]) == [
        "A-392e68280c27443923e4929b6efed3c9052bf3b377c9b90c2e937564d9d28f88",
        "A-5f883376563ca50e96f9c193ed2b45093695fa96b7f61c2c50c9efbc5d409d28",
        "A-c6d850da6c8c14e1d7f69cfdf86758a780ec1ff48029811192812f6370af34fb"]


@pytest.mark.parametrize("change", ["hop", "seed", "witness", "bound", "overflow", "extra"])
def test_scope_contract_rejects_inconsistent_or_unbounded_artifacts(change):
    state, _ = persisted((span(),))
    payload = state["investigation_scope"].model_dump()
    if change == "hop":
        payload["expansions"][0]["hop"] = 2
    elif change == "seed":
        payload["seed_services"] = ("shipping",)
    elif change == "witness":
        payload["expansions"][0]["canonical_witness_observation_id"] = "E-T1"
    elif change == "bound":
        payload["expansions"] = payload["expansions"] * 65
    elif change == "overflow":
        payload["overflow_count"] = 1
    else:
        payload["overflow_services"] = ["unbounded-list"]
    with pytest.raises(ValidationError):
        InvestigationScope.model_validate(payload)


def test_scope_accessor_rejects_witness_from_another_acquisition():
    state, _ = persisted((span(),))
    scope = state["investigation_scope"]
    record = scope.expansions[0].model_copy(update={"canonical_witness_attempt_id": "A-" + "f" * 64})
    changed = {**state, "investigation_scope": scope.model_copy(update={"expansions": (record,)})}
    with pytest.raises(ValueError, match="scope witness"):
        current_investigation_scope(changed)


def test_invalid_directed_edge_key_cannot_expand():
    state, backend = acquisition((span(),))
    attempt, spec = seed_trace_attempts(state)[0]
    resolved = native.resolve_trace_attempt(attempt, spec, backend)
    fact = resolved.observations[0]
    damaged = fact.model_copy(update={"correlation": fact.correlation.model_copy(update={"rpc_edge_key": None})})
    generation = native.ResolvedTraceAttempt(attempt, spec, "verified", (damaged,))
    assert derive_investigation_scope(state, {attempt.id: generation}).expansions == ()


def test_expanded_service_acquisition_is_not_automatically_a_seed_or_connectivity_input():
    state, _ = persisted((span(),))
    other, _ = acquisition((span("accounting", service="payment"),), services=("payment",))
    state["task_specs"].extend(other["task_specs"])
    state["attempt_records"].extend(other["attempt_records"])
    assert current_investigation_scope(state).admitted_services == ("checkout", "payment")
    assert current_connectivity_verification(state).trace_check.verdict == "SUPPORT"
    assert len(seed_trace_attempts(state)) == 1
