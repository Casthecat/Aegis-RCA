"""Bounded current hypothesis semantics, state freshness and resolution provenance."""
from typing import Literal
from pydantic import Field, model_validator
from .incident import Contract, TimeWindow
from .hypothesis import HypothesisSpec
from .identity import structural_id
from ..trace_acquisition import TraceBindingOutcome

MAX_ACTIVE_HYPOTHESES = 16


def hypothesis_order(h):
    return h.caller, h.callee, h.id


class HypothesisAdmissionPolicy(Contract):
    schema_version: Literal[1] = 1
    kind: Literal["dependency_connectivity"] = "dependency_connectivity"
    max_active_hypotheses: int = Field(default=4, ge=1, le=MAX_ACTIVE_HYPOTHESES, strict=True)
    operation_mode: Literal["edge"] = "edge"

    @property
    def id(self):
        return structural_id("HP-", self.model_dump())


class HypothesisAcquisitionReference(Contract):
    task_spec_id: str
    attempt_id: str
    semantic_state_digest: str


class HypothesisStateBasis(Contract):
    schema_version: Literal[1] = 1
    policy_id: str
    seed_services: tuple[str, ...]
    window: TimeWindow
    scope_id: str | None
    scope_basis_id: str | None
    acquisitions: tuple[HypothesisAcquisitionReference, ...] = Field(default=(), max_length=100)

    @property
    def id(self):
        return structural_id("HSB-", self.model_dump())


class HypothesisTraceResolution(Contract):
    attempt_id: str
    outcome: TraceBindingOutcome
    observation_count: int = Field(ge=0)
    observation_ids_digest: str = Field(pattern=r"^sha256-[0-9a-f]{64}$")

    @model_validator(mode="after")
    def verified_only(self):
        if self.outcome != "verified" and (self.observation_count or self.observation_ids_digest != structural_id("sha256-", {"observation_ids": []})):
            raise ValueError("unverified resolution cannot contribute observations")
        return self


class HypothesisResolutionBasis(Contract):
    schema_version: Literal[1] = 1
    resolutions: tuple[HypothesisTraceResolution, ...] = Field(default=(), max_length=100)
    eligible_count: int = Field(ge=0)
    eligible_hypothesis_ids_digest: str = Field(pattern=r"^sha256-[0-9a-f]{64}$")
    active_hypothesis_ids: tuple[str, ...] = Field(max_length=MAX_ACTIVE_HYPOTHESES)
    candidate_support_digest: str = Field(pattern=r"^sha256-[0-9a-f]{64}$")
    overflow_digest: str = Field(pattern=r"^sha256-[0-9a-f]{64}$")

    @property
    def id(self):
        return structural_id("HRB-", self.model_dump())


class HypothesisPortfolio(Contract):
    schema_version: Literal[1] = 1
    policy: HypothesisAdmissionPolicy
    active: tuple[HypothesisSpec, ...] = Field(max_length=MAX_ACTIVE_HYPOTHESES)
    state_basis: HypothesisStateBasis
    resolution_basis: HypothesisResolutionBasis
    overflow_count: int = Field(ge=0)
    overflow_digest: str = Field(pattern=r"^sha256-[0-9a-f]{64}$")

    @model_validator(mode="after")
    def coherent(self):
        ids = tuple(h.id for h in self.active)
        if len(ids) != len(set(ids)) or self.active != tuple(sorted(self.active, key=hypothesis_order)):
            raise ValueError("active hypotheses require unique canonical structural order")
        if len(ids) > self.policy.max_active_hypotheses or any(h.operation is not None for h in self.active):
            raise ValueError("portfolio violates edge-level admission policy")
        if self.state_basis.policy_id != self.policy.id:
            raise ValueError("hypothesis policy basis mismatch")
        rb = self.resolution_basis
        if (rb.active_hypothesis_ids != ids or rb.eligible_count != len(ids) + self.overflow_count
                or rb.overflow_digest != self.overflow_digest):
            raise ValueError("portfolio resolution/overflow contract mismatch")
        if self.overflow_count and len(ids) != self.policy.max_active_hypotheses:
            raise ValueError("overflow requires a full active set")
        if not self.overflow_count and self.overflow_digest != structural_id("sha256-", {"hypothesis_ids": []}):
            raise ValueError("empty overflow must use canonical empty digest")
        if not self.overflow_count and rb.eligible_hypothesis_ids_digest != structural_id("sha256-", {"hypothesis_ids": list(ids)}):
            raise ValueError("non-overflow candidate digest must agree with active set")
        keys = tuple(r.attempt_id for r in rb.resolutions)
        if keys != tuple(sorted(set(keys))):
            raise ValueError("resolution references must be unique and canonical")
        if tuple(r.attempt_id for r in rb.resolutions) != tuple(a.attempt_id for a in self.state_basis.acquisitions):
            raise ValueError("resolution attempts must equal state basis acquisitions")
        return self

    @property
    def id(self):
        return structural_id("HPS-", {"policy_id": self.policy.id, "active_hypothesis_ids": [h.id for h in self.active]})
