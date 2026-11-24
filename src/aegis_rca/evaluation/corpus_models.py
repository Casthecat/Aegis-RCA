"""Private corpus production/splits and the narrow execution-facing projection.

Execution manifests are harness-readable, not promises of human-public access.
Neither injection truth nor diagnostic Gold is part of that projection.
"""
from typing import Literal

from pydantic import AwareDatetime, Field, StrictBool, field_validator, model_validator

from ..models.identity import structural_id
from ..models.incident import Contract
from .stage_c_models import (AdmissionReference, BenchmarkBudgetSpec,
    BenchmarkExecutionProtocol, VARIANTS, opaque)
from .corpus_recording import CapturePolicy, PILOT_CAPTURE_POLICY
from .capture_freeze import CapturePolicyFreeze


SplitName = Literal["SMOKE", "DEV", "REPLICATE_HOLDOUT", "FAMILY_HOLDOUT"]
HOLDOUTS = {"REPLICATE_HOLDOUT", "FAMILY_HOLDOUT"}
MAX_EPISODES = 256


class ReplicateTrialRule(Contract):
    """Declared before capture; known-family robustness, never unseen-family truth."""
    scenario_family_id: str
    declared_at: AwareDatetime
    dev_trials: tuple[int, ...] = Field(min_length=1, max_length=64)
    replicate_holdout_trials: tuple[int, ...] = Field(min_length=1, max_length=64)
    telemetry_labels_and_ids_sealed: Literal[True]

    _opaque = field_validator("scenario_family_id")(opaque)

    @model_validator(mode="after")
    def disjoint(self):
        for values in (self.dev_trials, self.replicate_holdout_trials):
            if values != tuple(sorted(set(values))) or any(type(v) is not int or v < 1 for v in values):
                raise ValueError("trial rules require sorted unique positive indices")
        if set(self.dev_trials) & set(self.replicate_holdout_trials):
            raise ValueError("DEV and replicate holdout trials must be disjoint")
        return self


class HoldoutAccessEvent(Contract):
    candidate_id: str
    at: AwareDatetime
    actor: str = Field(min_length=1, max_length=200)
    role: Literal["answerability_reviewer", "qa_operator", "developer", "stage_c_harness"]
    material: Literal["telemetry", "gold", "candidate_identity"]
    purpose: Literal["blinded_review", "qa_reconciliation", "declared_checkpoint", "development", "manual_inspection"]
    checkpoint_id: str | None = Field(default=None, min_length=1, max_length=80)

    _opaque = field_validator("candidate_id")(opaque)

    @model_validator(mode="after")
    def checkpoint(self):
        if self.purpose == "declared_checkpoint" and not self.checkpoint_id:
            raise ValueError("checkpoint access requires a declared checkpoint identifier")
        return self


class CorpusProtocol(Contract):
    schema_version: Literal[1] = 1
    revision: Literal["p2.2-v1"] = "p2.2-v1"
    diagnostic_truth: Literal["blinded_approved_telemetry_plus_frozen_rubric"] = "blinded_approved_telemetry_plus_frozen_rubric"
    split_meaning: Literal["replicate_known_family_and_unseen_family_separated"] = "replicate_known_family_and_unseen_family_separated"
    holdout_access: Literal["telemetry_labels_and_ids_sealed_except_declared_checkpoint"] = "telemetry_labels_and_ids_sealed_except_declared_checkpoint"
    benchmark_candidate_version: Literal[1] = 1
    planned_real_multi_edge: Literal[0] = 0

    @property
    def id(self):
        return structural_id("CPR-", self.model_dump())


