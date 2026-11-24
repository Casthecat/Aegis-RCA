"""Private corpus recording contracts; no diagnosis or answerability inference.

These records belong under recordings/private. They are never Stage C inputs.
The collector records operator assertions, collection quality and source bytes;
only a later blinded human review may establish benchmark Gold.
"""
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from ..models.identity import structural_id
from ..models.incident import Contract, IncidentInput, TimeWindow
from .demo_environment import PILOT_DEMO_RESOURCE_PROFILE

NEUTRAL_QUERY = "Investigate service degradation during this incident window."
SANITIZER_RULESET = "control-metadata-v1"


def canonical_uuid(value: str) -> str:
    if str(UUID(value)) != value:
        raise ValueError("canonical UUID required")
    return value


class QueryPolicy(Contract):
    schema_version: Literal[1] = 1
    revision: Literal["neutral-query-phase-a-v1"] = "neutral-query-phase-a-v1"
    query: Literal[NEUTRAL_QUERY] = NEUTRAL_QUERY
    seed_services: tuple[Literal["checkout"], ...] = ("checkout",)

    @model_validator(mode="after")
    def one_seed(self):
        if self.seed_services != ("checkout",):
            raise ValueError("Phase A uses the legitimate checkout incident entry context")
        return self

    @property
    def id(self):
        return structural_id("QP-", self.model_dump())

    def validate_incident(self, incident: IncidentInput):
        return validate_corpus_incident(incident)


QUERY_POLICY = QueryPolicy()


def validate_corpus_incident(incident: IncidentInput):
    """Exact query/seed validation, independent of private injection target."""
    if incident.query != NEUTRAL_QUERY:
        raise ValueError("query_leakage: corpus recording requires the exact neutral query")
    if incident.seed_services != QUERY_POLICY.seed_services:
        raise ValueError("Phase A seed must be the legitimate checkout incident entry service")
    return incident


class CapturePolicy(Contract):
    schema_version: Literal[2] = 2
    policy_name: Literal["pilot-v1", "capture-policy-phase-a-v1"] = "pilot-v1"
    otel_demo_version: Literal["3.0.0"] = "3.0.0"
    # Required on deserialization: an old policy cannot acquire environment
    # provenance by silently accepting a default introduced after collection.
    demo_resource_profile_id: str = Field(pattern=r"^DRP-[0-9a-f]{64}$")
    warmup_seconds: int = Field(default=30, ge=0, le=300)
    duration_seconds: int = Field(default=60, ge=10, le=300)
    ingest_wait_seconds: int = Field(default=15, ge=0, le=60)
    cooldown_seconds: int = Field(default=30, ge=0, le=300)
    max_results: int = Field(default=100, ge=1, le=500)
    metric_step_seconds: int = Field(default=15, ge=1, le=300)
    max_response_bytes: int = Field(default=2_000_000, ge=1024, le=10_000_000)
    request_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    truncation_policy: Literal["review_required_never_assume_complete"] = "review_required_never_assume_complete"
    capture_semantics: Literal["one_nonoverlapping_window_per_quarantine_recording"] = "one_nonoverlapping_window_per_quarantine_recording"
    health_protocol: Literal["operator_workload_checkout_success_faults_restored_services_healthy_v1"] = "operator_workload_checkout_success_faults_restored_services_healthy_v1"

    @property
    def id(self):
        return structural_id("CP-", self.model_dump())

    def validate_settings(self, settings):
        for field in ("max_results", "metric_step_seconds", "max_response_bytes", "request_timeout_seconds"):
            if getattr(settings, field) != getattr(self, field):
                raise ValueError(f"Backend {field} differs from frozen capture policy")
        if settings.max_window_seconds < self.duration_seconds:
            raise ValueError("Backend window bound is smaller than frozen capture duration")


PILOT_CAPTURE_POLICY = CapturePolicy(demo_resource_profile_id=PILOT_DEMO_RESOURCE_PROFILE.id)


