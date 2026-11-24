"""Logical acquisition identity and monotonic per-run scheduling provenance."""
from typing import Literal
from datetime import timezone

from pydantic import Field, field_validator, model_validator

from .identity import structural_id
from .incident import Contract, InvestigationTask, TimeWindow
from .recorded import RecordedAcquisitionSelector, RecordedTaskRequest
from ..service_scope import MAX_SEED_SERVICES, valid_service

MAX_INTENTS = 256
Origin = Literal["initial_survey", "required_check", "llm", "programmatic"]
BlockingReason = Literal["unsupported_capability", "replay_unavailable", "replay_ambiguous",
                         "unsupported_check_acquisition_mapping"]


class TaskIntent(Contract):
    schema_version: Literal[1] = 1
    signal: Literal["metric", "trace", "log"]
    services: tuple[str, ...] = Field(min_length=1, max_length=MAX_SEED_SERVICES)
    window: TimeWindow

    @field_validator("services")
    @classmethod
    def services_valid(cls, values):
        if not all(map(valid_service, values)):
            raise ValueError("intent requires bounded nonblank services")
        return tuple(sorted(set(values)))

    @field_validator("window")
    @classmethod
    def valid_window(cls, value):
        return TimeWindow(start=value.start.astimezone(timezone.utc), end=value.end.astimezone(timezone.utc))

    @property
    def id(self):
        return structural_id("TI-", self.model_dump())


class SchedulingNeed(Contract):
    intent_id: str = Field(pattern=r"^TI-[0-9a-f]{64}$")
    requested_for_check_ids: tuple[str, ...] = Field(default=(), max_length=256)
    requested_for_hypothesis_ids: tuple[str, ...] = Field(default=(), max_length=256)
    origin: Origin

    @field_validator("requested_for_check_ids", "requested_for_hypothesis_ids")
    @classmethod
    def canonical(cls, values):
        return tuple(sorted(set(values)))


def merge_needs(needs):
    groups = {}
    for need in needs:
        key = (need.intent_id, need.origin)
        checks, hypotheses = groups.setdefault(key, (set(), set()))
        checks.update(need.requested_for_check_ids)
        hypotheses.update(need.requested_for_hypothesis_ids)
    return tuple(SchedulingNeed(intent_id=key[0], origin=key[1],
        requested_for_check_ids=tuple(checks), requested_for_hypothesis_ids=tuple(hypotheses))
        for key, (checks, hypotheses) in sorted(groups.items()))


class ScheduledAcquisitionRequest(Contract):
    """Ephemeral proposal; its existence is not an admission or budget charge."""
    intent: TaskIntent
    needs: tuple[SchedulingNeed, ...] = Field(min_length=1, max_length=4)
    goal: str = "Acquire requested telemetry"
    recorded_selection: RecordedAcquisitionSelector | None = None

    @model_validator(mode="after")
    def consistent(self):
        if any(n.intent_id != self.intent.id for n in self.needs):
            raise ValueError("request needs must refer to its logical intent")
        return self

    def physical_request(self):
        values = dict(id=f"query-{self.intent.signal}", signal=self.intent.signal,
            services=self.intent.services, window=self.intent.window, goal=self.goal)
        if self.recorded_selection is not None:
            return RecordedTaskRequest(**values, recorded_selection=self.recorded_selection)
        return InvestigationTask(**values)