class CorpusFreezeSpec(Contract):
    schema_version: Literal[1] = 1
    corpus_version: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,79}$")
    phase: Literal["pilot", "phase_a", "smoke"] = "pilot"
    frozen_at: AwareDatetime
    frozen_by: str = Field(min_length=1, max_length=200)
    capture_policy_id: str = Field(pattern=r"^CP-[0-9a-f]{64}$")
    capture_policy: CapturePolicy = PILOT_CAPTURE_POLICY
    phase_a_capture_freeze: CapturePolicyFreeze | None = None
    query_policy_id: str = Field(pattern=r"^QP-[0-9a-f]{64}$")
    sanitizer_ruleset: Literal["control-metadata-v1"] = "control-metadata-v1"
    gold_diagnostic_rubric_id: str = Field(pattern=r"^GDR-[0-9a-f]{64}$")
    scoring_protocol_id: str = Field(pattern=r"^BSP-[0-9a-f]{64}$")
    corpus_protocol: CorpusProtocol = CorpusProtocol()
    benchmark_budget: BenchmarkBudgetSpec = BenchmarkBudgetSpec()
    benchmark_execution_protocol: BenchmarkExecutionProtocol = BenchmarkExecutionProtocol()
    benchmark_variant_ids: tuple[str, ...] = tuple(VARIANTS[key].id for key in ("B0", "B1", "B2"))
    replicate_trial_rules: tuple[ReplicateTrialRule, ...] = Field(default=(), max_length=MAX_EPISODES)
    holdout_access_events: tuple[HoldoutAccessEvent, ...] = Field(default=(), max_length=1024)
    performance_results_unseen: Literal[True]
    holdout_telemetry_labels_and_ids_sealed: StrictBool = False

    @model_validator(mode="after")
    def protocols(self):
        if self.capture_policy_id != self.capture_policy.id:
            raise ValueError("capture policy ID must bind the frozen capture policy")
        if self.phase == "phase_a":
            if self.phase_a_capture_freeze is None or self.phase_a_capture_freeze.policy != self.capture_policy:
                raise ValueError("Phase-A requires a matching capture-policy freeze after pilot QA")
            if self.phase_a_capture_freeze.frozen_at >= self.frozen_at:
                raise ValueError("capture-policy freeze must precede corpus freeze")
        elif self.phase_a_capture_freeze is not None:
            raise ValueError("pilot and SMOKE do not consume a Phase-A capture-policy freeze")
        if self.benchmark_variant_ids != tuple(VARIANTS[key].id for key in ("B0", "B1", "B2")):
            raise ValueError("P2.2 freezes the existing B0/B1/B2 identities")
        families = [rule.scenario_family_id for rule in self.replicate_trial_rules]
        if len(families) != len(set(families)):
            raise ValueError("one replicate rule per family is required")
        if any(event.at > self.frozen_at for event in self.holdout_access_events):
            raise ValueError("future access events cannot be declared at freeze")
        return self


class ExecutionCorpusMember(Contract):
    admission: AdmissionReference
    split: SplitName


class ExecutionCorpusManifest(Contract):
    schema_version: Literal[1] = 1
    artifact_kind: Literal["execution_corpus_manifest"] = "execution_corpus_manifest"
    corpus_id: str = Field(pattern=r"^CORP-[0-9a-f]{64}$")
    corpus_version: str = Field(min_length=1, max_length=80)
    frozen_at: AwareDatetime
    corpus_protocol_id: str = Field(pattern=r"^CPR-[0-9a-f]{64}$")
    snapshot_version: Literal[1] = 1
    sanitizer_ruleset: Literal["control-metadata-v1"] = "control-metadata-v1"
    capture_policy_id: str = Field(pattern=r"^CP-[0-9a-f]{64}$")
    query_policy_id: str = Field(pattern=r"^QP-[0-9a-f]{64}$")
    expected_scoring_protocol_id: str = Field(pattern=r"^BSP-[0-9a-f]{64}$")
    members: tuple[ExecutionCorpusMember, ...] = Field(min_length=1, max_length=MAX_EPISODES)
    human_access: Literal["restricted_by_split_protocol_not_public"] = "restricted_by_split_protocol_not_public"

    @model_validator(mode="after")
    def unique(self):
        ids = tuple(member.admission.candidate_id for member in self.members)
        if ids != tuple(sorted(set(ids))):
            raise ValueError("execution members must have unique canonical order")
        hashes = [member.admission.snapshot_sha256 for member in self.members]
        if len(hashes) != len(set(hashes)):
            raise ValueError("duplicate candidate snapshot in corpus")
        if any(member.admission.ruleset != self.sanitizer_ruleset for member in self.members):
            raise ValueError("mixed sanitizer rulesets are not a frozen corpus")
        return self

    @property
    def id(self):
        return structural_id("ECP-", self.model_dump(mode="json"))


class PrivateCorpusMember(Contract):
    private_episode_id: str
    candidate_id: str
    scenario_family_id: str
    family_trial_index: int = Field(ge=1, le=10000)
    split: SplitName
    admission: AdmissionReference
    injection_truth_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    production_record_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    gold_label_id: str = Field(pattern=r"^GL-[0-9a-f]{64}$")
    gold_label_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    telemetry_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    injection_truth_reference: str
    production_record_reference: str
    gold_label_reference: str

    _opaque = field_validator("private_episode_id", "candidate_id", "scenario_family_id")(opaque)

    @model_validator(mode="after")
    def safe_references(self):
        if self.admission.candidate_id != self.candidate_id:
            raise ValueError("private member admission mismatch")
        expected = {"injection_truth_reference": f"episodes/{self.private_episode_id}/injection.json",
            "production_record_reference": f"episodes/{self.private_episode_id}/production.json",
            "gold_label_reference": f"gold/{self.candidate_id}.json"}
        if any(getattr(self, field) != value for field, value in expected.items()):
            raise ValueError("private references must use canonical local artifact locations")
        return self


class PrivateEpisodeReference(Contract):
    private_episode_id: str
    injection_truth_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    production_record_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    _opaque = field_validator("private_episode_id")(opaque)


