"""Runtime acquisition products, separate from legacy diagnostic ToolResult."""

from typing import Literal

from pydantic import Field, model_validator

from .acquisition import AttemptRecord
from .evidence import Evidence, Finding, ToolCallRecord, ToolResult
from .identity import structural_id
from .incident import Contract, Signal
from .telemetry import LogResult, MetricResult, TraceResult
from .recorded import RecordedSourceReference


MATERIAL_TYPES = {"trace": TraceResult, "log": LogResult, "metric": MetricResult}


class SourceMetadata(Contract):
    rpc_service_map: dict[str, str] = Field(default_factory=dict)
    backend: str = "jaeger"
    synthetic: bool = False


class AcquisitionResult(Contract):
    signal: Signal
    projection: ToolResult
    material: TraceResult | LogResult | MetricResult | None = None
    source: SourceMetadata | None = None
    projection_origin: Literal["normalized", "fixture", "legacy"] = "normalized"
    recorded_from: RecordedSourceReference | None = None

    @model_validator(mode="before")
    @classmethod
    def decode_material(cls, values):
        if isinstance(values, dict) and isinstance(values.get("material"), dict):
            target = MATERIAL_TYPES.get(values.get("signal"))
            if target is not None:
                values = {**values, "material": target.model_validate(values["material"])}
        return values

    @model_validator(mode="after")
    def coherence(self):
        if self.recorded_from is not None and self.material is None:
            raise ValueError("recorded lineage requires returned raw material")
        successful = self.projection.status in {"ok", "empty"}
        if not successful and (self.material is not None or self.source is not None):
            raise ValueError("failed acquisition cannot carry successful raw material")
        if self.material is not None:
            if not isinstance(self.material, MATERIAL_TYPES.get(self.signal, type(None))):
                raise ValueError("material must match acquisition signal")
            if self.source is None or self.projection_origin == "legacy":
                raise ValueError("raw acquisition requires source metadata and a material-aware projection")
        elif self.source is not None:
            raise ValueError("source metadata requires material")
        elif successful and self.signal in MATERIAL_TYPES and self.projection_origin != "legacy":
            raise ValueError("successful telemetry requires explicit material, even when empty")
        if self.projection_origin == "normalized" and self.material is not None:
            if (self.projection.truncated != self.material.truncated
                    or self.projection.coverage_complete != self.material.coverage_complete):
                raise ValueError("normalized projection must preserve material coverage")
            if (self.projection.status == "ok") != bool(self.projection.evidence):
                raise ValueError("normalized status reflects the diagnostic projection")
        return self


class TelemetryPartition(Contract):
    attempt_id: str = Field(pattern=r"^A-[0-9a-f]{64}$")
    task_spec_id: str = Field(pattern=r"^T-[0-9a-f]{64}$")
    signal: Literal["trace", "log", "metric"]
    material: TraceResult | LogResult | MetricResult
    source: SourceMetadata

    @model_validator(mode="before")
    @classmethod
    def decode_material(cls, values):
        return AcquisitionResult.decode_material(values)

    @model_validator(mode="after")
    def signal_material(self):
        if not isinstance(self.material, MATERIAL_TYPES[self.signal]):
            raise ValueError("partition material must match signal")
        return self

    @property
    def content_digest(self):
        return structural_id("sha256-", self.model_dump(exclude={"attempt_id", "task_spec_id"}))


class CommittedAcquisition(Contract):
    attempt: AttemptRecord
    tool_call: ToolCallRecord
    evidence: tuple[Evidence, ...] = ()
    partition: TelemetryPartition | None = None
    recorded_from: RecordedSourceReference | None = None

    @model_validator(mode="after")
    def one_completion(self):
        a, call, part = self.attempt, self.tool_call, self.partition
        if self.recorded_from is not None and (part is None or self.recorded_from.recorded_content_digest != part.content_digest):
            raise ValueError("recorded lineage must match committed raw content")
        if a.observation_binding == "bound" and part is None:
            raise ValueError("native bound completion requires its raw partition")
        if call.attempt_id != a.id or call.task_spec_id != a.task_spec_id or call.status != a.tool_status:
            raise ValueError("terminal projection must reference its authoritative attempt")
        if (set(call.evidence_ids) != {e.id for e in self.evidence}
                or set(a.admitted_evidence_ids) != set(call.evidence_ids)
                or call.truncated != a.truncated or call.coverage_complete != a.coverage_complete
                or call.warnings != a.warnings):
            raise ValueError("terminal projection/evidence must agree with attempt")
        if part is not None:
            if a.tool_status not in {"ok", "empty"}:
                raise ValueError("failure/timeout cannot commit a successful partition")
            if (part.attempt_id != a.id or part.task_spec_id != a.task_spec_id
                    or part.signal != call.task.signal):
                raise ValueError("partition must belong to the same acquisition")
        return self

    def state_update(self):
        return {"evidence": list(self.evidence),
                "findings": [Finding(id=f"F-{e.id}", statement=e.excerpt, evidence_ids=(e.id,)) for e in self.evidence],
                "tool_calls": [self.tool_call], "attempt_records": [self.attempt]}
