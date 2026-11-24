"""Materialized acquisition intent and concrete execution identities."""

from datetime import timezone
from typing import Literal

from pydantic import Field, field_validator, model_validator, model_serializer

from .identity import structural_id
from .incident import Contract, InvestigationTask, Signal, TimeWindow
from .recorded import RecordedAcquisitionSelector


class LogQueryScope(Contract):
    index: str = Field(min_length=1)
    timestamp_field: str = Field(min_length=1)
    service_exact_field: str = Field(min_length=1)
    server_timeout_ms: int = Field(ge=0)


class TaskSpec(Contract):
    schema_version: Literal[1, 2] = 1
    signal: Signal
    services: tuple[str, ...] = Field(min_length=1)
    window: TimeWindow
    query_template_id: str = Field(min_length=1)
    query_expression: str | None = None
    result_limit: int | None = Field(default=None, ge=1)
    metric_step_seconds: int | None = Field(default=None, ge=1)
    log_query: LogQueryScope | None = None
    alias: str | None = None
    recorded_selection: RecordedAcquisitionSelector | None = None

    @model_serializer(mode="wrap")
    def versioned_wire(self, handler):
        result = handler(self)
        if self.schema_version == 1:
            result.pop("recorded_selection", None)
        return result

    @field_validator("services")
    @classmethod
    def canonical_services(cls, values):
        if any(not value.strip() for value in values):
            raise ValueError("services must not be blank")
        return tuple(sorted(set(values)))

    @field_validator("window")
    @classmethod
    def utc_window(cls, window):
        return TimeWindow(start=window.start.astimezone(timezone.utc), end=window.end.astimezone(timezone.utc))

    @model_validator(mode="after")
    def signal_parameters(self):
        if self.schema_version == 1:
            if self.recorded_selection is not None or self.query_template_id == "replay.select.recorded.v1":
                raise ValueError("recorded selection requires TaskSpec version 2")
        elif (self.recorded_selection is None or self.query_template_id != "replay.select.recorded.v1"
                or any(v is not None for v in (self.query_expression, self.result_limit,
                                               self.metric_step_seconds, self.log_query))):
            raise ValueError("TaskSpec version 2 requires exact recorded selection without physical overrides")
        if self.signal != "metric" and (self.query_expression is not None or self.metric_step_seconds is not None):
            raise ValueError("metric parameters require a metric task")
        if self.signal != "log" and self.log_query is not None:
            raise ValueError("log query scope requires a log task")
        return self

    @property
    def id(self) -> str:
        return structural_id("T-", self.model_dump(exclude={"alias"}))


class MaterializedTask(InvestigationTask):
    """Ephemeral backend request; compatibility artifacts retain original intent."""

    task_spec: TaskSpec

    @model_validator(mode="after")
    def exact_intent(self):
        if (self.signal != self.task_spec.signal or tuple(sorted(set(self.services))) != self.task_spec.services
                or self.window != self.task_spec.window):
            raise ValueError("materialized request must match its task intent")
        return self


class AttemptIdentity(Contract):
    execution_id: str = Field(min_length=1)
    task_spec_id: str = Field(pattern=r"^T-[0-9a-f]{64}$")
    ordinal: int = Field(ge=1, strict=True)

    @field_validator("execution_id")
    @classmethod
    def nonblank_execution(cls, value):
        if not value.strip():
            raise ValueError("execution identity must not be blank")
        return value

    @property
    def id(self) -> str:
        return structural_id("A-", {"execution_id": self.execution_id,
                                    "task_spec_id": self.task_spec_id, "ordinal": self.ordinal})


class AttemptRecord(AttemptIdentity):
    schema_version: Literal[1] = 1
    tool_status: Literal["ok", "empty", "error", "timeout"]
    backend_kind: str
    timeout_seconds: float = Field(gt=0, allow_inf_nan=False)
    error_class: Literal["none", "backend_error", "invalid_result", "tool_timeout", "global_deadline"] = "none"
    truncated: bool = False
    coverage_complete: bool | None = None
    pre_capture_omissions: Literal["unknown"] = "unknown"
    admitted_observation_ids: tuple[str, ...] = ()
    admitted_evidence_ids: tuple[str, ...] = ()
    observation_binding: Literal["bound", "unknown"] = "unknown"
    evidence_binding: Literal["bound", "unknown"] = "unknown"
    tool_result_digest: str | None = Field(default=None, pattern=r"^sha256-[0-9a-f]{64}$")
    source_material_digest: str | None = Field(default=None, pattern=r"^sha256-[0-9a-f]{64}$")
    warnings: tuple[str, ...] = ()

    @field_validator("admitted_observation_ids", "admitted_evidence_ids")
    @classmethod
    def canonical_ids(cls, values, info):
        prefix = "O-" if info.field_name == "admitted_observation_ids" else "E-"
        if any(not value.startswith(prefix) for value in values):
            raise ValueError("admitted IDs must preserve their native namespace")
        return tuple(sorted(set(values)))

    @model_validator(mode="after")
    def status_error_consistency(self):
        allowed = {
            "ok": {"none"},
            "empty": {"none"},
            "error": {"backend_error", "invalid_result"},
            "timeout": {"tool_timeout", "global_deadline"},
        }
        if self.error_class not in allowed[self.tool_status]:
            raise ValueError("error_class is inconsistent with tool_status")
        return self

    @model_validator(mode="after")
    def binding_consistency(self):
        if self.observation_binding == "unknown" and (self.admitted_observation_ids or self.source_material_digest):
            raise ValueError("unknown native binding cannot claim observation IDs/source digest")
        if self.observation_binding == "bound" and self.source_material_digest is None:
            raise ValueError("bound native facts require their source-material digest")
        if self.evidence_binding == "unknown" and self.admitted_evidence_ids:
            raise ValueError("unknown evidence binding cannot claim admitted evidence IDs")
        return self
