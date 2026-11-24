"""Human collection-quality decision freezes Phase-A capture policy after pilot."""
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from ..models.identity import structural_id
from ..models.incident import Contract
from .corpus_recording import CapturePolicy, PILOT_CAPTURE_POLICY, QUERY_POLICY
from . import benchmark
from .sanitization import digest
from .stage_c import publish, read_contract


class CapturePolicyDecision(Contract):
    reviewer: str = Field(min_length=1, max_length=200)
    reviewed_at: AwareDatetime
    collection_quality_reviewed: Literal[True]
    workload_and_source_stability_reviewed: Literal[True]
    ingestion_lag_and_limits_reviewed: Literal[True]
    sanitizer_leakage_reviewed: Literal[True]
    aegis_results_not_used: Literal[True]
    private_notes: str = Field(default="", max_length=4000)


class CapturePolicyFreeze(Contract):
    schema_version: Literal[1] = 1
    policy: CapturePolicy
    pilot_qa_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    pilot_attempted_count: int = Field(ge=3, le=256)
    pilot_recorded_count: int = Field(ge=3, le=256)
    decision: CapturePolicyDecision
    frozen_at: AwareDatetime

    @model_validator(mode="after")
    def phase_a(self):
        if self.policy.policy_name != "capture-policy-phase-a-v1":
            raise ValueError("Phase-A freeze requires a distinct Phase-A capture policy")
        if self.pilot_recorded_count > self.pilot_attempted_count or self.frozen_at < self.decision.reviewed_at:
            raise ValueError("capture policy freeze must follow completed pilot collection-quality review")
        return self

    @property
    def id(self):
        return structural_id("CPF-", self.model_dump())


def freeze_capture_policy(policy_path, pilot_qa_path, decision_path, output):
    from .corpus_models import CorpusQASummary
    policy = read_contract(Path(policy_path), CapturePolicy)
    qa_bytes = benchmark._read_bytes(Path(pilot_qa_path))
    qa = CorpusQASummary.model_validate(benchmark._json(qa_bytes))
    if (qa.phase != "pilot" or qa.real_recorded_count < 3 or
        qa.capture_policy_ids != (PILOT_CAPTURE_POLICY.id,) or
        qa.query_policy_ids != (QUERY_POLICY.id,) or qa.sanitizer_rulesets != ("control-metadata-v1",)):
        raise ValueError("Phase-A capture freeze requires three real episodes under the frozen pilot policies")
    decision = read_contract(Path(decision_path), CapturePolicyDecision)
    frozen = CapturePolicyFreeze(policy=policy, pilot_qa_sha256=digest(qa_bytes),
        pilot_attempted_count=qa.attempted_episode_count, pilot_recorded_count=qa.successfully_recorded_count,
        decision=decision, frozen_at=datetime.now(timezone.utc))
    root = benchmark._root(Path(output))
    if root.exists():
        raise ValueError("capture policy freeze output must be new")
    publish(root / "capture-policy.json", policy.model_dump(mode="json"))
    publish(root / "freeze.json", frozen.model_dump(mode="json"))
    return frozen
