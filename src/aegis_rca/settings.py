import json
from pathlib import Path
from urllib.parse import urlsplit
from typing import Literal

from pydantic import Field, field_validator

from .models.incident import Contract


class BackendSettings(Contract):
    prometheus_url: str
    jaeger_url: str
    opensearch_url: str
    log_index: str = "otel-logs-*"
    log_timestamp_field: str = "@timestamp"
    log_service_field: str = "resource.service.name"
    log_service_exact_field: str = "resource.service.name.keyword"
    log_body_field: str = "body"
    log_severity_field: str = "severity.text"
    log_trace_id_field: str = "traceId"
    log_trace_id_exact_field: str = "traceId.keyword"
    log_span_id_field: str = "spanId"
    log_timestamp_format: Literal["iso", "epoch_millis", "epoch_seconds"] = "iso"
    metric_query: str | None = None
    metric_step_seconds: int = Field(default=15, ge=1, le=300)
    max_results: int = Field(default=100, ge=1, le=500)
    max_window_seconds: int = Field(default=3600, ge=1, le=86400)
    request_timeout_seconds: float = Field(default=5, gt=0, le=60)
    max_response_bytes: int = Field(default=2_000_000, ge=1024, le=10_000_000)
    rpc_service_map: dict[str, str] = Field(default_factory=lambda: {"oteldemo.PaymentService": "payment"})

    @field_validator("prometheus_url", "jaeger_url", "opensearch_url")
    @classmethod
    def endpoint(cls, value):
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Telemetry endpoint must be an http(s) base URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Credentials, query strings and fragments are not allowed in base URLs")
        return value.rstrip("/")


def load_settings(path: Path) -> BackendSettings:
    if path.stat().st_size > 65536:
        raise ValueError("Settings file exceeds size limit")
    return BackendSettings.model_validate(json.loads(path.read_text(encoding="utf-8-sig")))
