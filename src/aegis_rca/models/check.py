"""Versioned, structural checks over admitted facts; no diagnostic inference."""

from datetime import timezone
from typing import Annotated, Literal

from pydantic import Field, FiniteFloat, field_validator, model_validator

from .identity import structural_id
from .incident import Contract, TimeWindow
from .metric_observation import ClosedSampleInterval, MetricSeriesKey, QueryStatus
from .observation import GrpcStatusName


class CheckScope(Contract):
    signal: Literal["trace", "metric", "log"]
    services: tuple[str, ...] = Field(min_length=1)
    window: TimeWindow
    query_identity: str | None = Field(default=None, min_length=1)
    acquisition_id: str | None = Field(default=None, min_length=1)

    @field_validator("services")
    @classmethod
    def canonical_services(cls, values):
        if any(not value.strip() for value in values):
            raise ValueError("service identities must not be blank")
        return tuple(sorted(set(values)))

    @field_validator("window")
    @classmethod
    def utc_window(cls, window):
        return TimeWindow(start=window.start.astimezone(timezone.utc),
                          end=window.end.astimezone(timezone.utc))


class CheckCoverageView(CheckScope):
    """Caller attestation linking an acquisition to its admitted fact IDs.

    Logical identities are not physical locators. A complete query alone does
    not certify that the caller supplied the complete admitted fact set.
    """

    input_kind: Literal["observation", "legacy_evidence"] = "observation"
    acquisition_id: str = Field(min_length=1)
    query_status: QueryStatus = "unknown"
    truncated: bool = False
    coverage_complete: bool | None = None
    pre_capture_omissions: Literal["none", "present", "unknown"] = "unknown"
    admitted_ids: tuple[str, ...] = ()
    fact_set_complete: bool = False
    warnings: tuple[str, ...] = ()

    @field_validator("admitted_ids")
    @classmethod
    def canonical_ids(cls, values):
        if any(not value for value in values):
            raise ValueError("admitted fact IDs must not be empty")
        return tuple(sorted(set(values)))

    def identity_payload(self) -> dict:
        return self.model_dump(exclude={"warnings"})


class RpcExists(Contract):
    kind: Literal["rpc_exists"] = "rpc_exists"
    local_service: str = Field(min_length=1)
    span_kind: Literal["CLIENT", "SERVER"] | None = None
    peer_service: str | None = Field(default=None, min_length=1)
    require_resolved_peer: bool = False
    require_unambiguous_peer: bool = False
    outcomes: tuple[Literal["success", "failure", "unknown", "inconsistent"], ...] = ()
    rpc_status_names: tuple[GrpcStatusName, ...] = ()
    otlp_status: Literal["OK", "ERROR", "UNSET", "UNKNOWN"] | None = None
    operation: str | None = None
    rpc_system: str | None = None

    @field_validator("outcomes", "rpc_status_names")
    @classmethod
    def canonical_whitelists(cls, values):
        return tuple(sorted(set(values)))


class MetricSummaryExists(Contract):
    kind: Literal["metric_summary_exists"] = "metric_summary_exists"
    series: MetricSeriesKey