class HealthQA(Contract):
    at: AwareDatetime
    reviewer: str = Field(min_length=1, max_length=160)
    workload_ran: bool
    business_path_succeeded: bool
    no_residual_fault: bool
    services_healthy: bool
    notes: str = Field(default="", max_length=4000)

    @field_validator("reviewer")
    @classmethod
    def named_operator(cls, value):
        if not value.strip():
            raise ValueError("Health QA needs an identifiable operator")
        return value

    @property
    def passed(self):
        return all((self.workload_ran, self.business_path_succeeded,
                    self.no_residual_fault, self.services_healthy))


class CaptureQuality(Contract):
    trace_records: int = Field(default=0, ge=0)
    log_records: int = Field(default=0, ge=0)
    metric_series: int = Field(default=0, ge=0)
    truncated_signals: tuple[Literal["metric", "trace", "log"], ...] = ()
    result_limit_pressure_signals: tuple[Literal["metric", "trace", "log"], ...] = ()
    coverage_incomplete_signals: tuple[Literal["metric", "trace", "log"], ...] = ()
    collection_error_signals: tuple[str, ...] = ()

    @field_validator("truncated_signals", "result_limit_pressure_signals", "coverage_incomplete_signals", "collection_error_signals")
    @classmethod
    def canonical_signals(cls, value):
        return tuple(sorted(set(value)))


def collection_quality(snapshot, policy: CapturePolicy) -> CaptureQuality:
    material = {"trace": snapshot.traces, "log": snapshot.logs, "metric": snapshot.metrics}
    counts = {"trace": len(snapshot.traces.spans), "log": len(snapshot.logs.logs),
              "metric": len(snapshot.metrics.series)}
    # Jaeger limits matched traces, which may each contain multiple spans.
    result_counts = {**counts, "trace": len({span.trace_id for span in snapshot.traces.spans})}
    return CaptureQuality(trace_records=counts["trace"], log_records=counts["log"],
        metric_series=counts["metric"],
        truncated_signals=tuple(k for k, v in material.items() if v.truncated),
        result_limit_pressure_signals=tuple(k for k, v in result_counts.items() if v >= policy.max_results),
        coverage_incomplete_signals=tuple(k for k, v in material.items() if v.coverage_complete is not True),
        collection_error_signals=tuple(snapshot.collection_errors))


LifecycleStage = Literal["ENV_READY", "STABILIZE", "INJECT", "WARMUP", "CAPTURE", "INGEST_WAIT",
    "RESTORE", "COOLDOWN", "SANITIZE", "SCAN", "HUMAN_REVIEW", "APPROVE", "REJECT",
    "BLINDED_ANSWERABILITY_REVIEW", "GOLD_FREEZE", "SPLIT_FREEZE"]


class LifecycleEvent(Contract):
    stage: LifecycleStage
    at: AwareDatetime


ProductionStatus = Literal["recording_failed", "quarantined", "sanitized", "scan_blocked",
    "review_pending", "review_rejected", "approved", "answerability_labeled", "included_dev",
    "included_replicate_holdout", "included_family_holdout", "excluded"]
ProductionReason = Literal["structural_leakage", "sanitizer_destructive", "telemetry_insufficient",
    "capture_failed", "source_unstable", "review_rejected", "injection_not_effective",
    "window_policy_violation", "overlapping_window", "query_leakage", "duplicate_slice",
    "health_qa_failed", "recovery_failed", "lock_unavailable", "capture_cancelled",
    "collection_error", "truncation", "holdout_access_violation"]


