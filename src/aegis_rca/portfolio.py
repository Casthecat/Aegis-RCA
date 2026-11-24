"""Refresh-only candidate derivation; backend-free current portfolio access."""
from .models.portfolio import (
    HypothesisAdmissionPolicy, HypothesisPortfolio, HypothesisStateBasis,
    HypothesisResolutionBasis, HypothesisAcquisitionReference, HypothesisTraceResolution,
)
from .models.identity import structural_id
from .investigation_scope import current_investigation_scope, seed_trace_attempts


def hypothesis_state_basis(state, policy):
    scope = current_investigation_scope(state)
    fields = {"task_spec_id", "tool_status", "observation_binding", "source_material_digest", "admitted_observation_ids"}
    return HypothesisStateBasis(policy_id=policy.id, seed_services=tuple(sorted(set(state["incident"].seed_services))),
        window=state["incident"].window, scope_id=scope.id if scope else None, scope_basis_id=scope.basis.id if scope else None,
        acquisitions=tuple(HypothesisAcquisitionReference(task_spec_id=t.id, attempt_id=a.id,
            semantic_state_digest=structural_id("sha256-", a.model_dump(include=fields))) for a, t in seed_trace_attempts(state)))


def derive_hypothesis_portfolio(state, resolved_traces, policy=HypothesisAdmissionPolicy()):
    from .hypotheses import derive_dependency_candidates
    candidates, support = derive_dependency_candidates(state, resolved_traces)
    active = candidates[:policy.max_active_hypotheses]
    overflow = candidates[policy.max_active_hypotheses:]
    overflow_digest = structural_id("sha256-", {"hypothesis_ids": [h.id for h in overflow]})
    rb = HypothesisResolutionBasis(resolutions=tuple(HypothesisTraceResolution(attempt_id=a.id,
        outcome=resolved_traces[a.id].outcome, observation_count=len(resolved_traces[a.id].observations),
        observation_ids_digest=structural_id("sha256-", {"observation_ids": sorted(o.id for o in resolved_traces[a.id].observations)}))
        for a, _ in seed_trace_attempts(state)), eligible_count=len(candidates),
        eligible_hypothesis_ids_digest=structural_id("sha256-", {"hypothesis_ids": [h.id for h in candidates]}),
        active_hypothesis_ids=tuple(h.id for h in active), overflow_digest=overflow_digest,
        candidate_support_digest=structural_id("sha256-", {"support": support}))
    return HypothesisPortfolio(policy=policy, active=active, state_basis=hypothesis_state_basis(state, policy),
        resolution_basis=rb, overflow_count=len(overflow), overflow_digest=overflow_digest)


def current_hypothesis_portfolio(state):
    value = state.get("hypothesis_portfolio")
    if value is None:
        return None
    value = HypothesisPortfolio.model_validate(value.model_dump())
    if value.state_basis.id != hypothesis_state_basis(state, value.policy).id:
        raise ValueError("stale hypothesis portfolio")
    scope = current_investigation_scope(state)
    seeds = state["incident"].seed_services
    allowed = scope.admitted_services if scope else seeds
    if any(h.caller not in seeds or h.callee not in allowed or h.window != state["incident"].window for h in value.active):
        raise ValueError("stale hypothesis permission/window")
    return value
