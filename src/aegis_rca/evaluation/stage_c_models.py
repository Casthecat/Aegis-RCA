"""Stage C identities: architectural treatment, trial, semantics and archive bytes."""
from typing import Literal
from uuid import UUID
from pydantic import Field, field_validator, model_validator

from ..models.incident import Contract, Budget
from ..models.identity import structural_id
from ..models.execution import InvestigationExecutionPolicy
from ..models.portfolio import HypothesisAdmissionPolicy, HypothesisPortfolio
from ..models.hypothesis import IncidentReport
from ..models.acquisition import AttemptRecord, TaskSpec
from ..models.scheduling import IntentRecord, TaskIntent
from ..verification.persisted import PersistedHypothesisVerification
from .sanitization import canonical_bytes, digest


def opaque(value):
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError("canonical candidate UUID required")
    return value


class BenchmarkVariantSpec(Contract):
    schema_version: Literal[1] = 1
    display_key: str = Field(max_length=80)
    display_name: str = Field(max_length=160)
    runtime_policy: InvestigationExecutionPolicy
    initial_survey: Literal["metric_and_trace_original_seeds"] = "metric_and_trace_original_seeds"
    llm: Literal["off"] = "off"

    @property
    def id(self):
        return structural_id("BV-", self.model_dump(exclude={"display_key", "display_name"}))


VARIANTS = {
    "B0": BenchmarkVariantSpec(display_key="B0", display_name="survey_stop", runtime_policy=InvestigationExecutionPolicy(
        required_check_scheduling=False, hypothesis_policy=HypothesisAdmissionPolicy(max_active_hypotheses=1))),
    "B1": BenchmarkVariantSpec(display_key="B1", display_name="single_hypothesis_check_driven", runtime_policy=InvestigationExecutionPolicy(
        hypothesis_policy=HypothesisAdmissionPolicy(max_active_hypotheses=1))),
    "B2": BenchmarkVariantSpec(display_key="B2", display_name="full_p1", runtime_policy=InvestigationExecutionPolicy()),
}


class BenchmarkBudgetSpec(Contract):
    schema_version: Literal[1] = 1
    max_rounds: Literal[3] = 3
    max_tool_calls: Literal[6] = 6
    tool_timeout_seconds: Literal[5] = 5
    global_deadline_seconds: Literal[120] = 120

    @property
    def id(self):
        return structural_id("BB-", self.model_dump())

    def runtime_budget(self):
        return Budget(**self.model_dump(exclude={"schema_version", "global_deadline_seconds"}))


class BenchmarkExecutionProtocol(Contract):
    schema_version: Literal[1] = 1
    admission: Literal["stage_c_approved_only"] = "stage_c_approved_only"
    snapshot_version: Literal[1] = 1
    variant_schema: Literal[1] = 1
    budget_schema: Literal[1] = 1
    execution_rules: Literal["replay_exact_admitted_object_no_reread_no_live_no_llm"] = "replay_exact_admitted_object_no_reread_no_live_no_llm"
    implementation_revision: Literal["p2.1-v1"] = "p2.1-v1"

    @property
    def id(self):
        return structural_id("BEP-", self.model_dump())


class BenchmarkScoringProtocol(Contract):
    schema_version: Literal[1] = 1
    label_schema: Literal["legacy_Label_v1_nonexhaustive"] = "legacy_Label_v1_nonexhaustive"
    metrics: Literal["stage_c_multiclaim_known_edge_recall_v1"] = "stage_c_multiclaim_known_edge_recall_v1"
    extra_edges: Literal["unscored_without_exhaustive_labels"] = "unscored_without_exhaustive_labels"
    # Scoring provenance can be revised without changing the execution protocol.
    revision: str = Field(default="p2.1-v1", min_length=1, max_length=80)

    @property
    def id(self):
        return structural_id("BSP-", self.model_dump())


class TrialSpec(Contract):
    deterministic: Literal[True] = True
    trial_index: Literal[0] = 0

    @field_validator("trial_index", mode="before")
    @classmethod
    def exact_zero(cls, value):
        if type(value) is not int or value != 0:
            raise ValueError("deterministic trials require integer index zero")
        return value


class AdmissionReference(Contract):
    candidate_id: str
    bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    ruleset: str
    snapshot_version: Literal[1] = 1
    review_status: Literal["approved"] = "approved"

    _opaque = field_validator("candidate_id")(opaque)


class BenchmarkRunKey(Contract):
    schema_version: Literal[1] = 1
    candidate_id: str
    bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    variant_id: str = Field(pattern=r"^BV-[0-9a-f]{64}$")
    budget_id: str = Field(pattern=r"^BB-[0-9a-f]{64}$")
    execution_protocol_id: str = Field(pattern=r"^BEP-[0-9a-f]{64}$")
    trial: TrialSpec = TrialSpec()

    _opaque = field_validator("candidate_id")(opaque)

    @property
    def id(self):
        return structural_id("BRK-", self.model_dump())


