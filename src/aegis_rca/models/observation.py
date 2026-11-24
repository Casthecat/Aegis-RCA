"""Immutable, source-backed telemetry observations without diagnostic claims."""

from typing import Literal

from pydantic import AwareDatetime, Field

from .incident import Contract


GrpcStatusName = Literal[
    "OK",
    "CANCELLED",
    "UNKNOWN",
    "INVALID_ARGUMENT",
    "DEADLINE_EXCEEDED",
    "NOT_FOUND",
    "ALREADY_EXISTS",
    "PERMISSION_DENIED",
    "RESOURCE_EXHAUSTED",
    "FAILED_PRECONDITION",
    "ABORTED",
    "OUT_OF_RANGE",
    "UNIMPLEMENTED",
    "INTERNAL",
    "UNAVAILABLE",
    "DATA_LOSS",
    "UNAUTHENTICATED",
    "UNRECOGNIZED",
]


class RpcStatusSource(Contract):
    field_path: str
    raw_value: int | str
    code: int | None = None
    name: GrpcStatusName


class PeerCandidate(Contract):
    method: Literal["explicit_peer", "rpc_service_map", "span_name_map"]
    service: str
    field_path: str


class RpcOutcome(Contract):
    operation: str
    span_kind: Literal["CLIENT", "SERVER"]
    otlp_status: Literal["OK", "ERROR", "UNSET", "UNKNOWN"]
    rpc_status_name: GrpcStatusName | None = None
    rpc_status_code: int | None = None
    rpc_status_raw: int | str | None = None
    rpc_status_field: str | None = None
    rpc_status_sources: tuple[RpcStatusSource, ...] = ()
    outcome: Literal["success", "failure", "unknown", "inconsistent"]
    peer_service: str | None = None
    rpc_system: str | None = None


class ObservationProvenance(Contract):
    backend: str
    locator: str = Field(min_length=1)
    trace_id: str
    span_id: str
    derivation: Literal["rpc_span_observation"] = "rpc_span_observation"
    derivation_version: Literal[1] = 1
    field_paths: tuple[str, ...] = ()
    synthetic: bool = False


class CorrelationKeys(Contract):
    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    rpc_edge_key: str | None = None


class ObservationQuality(Contract):
    rpc_status_explicit: bool
    peer_resolution_method: Literal[
        "explicit_peer", "rpc_service_map", "span_name_map", "unresolved"
    ]
    peer_candidates: tuple[PeerCandidate, ...] = ()
    ambiguous: bool = False
    warnings: tuple[str, ...] = ()


class ObservationRecord(Contract):
    id: str
    schema_version: Literal[1] = 1
    signal: Literal["trace"] = "trace"
    kind: Literal["rpc_outcome"] = "rpc_outcome"
    service: str
    timestamp: AwareDatetime
    value: RpcOutcome
    provenance: ObservationProvenance
    correlation: CorrelationKeys
    quality: ObservationQuality
    summary: str = Field(max_length=2000)