class _MetricCompare(Contract):
    series: MetricSeriesKey
    operator: Literal["eq", "ne", "lt", "lte", "gt", "gte"]
    threshold: FiniteFloat
    interval: ClosedSampleInterval | None = None

    @field_validator("threshold", mode="before")
    @classmethod
    def numeric_threshold(cls, value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("threshold must be a finite numeric value")
        return value


class CounterDeltaCompare(_MetricCompare):
    kind: Literal["counter_delta_compare"] = "counter_delta_compare"


class IntervalRateCompare(_MetricCompare):
    kind: Literal["interval_rate_compare"] = "interval_rate_compare"


class CategoryPresent(Contract):
    kind: Literal["category_present"] = "category_present"
    label: str = Field(min_length=1)
    value: str
    membership_scope: Literal["returned_series"] = "returned_series"


class LegacyEvidenceExists(Contract):
    kind: Literal["legacy_evidence_exists"] = "legacy_evidence_exists"
    service: str = Field(min_length=1)
    peer_service: str = Field(min_length=1)
    codes: tuple[Literal["connection_refused", "dns_resolution_failed"], ...] = Field(min_length=1)

    @field_validator("codes")
    @classmethod
    def canonical_codes(cls, values):
        return tuple(sorted(set(values)))


CheckPredicate = Annotated[
    RpcExists | MetricSummaryExists | CounterDeltaCompare | IntervalRateCompare
    | CategoryPresent | LegacyEvidenceExists, Field(discriminator="kind")
]


class CheckSpec(Contract):
    schema_version: Literal[1] = 1
    semantic_version: str = Field(default="1", min_length=1)
    scope: CheckScope
    predicate: CheckPredicate
    relation: Literal["supports_hypothesis", "contradicts_hypothesis"] = "supports_hypothesis"
    requirement: Literal["required", "optional"] = "required"
    alias: str | None = None
    description: str = ""

    @model_validator(mode="after")
    def appropriate_scope(self):
        predicate = self.predicate
        signal = ("trace" if isinstance(predicate, RpcExists) else
                  "log" if isinstance(predicate, LegacyEvidenceExists) else "metric")
        if self.scope.signal != signal:
            raise ValueError("predicate and scope signals must agree")
        service = (predicate.local_service if isinstance(predicate, RpcExists) else
                   predicate.service if isinstance(predicate, LegacyEvidenceExists) else None)
        if service is not None and service not in self.scope.services:
            raise ValueError("predicate service must be within the check scope")
        return self

    @property
    def id(self) -> str:
        return structural_id("C-", self.model_dump(exclude={"alias", "description"}))


Verdict = Literal["SUPPORT", "CONTRADICT", "NEUTRAL", "UNKNOWN"]
CheckReason = Literal[
    "conflicting_fact_identity", "unsupported_relation", "unsupported_check_version",
    "positive_match", "unresolved_rpc_fields", "complete_no_match", "open_scope_no_match",
    "returned_category_present", "ambiguous_category_fact", "returned_category_not_present",
    "missing_category_fact", "summary_present", "missing_summary_fact",
    "ambiguous_metric_fact", "missing_metric_fact", "comparison_true", "comparison_false",
    "unresolved_legacy_peer",
]
PlanReason = Literal[
    "plan_missing", "unsupported_failure_mode", "unsupported_plan_version",
    "unsupported_check_version", "no_required_support_checks", "contradiction_relations_reserved",
    "explicit_contradiction", "required_results_missing", "required_check_not_supported",
    "all_required_support_checks_established",
]


class CheckResult(Contract):
    schema_version: Literal[1] = 1
    check_id: str
    verdict: Verdict
    input_kind: Literal["observation", "legacy_evidence"]
    evaluator: Literal["observation_verifier_v1", "legacy_compatibility_v1"]
    matched_ids: tuple[str, ...] = ()
    relevant_ids: tuple[str, ...] = ()
    incompatible_ids: tuple[str, ...] = ()
    coverage_used: tuple[CheckCoverageView, ...] = ()
    reason: CheckReason
    summary: str = Field(default="", max_length=2000)

    @field_validator("matched_ids", "relevant_ids", "incompatible_ids")
    @classmethod
    def canonical_ids(cls, values):
        return tuple(sorted(set(values)))

    @field_validator("coverage_used")
    @classmethod
    def canonical_coverage(cls, views):
        # Warning prose is display-only, including when copies are deduplicated.
        unique = {structural_id("", view.identity_payload()):
                  view.model_copy(update={"warnings": ()}) for view in views}
        return tuple(unique[key] for key in sorted(unique))

    @model_validator(mode="after")
    def origin_and_ids(self):
        expected = ("legacy_compatibility_v1" if self.input_kind == "legacy_evidence"
                    else "observation_verifier_v1")
        if self.evaluator != expected:
            raise ValueError("input origin and evaluator must agree")
        if not set(self.matched_ids + self.incompatible_ids) <= set(self.relevant_ids):
            raise ValueError("matched/incompatible IDs must be relevant")
        if any(view.input_kind != self.input_kind for view in self.coverage_used):
            raise ValueError("coverage input origin must agree")
        return self

    @property
    def id(self) -> str:
        payload = self.model_dump(exclude={"summary"})
        payload["coverage_used"] = [view.identity_payload() for view in self.coverage_used]
        return structural_id("CR-", payload)


class HypothesisVerificationPlan(Contract):
    schema_version: Literal[1] = 1
    semantic_version: str = Field(default="1", min_length=1)
    failure_mode: str = Field(min_length=1)
    checks: tuple[CheckSpec, ...] = ()
    catalog_key: str | None = None
    description: str = ""

    @field_validator("checks")
    @classmethod
    def unique_checks(cls, checks):
        if len({check.id for check in checks}) != len(checks):
            raise ValueError("duplicate semantic checks are not allowed")
        return tuple(sorted(checks, key=lambda check: check.id))

    @property
    def id(self) -> str:
        return structural_id("P-", {"schema_version": self.schema_version,
                                    "semantic_version": self.semantic_version,
                                    "failure_mode": self.failure_mode,
                                    "check_ids": [check.id for check in self.checks]})


class PlanVerificationResult(Contract):
    schema_version: Literal[1] = 1
    plan_id: str | None
    status: Literal["SUPPORTED", "UNCERTAIN"]
    check_result_ids: tuple[str, ...] = ()
    reason: PlanReason

    @field_validator("check_result_ids")
    @classmethod
    def canonical_results(cls, values):
        return tuple(sorted(set(values)))

    @property
    def id(self) -> str:
        return structural_id("PR-", self.model_dump())
