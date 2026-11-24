from typing import Literal
from datetime import timezone
from pydantic import Field, field_validator, model_validator

from .incident import Contract, TimeWindow
from .identity import structural_id


class HypothesisSpec(Contract):
    schema_version: Literal[1] = 1
    semantic_version: Literal["1"] = "1"
    kind: Literal["dependency_connectivity"] = "dependency_connectivity"
    caller: str
    callee: str
    window: TimeWindow
    failure_class: Literal["rpc_connection_establishment"] = "rpc_connection_establishment"
    operation: str | None = None

    @field_validator("caller", "callee")
    @classmethod
    def service(cls, value):
        from ..service_scope import valid_service
        if not valid_service(value):
            raise ValueError("invalid bounded service identifier")
        return value

    @field_validator("operation")
    @classmethod
    def operation_name(cls, value):
        if value is not None and not value.strip():
            raise ValueError("operation must be nonblank when constrained")
        return value

    @field_validator("window")
    @classmethod
    def utc_window(cls, value):
        return TimeWindow(start=value.start.astimezone(timezone.utc), end=value.end.astimezone(timezone.utc))

    @model_validator(mode="after")
    def dependency(self):
        if self.caller == self.callee:
            raise ValueError("dependency endpoints must differ")
        return self

    @property
    def id(self):
        return structural_id("HS-", self.model_dump(exclude={"operation"} if self.operation is None else set()))


class Hypothesis(Contract):
    id: str
    claim: str
    affected_service: str
    failure_mode: str
    supporting_evidence_ids: tuple[str, ...] = ()
    contradicting_evidence_ids: tuple[str, ...] = ()
    missing_checks: tuple[str, ...] = ()
    status: Literal["candidate", "supported", "uncertain", "rejected"] = "candidate"


class Verification(Contract):
    decision: Literal["finalize", "targeted_followup", "replan", "stop_budget", "inconclusive"]
    leading_hypothesis_id: str | None = None
    reason: str


class ReportClaim(Contract):
    text: str
    evidence_ids: tuple[str, ...]
    claim_class: Literal["legacy_display", "verified_failure_mode"] = "legacy_display"
    hypothesis_id: str | None = None
    plan_result_id: str | None = None
    check_result_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def provenance(self):
        if self.claim_class == "verified_failure_mode":
            if (not (self.hypothesis_id or "").startswith("HS-") or not (self.plan_result_id or "").startswith("PR-")
                    or not self.check_result_ids or any(not k.startswith("CR-") for k in self.check_result_ids)):
                raise ValueError("verified failure mode requires HS/PR/CR provenance")
            if any(not k.startswith("E-") for k in self.evidence_ids):
                raise ValueError("compatibility citations must remain E IDs")
        return self


AssessmentStatus = Literal["SUPPORTED", "UNRESOLVED", "CONTRADICTED"]


class AssessmentLimitation(Contract):
    reason: Literal["required_acquisition_failed", "required_acquisition_unavailable", "replay_selection_ambiguous",
                    "unsupported_check_acquisition_mapping", "budget_prevented_required_acquisition"]
    check_id: str
    intent_id: str | None = None


class HypothesisAssessment(Contract):
    hypothesis_spec: HypothesisSpec
    status: AssessmentStatus
    unestablished_check_ids: tuple[str, ...]
    limitations: tuple[AssessmentLimitation, ...] = Field(default=(), max_length=32)
    next_question: str


class PortfolioOverflowSummary(Contract):
    count: int = Field(ge=0)
    policy_limit: int = Field(ge=1, le=16)


class IncidentReport(Contract):
    status: Literal["SUPPORTED", "INCONCLUSIVE"]
    diagnosis_level: Literal["failure_mode", "undetermined"]
    summary: str
    underlying_cause: str | None = None
    claims: tuple[ReportClaim, ...] = ()
    hypotheses: tuple[Hypothesis, ...] = ()
    assessments: tuple[HypothesisAssessment, ...] = Field(default=(), max_length=16)
    provisional_hypotheses: tuple[Hypothesis, ...] = Field(default=(), max_length=3)
    portfolio_overflow: PortfolioOverflowSummary | None = None
    limitations: tuple[str, ...] = ()
    next_checks: tuple[str, ...] = ()
    rounds: int
    tool_calls: int
    termination_reason: str
    mode: Literal["synthetic_fixture", "snapshot", "live"] = "synthetic_fixture"
    reasoning_mode: Literal["deterministic", "llm_assisted"] = "deterministic"
