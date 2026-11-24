"""Independent human diagnostic rubric and blinded, approved-telemetry Gold review.

This module has no injection-truth or investigation-result input. A reviewer
supplies every diagnostic judgment; packaging and validation never infer Gold.
"""
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, Field, StrictBool, field_validator, model_validator

from ..models.identity import structural_id
from ..models.incident import Contract
from ..service_scope import valid_service
from . import benchmark
from .sanitization import RULESET, canonical_bytes, digest
from .stage_c import admission_reference, publish, read_contract, separate_output
from .stage_c_models import AdmissionReference, opaque
from .corpus_recording import CapturePolicy, QUERY_POLICY


class GoldDiagnosticRubric(Contract):
    schema_version: Literal[1] = 1
    revision: Literal["human-rpc-establishment-v1"] = "human-rpc-establishment-v1"
    target: Literal["rpc_connection_establishment"] = "rpc_connection_establishment"
    target_definition: Literal[
        "During the incident window a caller attempted to invoke an identified dependency, "
        "but could not establish a usable RPC connection to that callee. Establishment "
        "failure is distinct from a callee application returning an error after connection."
    ] = (
        "During the incident window a caller attempted to invoke an identified dependency, "
        "but could not establish a usable RPC connection to that callee. Establishment "
        "failure is distinct from a callee application returning an error after connection."
    )
    evidence_rule: Literal[
        "Review the admitted telemetry in context: endpoint attribution, timing, transport "
        "diagnostics and corroboration. Direct connection-refusal or name-resolution failure "
        "tied to the caller/dependency attempt can establish the target. Availability status "
        "alone is ambiguous; topology alone establishes no failure. Resolve conflicting "
        "evidence explicitly and preserve uncertainty when the claim is not justified."
    ] = (
        "Review the admitted telemetry in context: endpoint attribution, timing, transport "
        "diagnostics and corroboration. Direct connection-refusal or name-resolution failure "
        "tied to the caller/dependency attempt can establish the target. Availability status "
        "alone is ambiguous; topology alone establishes no failure. Resolve conflicting "
        "evidence explicitly and preserve uncertainty when the claim is not justified."
    )
    answerability_rule: Literal[
        "mechanism_identifiable: at least one justified target claim; symptom_only: an "
        "incident symptom is supported but no target mechanism is justified; no_incident: "
        "adequate successful business-path evidence supports no incident symptoms and an exhaustive empty target set; "
        "insufficient_evidence: collection or coverage does not permit that determination."
    ] = (
        "mechanism_identifiable: at least one justified target claim; symptom_only: an "
        "incident symptom is supported but no target mechanism is justified; no_incident: "
        "adequate successful business-path evidence supports no incident symptoms and an exhaustive empty target set; "
        "insufficient_evidence: collection or coverage does not permit that determination."
    )
    exhaustiveness_rule: Literal[
        "Known positives are not a complete claim set. Exhaustiveness requires a separate "
        "human coverage judgment about all target claims justified by this admitted episode."
    ] = (
        "Known positives are not a complete claim set. Exhaustiveness requires a separate "
        "human coverage judgment about all target claims justified by this admitted episode."
    )
    root_cause_policy: Literal["ultimate_root_cause_not_expected"] = "ultimate_root_cause_not_expected"
    operation_policy: Literal["unconstrained_only"] = "unconstrained_only"

    @property
    def id(self):
        return structural_id("GDR-", self.model_dump())


DEFAULT_GOLD_RUBRIC = GoldDiagnosticRubric()
Answerability = Literal["mechanism_identifiable", "symptom_only", "no_incident", "insufficient_evidence"]


class ExpectedDiagnosticClaim(Contract):
    failure_mode: Literal["rpc_connection_establishment"] = "rpc_connection_establishment"
    caller: str
    callee: str
    operation: Literal[None] = None  # v1 rubric makes no operation-specific judgments.

    @model_validator(mode="after")
    def endpoints(self):
        if self.caller == self.callee or not all(valid_service(v) and v == v.strip() for v in (self.caller, self.callee)):
            raise ValueError("a diagnostic claim needs distinct bounded service identifiers")
        return self

    @property
    def id(self):
        return structural_id("GC-", self.model_dump(exclude_none=True))


class GoldReviewerProvenance(Contract):
    reviewer_id: str = Field(min_length=1, max_length=200)
    mode: Literal["independent", "weak_blind"]
    reviewed_at: AwareDatetime
    approved_telemetry_reviewed: Literal[True]
    injection_truth_sealed: Literal[True]
    stage_c_results_unseen: Literal[True]
    prior_injection_knowledge: StrictBool
    coverage_reviewed: Literal[True]
    coverage_sufficient: StrictBool
    empty_set_justified: StrictBool = False
    successful_path_and_no_incident_symptoms: StrictBool = False

    @model_validator(mode="after")
    def independence(self):
        if not self.reviewer_id.strip() or (self.mode == "independent" and self.prior_injection_knowledge):
            raise ValueError("independent review cannot claim prior injection knowledge")
        return self


