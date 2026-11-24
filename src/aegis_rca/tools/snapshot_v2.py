"""Versioned acquisition archives; structural validation never executes producers."""

import json
import os
from pathlib import Path
import tempfile
from typing import Literal

from pydantic import Field, field_validator, model_validator

from ..models.acquisition import AttemptRecord, TaskSpec
from ..models.acquisition_result import TelemetryPartition
from ..models.evidence import Evidence, ToolCallRecord
from ..models.identity import structural_id
from ..models.incident import Contract, IncidentInput
from ..models.recorded import RecordedSourceReference
from ..models.scope import InvestigationScope


MAX_ARCHIVE_BYTES = 100_000_000


class RecordedAcquisitionV2(Contract):
    """Explicit archival fields, deliberately independent of the runtime envelope."""

    schema_version: Literal[2] = 2
    attempt: AttemptRecord
    tool_call: ToolCallRecord
    admitted_evidence: tuple[Evidence, ...] = ()
    partition: TelemetryPartition | None = None
    partition_content_digest: str | None = Field(default=None, pattern=r"^sha256-[0-9a-f]{64}$")
    recorded_from: RecordedSourceReference | None = None

    @field_validator("tool_call")
    @classmethod
    def canonical_references(cls, value):
        if len(set(value.evidence_ids)) != len(value.evidence_ids):
            raise ValueError("duplicate archival Evidence references")
        return value.model_copy(update={"evidence_ids": tuple(sorted(value.evidence_ids))})

    @field_validator("admitted_evidence")
    @classmethod
    def canonical_evidence(cls, values):
        if len({e.id for e in values}) != len(values):
            raise ValueError("duplicate archival Evidence ID")
        return tuple(sorted(values, key=lambda e: e.id))

    @model_validator(mode="after")
    def archival_consistency(self):
        a, c, p = self.attempt, self.tool_call, self.partition
        if (c.attempt_id != a.id or c.task_spec_id != a.task_spec_id or c.status != a.tool_status
                or c.truncated != a.truncated or c.coverage_complete != a.coverage_complete
                or c.warnings != a.warnings or set(c.evidence_ids) != set(a.admitted_evidence_ids)
                or set(c.evidence_ids) != {e.id for e in self.admitted_evidence}):
            raise ValueError("archival attempt/projection disagreement")
        if p is None:
            if a.observation_binding == "bound":
                raise ValueError("native historical binding requires a partition")
            if self.partition_content_digest is not None or self.recorded_from is not None:
                raise ValueError("absent raw partition cannot claim content or lineage")
            if a.tool_status in {"ok", "empty"} and c.task.signal in {"trace", "log", "metric"}:
                raise ValueError("successful telemetry archive requires explicit partition")
        elif (a.tool_status not in {"ok", "empty"} or p.attempt_id != a.id
                or p.task_spec_id != a.task_spec_id or p.signal != c.task.signal
                or p.content_digest != self.partition_content_digest):
            raise ValueError("archival partition identity/content mismatch")
        if self.recorded_from is not None and self.recorded_from.recorded_content_digest != p.content_digest:
            raise ValueError("archival lineage content mismatch")
        return self


