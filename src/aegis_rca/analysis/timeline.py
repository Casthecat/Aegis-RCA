"""A timestamp-ordered evidence view; ordering is not a causal conclusion."""

from collections.abc import Iterable
import hashlib
import json

from aegis_rca.models.telemetry import LogRecord, SpanRecord

from .models import MetricComparison, Timeline, TimelineEvent


def _identity(prefix: str, parts: list[object]) -> str:
    payload = json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str)
    return f"{prefix}:{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:20]}"


def build_timeline(
    *,
    spans: Iterable[SpanRecord] = (),
    logs: Iterable[LogRecord] = (),
    metric_comparisons: Iterable[MetricComparison] = (),
) -> Timeline:
    """Preserve recorded timestamps; metric summaries mark the window start.

    Log text and span names remain source observations. They are not promoted to
    verified diagnoses. Metric summary timestamps do not identify anomaly onset.
    """
    events: list[TimelineEvent] = []
    for span in spans:
        events.append(TimelineEvent(
            id=_identity("span", [span.source, span.trace_id, span.span_id, span.start, span.name, span.status, span.duration_ms]),
            timestamp=span.start,
            kind="span",
            summary=f"Observed {span.kind} span {span.name!r}; status={span.status}; duration_ms={span.duration_ms:g}.",
            service=span.service,
            source=span.source,
            trace_id=span.trace_id,
            span_id=span.span_id,
        ))
    for log in logs:
        events.append(TimelineEvent(
            id=_identity("log", [log.source, log.id, log.timestamp, log.service, log.severity, log.body]),
            timestamp=log.timestamp,
            kind="log",
            summary=f"Observed {log.severity} log: {log.body}",
            service=log.service,
            source=log.source,
            trace_id=log.trace_id,
            span_id=log.span_id,
        ))
    for comparison in metric_comparisons:
        outcome = "unknown" if comparison.anomalous is None else "detected" if comparison.anomalous else "not detected"
        events.append(TimelineEvent(
            id=_identity("metric", [comparison.source, comparison.name, comparison.labels, comparison.baseline.model_dump(mode="json"), comparison.incident.model_dump(mode="json")]),
            timestamp=comparison.incident.start,
            window_end=comparison.incident.end,
            kind="metric_window",
            summary=(f"Window comparison for {comparison.name}: anomaly {outcome}; "
                     f"baseline median={comparison.baseline_median}; incident median={comparison.incident_median}. "
                     "Timestamp marks window start, not anomaly onset."),
            service=comparison.labels.get("service_name") or comparison.labels.get("service.name") or comparison.labels.get("service"),
            source=comparison.source,
        ))
    unique = {event.model_dump_json(): event for event in events}
    ordered = tuple(sorted(unique.values(), key=lambda event: (event.timestamp, event.kind, event.id, event.summary)))
    return Timeline(events=ordered, warnings=(
        "Recorded timestamps may include clock skew, sampling gaps, and ingestion delay; temporal order does not establish causality.",
        "Metric events summarize explicit windows and do not determine anomaly onset.",
    ))
