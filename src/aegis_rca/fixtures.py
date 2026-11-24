"""Hand-authored telemetry for workflow tests, NOT captured OTel Demo data.

The rule policy sees only task results, never a scenario name or injection label.
This is still not a blinded benchmark: both the fixtures and rules are hand-built.
"""
from datetime import datetime, timezone

from .models.evidence import Evidence, EvidenceSource, Observation, ToolResult
from .models.incident import IncidentInput, InvestigationTask, TimeWindow
from .models.telemetry import SpanRecord, TraceResult, LogRecord, LogResult, MetricResult, MetricSeries, MetricPoint
from .models.acquisition_result import AcquisitionResult, SourceMetadata
from .runtime import TraceObservationSource


def demo_incident() -> IncidentInput:
    return IncidentInput(
        query="Why did checkout failures increase?", seed_services=("checkout",),
        window=TimeWindow(start=datetime(2026, 9, 8, 14, 30, tzinfo=timezone.utc),
                          end=datetime(2026, 9, 8, 14, 40, tzinfo=timezone.utc)),
    )


def demo_evidence() -> tuple[Evidence, ...]:
    rows = [
        ("E-M1", "metric", "checkout_errors_elevated", None,
         "Synthetic checkout error ratio: baseline 0.004; incident 0.46", "batch-1"),
        ("E-T1", "trace", "rpc_unavailable", "payment",
         "Synthetic checkout client span: peer=payment; gRPC UNAVAILABLE", "rpc-1"),
        ("E-L1", "log", "connection_refused", "payment",
         "Synthetic checkout log: dial payment:50051: connect: connection refused", "rpc-1"),
    ]
    return tuple(Evidence(
        id=key, signal=signal, service="checkout",
        timestamp=datetime(2026, 9, 8, 14, 32, tzinfo=timezone.utc),
        observation=Observation(code=code, peer_service=peer), excerpt=excerpt,
        source=EvidenceSource(backend="fixture", locator=f"fixtures.py#{key}", synthetic=True),
        correlation_group=group,
    ) for key, signal, code, peer, excerpt, group in rows)


def demo_traces() -> TraceResult:
    """Independent hand-authored source fixture, never reconstructed from E-T1."""
    return TraceResult(spans=(SpanRecord(
        trace_id="1" * 32, span_id="2" * 16, service="checkout",
        name="oteldemo.PaymentService/Charge", kind="CLIENT",
        start=datetime(2026, 9, 8, 14, 32, tzinfo=timezone.utc), duration_ms=1,
        status="ERROR", attributes={"rpc.service": "oteldemo.PaymentService", "rpc.grpc.status_code": 14},
        source="fixtures.py#demo_traces",
    ),))


class FixtureBackend:
    def __init__(self, records: tuple[Evidence, ...] | None = None, *, traces: TraceResult | None = None,
                 logs: LogResult | None = None, metrics: MetricResult | None = None,
                 materials=None, seed_services=("checkout",)):
        self.records = demo_evidence() if records is None else records
        # Custom scenarios provide their raw source independently of citations.
        self.traces = traces if traces is not None else (demo_traces() if records is None else TraceResult())
        self.logs = logs if logs is not None else (LogResult(logs=(LogRecord(
            id="fixture-log", service="checkout", timestamp=demo_incident().window.start,
            severity="ERROR", body="dial payment:50051: connect: connection refused",
            attributes={"peer.service": "payment"}, source="fixtures.py#raw_log",
        ),)) if records is None else LogResult())
        self.metrics = metrics if metrics is not None else (MetricResult(series=(MetricSeries(
            name="synthetic_checkout_error_ratio", labels={"service_name": "checkout"},
            points=(MetricPoint(timestamp=demo_incident().window.start, value=0.46),),
            source="fixtures.py#raw_metric",
        ),)) if records is None else MetricResult())
        self.seed_services = seed_services
        self.materials = dict(materials or {})

    async def acquire(self, task):
        from .tools.projection import project_material
        from .task_materialization import check_capability
        check_capability(task, self)
        key = (task.signal, tuple(sorted(set(task.services))))
        source = SourceMetadata(rpc_service_map={"oteldemo.PaymentService": "payment"}, backend="fixture", synthetic=True)
        if key in self.materials:
            material = self.materials[key].model_copy(deep=True)
            return AcquisitionResult(signal=task.signal, material=material, source=source,
                                     projection=project_material(task, material, source))
        # Synthetic fixture projections are hand-authored, not reverse-converted
        # into telemetry. Trace input is the independent fixture source as before.
        projection = await self.query(task)
        if projection.status not in {"ok", "empty"}:
            return AcquisitionResult(signal=task.signal, projection=projection, projection_origin="fixture")
        material = {"trace": self.traces, "log": self.logs, "metric": self.metrics}.get(task.signal)
        return AcquisitionResult(signal=task.signal, projection=projection, material=material,
                                 source=source if material is not None else None, projection_origin="fixture")

    def trace_source(self) -> TraceObservationSource:
        return TraceObservationSource(self.traces, {"oteldemo.PaymentService": "payment"},
                                      backend="fixture", synthetic=True)

    async def query(self, task: InvestigationTask) -> ToolResult:
        records = tuple(e for e in self.records if
                        e.signal == task.signal and e.service in task.services and
                        task.window.start <= e.timestamp < task.window.end)
        return ToolResult(status="ok" if records else "empty", evidence=records,
                          coverage_complete=None,
                          warnings=("Synthetic fixture; telemetry coverage is not established.",))
