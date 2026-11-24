from ..models.evidence import ToolResult
from ..models.incident import InvestigationTask
from ..runtime import TraceObservationSource
from ..models.acquisition_result import AcquisitionResult, SourceMetadata
from .normalize import normalize_logs, normalize_spans
from .snapshot import TelemetrySnapshot


class ReplayBackend:
    def __init__(self, snapshot: TelemetrySnapshot):
        self.snapshot = snapshot

    def trace_source(self) -> TraceObservationSource:
        return TraceObservationSource(self.snapshot.traces, dict(self.snapshot.rpc_service_map),
                                      synthetic=self.snapshot.provenance == "synthetic_wire_fixture")

    async def acquire(self, task):
        from ..task_materialization import check_capability
        check_capability(task, self)
        # V1 is an immutable top-level capture, not historical T/A selection.
        result = await self.query(task)
        material = None
        if result.status in {"ok", "empty"}:
            material = {"trace": self.snapshot.traces, "log": self.snapshot.logs,
                        "metric": self.snapshot.metrics}.get(task.signal)
        source = SourceMetadata(rpc_service_map=dict(self.snapshot.rpc_service_map),
            synthetic=self.snapshot.provenance == "synthetic_wire_fixture") if material is not None else None
        return AcquisitionResult(signal=task.signal, projection=result, material=material, source=source)

    async def query(self, task: InvestigationTask) -> ToolResult:
        captured = self.snapshot.incident
        if (task.window.start < captured.window.start or task.window.end > captured.window.end
                or not set(task.services) <= set(captured.seed_services)):
            return ToolResult(status="error", warnings=("Query is outside the recorded snapshot scope.",))
        error = self.snapshot.collection_errors.get(task.signal)
        if error:
            return ToolResult(status="error", warnings=(f"Capture failed for {task.signal}: {error}",))
        if task.signal == "trace":
            data = self.snapshot.traces
            evidence = normalize_spans(data.spans, self.snapshot.rpc_service_map)
        elif task.signal == "log":
            data = self.snapshot.logs
            evidence = normalize_logs(data.logs, self.snapshot.traces.spans, self.snapshot.rpc_service_map)
        elif task.signal == "metric":
            data = self.snapshot.metrics
            return ToolResult(status="empty", warnings=(*data.warnings,
                "Raw metric samples retained; no metric-specific diagnostic threshold was configured."),
                truncated=data.truncated, coverage_complete=data.coverage_complete)
        else:
            return ToolResult(status="empty", warnings=("Knowledge retrieval is not configured.",))
        evidence = tuple(e for e in evidence if e.service in task.services
                         and task.window.start <= e.timestamp < task.window.end)
        if self.snapshot.provenance == "synthetic_wire_fixture":
            evidence = tuple(e.model_copy(update={"source": e.source.model_copy(update={"synthetic": True})})
                             for e in evidence)
        return ToolResult(status="ok" if evidence else "empty", evidence=evidence,
                          warnings=(*data.warnings, "Derived observations use bounded deterministic parsing."),
                          truncated=data.truncated, coverage_complete=data.coverage_complete)