class SnapshotV2(Contract):
    schema_version: Literal[2] = 2
    incident: IncidentInput
    historical_execution_id: str = Field(min_length=1)
    provenance: Literal["live_investigation", "fixture_investigation", "replay_investigation"]
    task_specs: tuple[TaskSpec, ...]
    recorded_acquisitions: tuple[RecordedAcquisitionV2, ...]
    investigation_scope: InvestigationScope | None = None
    replay_manifest_id: str = Field(pattern=r"^RM-[0-9a-f]{64}$")
    archive_content_digest: str = Field(pattern=r"^sha256-[0-9a-f]{64}$")

    @field_validator("task_specs", "recorded_acquisitions")
    @classmethod
    def canonical_rows(cls, values, info):
        key = (lambda r: r.id) if info.field_name == "task_specs" else (lambda r: r.attempt.id)
        if len({key(v) for v in values}) != len(values):
            raise ValueError("duplicate historical T/A identity")
        return tuple(sorted(values, key=key))

    def manifest_identity(self):
        rows = []
        for row in sorted(self.recorded_acquisitions, key=lambda r: r.attempt.id):
            part = row.partition
            # Result-level warnings are archival diagnostics, not replay source
            # selection. Raw record fields (including digest-relevant locators)
            # and all source metadata remain bound. Full content is checked below.
            raw_identity = None if part is None else structural_id("sha256-", {
                "signal": part.signal, "material": part.material.model_dump(exclude={"warnings"}),
                "source": part.source.model_dump()})
            rows.append({"attempt_id": row.attempt.id, "task_spec_id": row.attempt.task_spec_id,
                "status": row.attempt.tool_status, "has_partition": part is not None,
                "replay_content_identity": raw_identity,
                "recorded_from": row.recorded_from.model_dump() if row.recorded_from else None})
        return structural_id("RM-", {"schema_version": 2, "historical_execution_id": self.historical_execution_id,
            "incident_scope": {"seed_services": sorted(set(self.incident.seed_services)),
                               "window": self.incident.window.model_dump()},
            "task_specs": [t.id for t in sorted(self.task_specs, key=lambda t: t.id)], "acquisitions": rows})

    def content_digest(self):
        return structural_id("sha256-", self.model_dump(exclude={"archive_content_digest"}))

    @model_validator(mode="after")
    def validate_archive(self):
        specs = {t.id: t for t in self.task_specs}
        if not self.historical_execution_id.strip():
            raise ValueError("historical execution namespace must not be blank")
        for row in self.recorded_acquisitions:
            a, call, part = row.attempt, row.tool_call, row.partition
            spec = specs.get(a.task_spec_id)
            if a.execution_id != self.historical_execution_id or spec is None:
                raise ValueError("historical A must reference its execution and included T")
            if (call.task.signal != spec.signal or tuple(sorted(set(call.task.services))) != spec.services
                    or call.task.window != spec.window):
                raise ValueError("historical projection scope differs from TaskSpec")
            if any(e.signal != spec.signal or e.service not in spec.services
                   or not spec.window.start <= e.timestamp < spec.window.end for e in row.admitted_evidence):
                raise ValueError("archived admitted evidence exceeds acquisition scope")
            if spec.schema_version == 2 and part is not None:
                if row.recorded_from is None or any(getattr(row.recorded_from, k) != v for k, v in
                                                   spec.recorded_selection.model_dump().items()):
                    raise ValueError("recorded transport selection and lineage disagree")
            if row.recorded_from is not None and spec.schema_version != 2:
                raise ValueError("recorded replay lineage requires versioned TaskSpec selection")
            if part is not None and spec.signal == "trace" and a.observation_binding == "bound":
                spans = [s.model_dump(mode="json") for s in part.material.spans if s.service in spec.services
                         and spec.window.start <= s.start < spec.window.end]
                # Frozen P1.5 acquisition digest inputs, not current Observation logic.
                digest = structural_id("sha256-", {"spans": spans, **part.source.model_dump()})
                if digest != a.source_material_digest:
                    raise ValueError("historical trace source-material digest mismatch")
        if self.investigation_scope is not None:
            from ..investigation_scope import current_investigation_scope
            current_investigation_scope({"incident": self.incident, "execution_id": self.historical_execution_id,
                "task_specs": list(self.task_specs), "attempt_records": [r.attempt for r in self.recorded_acquisitions],
                "investigation_scope": self.investigation_scope})
        if self.replay_manifest_id != self.manifest_identity():
            raise ValueError("replay manifest declaration mismatch")
        if self.archive_content_digest != self.content_digest():
            raise ValueError("archive content digest declaration mismatch")
        return self

    @classmethod
    def seal(cls, *, incident, historical_execution_id, provenance, task_specs, recorded_acquisitions,
             investigation_scope=None):
        """Compute declarations for a newly built archive, then validate everything."""
        draft = cls.model_construct(incident=incident, historical_execution_id=historical_execution_id,
            provenance=provenance, task_specs=tuple(sorted(task_specs, key=lambda t: t.id)),
            recorded_acquisitions=tuple(sorted(recorded_acquisitions, key=lambda r: r.attempt.id)),
            investigation_scope=investigation_scope, replay_manifest_id="", archive_content_digest="")
        draft = draft.model_copy(update={"replay_manifest_id": draft.manifest_identity()})
        draft = draft.model_copy(update={"archive_content_digest": draft.content_digest()})
        return cls.model_validate_json(draft.model_dump_json())


