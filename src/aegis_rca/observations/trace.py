"""Deterministic RPC span observations without diagnostic interpretation."""

import hashlib
import json
import re

from ..models.observation import (
    CorrelationKeys,
    ObservationProvenance,
    ObservationQuality,
    ObservationRecord,
    RpcOutcome,
    RpcStatusSource,
)
from ..models.telemetry import SpanRecord
from .entity import resolve_peer, rpc_edge_key


GRPC_STATUS_NAMES = (
    "OK", "CANCELLED", "UNKNOWN", "INVALID_ARGUMENT", "DEADLINE_EXCEEDED",
    "NOT_FOUND", "ALREADY_EXISTS", "PERMISSION_DENIED", "RESOURCE_EXHAUSTED",
    "FAILED_PRECONDITION", "ABORTED", "OUT_OF_RANGE", "UNIMPLEMENTED",
    "INTERNAL", "UNAVAILABLE", "DATA_LOSS", "UNAUTHENTICATED",
)
GRPC_BY_CODE = dict(enumerate(GRPC_STATUS_NAMES))
GRPC_BY_NAME = {name: code for code, name in GRPC_BY_CODE.items()}
RPC_STATUS_FIELDS = ("rpc.grpc.status_code", "rpc.response.status_code")
RPC_OTLP_OUTCOME_MATRIX = {
    "missing": {
        "OK": "success", "ERROR": "failure", "UNSET": "unknown", "UNKNOWN": "unknown",
    },
    "ok": {
        "OK": "success", "ERROR": "inconsistent", "UNSET": "success", "UNKNOWN": "success",
    },
    "non_ok": {
        "OK": "inconsistent", "ERROR": "failure", "UNSET": "failure", "UNKNOWN": "failure",
    },
    "unrecognized": {
        "OK": "unknown", "ERROR": "failure", "UNSET": "unknown", "UNKNOWN": "unknown",
    },
}


def _stable_observation_id(payload: dict) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode()
    return "O-trace-" + hashlib.sha256(encoded).hexdigest()[:20]


def _rpc_status(raw: object, field_path: str) -> RpcStatusSource | None:
    if isinstance(raw, bool) or not isinstance(raw, (int, str)):
        return None
    value: int | str = raw
    code = None
    name = "UNRECOGNIZED"
    if isinstance(raw, int):
        code = raw
        name = GRPC_BY_CODE.get(raw, "UNRECOGNIZED")
    else:
        cleaned = raw.strip().upper()
        if re.fullmatch(r"[+-]?\d+", cleaned):
            code = int(cleaned)
            name = GRPC_BY_CODE.get(code, "UNRECOGNIZED")
        elif cleaned in GRPC_BY_NAME:
            code = GRPC_BY_NAME[cleaned]
            name = cleaned
    return RpcStatusSource(field_path=field_path, raw_value=value, code=code, name=name)


def _rpc_status_sources(span: SpanRecord) -> tuple[RpcStatusSource, ...]:
    return tuple(
        parsed
        for field in RPC_STATUS_FIELDS
        if field in span.attributes
        for parsed in (_rpc_status(span.attributes[field], f"attributes.{field}"),)
        if parsed is not None
    )


def _otel_status(status: str) -> str:
    normalized = status.upper()
    return normalized if normalized in {"OK", "ERROR", "UNSET"} else "UNKNOWN"


def _outcome(
    statuses: tuple[RpcStatusSource, ...], otlp_status: str
) -> tuple[str, RpcStatusSource | None, tuple[str, ...]]:
    warnings: list[str] = []
    chosen = statuses[0] if statuses else None
    semantic_statuses = {(status.code, status.name) for status in statuses}
    if len(semantic_statuses) > 1:
        return "inconsistent", chosen, ("Conflicting explicit RPC status fields.",)
    category = (
        "missing" if chosen is None else
        "unrecognized" if chosen.name == "UNRECOGNIZED" else
        "ok" if chosen.name == "OK" else
        "non_ok"
    )
    outcome = RPC_OTLP_OUTCOME_MATRIX[category][otlp_status]
    if category == "unrecognized":
        warnings.append("Explicit RPC status value is unrecognized.")
    if outcome == "inconsistent":
        warnings.append(
            "Explicit RPC OK conflicts with OTLP ERROR."
            if category == "ok"
            else "Explicit non-OK RPC status conflicts with OTLP OK."
        )
    return outcome, chosen, tuple(warnings)


