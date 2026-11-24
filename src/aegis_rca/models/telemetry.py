"""Normalized raw telemetry; diagnostic observations are derived separately."""
from typing import Any, Literal

from pydantic import AwareDatetime, Field

from .incident import Contract


class MetricPoint(Contract):
    timestamp: AwareDatetime
    value: float


class MetricSeries(Contract):
    name: str
    labels: dict[str, str] = Field(default_factory=dict)
    points: tuple[MetricPoint, ...] = ()
    source: str


class SpanRecord(Contract):
    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    service: str
    name: str
    kind: Literal["CLIENT", "SERVER", "INTERNAL", "PRODUCER", "CONSUMER", "UNKNOWN"] = "UNKNOWN"
    start: AwareDatetime
    duration_ms: float = Field(ge=0)
    status: str = "UNSET"
    attributes: dict[str, Any] = Field(default_factory=dict)
    source: str


class LogRecord(Contract):
    id: str
    timestamp: AwareDatetime
    service: str
    severity: str
    body: str
    trace_id: str | None = None
    span_id: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    source: str


class ResultMeta(Contract):
    warnings: tuple[str, ...] = ()
    truncated: bool = False
    coverage_complete: bool | None = None


class MetricResult(ResultMeta):
    series: tuple[MetricSeries, ...] = ()


class TraceResult(ResultMeta):
    spans: tuple[SpanRecord, ...] = ()


class LogResult(ResultMeta):
    logs: tuple[LogRecord, ...] = ()