FailureCategory = Literal["no_hypothesis_candidate", "variant_did_not_schedule_required_intent",
    "required_acquisition_unavailable", "replay_selection_ambiguous", "acquisition_failed", "budget_exhausted",
    "budget_prevented_required_acquisition", "evidence_acquired_check_unknown", "hypothesis_overflow",
    "unsupported_check_acquisition_mapping", "invalid_scope", "llm_error", "harness_error"]


class BenchmarkUsage(Contract):
    rounds: int = Field(ge=0, le=10)
    tool_calls: int = Field(ge=0, le=100)
    completed_intents: int = Field(ge=0)
    failed_intents: int = Field(ge=0)
    unavailable_intents: int = Field(ge=0)
    ambiguous_intents: int = Field(ge=0)
    pending_intents: int = Field(ge=0)
    timeouts: int = Field(ge=0)
    duplicate_intent_count: int = Field(ge=0)
    llm_calls: Literal[0] = 0
    llm_input_tokens: Literal[0] = 0
    llm_output_tokens: Literal[0] = 0
    llm_cost: Literal[0] = 0
    network_calls: Literal[0] = 0


class BenchmarkSemanticResult(Contract):
    schema_version: Literal[1] = 1
    run_key_id: str = Field(pattern=r"^BRK-[0-9a-f]{64}$")
    execution_id: str
    termination_reason: str
    report: IncidentReport
    portfolio: HypothesisPortfolio | None
    hypothesis_verifications: tuple[PersistedHypothesisVerification, ...] = Field(max_length=16)
    # Ordered by physical reservation sequence, not async completion time.
    attempted_tasks: tuple[TaskSpec, ...] = Field(max_length=100)
    attempts: tuple[AttemptRecord, ...] = Field(max_length=100)
    intents: tuple[IntentRecord, ...] = Field(max_length=256)
    usage: BenchmarkUsage
    failure_categories: tuple[FailureCategory, ...]

    @model_validator(mode="after")
    def coherent(self):
        if self.execution_id != self.run_key_id:
            raise ValueError("execution namespace must equal BRK")
        if any(a.execution_id != self.execution_id for a in self.attempts):
            raise ValueError("attempt namespace mismatch")
        if tuple(a.task_spec_id for a in self.attempts) != tuple(t.id for t in self.attempted_tasks):
            raise ValueError("attempt sequence must reference exact task sequence")
        if len({a.id for a in self.attempts}) != len(self.attempts):
            raise ValueError("duplicate attempts")
        if len({r.id for r in self.intents}) != len(self.intents):
            raise ValueError("duplicate ledger dispositions")
        statuses = [r.status for r in self.intents]
        for status, field in (("completed", "completed_intents"), ("failed", "failed_intents"),
            ("unavailable", "unavailable_intents"), ("ambiguous_replay_selection", "ambiguous_intents"), ("pending", "pending_intents")):
            if getattr(self.usage, field) != statuses.count(status):
                raise ValueError("intent usage disagrees with ledger")
        if self.usage.timeouts != sum(a.tool_status == "timeout" for a in self.attempts):
            raise ValueError("timeout usage disagrees with attempts")
        logical = {TaskIntent(signal=t.signal, services=t.services, window=t.window).id for t in self.attempted_tasks}
        if self.usage.duplicate_intent_count != len(self.attempts) - len(logical):
            raise ValueError("duplicate logical intent cost disagrees with attempts")
        attempts_by_id = {a.id: a for a in self.attempts}
        for row in self.intents:
            if row.status in {"completed", "failed"}:
                attempt = attempts_by_id.get(row.committed_attempt_id)
                if attempt is None or attempt.task_spec_id != row.materialized_task_spec_id:
                    raise ValueError("ledger must refer to saved acquisition provenance")
        if self.report.tool_calls != len(self.attempts) or self.usage.tool_calls != len(self.attempts):
            raise ValueError("tool cost must equal attempts")
        if self.report.rounds != self.usage.rounds or self.termination_reason != self.report.termination_reason:
            raise ValueError("report/control mismatch")
        active = tuple(h.id for h in self.portfolio.active) if self.portfolio else ()
        if tuple(p.hypothesis_spec.id for p in self.hypothesis_verifications) != active:
            raise ValueError("PHV set must equal active portfolio")
        if tuple(a.hypothesis_spec.id for a in self.report.assessments) != active:
            raise ValueError("assessment set must equal active portfolio")
        by_id = {p.hypothesis_spec.id: p for p in self.hypothesis_verifications}
        claimed = tuple(c.hypothesis_id for c in self.report.claims)
        supported = tuple(p.hypothesis_spec.id for p in self.hypothesis_verifications if p.evaluation.plan_result.status == "SUPPORTED")
        if claimed != supported:
            raise ValueError("claims must exactly project supported PHVs in portfolio order")
        for assessment, phv in zip(self.report.assessments, self.hypothesis_verifications):
            if (assessment.status == "SUPPORTED") != (phv.evaluation.plan_result.status == "SUPPORTED"):
                raise ValueError("assessment disagrees with PHV")
        if self.report.status != ("SUPPORTED" if supported else "INCONCLUSIVE"):
            raise ValueError("report status disagrees with supported set")
        for claim in self.report.claims:
            phv = by_id.get(claim.hypothesis_id)
            if (claim.claim_class != "verified_failure_mode" or phv is None or
                phv.evaluation.plan_result.status != "SUPPORTED" or claim.plan_result_id != phv.evaluation.plan_result.id or
                set(claim.check_result_ids) != set(phv.evaluation.plan_result.check_result_ids)):
                raise ValueError("claim must reference its supported PHV")
        if self.failure_categories != tuple(sorted(set(self.failure_categories))):
            raise ValueError("failure categories must be canonical")
        return self

    @property
    def id(self):
        return structural_id("BRS-", self.model_dump())


