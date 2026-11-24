from dataclasses import dataclass, field
from typing import Protocol
from typing import Any

from .models.evidence import ToolCallRecord, ToolResult
from .models.incident import InvestigationTask
from .models.acquisition import AttemptIdentity, AttemptRecord, TaskSpec
from .models.identity import structural_id
from .models.execution import InvestigationExecutionPolicy, DEFAULT_EXECUTION_POLICY
from .models.acquisition_result import AcquisitionResult, CommittedAcquisition, TelemetryPartition
from .acquisition_store import AcquisitionStore, PartitionReader
from .trace_acquisition import TraceObservationSource, trace_binding, resolve_trace_attempt
from .investigation_scope import (
    DEFAULT_SCOPE_POLICY, current_investigation_scope, derive_investigation_scope, seed_trace_attempts,
)
from .state import IncidentState
from .verification.connectivity import evaluate_connectivity
from .verification.persisted import (
    AttemptBindingResolution, PersistedHypothesisVerification, ResolvedEvaluationBasis,
    attempt_coverage, evidence_references,
    relevant_attempts, state_verification_basis,
)


class TelemetryBackend(Protocol):
    async def query(self, task: InvestigationTask) -> ToolResult:
        """Perform one bounded, read-only query. Return data quality explicitly."""
        ...


@dataclass(frozen=True)
class ReservedAttempt:
    identity: AttemptIdentity
    task: InvestigationTask
    spec: TaskSpec
    timeout_seconds: float
    call_id: str

    @property
    def id(self):
        return self.identity.id


@dataclass(frozen=True)
class Dependencies:
    backend: TelemetryBackend
    reasoner: Any = None
    execution_policy: InvestigationExecutionPolicy = DEFAULT_EXECUTION_POLICY
    attempts: dict[str, ReservedAttempt] = field(default_factory=dict)
    acquisitions: AcquisitionStore = field(default_factory=AcquisitionStore)
    started_attempts: set[str] = field(default_factory=set)
    # Admission journal also recovers subgraph ledger writes after global deadline.
    intent_admissions: dict = field(default_factory=dict)

    def __post_init__(self):
        if hasattr(self.backend, "__dict__"):
            self.backend._partition_reader = PartitionReader(self.acquisitions)

    @property
    def completions(self):
        # Compatibility projection, not a second authoritative mutable map.
        return {c.attempt.id: c.state_update() for c in self.acquisitions.values()}

    def partition_for(self, attempt_id):
        return self.acquisitions.partition_for(attempt_id)


def finish_attempt(reserved: ReservedAttempt, backend: TelemetryBackend, result: ToolResult, *,
                   admitted_evidence=(), warnings=(), error_class="none", original_result=True, acquisition=None):
    observations, source_digest = (), None
    if reserved.task.signal == "trace" and result.status in {"ok", "empty"}:
        try:
            source = backend
            if acquisition is not None:
                source = MaterialSource(acquisition)
            observations, source_digest = trace_binding(reserved.spec, source)
        except Exception:
            # A missing native binding must not manufacture an empty acquisition.
            observations, source_digest = (), None
    attempt = AttemptRecord(**reserved.identity.model_dump(), tool_status=result.status,
        backend_kind=type(backend).__name__, timeout_seconds=reserved.timeout_seconds, error_class=error_class,
        truncated=result.truncated, coverage_complete=result.coverage_complete,
        admitted_observation_ids=tuple(item.id for item in observations),
        admitted_evidence_ids=tuple(item.id for item in admitted_evidence),
        observation_binding="bound" if source_digest is not None else "unknown",
        evidence_binding="bound" if original_result else "unknown",
        tool_result_digest=structural_id("sha256-", result.model_dump(mode="json")) if original_result else None,
        source_material_digest=source_digest, warnings=tuple(warnings))
    projection = ToolCallRecord(id=reserved.call_id, task=reserved.task,
        attempt_id=attempt.id, task_spec_id=reserved.spec.id, status=attempt.tool_status,
        evidence_ids=tuple(item.id for item in admitted_evidence), warnings=attempt.warnings,
        truncated=attempt.truncated, coverage_complete=attempt.coverage_complete)
    return attempt, projection


