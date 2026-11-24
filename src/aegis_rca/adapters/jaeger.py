"""Jaeger public v3 queries, with OTLP trace normalization and bounded envelopes."""
from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from aegis_rca.models.telemetry import SpanRecord, TraceResult

from .common import CatalogResult, array, bounded_limit, iso, obj, optional_window, string, strings, window
from .http import AdapterError, JsonHttp, SchemaError

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
KINDS = {0: "UNKNOWN", 1: "INTERNAL", 2: "SERVER", 3: "CLIENT", 4: "PRODUCER", 5: "CONSUMER"}
STATUSES = {0: "UNSET", 1: "OK", 2: "ERROR"}


def nanos(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not re.fullmatch(r"[0-9]+", str(value)):
        raise SchemaError("malformed OTLP nanosecond timestamp")
    if len(str(value)) > 20:
        raise SchemaError("OTLP timestamp outside uint64 range")
    result = int(value)
    if not 0 <= result <= 18446744073709551615:
        raise SchemaError("OTLP timestamp outside uint64 range")
    return result


def datetime_nanos(value: datetime) -> int:
    delta = value - EPOCH
    return ((delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds) * 1000


def hex_id(value: Any, length: int, *, optional: bool = False) -> str | None:
    if optional and (value is None or value == "" or value == "0" * length):
        return None
    if not isinstance(value, str) or not re.fullmatch(rf"[0-9a-fA-F]{{{length}}}", value) or int(value, 16) == 0:
        raise SchemaError("malformed OTLP hexadecimal identifier")
    return value.lower()


def any_value(value: Any) -> Any:
    item = obj(value, "OTLP attribute value")
    if not item:
        return None  # An unset AnyValue is a valid empty value in protobuf.
    if len(item) != 1:
        raise SchemaError("OTLP AnyValue must have one value member")
    kind, raw = next(iter(item.items()))
    if kind in {"stringValue", "bytesValue"}:
        return string(raw, "OTLP attribute value")
    if kind == "boolValue" and isinstance(raw, bool):
        return raw
    if kind == "intValue" and not isinstance(raw, bool) and isinstance(raw, (int, str)):
        try:
            return int(raw)
        except ValueError:
            pass
    if kind == "doubleValue" and isinstance(raw, (int, float)) and not isinstance(raw, bool):
        if math.isfinite(raw):
            return raw
    if kind == "arrayValue":
        return [any_value(v) for v in array(obj(raw, "OTLP array value").get("values", []), "OTLP values")]
    if kind == "kvlistValue":
        return attributes(obj(raw, "OTLP kvlist value").get("values", []))
    raise SchemaError("malformed or unsupported OTLP attribute value")


def attributes(value: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for raw in array(value, "OTLP attributes"):
        pair = obj(raw, "OTLP attribute")
        key = string(pair.get("key"), "OTLP attribute key")
        if key in result:
            raise SchemaError("duplicate OTLP attribute key")
        result[key] = any_value(pair.get("value", {}))
    return result


class JaegerAdapter:
    def __init__(self, http: JsonHttp, *, max_spans: int = 10000):
        if not 1 <= max_spans <= 100000:
            raise ValueError("max_spans must be in [1, 100000]")
        self.http = http
        self.max_spans = max_spans

    def services(self) -> CatalogResult:
        data = obj(self.http.request("GET", "/api/v3/services"), "Jaeger services response")
        if "error" in data:
            raise AdapterError("Jaeger returned an API error")
        # Proto JSON can omit the zero-valued repeated field.
        if data and "services" not in data:
            raise SchemaError("malformed Jaeger services envelope")
        values = strings(data.get("services", []), "Jaeger services")
        capped = len(values) > 10000
        return CatalogResult(values=values[:10000], truncated=capped,
                             warnings=("service catalog locally capped",) if capped else ())

    def search(self, service: str, start: datetime, end: datetime,
               limit: int = 100, operation: str | None = None) -> TraceResult:
        start, end = window(start, end)
        bounded_limit(limit)
        search_depth = min(limit, 10)
        if not service or len(service) > 256:
            raise ValueError("service must contain 1 to 256 characters")
        params = {"query.serviceName": service, "query.startTimeMin": iso(start),
                  "query.startTimeMax": iso(end), "query.searchDepth": search_depth,
                  "query.rawTraces": "true"}
        if operation is not None:
            params["query.operationName"] = operation
        documents = self.http.stream_documents("/api/v3/traces", params=params)
        return self._normalize(documents, start, end, trace_limit=search_depth)

    def get_trace(self, trace_id: str, start: datetime | None = None,
                  end: datetime | None = None) -> TraceResult:
        if not re.fullmatch(r"(?:[0-9a-fA-F]{16}|[0-9a-fA-F]{32})", trace_id) or int(trace_id, 16) == 0:
            raise ValueError("trace_id must be a nonzero 64-bit or 128-bit hexadecimal ID")
        bounds = optional_window(start, end)
        params = {"rawTraces": "true"}
        if bounds:
            params.update({"startTime": bounds["start"], "endTime": bounds["end"]})
            start, end = window(start, end)
        documents = self.http.stream_documents(f"/api/v3/traces/{trace_id.lower()}", params=params)
        return self._normalize(documents, start, end)

    def _normalize(self, documents: tuple[dict, ...], start: datetime | None,
                   end: datetime | None, trace_limit: int | None = None) -> TraceResult:
        spans: list[SpanRecord] = []
        warnings: list[str] = []
        trace_ids: set[str] = set()
        seen: dict[tuple[str, str], SpanRecord] = {}
        clipped = dropped = missing_service = precision_loss = False
        capped = False
        low = datetime_nanos(start) if start is not None else None
        high = datetime_nanos(end) if end is not None else None
        for document in documents:
            if "error" in document:
                # A successful first chunk does not make a failed tail successful.
                raise AdapterError("Jaeger stream returned an error; partial response discarded")
            if "result" not in document:
                raise SchemaError("Jaeger v3 trace response requires a result envelope")
            data = obj(document["result"], "Jaeger result envelope")
            if data and "resourceSpans" not in data:
                raise SchemaError("malformed Jaeger OTLP trace data")
            for raw_resource in array(data.get("resourceSpans", []), "OTLP resourceSpans"):
                resource_spans = obj(raw_resource, "OTLP resource span")
                resource = obj(resource_spans.get("resource", {}), "OTLP resource")
                resource_attrs = attributes(resource.get("attributes", []))
                service = resource_attrs.get("service.name", "unknown")
                if not isinstance(service, str):
                    raise SchemaError("OTLP service.name must be a string")
                missing_service |= service == "unknown"
                dropped |= bool(resource.get("droppedAttributesCount", 0))
                for raw_scope in array(resource_spans.get("scopeSpans", []), "OTLP scopeSpans"):
                    scope = obj(raw_scope, "OTLP scope span")
                    for raw_span in array(scope.get("spans", []), "OTLP spans"):
                        span = obj(raw_span, "OTLP span")
                        trace_id = hex_id(span.get("traceId"), 32)
                        span_id = hex_id(span.get("spanId"), 16)
                        trace_ids.add(trace_id)
                        begin = nanos(span.get("startTimeUnixNano"))
                        finish = nanos(span.get("endTimeUnixNano"))
                        if finish < begin:
                            raise SchemaError("OTLP span ends before it starts")
                        if low is not None and not low <= begin < high:
                            clipped = True
                            continue
                        if len(spans) >= self.max_spans:
                            capped = True
                            continue
                        if trace_limit and len(trace_ids) > trace_limit:
                            capped = True
                            continue
                        try:
                            stamp = EPOCH + timedelta(microseconds=begin // 1000)
                        except OverflowError:
                            raise SchemaError("OTLP span timestamp outside datetime range") from None
                        precision_loss |= begin % 1000 != 0
                        attrs = attributes(span.get("attributes", []))
                        # Preserve raw ns and context without allowing attribute collisions.
                        attrs["aegis.otlp"] = {"startTimeUnixNano": str(begin), "endTimeUnixNano": str(finish),
                                               "resource": resource_attrs,
                                               "events": array(span.get("events", []), "OTLP events")}
                        status = obj(span.get("status", {}), "OTLP span status")
                        if status.get("message"):
                            attrs["aegis.otlp"]["statusMessage"] = string(status["message"], "OTLP status message")
                        kind_value = span.get("kind", 0)
                        code_value = status.get("code", 0)
                        if (isinstance(kind_value, bool) or not isinstance(kind_value, int)
                                or isinstance(code_value, bool) or not isinstance(code_value, int)
                                or kind_value not in KINDS or code_value not in STATUSES):
                            raise SchemaError("unsupported OTLP span kind or status enum")
                        record = SpanRecord(
                            trace_id=trace_id, span_id=span_id,
                            parent_span_id=hex_id(span.get("parentSpanId"), 16, optional=True),
                            service=service, name=string(span.get("name"), "OTLP span name"),
                            kind=KINDS[kind_value], start=stamp, duration_ms=(finish - begin) / 1000000,
                            status=STATUSES[code_value], attributes=attrs,
                            source=f"jaeger:/api/v3/traces/{trace_id}#span={span_id}",
                        )
                        key = (trace_id, span_id)
                        if key in seen:
                            if seen[key] != record:
                                raise SchemaError("conflicting duplicate Jaeger span")
                            continue
                        seen[key] = record
                        spans.append(record)
                        dropped |= any(bool(span.get(field, 0)) for field in (
                            "droppedAttributesCount", "droppedEventsCount", "droppedLinksCount"))
        if trace_limit and len(trace_ids) >= trace_limit:
            capped = True
            warnings.append("trace search depth reached; additional matching traces may exist")
        if len(spans) >= self.max_spans:
            capped = True
            warnings.append("local span cap reached; returned traces may be incomplete")
        if clipped:
            warnings.append("spans outside [start, end) omitted; cross-boundary traces are partial")
        if dropped:
            warnings.append("OTLP reports dropped attributes, events, or links")
        if missing_service:
            warnings.append("some resource spans lack service.name")
        if precision_loss:
            warnings.append("datetime start truncated to microseconds; raw nanoseconds preserved in attributes")
        if not documents:
            warnings.append("empty Jaeger stream; telemetry coverage is unknown")
        return TraceResult(spans=tuple(spans), warnings=tuple(warnings), truncated=capped,
                           coverage_complete=False if capped or clipped or dropped or missing_service else None)
