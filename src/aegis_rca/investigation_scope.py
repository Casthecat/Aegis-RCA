"""Deterministic one-hop permissions from verified native trace acquisitions."""

from .models.identity import structural_id
from .models.scope import InvestigationScope, ScopeDerivationBasis, ScopeExpansionPolicy, ScopeExpansionRecord
from .observations.entity import directed_rpc_edge_key, rpc_edge_endpoints
from .trace_acquisition import ResolvedTraceAttempt


DEFAULT_SCOPE_POLICY = ScopeExpansionPolicy()


def seed_trace_attempts(state):
    seeds, window = set(state["incident"].seed_services), state["incident"].window
    specs = {spec.id: spec for spec in state.get("task_specs", ())}
    selected = []
    for attempt in state.get("attempt_records", ()):
        spec = specs.get(attempt.task_spec_id)
        if spec is None:
            raise ValueError("attempt must reference a saved TaskSpec")
        if (spec.signal == "trace" and seeds & set(spec.services)
                and spec.window.start < window.end and window.start < spec.window.end):
            if attempt.execution_id != state["execution_id"]:
                raise ValueError("seed trace attempt belongs to another execution")
            selected.append((attempt, spec))
    return tuple(sorted(selected, key=lambda pair: pair[0].id))


def scope_acquisition_digest(state, policy=DEFAULT_SCOPE_POLICY):
    """State-only dependency digest; no backend, topology extraction or verification."""
    fields = {"task_spec_id", "tool_status", "observation_binding", "source_material_digest",
              "admitted_observation_ids"}
    return structural_id("sha256-", {"seed_services": sorted(set(state["incident"].seed_services)),
        "window": state["incident"].window.model_dump(), "policy_id": policy.id,
        "attempts": [{"attempt_id": a.id, "task_spec_id": t.id,
                      "state": a.model_dump(include=fields)} for a, t in seed_trace_attempts(state)]})


def derive_investigation_scope(state, resolved_traces: dict[str, ResolvedTraceAttempt], policy=DEFAULT_SCOPE_POLICY):
    """Derive permissions, never hypotheses, tool requests, or mutable hop origins."""
    seeds, window = set(state["incident"].seed_services), state["incident"].window
    support, rejected, resolutions = {}, set(), []
    for attempt, spec in seed_trace_attempts(state):
        resolved = resolved_traces[attempt.id]
        if not isinstance(resolved, ResolvedTraceAttempt) or resolved.attempt != attempt or resolved.spec != spec:
            raise ValueError("scope requires the current native resolved acquisition generation")
        resolutions.append({"attempt_id": attempt.id, "outcome": resolved.outcome,
                            "observation_ids": sorted({o.id for o in resolved.observations})})
        for fact in resolved.observations:
            if (fact.kind != "rpc_outcome"
                    or not window.start <= fact.timestamp < window.end
                    or fact.quality.ambiguous or fact.quality.peer_resolution_method == "unresolved"):
                continue
            edge = rpc_edge_endpoints(fact.service, fact.value.span_kind, fact.value.peer_service)
            if edge is None or fact.correlation.rpc_edge_key != directed_rpc_edge_key(*edge, fact.value.operation):
                continue
            caller, callee = edge
            # Seed membership belongs to the directed endpoints, not fact.service:
            # a payment SERVER span can represent checkout -> payment.
            if caller == callee or (caller in seeds) == (callee in seeds):
                continue
            if any(not name.strip() or len(name) > policy.max_service_name_length for name in edge):
                rejected.add(edge)
                continue
            seed, neighbor = (caller, callee) if caller in seeds else (callee, caller)
            key = (neighbor, seed, caller, callee)
            support.setdefault(key, set()).add((fact.id, attempt.id))
    # Semantic edge selection comes before witness selection, so a duplicate
    # span cannot change the chosen seed/direction by winning an O-ID tie-break.
    candidates = {}
    for key in sorted(support):
        candidates.setdefault(key[0], key)
    names = sorted(candidates)
    admitted = names[:policy.max_admitted_neighbors]
    overflow = names[policy.max_admitted_neighbors:]
    expansions = []
    for neighbor in admitted:
        key = candidates[neighbor]
        witnesses = sorted(support[key])
        observation_id, attempt_id = witnesses[0]
        expansions.append(ScopeExpansionRecord(discovered_service=neighbor, from_seed_service=key[1],
            caller=key[2], callee=key[3], canonical_witness_observation_id=observation_id,
            canonical_witness_attempt_id=attempt_id, support_set_digest=structural_id("sha256-", {"witnesses": witnesses})))
    basis = ScopeDerivationBasis(policy_id=policy.id, acquisition_state_digest=scope_acquisition_digest(state, policy),
        resolution_digest=structural_id("sha256-", {"resolutions": resolutions}),
        topology_witness_digest=structural_id("sha256-", {"edges": [
            {"edge": key, "witnesses": sorted(support[key])} for key in sorted(support)]}))
    return InvestigationScope(policy=policy, basis=basis, seed_services=tuple(seeds), expansions=tuple(expansions),
        overflow_count=len(overflow), overflow_digest=structural_id("sha256-", {"services": overflow}),
        excluded_identifier_count=len(rejected),
        excluded_identifier_digest=structural_id("sha256-", {"edges": sorted(rejected)}))


def current_investigation_scope(state) -> InvestigationScope | None:
    """State-only freshness; backend changes require an explicit acquisition refresh."""
    value = state.get("investigation_scope")
    if value is None:
        return None
    value = InvestigationScope.model_validate(value.model_dump())
    if (value.seed_services != tuple(sorted(set(state["incident"].seed_services)))
            or value.basis.acquisition_state_digest != scope_acquisition_digest(state, value.policy)):
        raise ValueError("stale investigation scope")
    attempts = {a.id: a for a, _ in seed_trace_attempts(state)}
    for expansion in value.expansions:
        attempt = attempts.get(expansion.canonical_witness_attempt_id)
        if (attempt is None or attempt.observation_binding != "bound" or attempt.tool_status not in {"ok", "empty"}
                or expansion.canonical_witness_observation_id not in attempt.admitted_observation_ids):
            raise ValueError("scope witness must belong to its historical seed-side attempt")
    return value