class BenchmarkRunResult(Contract):
    schema_version: Literal[1] = 1
    run_key: BenchmarkRunKey
    run_key_id: str
    admission: AdmissionReference
    variant: BenchmarkVariantSpec
    budget: BenchmarkBudgetSpec
    execution_protocol: BenchmarkExecutionProtocol
    semantic: BenchmarkSemanticResult
    semantic_result_id: str
    duration_ms: float = Field(ge=0, allow_inf_nan=False)
    archive_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def valid_identities(self):
        key = self.run_key
        if (key.id != self.run_key_id or self.semantic.run_key_id != key.id or
            self.semantic.id != self.semantic_result_id or key.variant_id != self.variant.id or
            key.budget_id != self.budget.id or key.execution_protocol_id != self.execution_protocol.id):
            raise ValueError("run identity mismatch")
        if any(getattr(key, k) != getattr(self.admission, k) for k in ("candidate_id", "bundle_sha256", "snapshot_sha256")):
            raise ValueError("admission mismatch")
        if self.semantic.portfolio is not None and self.semantic.portfolio.policy != self.variant.runtime_policy.hypothesis_policy:
            raise ValueError("observed admission policy differs from declared variant")
        if (self.semantic.usage.rounds > self.budget.max_rounds or
            self.semantic.usage.tool_calls > self.budget.max_tool_calls):
            raise ValueError("observed execution exceeded the frozen budget")
        if not self.variant.runtime_policy.required_check_scheduling and any(t.signal == "log" for t in self.semantic.attempted_tasks):
            raise ValueError("survey-only treatment cannot acquire follow-up logs")
        if digest(canonical_bytes(self.model_dump(mode="json", exclude={"archive_sha256"}))) != self.archive_sha256:
            raise ValueError("archive digest mismatch")
        return self


class ExperimentSpec(Contract):
    schema_version: Literal[1] = 1
    candidate_ids: tuple[str, ...] = Field(min_length=1, max_length=256)
    variants: tuple[Literal["B0", "B1", "B2"], ...] = ("B0", "B1", "B2")
    trial: TrialSpec = TrialSpec()

    @field_validator("candidate_ids")
    @classmethod
    def canonical_candidates(cls, values):
        if len(set(values)) != len(values):
            raise ValueError("duplicate candidate is not an independent sample")
        return tuple(sorted(opaque(v) for v in values))

    @field_validator("variants")
    @classmethod
    def canonical_variants(cls, values):
        if not values or len(set(values)) != len(values):
            raise ValueError("unique nonempty variants required")
        return tuple(sorted(values))


class ExperimentManifest(Contract):
    schema_version: Literal[1] = 1
    spec: ExperimentSpec
    variants: tuple[BenchmarkVariantSpec, ...]
    admissions: tuple[AdmissionReference, ...] = Field(default=(), max_length=256)
    budget: BenchmarkBudgetSpec = BenchmarkBudgetSpec()
    execution_protocol: BenchmarkExecutionProtocol = BenchmarkExecutionProtocol()

    @model_validator(mode="after")
    def registry_matches(self):
        if self.variants != tuple(VARIANTS[k] for k in self.spec.variants):
            raise ValueError("manifest variant registry mismatch")
        ids = tuple(a.candidate_id for a in self.admissions)
        if ids != tuple(sorted(set(ids))) or not set(ids) <= set(self.spec.candidate_ids):
            raise ValueError("manifest admissions must be unique canonical requested candidates")
        return self

    @property
    def id(self):
        return structural_id("BM-", self.model_dump())


class HarnessFailure(Contract):
    schema_version: Literal[1] = 1
    candidate_id: str
    variant_id: str
    category: Literal["harness_error"] = "harness_error"
    reason: Literal["admission_rejected", "execution_or_archive_failed"]

    _opaque = field_validator("candidate_id")(opaque)