def _is_rpc_span(span: SpanRecord, service_map: dict[str, str]) -> bool:
    if span.kind not in {"CLIENT", "SERVER"}:
        return False
    if any(field in span.attributes for field in (
        *RPC_STATUS_FIELDS, "rpc.system", "rpc.system.name", "rpc.service", "rpc.method",
    )):
        return True
    return any(
        span.name.lstrip("/").startswith(qualified + "/")
        for qualified in service_map
    )


def observe_spans(
    spans: tuple[SpanRecord, ...],
    service_map: dict[str, str],
    *,
    backend: str = "jaeger",
    synthetic: bool = False,
) -> tuple[ObservationRecord, ...]:
    records = []
    for span in spans:
        if not _is_rpc_span(span, service_map):
            continue
        statuses = _rpc_status_sources(span)
        otlp_status = _otel_status(span.status)
        outcome, chosen_status, status_warnings = _outcome(statuses, otlp_status)
        resolution = resolve_peer(span, service_map)
        warnings = [*status_warnings, *resolution.warnings]
        field_paths = {
            "kind", "name", "service", "start", "status", "trace_id", "span_id",
        }
        if span.parent_span_id is not None:
            field_paths.add("parent_span_id")
        field_paths.update(status.field_path for status in statuses)
        field_paths.update(
            candidate.field_path for candidate in resolution.candidates
        )
        for field in (
            *RPC_STATUS_FIELDS, "rpc.system", "rpc.system.name", "rpc.service",
            "rpc.method", "peer.service", "service.peer.name",
        ):
            if field in span.attributes:
                field_paths.add(f"attributes.{field}")
        rpc_system = span.attributes.get("rpc.system", span.attributes.get("rpc.system.name"))
        if not isinstance(rpc_system, str):
            rpc_system = None
        edge_key = rpc_edge_key(span, resolution.peer_service)
        value = RpcOutcome(
            operation=span.name, span_kind=span.kind, otlp_status=otlp_status,
            rpc_status_name=chosen_status.name if chosen_status else None,
            rpc_status_code=chosen_status.code if chosen_status else None,
            rpc_status_raw=chosen_status.raw_value if chosen_status else None,
            rpc_status_field=chosen_status.field_path if chosen_status else None,
            rpc_status_sources=statuses, outcome=outcome,
            peer_service=resolution.peer_service, rpc_system=rpc_system,
        )
        provenance = ObservationProvenance(
            backend=backend, locator=span.source, trace_id=span.trace_id,
            span_id=span.span_id, field_paths=tuple(sorted(field_paths)), synthetic=synthetic,
        )
        correlation = CorrelationKeys(
            trace_id=span.trace_id, span_id=span.span_id,
            parent_span_id=span.parent_span_id, rpc_edge_key=edge_key,
        )
        quality = ObservationQuality(
            rpc_status_explicit=bool(statuses),
            peer_resolution_method=resolution.method,
            peer_candidates=resolution.candidates,
            ambiguous=resolution.ambiguous,
            warnings=tuple(warnings),
        )
        identity = {
            "schema_version": 1,
            "kind": "rpc_outcome",
            "service": span.service,
            "timestamp": span.start.isoformat(),
            "value": value.model_dump(mode="json"),
            "provenance": provenance.model_dump(mode="json"),
            "correlation": correlation.model_dump(mode="json"),
            "quality": quality.model_dump(mode="json", exclude={"warnings"}),
        }
        rpc_label = value.rpc_status_name or "insufficient"
        summary = (
            f"{span.service} {span.kind.lower()} RPC {span.name}: "
            f"OTLP={otlp_status}; gRPC={rpc_label}; outcome={outcome}; "
            f"peer={resolution.peer_service or 'unknown'}"
        )
        records.append(ObservationRecord(
            id=_stable_observation_id(identity), service=span.service,
            timestamp=span.start, value=value, provenance=provenance,
            correlation=correlation, quality=quality, summary=summary[:2000],
        ))
    return tuple(records)
