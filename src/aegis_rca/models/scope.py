"""Bounded investigation permissions, distinct from diagnosis or acquisition."""

from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from .identity import structural_id
from .incident import Contract


Digest = Annotated[str, Field(pattern=r"^sha256-[0-9a-f]{64}$")]


class ScopeExpansionPolicy(Contract):
    schema_version: Literal[1] = 1
    rule: Literal["original_seed_one_hop_rpc"] = "original_seed_one_hop_rpc"
    rule_version: Literal[1] = 1
    max_admitted_neighbors: int = Field(default=8, ge=0, le=64, strict=True)
    max_service_name_length: int = Field(default=256, ge=1, le=1024, strict=True)

    @property
    def id(self):
        return structural_id("SP-", self.model_dump())


class ScopeExpansionRecord(Contract):
    discovered_service: str = Field(min_length=1, max_length=1024)
    from_seed_service: str = Field(min_length=1, max_length=1024)
    caller: str = Field(min_length=1, max_length=1024)
    callee: str = Field(min_length=1, max_length=1024)
    hop: Literal[1] = 1
    rule: Literal["original_seed_one_hop_rpc"] = "original_seed_one_hop_rpc"
    rule_version: Literal[1] = 1
    canonical_witness_observation_id: str = Field(pattern=r"^O-trace-[0-9a-f]{20}$")
    canonical_witness_attempt_id: str = Field(pattern=r"^A-[0-9a-f]{64}$")
    support_set_digest: Digest

    @model_validator(mode="after")
    def one_edge(self):
        if (self.caller == self.callee or self.discovered_service == self.from_seed_service
                or {self.caller, self.callee} != {self.from_seed_service, self.discovered_service}):
            raise ValueError("expansion must be one distinct directed seed-neighbor edge")
        return self

    @property
    def id(self):
        return structural_id("SX-", self.model_dump(include={"discovered_service", "from_seed_service",
            "caller", "callee", "hop", "rule", "rule_version"}))


class ScopeDerivationBasis(Contract):
    schema_version: Literal[1] = 1
    policy_id: str = Field(pattern=r"^SP-[0-9a-f]{64}$")
    acquisition_state_digest: Digest
    resolution_digest: Digest
    topology_witness_digest: Digest

    @property
    def id(self):
        return structural_id("SB-", self.model_dump())


class InvestigationScope(Contract):
    schema_version: Literal[1] = 1
    policy: ScopeExpansionPolicy
    basis: ScopeDerivationBasis
    seed_services: tuple[str, ...] = Field(min_length=1)
    expansions: tuple[ScopeExpansionRecord, ...] = Field(default=(), max_length=64)
    overflow_count: int = Field(ge=0, strict=True)
    overflow_digest: Digest
    excluded_identifier_count: int = Field(ge=0, strict=True)
    excluded_identifier_digest: Digest

    @field_validator("seed_services")
    @classmethod
    def canonical_seeds(cls, values):
        return tuple(sorted(set(values)))

    @field_validator("expansions")
    @classmethod
    def canonical_expansions(cls, values):
        return tuple(sorted(values, key=lambda item: item.discovered_service))

    @model_validator(mode="after")
    def permission_boundary(self):
        if self.basis.policy_id != self.policy.id:
            raise ValueError("scope basis must reference its structural policy")
        services = [item.discovered_service for item in self.expansions]
        if len(services) != len(set(services)) or len(services) > self.policy.max_admitted_neighbors:
            raise ValueError("scope neighbors must be unique and policy-bounded")
        if self.overflow_count and len(services) != self.policy.max_admitted_neighbors:
            raise ValueError("overflow requires a full admitted neighbor allowance")
        for item in self.expansions:
            if item.from_seed_service not in self.seed_services or item.discovered_service in self.seed_services:
                raise ValueError("one hop is relative to original seeds only")
            if any(not name.strip() or len(name) > self.policy.max_service_name_length
                   for name in (item.caller, item.callee)):
                raise ValueError("topology endpoint exceeds the policy identifier bound")
        return self

    @property
    def admitted_services(self):
        return tuple(sorted({*self.seed_services, *(e.discovered_service for e in self.expansions)}))

    @property
    def discovered_neighbor_count(self):
        return len(self.expansions) + self.overflow_count

    @property
    def id(self):
        return structural_id("SIS-", {"schema_version": self.schema_version,
            "policy_id": self.policy.id, "seed_services": self.seed_services,
            "expansion_ids": [e.id for e in self.expansions]})
