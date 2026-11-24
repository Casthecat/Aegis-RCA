"""Native dependency candidates; topology permission is not diagnostic support."""
from .models.hypothesis import HypothesisSpec
from .investigation_scope import current_investigation_scope
from .service_scope import valid_service
from .verification.catalog import dependency_connectivity_plan
from .verification.evaluate import evaluate_check
from .trace_acquisition import ResolvedTraceAttempt


def derive_dependency_candidates(state, resolved_traces):
    scope = current_investigation_scope(state)
    seeds = set(state["incident"].seed_services)
    admitted = set(scope.admitted_services) if scope is not None else seeds
    window = state["incident"].window
    candidates = {}
    attempts = {a.id: a for a in state["attempt_records"]}
    specs = {t.id: t for t in state["task_specs"]}
    facts = []
    for resolved in resolved_traces.values():
        if (not isinstance(resolved, ResolvedTraceAttempt) or attempts.get(resolved.attempt.id) != resolved.attempt
                or specs.get(resolved.spec.id) != resolved.spec or resolved.attempt.execution_id != state["execution_id"]):
            raise ValueError("candidate source must be an authoritative resolved acquisition")
        if resolved.outcome != "verified":
            continue
        facts.extend(resolved.observations)
        for fact in resolved.observations:
            if fact.kind != "rpc_outcome" or fact.service not in seeds:
                continue
            peer = fact.value.peer_service
            if peer not in admitted or peer == fact.service or not valid_service(peer) or not valid_service(fact.service):
                continue
            subject = HypothesisSpec(caller=fact.service, callee=peer, window=window)
            candidates[subject.id] = subject
    supported, support = [], {}
    for subject in candidates.values():
        check = next(c for c in dependency_connectivity_plan(subject.caller, subject.callee, window).checks
                     if c.scope.signal == "trace")
        result = evaluate_check(check, observations=tuple(facts))
        if result.verdict == "SUPPORT":
            supported.append(subject)
            support[subject.id] = {"matched_ids": result.matched_ids,
                "witnesses": sorted((r.attempt.id, o.id) for r in resolved_traces.values()
                                    for o in r.observations if o.id in result.matched_ids)}
    return tuple(sorted(supported, key=lambda h: (h.caller, h.callee, h.id))), support


def dependency_candidates(state, resolved_traces):
    return derive_dependency_candidates(state, resolved_traces)[0]