class MaterialSource:
    def __init__(self, acquisition):
        self.acquisition = acquisition

    def trace_source(self):
        from .models.telemetry import TraceResult
        value = self.acquisition
        if not isinstance(value.material, TraceResult) or value.source is None:
            raise ValueError("no trace acquisition material")
        return TraceObservationSource(value.material, dict(value.source.rpc_service_map),
                                      backend=value.source.backend, synthetic=value.source.synthetic)


def finalize_acquisition(reserved, backend, acquisition, *, error_class="none", original_result=True):
    result = acquisition.projection
    evidence = tuple(e for e in result.evidence if result.status == "ok" and e.signal == reserved.spec.signal
        and e.service in reserved.spec.services and reserved.spec.window.start <= e.timestamp < reserved.spec.window.end)
    warnings = result.warnings
    if len(evidence) != len(result.evidence):
        warnings += ("Some returned evidence was outside scope or from an unsuccessful query.",)
    attempt, call = finish_attempt(reserved, backend, result, admitted_evidence=evidence, warnings=warnings,
        error_class=error_class, original_result=original_result, acquisition=acquisition)
    partition = None
    if acquisition.material is not None:
        partition = TelemetryPartition(attempt_id=attempt.id, task_spec_id=reserved.spec.id, signal=reserved.spec.signal,
                                       material=acquisition.material, source=acquisition.source)
    return CommittedAcquisition(attempt=attempt, tool_call=call, evidence=evidence, partition=partition,
                                recorded_from=acquisition.recorded_from)


def validate_execution_state(state: IncidentState):
    """Completed attempts are authoritative; tool calls are checked projections."""
    execution_id = state["execution_id"]
    if not execution_id.strip():
        raise ValueError("execution_id must not be blank")
    specs = {spec.id: spec for spec in state.get("task_specs", ())}
    attempts = {attempt.id: attempt for attempt in state.get("attempt_records", ())}
    for attempt in attempts.values():
        if attempt.execution_id != execution_id or attempt.task_spec_id not in specs:
            raise ValueError("Attempt must belong to this execution and a saved TaskSpec")
    projected = set()
    for call in state["tool_calls"]:
        if call.attempt_id is None and call.task_spec_id is None:
            continue  # Read-only legacy records are not silently assigned A-*.
        attempt = attempts.get(call.attempt_id)
        if attempt is None or call.task_spec_id != attempt.task_spec_id:
            raise ValueError("Tool call must reference its authoritative attempt")
        spec = specs[attempt.task_spec_id]
        if (call.task.signal != spec.signal or tuple(sorted(set(call.task.services))) != spec.services
                or call.task.window != spec.window):
            raise ValueError("Tool call intent differs from its materialized TaskSpec")
        if (call.status != attempt.tool_status or set(call.evidence_ids) != set(attempt.admitted_evidence_ids)
                or call.truncated != attempt.truncated or call.coverage_complete != attempt.coverage_complete
                or call.warnings != attempt.warnings):
            raise ValueError("Tool call differs from its authoritative attempt")
        if call.attempt_id in projected:
            raise ValueError("An attempt must have exactly one compatibility projection")
        projected.add(call.attempt_id)
    if projected != set(attempts):
        raise ValueError("Every completed attempt requires its compatibility projection")
    from .verification.persisted import current_hypothesis_verifications
    current_hypothesis_verifications(state)
    current_investigation_scope(state)


@dataclass
class ResolvedAcquisitionIndex:
    """One ephemeral generation shared by scope, portfolio and all PHVs."""
    traces: dict = field(default_factory=dict)
    logs: dict = field(default_factory=dict)

    def trace(self, attempt, spec, backend):
        if attempt.id not in self.traces:
            self.traces[attempt.id] = resolve_trace_attempt(attempt, spec, backend)
        resolved = self.traces[attempt.id]
        if resolved.attempt != attempt or resolved.spec != spec:
            raise ValueError("resolved trace generation differs from authoritative acquisition")
        return resolved

    def log(self, attempt, spec, state):
        if attempt.id not in self.logs:
            facts, outcome = (), "binding_unknown"
            if attempt.evidence_binding == "bound" and attempt.tool_status in {"ok", "empty"}:
                found = tuple(e for e in state["evidence"] if e.id in attempt.admitted_evidence_ids
                    and e.signal == "log" and e.service in spec.services
                    and spec.window.start <= e.timestamp < spec.window.end)
                outcome = "evidence_missing"
                if {e.id for e in found} == set(attempt.admitted_evidence_ids):
                    facts, outcome = found, "verified"
            self.logs[attempt.id] = (facts, outcome)
        return self.logs[attempt.id]