def export_snapshot_v2(state, dependencies, *, provenance=None):
    """Validate agreement; never repair either authority or mutate the store."""
    from ..runtime import validate_execution_state
    from .live import LiveBackend
    from ..fixtures import FixtureBackend
    validate_execution_state(state)
    completions = dependencies.acquisitions.values()
    state_attempts = {a.id: a for a in state["attempt_records"]}
    if len(state_attempts) != len(state["attempt_records"]) or set(state_attempts) != {c.attempt.id for c in completions}:
        raise ValueError("state/store terminal Attempt set mismatch")
    if dependencies.started_attempts - set(state_attempts):
        raise ValueError("cannot archive unresolved started acquisition")
    calls = {c.attempt_id: c for c in state["tool_calls"]}
    evidence = {e.id: e for e in state["evidence"]}
    specs = {t.id: t for t in state["task_specs"]}
    if (len(specs) != len(state["task_specs"]) or len(calls) != len(state["tool_calls"])
            or set(calls) != set(state_attempts) or len(evidence) != len(state["evidence"])):
        raise ValueError("duplicate state acquisition references")
    for reserved in dependencies.attempts.values():
        if reserved.identity.execution_id != state["execution_id"] or specs.get(reserved.spec.id) != reserved.spec:
            raise ValueError("reservation namespace/TaskSpec differs from state")
    rows = []
    for c in completions:
        if state_attempts[c.attempt.id] != c.attempt or calls.get(c.attempt.id) != c.tool_call:
            raise ValueError("state/store completion disagreement")
        if any(evidence.get(e.id) != e for e in c.evidence):
            raise ValueError("state/store admitted Evidence disagreement")
        # Explicit field conversion keeps future runtime fields out of the wire.
        rows.append(RecordedAcquisitionV2(attempt=c.attempt, tool_call=c.tool_call,
            admitted_evidence=c.evidence, partition=c.partition,
            partition_content_digest=c.partition.content_digest if c.partition else None,
            recorded_from=c.recorded_from))
    if provenance is None:
        from .replay import ReplayBackend
        from .recorded_replay import RecordedReplayBackend
        if isinstance(dependencies.backend, LiveBackend):
            provenance = "live_investigation"
        elif isinstance(dependencies.backend, FixtureBackend):
            provenance = "fixture_investigation"
        elif isinstance(dependencies.backend, (ReplayBackend, RecordedReplayBackend)):
            provenance = "replay_investigation"
        else:
            raise ValueError("custom archive producer must declare its provenance explicitly")
    return SnapshotV2.seal(incident=state["incident"], historical_execution_id=state["execution_id"],
        provenance=provenance, task_specs=state["task_specs"], recorded_acquisitions=rows,
        investigation_scope=state.get("investigation_scope"))


def read_snapshot_v2(path: Path):
    if path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ValueError("Snapshot V2 exceeds 100 MB limit")
    return SnapshotV2.model_validate_json(path.read_text(encoding="utf-8-sig"))


def write_snapshot_v2(snapshot: SnapshotV2, path: Path):
    snapshot = SnapshotV2.model_validate_json(snapshot.model_dump_json())
    payload = (json.dumps(snapshot.model_dump(mode="json"), ensure_ascii=False,
                          sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    if len(payload) > MAX_ARCHIVE_BYTES:
        raise ValueError("Snapshot V2 exceeds 100 MB limit")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".aegis-v2-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        # Atomic exclusive publication; unlike replace(), cannot overwrite a run.
        os.link(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def load_recording(path: Path):
    """Explicit engineering replay dispatch; benchmark/read_snapshot remain V1-only."""
    from .snapshot import read_snapshot
    if path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ValueError("Recording exceeds size limit")
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if value.get("schema_version", 1) == 1:
        return read_snapshot(path)
    if value.get("schema_version") == 2:
        return SnapshotV2.model_validate(value)
    raise ValueError("unsupported recording version")