class PrivateCorpusManifest(Contract):
    schema_version: Literal[1] = 1
    artifact_kind: Literal["private_corpus_manifest"] = "private_corpus_manifest"
    spec: CorpusFreezeSpec
    members: tuple[PrivateCorpusMember, ...] = Field(min_length=1, max_length=MAX_EPISODES)
    attempted_episodes: tuple[PrivateEpisodeReference, ...] = Field(min_length=1, max_length=MAX_EPISODES)

    @model_validator(mode="after")
    def coherent(self):
        ids = [row.candidate_id for row in self.members]
        if ids != sorted(set(ids)):
            raise ValueError("private members require unique sorted candidate IDs")
        episodes = [row.private_episode_id for row in self.attempted_episodes]
        if episodes != sorted(set(episodes)):
            raise ValueError("attempt ledger requires unique sorted episode IDs")
        if any(row.private_episode_id not in episodes for row in self.members):
            raise ValueError("every member must refer to the attempted-episode ledger")
        return self

    @property
    def id(self):
        return structural_id("CORP-", self.model_dump(mode="json"))


class CorpusQASummary(Contract):
    schema_version: Literal[1] = 1
    phase: Literal["pilot", "phase_a", "smoke"]
    corpus_id: str = Field(pattern=r"^CORP-[0-9a-f]{64}$")
    attempted_episode_count: int = Field(ge=1, le=MAX_EPISODES)
    successfully_recorded_count: int = Field(ge=0, le=MAX_EPISODES)
    real_recorded_count: int = Field(ge=0, le=MAX_EPISODES)
    sanitize_rejected_count: int = Field(ge=0, le=MAX_EPISODES)
    scan_blocked_count: int = Field(ge=0, le=MAX_EPISODES)
    review_rejected_count: int = Field(ge=0, le=MAX_EPISODES)
    approved_count: int = Field(ge=0, le=MAX_EPISODES)
    included_count: int = Field(ge=0, le=MAX_EPISODES)
    answerability_distribution: dict[str, int]
    diagnostic_family_distribution: dict[str, int]
    split_distribution: dict[SplitName, int]
    sanitizer_rulesets: tuple[str, ...]
    capture_policy_ids: tuple[str, ...]
    query_policy_ids: tuple[str, ...]
    seed_distribution: dict[str, int]
    capture_duration_seconds_distribution: dict[str, int]
    truncated_signal_distribution: dict[str, int]
    collection_error_distribution: dict[str, int]
    rejection_reason_histogram: dict[str, int]
    real_multi_edge_count: int = Field(ge=0, le=MAX_EPISODES)
    injection_effective_but_insufficient_evidence_count: int = Field(ge=0, le=MAX_EPISODES)
    planned_real_multi_edge: Literal[0] = 0
    unseen_family_positive_count: int = Field(ge=0, le=MAX_EPISODES)
    interpretation: Literal["collection_QA_not_Aegis_performance"] = "collection_QA_not_Aegis_performance"

    @model_validator(mode="after")
    def counts(self):
        if not self.included_count <= self.approved_count <= self.successfully_recorded_count <= self.attempted_episode_count:
            raise ValueError("corpus QA lifecycle counts are inconsistent")
        if self.real_recorded_count > self.successfully_recorded_count:
            raise ValueError("real recordings cannot exceed verified recorded sources")
        if self.phase == "smoke" and self.real_recorded_count:
            raise ValueError("SMOKE QA cannot claim real recording coverage")
        if sum(self.split_distribution.values()) != self.included_count:
            raise ValueError("split distribution differs from membership")
        if sum(self.answerability_distribution.values()) > self.approved_count:
            raise ValueError("Gold count cannot exceed approved count")
        for name in ("answerability_distribution", "diagnostic_family_distribution", "split_distribution",
                "seed_distribution", "capture_duration_seconds_distribution", "truncated_signal_distribution",
                "collection_error_distribution", "rejection_reason_histogram"):
            if any(v < 1 or v > self.attempted_episode_count for v in getattr(self, name).values()):
                raise ValueError("QA histograms require bounded positive episode counts")
        return self


class CorpusFreezeReceipt(Contract):
    schema_version: Literal[1] = 1
    corpus_id: str = Field(pattern=r"^CORP-[0-9a-f]{64}$")
    execution_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    private_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    qa_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class HoldoutAccessAssessment(Contract):
    schema_version: Literal[1] = 1
    corpus_id: str = Field(pattern=r"^CORP-[0-9a-f]{64}$")
    split_valid: StrictBool
    events: tuple[HoldoutAccessEvent, ...] = Field(max_length=1024)
    invalidated_candidate_ids: tuple[str, ...] = Field(max_length=MAX_EPISODES)
    reason: Literal["holdout_access_compliant", "holdout_access_violation"]

    @model_validator(mode="after")
    def consistent(self):
        if self.split_valid != (not self.invalidated_candidate_ids):
            raise ValueError("access assessment validity disagrees with violations")
        if self.reason != ("holdout_access_compliant" if self.split_valid else "holdout_access_violation"):
            raise ValueError("access assessment reason disagrees with violations")
        return self
