from typing import Annotated, TypeVar
from typing_extensions import TypedDict
from uuid import uuid4

from .models.evidence import Evidence, Finding, ToolCallRecord
from .models.hypothesis import Hypothesis, IncidentReport, Verification
from .models.incident import Budget, IncidentInput, IncidentScope, InvestigationTask
from .models.acquisition import AttemptRecord, TaskSpec
from .models.scope import InvestigationScope
from .verification.persisted import PersistedHypothesisVerification
from .models.portfolio import HypothesisPortfolio
from .models.scheduling import (ScheduledAcquisitionRequest, IntentRecord, SchedulingLimitation,
                                merge_intent_ledger, merge_scheduling_limitations)
from .models.recorded import RecordedTaskRequest

T = TypeVar("T", Evidence, Finding, ToolCallRecord, AttemptRecord, TaskSpec)


def merge_records(left: list[T], right: list[T]) -> list[T]:
    """Order independent and idempotent; conflicting immutable IDs are a bug."""
    records = {}
    for record in [*left, *right]:
        previous = records.get(record.id)
        if previous is not None and previous != record:
            raise ValueError(f"Conflicting records for ID {record.id}")
        records[record.id] = record
    return [records[key] for key in sorted(records)]


class IncidentState(TypedDict):
    execution_id: str
    task_specs: Annotated[list[TaskSpec], merge_records]
    attempt_records: Annotated[list[AttemptRecord], merge_records]
    hypothesis_portfolio: HypothesisPortfolio | None
    hypothesis_verifications: tuple[PersistedHypothesisVerification, ...]
    investigation_scope: InvestigationScope | None
    incident: IncidentInput
    budget: Budget
    scope: IncidentScope | None
    pending_tasks: list[ScheduledAcquisitionRequest | RecordedTaskRequest | InvestigationTask]
    intent_ledger: Annotated[list[IntentRecord], merge_intent_ledger]
    scheduling_limitations: Annotated[list[SchedulingLimitation], merge_scheduling_limitations]
    evidence: Annotated[list[Evidence], merge_records]
    findings: Annotated[list[Finding], merge_records]
    tool_calls: Annotated[list[ToolCallRecord], merge_records]
    hypotheses: list[Hypothesis]
    provisional_hypotheses: list[Hypothesis]
    verification: Verification | None
    rounds: int
    final_report: IncidentReport | None
    source_mode: str
    reasoning_mode: str
    reasoning_notes: list[str]


def initial_state(incident: IncidentInput, budget: Budget | None = None, *,
                  source_mode="synthetic_fixture", reasoning_mode="deterministic", execution_id: str | None = None) -> IncidentState:
    execution_id = str(uuid4()) if execution_id is None else execution_id
    if not execution_id.strip():
        raise ValueError("execution_id must not be blank")
    return IncidentState(
        execution_id=execution_id, task_specs=[], attempt_records=[], hypothesis_portfolio=None, hypothesis_verifications=(),
        investigation_scope=None, intent_ledger=[], scheduling_limitations=[],
        incident=incident, budget=budget or Budget(), scope=None, pending_tasks=[],
        evidence=[], findings=[], tool_calls=[], hypotheses=[], provisional_hypotheses=[], verification=None,
        rounds=0, final_report=None, source_mode=source_mode,
        reasoning_mode=reasoning_mode, reasoning_notes=[],
    )
