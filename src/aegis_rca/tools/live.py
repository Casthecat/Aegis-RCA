import asyncio

from ..adapters import JsonHttp, JaegerAdapter, OpenSearchAdapter, OpenSearchFields, PrometheusAdapter
from ..adapters.http import AdapterError
from ..models.evidence import ToolResult
from ..models.incident import IncidentInput, InvestigationTask
from ..models.acquisition import MaterializedTask
from ..models.telemetry import LogResult, MetricResult, TraceResult
from ..settings import BackendSettings
from ..runtime import TraceObservationSource
from .projection import project_material
from ..models.acquisition_result import AcquisitionResult, SourceMetadata
from .snapshot import TelemetrySnapshot


class LiveBackend:
    """One bounded HTTP search per graph task. Catalog discovery is explicit."""

    def __init__(self, settings: BackendSettings, incident: IncidentInput):
        if (incident.window.end - incident.window.start).total_seconds() > settings.max_window_seconds:
            raise ValueError("Incident window exceeds configured query limit")
        if len(incident.seed_services) != 1:
            raise ValueError("Current live collection requires exactly one seed service")
        self.settings = settings
        self.incident = incident
        options = dict(timeout_seconds=settings.request_timeout_seconds,
                       max_response_bytes=settings.max_response_bytes)
        self.clients = [JsonHttp(url, **options) for url in (
            settings.prometheus_url, settings.jaeger_url, settings.opensearch_url)]
        self.prometheus = PrometheusAdapter(self.clients[0])
        self.jaeger = JaegerAdapter(self.clients[1])
        self.opensearch = OpenSearchAdapter(self.clients[2], settings.log_index, OpenSearchFields(
            timestamp=settings.log_timestamp_field, service=settings.log_service_field,
            service_exact=settings.log_service_exact_field, body=settings.log_body_field,
            severity=settings.log_severity_field, trace_id=settings.log_trace_id_field,
            trace_id_exact=settings.log_trace_id_exact_field, span_id=settings.log_span_id_field,
            timestamp_format=settings.log_timestamp_format))
        self.metrics = MetricResult()
        self.traces = TraceResult()
        self.logs = LogResult()
        self.errors: dict[str, str] = {}

    def close(self):
        for client in self.clients:
            client.close()

    def trace_source(self) -> TraceObservationSource:
        return TraceObservationSource(self.traces, dict(self.settings.rpc_service_map))

    def snapshot(self) -> TelemetrySnapshot:
        reader = getattr(self, "_partition_reader", None)
        if reader is not None:
            partitions = [c.partition for c in reader.values() if c.partition is not None]
            if any(sum(p.signal == signal for p in partitions) > 1 for signal in ("trace", "log", "metric")):
                raise ValueError("Snapshot V1 cannot represent multiple committed acquisitions of one signal")
            if any(c.tool_call.task.services != self.incident.seed_services for c in reader.values() if c.partition is not None):
                raise ValueError("Snapshot V1 cannot represent expanded-service acquisition")
        return TelemetrySnapshot(incident=self.incident, provenance="live_capture",
                                 metrics=self.metrics, traces=self.traces, logs=self.logs,
                                 collection_errors=dict(self.errors), rpc_service_map=self.settings.rpc_service_map)

    async def query(self, task: InvestigationTask) -> ToolResult:
        # Standalone V1 capture/direct-query compatibility remains seed-only.
        if task.services != self.incident.seed_services or task.window != self.incident.window:
            return ToolResult(status="error", warnings=("Task exceeds the configured incident scope.",))
        return (await self.acquire(task)).projection

    async def acquire(self, task: InvestigationTask) -> AcquisitionResult:
        from ..task_materialization import check_capability, materialize_task
        check_capability(task, self)
        spec = task.task_spec if isinstance(task, MaterializedTask) else materialize_task(task, self)
        source = SourceMetadata(rpc_service_map=dict(self.settings.rpc_service_map))
        try:
            if task.signal == "metric":
                if spec.query_expression is None:
                    material = MetricResult(warnings=("No catalog-verified metric query is configured.",))
                else:
                    material = await asyncio.to_thread(self.prometheus.query_range,
                        spec.query_expression, task.window.start, task.window.end,
                        step_seconds=spec.metric_step_seconds, limit=spec.result_limit)
            elif task.signal == "trace":
                material = await asyncio.to_thread(self.jaeger.search, task.services[0],
                    task.window.start, task.window.end, limit=spec.result_limit)
            elif task.signal == "log":
                if materialize_task(task, self).log_query != spec.log_query:
                    raise ValueError("Materialized log query configuration changed before execution")
                material = await asyncio.to_thread(self.opensearch.search, task.window.start,
                    task.window.end, service=task.services[0], limit=spec.result_limit)
            else:
                return AcquisitionResult(signal=task.signal, projection=ToolResult(status="empty",
                    warnings=("Knowledge retrieval is not configured.",)))
            # Both products derive from this local fetch value before updating any
            # compatibility buffers. Concurrent acquisitions cannot mix them.
            result = AcquisitionResult(signal=task.signal, material=material, source=source,
                                       projection=project_material(task, material, source))
        except asyncio.CancelledError:
            self.errors[task.signal] = "cancelled_or_timed_out"
            raise
        except Exception as exc:
            detail = str(exc) if isinstance(exc, AdapterError) else type(exc).__name__
            self.errors[task.signal] = detail
            return AcquisitionResult(signal=task.signal,
                projection=ToolResult(status="error", warnings=(f"Backend query failed: {detail}",)))
        self.errors.pop(task.signal, None)
        setattr(self, {"metric": "metrics", "trace": "traces", "log": "logs"}[task.signal], material)
        return result

    async def catalog(self) -> dict:
        jobs = [asyncio.to_thread(self.prometheus.label_values, "__name__"),
                asyncio.to_thread(self.prometheus.label_names),
                asyncio.to_thread(self.jaeger.services),
                asyncio.to_thread(self.opensearch.mapping)]
        values = await asyncio.gather(*jobs, return_exceptions=True)
        result = {}
        for name, value in zip(("metric_names", "metric_labels", "services", "log_mapping"), values):
            if isinstance(value, Exception):
                result[name] = {"error": type(value).__name__,
                                "detail": str(value) if isinstance(value, AdapterError) else "Backend discovery failed"}
            else:
                result[name] = value.model_dump(mode="json") if hasattr(value, "model_dump") else value
        return result
