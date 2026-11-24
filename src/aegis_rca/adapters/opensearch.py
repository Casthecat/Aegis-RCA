"""Read-only lexical log search using explicit document and keyword field names."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal
from urllib.parse import quote

from aegis_rca.models.telemetry import LogRecord, LogResult

from .common import array, bounded_limit, iso, obj, string, window
from .http import AdapterError, JsonHttp, SchemaError
from .jaeger import hex_id


@dataclass(frozen=True)
class OpenSearchFields:
    """Defaults match Demo 3.0.0 / Collector 0.157.0 SS4O logs with dynamic mapping."""

    timestamp: str = "@timestamp"
    timestamp_format: Literal["iso", "epoch_millis", "epoch_seconds"] = "iso"
    service: str = "resource.service.name"
    service_exact: str = "resource.service.name.keyword"
    body: str = "body"
    severity: str = "severity.text"
    trace_id: str = "traceId"
    trace_id_exact: str = "traceId.keyword"
    span_id: str = "spanId"
    attributes: str = "attributes"

    def __post_init__(self):
        if self.timestamp_format not in {"iso", "epoch_millis", "epoch_seconds"}:
            raise ValueError("unsupported timestamp_format")
        for name, value in vars(self).items():
            if name != "timestamp_format" and (not isinstance(value, str) or not value or len(value) > 256):
                raise ValueError("OpenSearch field names must contain 1 to 256 characters")


def field_value(document: dict, path: str, default: Any = None) -> Any:
    """Read nested JSON and literal dotted resource keys without guessing field names."""
    if path in document:
        return document[path]
    head, separator, tail = path.partition(".")
    if separator and isinstance(document.get(head), dict):
        return field_value(document[head], tail, default)
    return default


class OpenSearchAdapter:
    def __init__(self, http: JsonHttp, index: str = "otel-logs-*",
                 fields: OpenSearchFields | None = None):
        if (not re.fullmatch(r"[a-z0-9][a-z0-9_.*-]{0,254}", index)
                or ".." in index):
            raise ValueError("index must be a bounded OpenSearch index name or prefix pattern")
        self.http = http
        self.index = index
        self.fields = fields or OpenSearchFields()

    def mapping(self) -> dict:
        """Inspect mappings explicitly before overriding the configured field names."""
        return obj(self.http.request("GET", f"/{quote(self.index, safe='*')}/_mapping"),
                   "OpenSearch index mapping")

    def context(self, trace_id: str, start: datetime, end: datetime,
                service: str | None = None, limit: int = 100) -> LogResult:
        return self.search(start, end, service=service, trace_id=trace_id, limit=limit)

    def search(self, start: datetime, end: datetime, service: str | None = None,
               text: str | None = None, trace_id: str | None = None, limit: int = 100) -> LogResult:
        start, end = window(start, end)
        bounded_limit(limit)
        f = self.fields
        filters: list[dict] = [{"range": {f.timestamp: {"gte": iso(start), "lt": iso(end)}}}]
        if service:
            filters.append({"term": {f.service_exact: service}})
        if trace_id:
            normalized_id = hex_id(trace_id, 32)
            filters.append({"term": {f.trace_id_exact: normalized_id}})
        must: list[dict] = []
        if text:
            if len(text) > 2000:
                raise ValueError("text must contain at most 2000 characters")
            # User text is a match value, never raw query DSL or a query_string expression.
            must.append({"match": {f.body: {"query": text, "operator": "and"}}})
        query = {"size": limit + 1, "track_total_hits": True,
                 "timeout": f"{int(self.http.timeout_seconds * 1000)}ms",
                 "sort": [{f.timestamp: {"order": "asc"}}],
                 "query": {"bool": {"filter": filters, "must": must}}}
        data = obj(self.http.request("POST", f"/{quote(self.index, safe='*')}/_search", json=query),
                   "OpenSearch response")
        if "error" in data:
            raise AdapterError("OpenSearch returned an API error")
        hits_data = obj(data.get("hits"), "OpenSearch hits")
        hits = array(hits_data.get("hits"), "OpenSearch hit list")
        warnings: list[str] = []
        partial = False
        for flag in ("timed_out", "terminated_early"):
            value = data.get(flag, False)
            if not isinstance(value, bool):
                raise SchemaError("malformed OpenSearch completion flag")
            if value:
                warnings.append(f"OpenSearch {flag}; results are partial")
                partial = True
        shards = obj(data.get("_shards"), "OpenSearch shard status")
        failed = shards.get("failed")
        if isinstance(failed, bool) or not isinstance(failed, int) or failed < 0:
            raise SchemaError("malformed OpenSearch failed-shard count")
        if failed:
            warnings.append(f"{failed} OpenSearch shards failed; results are partial")
            partial = True
        total = hits_data.get("total")
        if isinstance(total, int) and not isinstance(total, bool):
            count, relation = total, "eq"
        elif isinstance(total, dict):
            count, relation = total.get("value"), total.get("relation")
        elif total is None:
            count, relation = len(hits), "gte"
            warnings.append("OpenSearch total hit count unavailable")
        else:
            raise SchemaError("malformed OpenSearch total hit count")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0 or relation not in {"eq", "gte"}:
            raise SchemaError("malformed OpenSearch total hit count")
        truncated = len(hits) > limit or count > limit or relation == "gte"
        if truncated:
            warnings.append("log result limit or lower-bound total reached; additional logs may exist")
        logs: list[LogRecord] = []
        omitted = 0
        for hit in hits[:limit]:
            hit = obj(hit, "OpenSearch hit")
            source = obj(hit.get("_source"), "OpenSearch log source")
            stamp = self._timestamp(field_value(source, f.timestamp))
            if not start <= stamp < end:
                omitted += 1
                continue
            resource_service = field_value(source, f.service)
            if resource_service is None:
                raise SchemaError("configured OpenSearch service field is missing")
            severity = field_value(source, f.severity, "UNSPECIFIED")
            attrs = dict(obj(field_value(source, f.attributes, {}), "OpenSearch log attributes"))
            attrs["aegis.opensearch"] = {"index": string(hit.get("_index"), "OpenSearch index"),
                                         "timestampRaw": field_value(source, f.timestamp),
                                         "resource": source.get("resource", {})}
            logs.append(LogRecord(
                id=string(hit.get("_index"), "OpenSearch index") + ":" + string(hit.get("_id"), "OpenSearch document ID"),
                timestamp=stamp, service=string(resource_service, "OpenSearch service"),
                severity=string(severity, "OpenSearch severity"),
                body=string(field_value(source, f.body), "OpenSearch log body"),
                trace_id=hex_id(field_value(source, f.trace_id), 32, optional=True),
                span_id=hex_id(field_value(source, f.span_id), 16, optional=True),
                attributes=attrs, source="opensearch:" + quote(hit["_index"], safe="")
                + "/_doc/" + quote(hit["_id"], safe=""),
            ))
        if omitted:
            warnings.append(f"{omitted} logs outside [start, end) omitted")
            partial = True
        return LogResult(logs=tuple(logs), warnings=tuple(warnings), truncated=truncated,
                         coverage_complete=False if partial or truncated else None)

    def _timestamp(self, value: Any) -> datetime:
        try:
            if self.fields.timestamp_format == "iso":
                result = datetime.fromisoformat(string(value, "OpenSearch timestamp").replace("Z", "+00:00"))
                if result.tzinfo is None or result.utcoffset() is None:
                    raise ValueError("timezone missing")
                return result.astimezone(timezone.utc)
            if isinstance(value, bool):
                raise ValueError("boolean timestamp")
            divisor = 1000 if self.fields.timestamp_format == "epoch_millis" else 1
            return datetime.fromtimestamp(float(value) / divisor, timezone.utc)
        except (ValueError, TypeError, OverflowError, OSError):
            raise SchemaError("malformed configured OpenSearch timestamp") from None