class IntentRecord(Contract):
    intent: TaskIntent
    needs: tuple[SchedulingNeed, ...] = Field(min_length=1, max_length=4)
    status: Literal["pending", "completed", "failed", "unavailable", "ambiguous_replay_selection"]
    materialized_task_spec_id: str | None = Field(default=None, pattern=r"^T-[0-9a-f]{64}$")
    reserved_attempt_id: str | None = Field(default=None, pattern=r"^A-[0-9a-f]{64}$")
    committed_attempt_id: str | None = Field(default=None, pattern=r"^A-[0-9a-f]{64}$")
    recorded_selection: RecordedAcquisitionSelector | None = None
    blocking_reason: BlockingReason | None = None

    @field_validator("needs")
    @classmethod
    def canonical_needs(cls, values):
        return merge_needs(values)

    @model_validator(mode="after")
    def coherent(self):
        if any(n.intent_id != self.intent.id for n in self.needs):
            raise ValueError("ledger consumer belongs to another intent")
        executed = self.status in {"pending", "completed", "failed"}
        if executed:
            if not self.materialized_task_spec_id or not self.reserved_attempt_id or self.blocking_reason is not None:
                raise ValueError("admitted intents require T/reserved A and no pre-execution blocking reason")
            if self.status == "pending" and self.committed_attempt_id is not None:
                raise ValueError("pending is not yet committed")
            if self.status != "pending" and self.committed_attempt_id != self.reserved_attempt_id:
                raise ValueError("terminal execution must reference its reserved A")
        else:
            if any((self.materialized_task_spec_id, self.reserved_attempt_id, self.committed_attempt_id)):
                raise ValueError("nonexecuted dispositions cannot fabricate T/A")
            expected = {"replay_ambiguous"} if self.status == "ambiguous_replay_selection" else {
                "unsupported_capability", "replay_unavailable", "unsupported_check_acquisition_mapping"}
            if self.blocking_reason not in expected:
                raise ValueError("blocking reason must agree with disposition")
        return self

    @property
    def id(self):
        return self.intent.id


def transition(old, new):
    """Strict forward transition; reducers may separately replay an ancestor."""
    if old.intent != new.intent:
        raise ValueError("logical intent cannot change")
    if old.status == new.status:
        if old.model_dump(exclude={"needs"}) != new.model_dump(exclude={"needs"}):
            raise ValueError("conflicting intent disposition")
    elif old.status == "pending" and new.status in {"completed", "failed"}:
        for key in ("materialized_task_spec_id", "reserved_attempt_id", "recorded_selection"):
            if getattr(old, key) != getattr(new, key):
                raise ValueError("intent execution linkage cannot change")
    elif old.status == "ambiguous_replay_selection" and new.status == "pending":
        if new.recorded_selection is None:
            raise ValueError("ambiguity requires explicit recorded selection")
    else:
        raise ValueError("non-monotonic intent transition; retries are disabled")
    return IntentRecord.model_validate({**new.model_dump(), "needs": merge_needs((*old.needs, *new.needs))})


def merge_intent_ledger(left, right):
    """Join forward transitions and repeated subgraph ancestor snapshots safely."""
    records = {}
    rank = {"ambiguous_replay_selection": 0, "pending": 1, "completed": 2, "failed": 2, "unavailable": 2}
    for record in (*left, *right):
        record = IntentRecord.model_validate(record.model_dump())
        old = records.get(record.id)
        if old is None:
            records[record.id] = record
        elif rank[old.status] > rank[record.status]:
            if record.status == "ambiguous_replay_selection" and old.status in {"completed", "failed"}:
                intermediate = old.model_copy(update={"status": "pending", "committed_attempt_id": None})
                records[record.id] = transition(transition(record, intermediate), old)
            else:
                records[record.id] = transition(record, old)
        elif old.status == "ambiguous_replay_selection" and record.status in {"completed", "failed"}:
            intermediate = record.model_copy(update={"status": "pending", "committed_attempt_id": None})
            records[record.id] = transition(transition(old, intermediate), record)
        else:
            records[record.id] = transition(old, record)
    if len(records) > MAX_INTENTS:
        raise ValueError("per-run intent ledger bound exceeded")
    return [records[key] for key in sorted(records)]


class SchedulingLimitation(Contract):
    """A check with no acquisition mapping has no invented executable TI."""
    reason: Literal["unsupported_check_acquisition_mapping"]
    check_id: str
    hypothesis_id: str

    @property
    def id(self):
        return structural_id("SL-", self.model_dump())


def merge_scheduling_limitations(left, right):
    records = {}
    for record in (*left, *right):
        record = SchedulingLimitation.model_validate(record.model_dump())
        records[record.id] = record
    if len(records) > MAX_INTENTS:
        raise ValueError("per-run scheduling limitation bound exceeded")
    return [records[key] for key in sorted(records)]