class InjectionTruth(Contract):
    schema_version: Literal[1] = 1
    private_episode_id: str
    candidate_id: str | None = None
    experiment_revision: str = Field(default="p2.2-pilot-v1", min_length=1, max_length=160)
    otel_demo_version: Literal["3.0.0"] = "3.0.0"
    recording_method: Literal["record_episode_same_pipeline"] = "record_episode_same_pipeline"
    scenario_family_id: str
    family_trial_index: int = Field(ge=1, le=10000)
    mechanism: Literal["none", "payment_unreachable", "payment_failure"]
    control_flag: Literal["paymentUnreachable", "paymentFailure"] | None = None
    variant: Literal["on", "100%"] | None = None
    private_target: str | None = Field(default=None, max_length=160)
    intended_condition: str = Field(default="", max_length=2000)
    quarantine_recording_id: str
    capture_window: TimeWindow | None = None
    source_snapshot_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    capture_policy_id: str = Field(pattern=r"^CP-[0-9a-f]{64}$")
    query_policy_id: str = Field(default=QUERY_POLICY.id, pattern=r"^QP-[0-9a-f]{64}$")
    sanitizer_ruleset: Literal["control-metadata-v1"] = SANITIZER_RULESET
    seed_services: tuple[str, ...] = ("checkout",)
    seed_justification: str = Field(min_length=1, max_length=2000)
    previous_fault_restored: bool = False
    cooldown_completed: bool = False
    injection_effective: bool | None = None
    pre_health: HealthQA | None = None
    recovery_health: HealthQA | None = None
    collection_errors: dict[str, str] = Field(default_factory=dict)
    quality: CaptureQuality | None = None
    lifecycle: tuple[LifecycleEvent, ...] = Field(default=(), max_length=64)
    private_notes: str = Field(default="", max_length=8000)

    @field_validator("private_episode_id", "scenario_family_id", "quarantine_recording_id", "candidate_id")
    @classmethod
    def ids(cls, value):
        return canonical_uuid(value) if value is not None else None

    @model_validator(mode="after")
    def truthful_fields(self):
        expected = {"none": (None, None), "payment_unreachable": ("paymentUnreachable", "on"),
                    "payment_failure": ("paymentFailure", "100%")}[self.mechanism]
        if (self.control_flag, self.variant) != expected:
            raise ValueError("Injection mechanism, control and variant must agree")
        if self.mechanism == "none" and self.injection_effective is not None:
            raise ValueError("No-injection control has no injection-effectiveness assertion")
        if self.quarantine_recording_id != self.private_episode_id:
            raise ValueError("One attempted episode corresponds to exactly one quarantine recording")
        if self.seed_services != ("checkout",):
            raise ValueError("Phase A incident entry seed is checkout, not the private injected target")
        if tuple(x.at for x in self.lifecycle) != tuple(sorted(x.at for x in self.lifecycle)):
            raise ValueError("Lifecycle timestamps must be ordered")
        return self


class CorpusProductionRecord(Contract):
    schema_version: Literal[1] = 1
    private_episode_id: str
    candidate_id: str | None = None
    status: ProductionStatus
    reason_codes: tuple[ProductionReason, ...] = ()
    created_at: AwareDatetime
    updated_at: AwareDatetime
    capture_window: TimeWindow | None = None
    source_snapshot_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    capture_policy_id: str = Field(pattern=r"^CP-[0-9a-f]{64}$")
    query_policy_id: str = Field(default=QUERY_POLICY.id, pattern=r"^QP-[0-9a-f]{64}$")
    sanitizer_ruleset: Literal["control-metadata-v1"] = SANITIZER_RULESET
    seed_services: tuple[str, ...] = ("checkout",)
    quality: CaptureQuality | None = None

    @field_validator("private_episode_id", "candidate_id")
    @classmethod
    def ids(cls, value):
        return canonical_uuid(value) if value is not None else None

    @field_validator("reason_codes")
    @classmethod
    def canonical_reasons(cls, value):
        return tuple(sorted(set(value)))

    @model_validator(mode="after")
    def coherent(self):
        if self.updated_at < self.created_at:
            raise ValueError("Production record timestamps must be ordered")
        if self.status == "recording_failed" and not self.reason_codes:
            raise ValueError("A failed attempt requires a closed failure reason")
        if self.status != "recording_failed" and self.status != "excluded" and (
                self.capture_window is None or self.source_snapshot_sha256 is None):
            raise ValueError("Recorded production states require original window and source bytes hash")
        return self
