"""Create explicitly SYNTHETIC normalized telemetry to demonstrate replay."""
from datetime import timedelta
from pathlib import Path

from aegis_rca.fixtures import demo_incident
from aegis_rca.models.telemetry import LogRecord, LogResult, MetricPoint, MetricResult, MetricSeries, SpanRecord, TraceResult
from aegis_rca.tools.snapshot import TelemetrySnapshot, write_snapshot


def sample():
    incident = demo_incident()
    trace_id = "1" * 32
    span_id = "2" * 16
    start = incident.window.start + timedelta(minutes=6)
    span = SpanRecord(trace_id=trace_id, span_id=span_id, service="checkout", name="oteldemo.PaymentService/Charge",
        kind="CLIENT", start=start, duration_ms=10, status="ERROR",
        attributes={"rpc.service": "oteldemo.PaymentService", "rpc.grpc.status_code": 14, "peer.service": "payment"},
        source=f"synthetic:trace/{trace_id}#{span_id}")
    log = LogRecord(id="synthetic-log-1", timestamp=start, service="checkout", severity="ERROR",
        body="Synthetic: dial tcp payment:50051: connect: connection refused", trace_id=trace_id,
        span_id=span_id, source="synthetic:log/1")
    series = MetricSeries(name="synthetic_error_ratio", labels={"service_name": "checkout"},
        points=tuple(MetricPoint(timestamp=incident.window.start + timedelta(minutes=i), value=0.01 if i < 5 else 0.45)
                     for i in range(10)), source="synthetic:metric/1")
    return TelemetrySnapshot(incident=incident, provenance="synthetic_wire_fixture",
        metrics=MetricResult(series=(series,)), traces=TraceResult(spans=(span,)), logs=LogResult(logs=(log,)))


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    path = root / "examples" / "snapshot.synthetic.json"
    write_snapshot(sample(), path)
    print(path)
