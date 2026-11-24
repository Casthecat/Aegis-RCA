"""Pure legacy projection from a single physical acquisition's material."""

from ..models.evidence import ToolResult
from ..models.telemetry import TraceResult, LogResult, MetricResult
from .normalize import normalize_spans, normalize_logs


def project_material(task, material, source):
    if isinstance(material, TraceResult):
        evidence = normalize_spans(material.spans, source.rpc_service_map)
    elif isinstance(material, LogResult):
        # normalize_logs does not use spans; peer resolution comes from this log
        # and the explicit RPC map, never mutable trace buffers.
        evidence = normalize_logs(material.logs, (), source.rpc_service_map)
    elif isinstance(material, MetricResult):
        return ToolResult(status="empty", warnings=(*material.warnings,
            "Raw metric samples retained; no metric-specific diagnostic threshold was configured."),
            truncated=material.truncated, coverage_complete=material.coverage_complete)
    else:
        raise TypeError("projection requires typed acquisition material")
    evidence = tuple(e for e in evidence if e.service in task.services
                     and task.window.start <= e.timestamp < task.window.end)
    if source.synthetic:
        evidence = tuple(e.model_copy(update={"source": e.source.model_copy(update={"synthetic": True})}) for e in evidence)
    return ToolResult(status="ok" if evidence else "empty", evidence=evidence,
        warnings=(*material.warnings, "Derived observations use bounded deterministic parsing."),
        truncated=material.truncated, coverage_complete=material.coverage_complete)