def refresh_hypothesis_verification(state, backend, *, subject, index):
    observations, evidence, resolutions = [], [], []
    for attempt, spec in relevant_attempts(state, subject):
        if spec.signal == "trace":
            resolved = index.trace(attempt, spec, backend)
            facts, outcome = resolved.observations, resolved.outcome
            observations.extend(facts)
        else:
            facts, outcome = index.log(attempt, spec, state)
            evidence.extend(facts)
        resolutions.append(AttemptBindingResolution(outcome=outcome,
            coverage=attempt_coverage(attempt, spec, admitted_ids=tuple(f.id for f in facts), verified=outcome == "verified")))
    basis = ResolvedEvaluationBasis(observation_ids=tuple(sorted({f.id for f in observations})),
        log_evidence=evidence_references(evidence), resolutions=tuple(resolutions))
    evaluation = evaluate_connectivity(subject.window, caller=subject.caller, callee=subject.callee, operation=subject.operation,
        trace_observations=tuple(observations), legacy_log_evidence=tuple(evidence),
        trace_coverage=tuple(r.coverage for r in basis.resolutions if r.coverage.signal == "trace"),
        log_coverage=tuple(r.coverage for r in basis.resolutions if r.coverage.signal == "log"))
    return PersistedHypothesisVerification(hypothesis_spec=subject, state_basis=state_verification_basis(state, subject),
        evaluation_basis=basis, evaluation=evaluation)


def refresh_connectivity_verification(state, backend, *, subject, resolved_traces=None):
    """Explicit-subject compatibility function; no writes and no selected-subject fallback."""
    if subject is None:
        return None
    return refresh_hypothesis_verification(state, backend, subject=subject,
        index=ResolvedAcquisitionIndex(traces=dict(resolved_traces or {})))


def refresh_acquisition_state(state: IncidentState, backend: TelemetryBackend, *, scope_policy=None, hypothesis_policy=None):
    """Replace the entire current semantic generation after terminal acquisition merge."""
    from .portfolio import derive_hypothesis_portfolio
    from .verification.persisted import current_hypothesis_verifications
    from .scheduling import reconcile_intents
    policy = scope_policy or (state["investigation_scope"].policy if state.get("investigation_scope") else DEFAULT_SCOPE_POLICY)
    hp = hypothesis_policy or (state["hypothesis_portfolio"].policy if state.get("hypothesis_portfolio") else
        getattr(backend, "execution_policy", DEFAULT_EXECUTION_POLICY).hypothesis_policy)
    validate_execution_state({**state, "hypothesis_portfolio": None, "hypothesis_verifications": (), "investigation_scope": None})
    index = ResolvedAcquisitionIndex()
    for attempt, spec in seed_trace_attempts(state):
        index.trace(attempt, spec, backend)
    scope = derive_investigation_scope(state, index.traces, policy)
    fresh = {**state, "investigation_scope": scope}
    portfolio = derive_hypothesis_portfolio(fresh, index.traces, hp)
    fresh = {**fresh, "hypothesis_portfolio": portfolio}
    union = {a.id: (a, t) for h in portfolio.active for a, t in relevant_attempts(fresh, h)}
    for key in sorted(union):
        attempt, spec = union[key]
        if spec.signal == "trace":
            index.trace(attempt, spec, backend)
        else:
            index.log(attempt, spec, fresh)
    # Each relevant acquisition is cached once, even when multiple HS share it.
    bundles = tuple(refresh_hypothesis_verification(fresh, backend, subject=h, index=index) for h in portfolio.active)
    update = {"investigation_scope": scope, "hypothesis_portfolio": portfolio,
              "hypothesis_verifications": bundles, "intent_ledger": reconcile_intents(state)}
    current_hypothesis_verifications({**state, **update})
    return update
