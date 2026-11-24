"""Prometheus stable HTTP v1 catalog and range query adapter."""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from urllib.parse import quote

from aegis_rca.models.telemetry import MetricPoint, MetricResult, MetricSeries

from .common import CatalogResult, array, bounded_limit, obj, optional_window, strings, window
from .http import AdapterError, JsonHttp, SchemaError


class PrometheusAdapter:
    def __init__(self, http: JsonHttp):
        self.http = http

    @staticmethod
    def _success(payload: object) -> tuple[object, list[str]]:
        data = obj(payload, "Prometheus response")
        if data.get("status") == "error":
            raise AdapterError("Prometheus returned an API error")
        if data.get("status") != "success" or "data" not in data:
            raise SchemaError("malformed Prometheus success envelope")
        warnings = list(strings(data.get("warnings", []), "Prometheus warnings"))
        warnings.extend("info: " + item for item in strings(data.get("infos", []), "Prometheus infos"))
        return data["data"], warnings

    def _catalog(self, path: str, start: datetime | None, end: datetime | None) -> CatalogResult:
        data, warnings = self._success(self.http.request("GET", path, params=optional_window(start, end)))
        values = strings(data, "Prometheus catalog")
        truncated = len(values) > 10000
        if truncated:
            warnings.append("catalog locally capped at 10000 values")
        return CatalogResult(values=values[:10000], warnings=tuple(warnings), truncated=truncated)

    def label_names(self, start: datetime | None = None, end: datetime | None = None) -> CatalogResult:
        return self._catalog("/api/v1/labels", start, end)

    def label_values(self, label: str, start: datetime | None = None,
                     end: datetime | None = None) -> CatalogResult:
        if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", label):
            raise ValueError("label must be a Prometheus ASCII label name")
        return self._catalog(f"/api/v1/label/{quote(label, safe='')}/values", start, end)

    def query_range(self, query: str, start: datetime, end: datetime,
                    step_seconds: float = 15, limit: int = 100) -> MetricResult:
        start, end = window(start, end)
        bounded_limit(limit)
        if not query or len(query) > 8192:
            raise ValueError("query must contain 1 to 8192 characters")
        if not math.isfinite(step_seconds) or step_seconds <= 0:
            raise ValueError("step_seconds must be finite and positive")
        if (end - start).total_seconds() / step_seconds > 11000:
            raise ValueError("query exceeds 11000 steps per series")
        data, warnings = self._success(self.http.request("GET", "/api/v1/query_range", params={
            "query": query, "start": start.timestamp(), "end": end.timestamp(),
            "step": step_seconds, "limit": limit,
            "timeout": f"{int(self.http.timeout_seconds * 1000)}ms",
        }))
        matrix = obj(data, "Prometheus query data")
        if matrix.get("resultType") != "matrix":
            raise SchemaError("Prometheus query_range must return matrix")
        results = array(matrix.get("result"), "Prometheus matrix")
        series: list[MetricSeries] = []
        nonfinite = 0
        for item in results[:limit]:
            record = obj(item, "Prometheus series")
            labels = obj(record.get("metric"), "Prometheus metric labels")
            if not all(isinstance(k, str) and isinstance(v, str) for k, v in labels.items()):
                raise SchemaError("malformed Prometheus metric labels")
            points: list[MetricPoint] = []
            for raw in array(record.get("values"), "Prometheus samples"):
                if not isinstance(raw, list) or len(raw) != 2 or isinstance(raw[0], bool):
                    raise SchemaError("malformed Prometheus sample pair")
                try:
                    stamp = datetime.fromtimestamp(float(raw[0]), timezone.utc)
                    value = float(raw[1])
                except (ValueError, TypeError, OverflowError, OSError):
                    raise SchemaError("malformed Prometheus numeric sample") from None
                if not math.isfinite(value):
                    nonfinite += 1
                    continue
                # Prometheus end is inclusive; the domain contract is always [start, end).
                if start <= stamp < end:
                    points.append(MetricPoint(timestamp=stamp, value=value))
            series.append(MetricSeries(name=labels.get("__name__", "expression"), labels=labels,
                                       points=tuple(points), source="prometheus:/api/v1/query_range"))
        truncated = len(results) >= limit
        if truncated:
            warnings.append("series limit reached; additional matching series may exist")
        if nonfinite:
            warnings.append(f"{nonfinite} non-finite samples omitted; they are not zero values")
        return MetricResult(series=tuple(series), warnings=tuple(warnings), truncated=truncated,
                            coverage_complete=False if truncated or nonfinite else None)
