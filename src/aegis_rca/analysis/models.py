"""Typed observations derived locally from raw telemetry, without causal claims."""

from typing import Literal

from pydantic import AwareDatetime, Field

from aegis_rca.models.incident import Contract, TimeWindow
from aegis_rca.models.telemetry import MetricSeries


class MetricComparison(Contract):
    name: str
    labels: dict[str, str] = Field(default_factory=dict)
    source: str
    baseline: TimeWindow
    incident: TimeWindow
    baseline_count: int
    incident_count: int
    baseline_median: float | None = None
    incident_median: float | None = None
    baseline_mad: float | None = None
    absolute_shift: float | None = None
    robust_score: float | None = None
    score_capped: bool = False
    min_absolute_shift: float
    anomalous: bool | None = None
    direction: Literal["increase", "decrease", "unchanged", "unknown"] = "unknown"
    warnings: tuple[str, ...] = ()


class CounterRateResult(Contract):
    series: MetricSeries
    resets: int = 0
    skipped_intervals: int = 0
    warnings: tuple[str, ...] = ()


class ObservedEdge(Contract):
    caller: str
    callee: str
    trace_ids: tuple[str, ...]
    observation_count: int
    error_count: int
    support: tuple[Literal["parent_relationship", "client_peer"], ...]


class ObservedTopology(Contract):
    edges: tuple[ObservedEdge, ...] = ()
    warnings: tuple[str, ...] = ()
    complete: bool = False


class TimelineEvent(Contract):
    id: str
    timestamp: AwareDatetime
    kind: Literal["span", "log", "metric_window"]
    summary: str
    service: str | None = None
    source: str
    trace_id: str | None = None
    span_id: str | None = None
    window_end: AwareDatetime | None = None


class Timeline(Contract):
    events: tuple[TimelineEvent, ...] = ()
    warnings: tuple[str, ...] = ()
