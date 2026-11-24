"""Declared corpus checkpoints; execution reads no private manifest or Gold."""
from datetime import datetime, timezone
from pathlib import Path

from pydantic import AwareDatetime, Field, model_validator

from ..models.identity import structural_id
from ..models.incident import Contract
from . import benchmark
from .corpus import load_execution_corpus, record_holdout_access
from .corpus_models import HoldoutAccessEvent, SplitName
from .stage_c import publish, read_contract, run_experiment, separate_output
from .stage_c_models import ExperimentSpec


class CorpusCheckpoint(Contract):
    schema_version: int = Field(default=1, ge=1, le=1)
    corpus_id: str = Field(pattern=r"^CORP-[0-9a-f]{64}$")
    execution_manifest_id: str = Field(pattern=r"^ECP-[0-9a-f]{64}$")
    splits: tuple[SplitName, ...] = Field(min_length=1, max_length=4)
    declared_at: AwareDatetime
    not_before: AwareDatetime
    declared_by: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def ordered(self):
        if self.splits != tuple(sorted(set(self.splits))) or self.not_before < self.declared_at:
            raise ValueError("checkpoint must declare unique splits before execution")
        return self

    @property
    def id(self):
        return structural_id("CPT-", self.model_dump())


def declare_checkpoint(corpus_root, splits, operator, *, not_before=None):
    manifest = load_execution_corpus(corpus_root)
    declared_at = datetime.now(timezone.utc)
    if declared_at < manifest.frozen_at:
        raise ValueError("corpus split freeze must precede checkpoint declaration")
    checkpoint = CorpusCheckpoint(corpus_id=manifest.corpus_id, execution_manifest_id=manifest.id,
        splits=tuple(sorted(set(splits))), declared_at=declared_at,
        not_before=not_before or declared_at, declared_by=operator)
    if not set(checkpoint.splits) <= {member.split for member in manifest.members}:
        raise ValueError("checkpoint requests a split absent from this corpus")
    root = benchmark._root(Path(corpus_root))
    publish(benchmark._safe(root, "checkpoints", checkpoint.id + ".json"), checkpoint.model_dump(mode="json"))
    return checkpoint


async def execute_corpus(recordings_root, corpus_root, checkpoint_id, output):
    """Only safe execution projection/checkpoint/access receipts cross this boundary."""
    import re
    separate_output(recordings_root, output)
    if not re.fullmatch(r"CPT-[0-9a-f]{64}", checkpoint_id):
        raise ValueError("canonical checkpoint ID required")
    root = benchmark._root(Path(corpus_root))
    destination = benchmark._root(Path(output))
    if destination.is_relative_to(root) or root.is_relative_to(destination):
        raise ValueError("experiment and frozen corpus directories must be separate")
    manifest = load_execution_corpus(root)
    checkpoint = read_contract(benchmark._safe(root, "checkpoints", checkpoint_id + ".json"), CorpusCheckpoint)
    now = datetime.now(timezone.utc)
    if (checkpoint.id != checkpoint_id or checkpoint.corpus_id != manifest.corpus_id or
        checkpoint.execution_manifest_id != manifest.id or checkpoint.declared_at < manifest.frozen_at or
        now < checkpoint.not_before):
        raise ValueError("checkpoint is stale, premature or belongs to another corpus")
    members = [member for member in manifest.members if member.split in checkpoint.splits]
    if not members or not set(checkpoint.splits) <= {member.split for member in members}:
        raise ValueError("checkpoint split not present")
    binding = dict(schema_version=1, corpus_id=manifest.corpus_id, execution_manifest_id=manifest.id,
        checkpoint_id=checkpoint.id, scoring_protocol_id=manifest.expected_scoring_protocol_id)
    path = benchmark._safe(destination, "corpus-binding.json")
    if path.exists():
        if benchmark._json(benchmark._read_bytes(path)) != binding:
            raise ValueError("experiment corpus/checkpoint binding is immutable")
    else:
        publish(path, binding)
    for member in members:
        assessment = record_holdout_access(root, HoldoutAccessEvent(candidate_id=member.admission.candidate_id,
            at=now, actor="stage_c_harness", role="stage_c_harness", material="telemetry",
            purpose="declared_checkpoint", checkpoint_id=checkpoint.id))
        if not assessment.split_valid:
            raise ValueError("holdout access invalidated the split")
    summary = await run_experiment(recordings_root,
        ExperimentSpec(candidate_ids=tuple(member.admission.candidate_id for member in members)), output,
        expected_admissions=tuple(member.admission for member in members))
    return {**binding, "experiment_summary": summary}