class GoldJudgment(Contract):
    answerability: Answerability
    expected_claims: tuple[ExpectedDiagnosticClaim, ...] = Field(max_length=64)
    claim_labels_exhaustive: StrictBool
    forbidden_claim_classes: tuple[Literal["verified_root_cause"], ...] = ("verified_root_cause",)
    root_cause_policy: Literal["ultimate_root_cause_not_expected"] = "ultimate_root_cause_not_expected"
    reviewer: GoldReviewerProvenance
    private_notes: str = Field(default="", max_length=8000)

    @model_validator(mode="after")
    def justified(self):
        if len({c.id for c in self.expected_claims}) != len(self.expected_claims):
            raise ValueError("duplicate diagnostic claims")
        if self.expected_claims != tuple(sorted(self.expected_claims, key=lambda c: c.id)):
            raise ValueError("expected claims require deterministic GC order")
        if self.forbidden_claim_classes != ("verified_root_cause",):
            raise ValueError("v1 rubric forbids ultimate root-cause claims")
        if self.answerability == "mechanism_identifiable":
            if not self.expected_claims or not self.reviewer.coverage_sufficient:
                raise ValueError("identifiable mechanism requires justified positive claims")
        elif self.expected_claims:
            raise ValueError("non-mechanism answerability cannot carry expected mechanism claims")
        if self.answerability == "no_incident" and not (
            self.claim_labels_exhaustive and self.reviewer.coverage_sufficient and self.reviewer.empty_set_justified
            and self.reviewer.successful_path_and_no_incident_symptoms
        ):
            raise ValueError("no_incident needs an independently justified exhaustive empty set")
        if self.answerability == "insufficient_evidence" and self.reviewer.coverage_sufficient:
            raise ValueError("insufficient evidence cannot assert sufficient coverage")
        return self


class BenchmarkGoldLabelV2(GoldJudgment):
    schema_version: Literal[2] = 2
    candidate_id: str
    snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sanitizer_ruleset: Literal["control-metadata-v1"] = "control-metadata-v1"
    gold_diagnostic_rubric_id: str = DEFAULT_GOLD_RUBRIC.id
    capture_policy_id: str = Field(pattern=r"^CP-[0-9a-f]{64}$")
    query_policy_id: str = Field(pattern=r"^QP-[0-9a-f]{64}$")
    review_packet_id: str | None = Field(default=None, pattern=r"^GRP-[0-9a-f]{64}$")
    frozen_at: AwareDatetime

    _opaque = field_validator("candidate_id")(opaque)

    @model_validator(mode="after")
    def frozen_rubric(self):
        if self.gold_diagnostic_rubric_id != DEFAULT_GOLD_RUBRIC.id:
            raise ValueError("unknown frozen diagnostic rubric")
        if self.frozen_at < self.reviewer.reviewed_at:
            raise ValueError("Gold freeze must follow answerability review")
        return self

    @property
    def id(self):
        return structural_id("GL-", self.model_dump())


class GoldReviewPacket(Contract):
    schema_version: Literal[1] = 1
    admission: AdmissionReference
    rubric: GoldDiagnosticRubric = DEFAULT_GOLD_RUBRIC
    capture_policy_id: str = Field(pattern=r"^CP-[0-9a-f]{64}$")
    query_policy_id: str = Field(pattern=r"^QP-[0-9a-f]{64}$")
    prepared_at: AwareDatetime
    capture_policy: CapturePolicy
    # All coverage information is derived solely from the approved snapshot.
    trace_records: int = Field(ge=0)
    log_records: int = Field(ge=0)
    metric_series: int = Field(ge=0)
    trace_truncated: StrictBool
    log_truncated: StrictBool
    metric_truncated: StrictBool
    trace_coverage_complete: StrictBool | None
    log_coverage_complete: StrictBool | None
    metric_coverage_complete: StrictBool | None
    collection_error_signals: tuple[str, ...]
    coverage_warnings: tuple[str, ...]

    @model_validator(mode="after")
    def policy_binding(self):
        if self.capture_policy.id != self.capture_policy_id or self.query_policy_id != QUERY_POLICY.id:
            raise ValueError("blind review packet capture/query policy mismatch")
        return self

    @property
    def id(self):
        return structural_id("GRP-", self.model_dump())


def _packet(admitted, capture_policy, query_policy, prepared_at):
    snapshot = admitted.snapshot
    query_policy.validate_incident(snapshot.incident)
    if (snapshot.incident.window.end-snapshot.incident.window.start).total_seconds() != capture_policy.duration_seconds:
        raise ValueError("review window differs from frozen capture policy")
    if admitted.ruleset != RULESET:
        raise ValueError("pilot Gold requires the frozen sanitizer ruleset")
    return GoldReviewPacket(admission=admission_reference(admitted), capture_policy_id=capture_policy.id,
        query_policy_id=query_policy.id, prepared_at=prepared_at, capture_policy=capture_policy,
        trace_records=len(snapshot.traces.spans), log_records=len(snapshot.logs.logs),
        metric_series=len(snapshot.metrics.series), trace_truncated=snapshot.traces.truncated,
        log_truncated=snapshot.logs.truncated, metric_truncated=snapshot.metrics.truncated,
        trace_coverage_complete=snapshot.traces.coverage_complete,
        log_coverage_complete=snapshot.logs.coverage_complete,
        metric_coverage_complete=snapshot.metrics.coverage_complete,
        collection_error_signals=tuple(sorted(snapshot.collection_errors)),
        coverage_warnings=tuple(sorted(set((*snapshot.traces.warnings, *snapshot.logs.warnings, *snapshot.metrics.warnings)))))


