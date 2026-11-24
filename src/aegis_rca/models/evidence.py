from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from .incident import Contract, InvestigationTask, Signal


class EvidenceSource(Contract):
    backend: str
    locator: str = Field(min_length=1)
    synthetic: bool = False


class Observation(Contract):
    """Adapter-normalized observation, not a model-authored causal assertion."""

    code: Literal[
        "checkout_errors_elevated",
        "rpc_unavailable",
        "connection_refused",
        "dns_resolution_failed",
        "application_error",
        "cpu_saturated",
        "server_span_absent",
    ]
    peer_service: str | None = None


class Evidence(Contract):
    id: str
    signal: Signal
    service: str
    timestamp: AwareDatetime
    observation: Observation
    excerpt: str = Field(max_length=2000)
    source: EvidenceSource
    correlation_group: str | None = None


class Finding(Contract):
    id: str
    statement: str
    evidence_ids: tuple[str, ...]


class ToolResult(Contract):
    status: Literal["ok", "empty", "error", "timeout"]
    evidence: tuple[Evidence, ...] = ()
    warnings: tuple[str, ...] = ()
    truncated: bool = False
    # None means unknown, not complete. An empty successful query proves no absence.
    coverage_complete: bool | None = None


class ToolCallRecord(Contract):
    id: str
    task: InvestigationTask
    attempt_id: str | None = Field(default=None, pattern=r"^A-[0-9a-f]{64}$")
    task_spec_id: str | None = Field(default=None, pattern=r"^T-[0-9a-f]{64}$")
    status: Literal["ok", "empty", "error", "timeout"]
    evidence_ids: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    truncated: bool = False
    coverage_complete: bool | None = None

    @model_validator(mode="after")
    def paired_structural_references(self):
        if (self.attempt_id is None) != (self.task_spec_id is None):
            raise ValueError("attempt_id and task_spec_id must be supplied together")
        return self
