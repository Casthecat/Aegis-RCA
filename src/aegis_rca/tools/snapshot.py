"""Portable normalized telemetry recordings, independent from private labels."""
import json
from pathlib import Path
from typing import Literal

from pydantic import Field

from ..models.incident import Contract, IncidentInput
from ..models.telemetry import LogResult, MetricResult, TraceResult


class TelemetrySnapshot(Contract):
    schema_version: Literal[1] = 1
    incident: IncidentInput
    provenance: Literal["live_capture", "synthetic_wire_fixture"]
    metrics: MetricResult = Field(default_factory=MetricResult)
    traces: TraceResult = Field(default_factory=TraceResult)
    logs: LogResult = Field(default_factory=LogResult)
    collection_errors: dict[str, str] = Field(default_factory=dict)
    rpc_service_map: dict[str, str] = Field(default_factory=lambda: {"oteldemo.PaymentService": "payment"})
    # Do not store injected flags, private labels, provider keys or config URLs here.


def read_snapshot(path: Path) -> TelemetrySnapshot:
    if path.stat().st_size > 20_000_000:
        raise ValueError("Snapshot exceeds 20 MB limit")
    return TelemetrySnapshot.model_validate_json(path.read_text(encoding="utf-8-sig"))


def write_snapshot(snapshot: TelemetrySnapshot, path: Path):
    content = snapshot.model_dump_json(indent=2)
    if len(content.encode("utf-8")) > 20_000_000:
        raise ValueError("Snapshot exceeds 20 MB limit")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as destination:
        destination.write(content)
