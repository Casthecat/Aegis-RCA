"""Acquisition-native exact historical trace resolution; no hypothesis dependency."""

from dataclasses import dataclass
from typing import Literal, Protocol, get_args, runtime_checkable

from .models.acquisition import AttemptRecord, TaskSpec
from .models.identity import structural_id
from .models.observation import ObservationRecord
from .models.telemetry import TraceResult
from .observations import observe_spans


@dataclass(frozen=True)
class TraceObservationSource:
    """Backend-owned records and context; reading these must not issue a query."""

    traces: TraceResult
    rpc_service_map: dict[str, str]
    backend: str = "jaeger"
    synthetic: bool = False


@runtime_checkable
class TraceSourceBackend(Protocol):
    def trace_source(self) -> TraceObservationSource: ...


TraceBindingOutcome = Literal[
    "verified", "source_unavailable", "source_digest_mismatch",
    "observation_id_mismatch", "binding_unknown",
]


@dataclass(frozen=True)
class ResolvedTraceAttempt:
    """Ephemeral resolved generation. Facts are never duplicated into IncidentState."""

    attempt: AttemptRecord
    spec: TaskSpec
    outcome: TraceBindingOutcome
    observations: tuple[ObservationRecord, ...] = ()

    def __post_init__(self):
        if self.outcome not in get_args(TraceBindingOutcome):
            raise ValueError("trace resolution outcome must be a structured code")
        if any(not isinstance(o, ObservationRecord) for o in self.observations):
            raise TypeError("trace resolution accepts only typed observations")
        if self.spec.signal != "trace" or self.attempt.task_spec_id != self.spec.id:
            raise ValueError("trace resolution must reference its authoritative TaskSpec")
        if self.outcome == "verified":
            if (self.attempt.observation_binding != "bound" or self.attempt.tool_status not in {"ok", "empty"}
                    or {o.id for o in self.observations} != set(self.attempt.admitted_observation_ids)):
                raise ValueError("verified resolution requires exact historical Observation IDs")
        elif self.observations:
            raise ValueError("unverified resolution cannot supply observations")


def trace_source_material(task, backend):
    if not isinstance(backend, TraceSourceBackend):
        return None
    source = backend.trace_source()
    spans = tuple(span for span in source.traces.spans if
                  span.service in task.services and task.window.start <= span.start < task.window.end)
    digest = structural_id("sha256-", {
        "spans": [span.model_dump(mode="json") for span in spans],
        "rpc_service_map": source.rpc_service_map,
        "backend": source.backend, "synthetic": source.synthetic,
    })
    return source, spans, digest


def trace_binding(task, backend):
    """Initial completion binding, preserving the P1.5 digest and O identity inputs."""
    material = trace_source_material(task, backend)
    if material is None:
        return (), None
    source, spans, digest = material
    return observe_spans(spans, source.rpc_service_map,
                         backend=source.backend, synthetic=source.synthetic), digest


def resolve_trace_attempt(attempt: AttemptRecord, spec: TaskSpec, backend) -> ResolvedTraceAttempt:
    outcome, facts = "binding_unknown", ()
    if attempt.observation_binding == "bound" and attempt.tool_status in {"ok", "empty"}:
        outcome = "source_unavailable"
        try:
            reader = backend if hasattr(backend, "partition_for") else getattr(backend, "_partition_reader", None)
            if reader is not None:
                partition = reader.partition_for(attempt.id)
                if partition is None:
                    return ResolvedTraceAttempt(attempt, spec, "source_unavailable")
                if (partition.attempt_id != attempt.id or partition.task_spec_id != spec.id
                        or partition.signal != "trace"):
                    return ResolvedTraceAttempt(attempt, spec, "source_digest_mismatch")
                source = TraceObservationSource(partition.material, dict(partition.source.rpc_service_map),
                    backend=partition.source.backend, synthetic=partition.source.synthetic)
                material = trace_source_material(spec, _OwnedSource(source))
            else:
                return ResolvedTraceAttempt(attempt, spec, "source_unavailable")
            if material is not None:
                source, spans, digest = material
                outcome = "source_digest_mismatch"
                if digest == attempt.source_material_digest:
                    produced = observe_spans(spans, source.rpc_service_map,
                        backend=source.backend, synthetic=source.synthetic)
                    outcome = "observation_id_mismatch"
                    if {fact.id for fact in produced} == set(attempt.admitted_observation_ids):
                        facts, outcome = produced, "verified"
        except Exception:
            outcome = "source_unavailable"
    return ResolvedTraceAttempt(attempt, spec, outcome, facts)


class _OwnedSource:
    def __init__(self, source):
        self.source = source

    def trace_source(self):
        return self.source
