from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TimeWindow(Contract):
    start: AwareDatetime
    end: AwareDatetime

    @model_validator(mode="after")
    def ordered(self):
        if self.start >= self.end:
            raise ValueError("start must precede end; windows use [start, end)")
        return self


class IncidentInput(Contract):
    query: str = Field(min_length=1, max_length=2000)
    window: TimeWindow
    seed_services: tuple[str, ...] = Field(min_length=1)


class Budget(Contract):
    max_rounds: int = Field(default=3, ge=1, le=10)
    max_tool_calls: int = Field(default=6, ge=0, le=100)
    tool_timeout_seconds: float = Field(default=5, gt=0, le=60)


Signal = Literal["metric", "trace", "log", "knowledge"]


class InvestigationTask(Contract):
    id: str
    signal: Signal
    goal: str
    services: tuple[str, ...]
    window: TimeWindow
    # One adapter call per task in P0. Later worker loops need reserved sub-budgets.


class IncidentScope(Contract):
    incident: IncidentInput
    assumptions: tuple[str, ...] = ()