def prepare_gold_review(recordings_root, candidate_id, capture_policy, output, *, prepared_at=None):
    """Custodian packages approved telemetry; no private truth/results are accepted."""
    separate_output(recordings_root, output)
    admitted = benchmark.open_approved_candidate(Path(recordings_root), candidate_id)
    packet = _packet(admitted, capture_policy, QUERY_POLICY, prepared_at or datetime.now(timezone.utc))
    output = benchmark._root(Path(output))
    if output.exists():
        raise ValueError("review packet destination must be new")
    output.mkdir(parents=True)
    # Completion receipt is published last. No preselected answerability or approval.
    publish(output / "snapshot.json", admitted.snapshot.model_dump(mode="json"))
    publish(output / "rubric.json", packet.rubric.model_dump(mode="json"))
    publish(output / "packet.json", packet.model_dump(mode="json"))
    publish(output / "decision.template.json", dict(answerability=None, expected_claims=[],
        claim_labels_exhaustive=False, reviewer=dict(reviewer_id="", mode="independent",
            reviewed_at=None, approved_telemetry_reviewed=False, injection_truth_sealed=False,
            stage_c_results_unseen=False, prior_injection_knowledge=False, coverage_reviewed=False,
            coverage_sufficient=False, empty_set_justified=False, successful_path_and_no_incident_symptoms=False), private_notes=""))
    publish(output / "ready.json", dict(packet_id=packet.id, snapshot_sha256=admitted.snapshot_sha256))
    return packet


def freeze_gold(recordings_root, review_packet_dir, decision_path, output, *, frozen_at=None):
    """Only an explicit human judgment can freeze Gold, after current approval."""
    separate_output(recordings_root, output)
    packet_root = benchmark._root(Path(review_packet_dir))
    packet = read_contract(benchmark._safe(packet_root, "packet.json"), GoldReviewPacket)
    ready = benchmark._json(benchmark._read_bytes(benchmark._safe(packet_root, "ready.json")))
    snapshot_bytes = benchmark._read_bytes(benchmark._safe(packet_root, "snapshot.json"))
    if ready != dict(packet_id=packet.id, snapshot_sha256=packet.admission.snapshot_sha256) or digest(snapshot_bytes) != packet.admission.snapshot_sha256:
        raise ValueError("review packet completion/binding mismatch")
    if read_contract(benchmark._safe(packet_root, "rubric.json"), GoldDiagnosticRubric) != packet.rubric:
        raise ValueError("review packet rubric changed")
    admitted = benchmark.open_approved_candidate(Path(recordings_root), packet.admission.candidate_id)
    if admission_reference(admitted) != packet.admission:
        raise ValueError("approved telemetry changed since blind review packaging")
    expected_packet = _packet(admitted, packet.capture_policy, QUERY_POLICY, packet.prepared_at)
    if packet != expected_packet:
        raise ValueError("review packet metadata does not describe approved telemetry")
    judgment = read_contract(Path(decision_path), GoldJudgment)
    if judgment.reviewer.reviewed_at < packet.prepared_at:
        raise ValueError("answerability review must follow approved packet preparation")
    if judgment.answerability == "no_incident" and (
        packet.trace_truncated or packet.log_truncated or packet.metric_truncated or
        any(v is False for v in (packet.trace_coverage_complete, packet.log_coverage_complete, packet.metric_coverage_complete)) or
        packet.collection_error_signals or not packet.trace_records or not packet.log_records
    ):
        raise ValueError("incomplete observed coverage cannot justify a healthy exhaustive empty set")
    label = BenchmarkGoldLabelV2(**judgment.model_dump(), candidate_id=admitted.candidate_id,
        snapshot_sha256=admitted.snapshot_sha256, bundle_sha256=admitted.bundle_sha256,
        sanitizer_ruleset=admitted.ruleset, gold_diagnostic_rubric_id=packet.rubric.id,
        capture_policy_id=packet.capture_policy_id, query_policy_id=packet.query_policy_id,
        review_packet_id=packet.id, frozen_at=frozen_at or datetime.now(timezone.utc))
    output = Path(output)
    # Output filename is opaque for the Stage C scorer; publication never overwrites.
    if output.name != f"{label.candidate_id}.json":
        raise ValueError("Gold filename must be its opaque candidate UUID")
    publish(output, label.model_dump(mode="json"))
    return label
