"""Deterministic entity resolution and RPC edge orientation."""

from dataclasses import dataclass
import hashlib
import json

from ..models.observation import PeerCandidate
from ..models.telemetry import SpanRecord


PEER_METHOD_ORDER = {"explicit_peer": 0, "rpc_service_map": 1, "span_name_map": 2}


@dataclass(frozen=True)
class PeerResolution:
    peer_service: str | None
    method: str
    candidates: tuple[PeerCandidate, ...]
    ambiguous: bool
    warnings: tuple[str, ...]


def peer_candidates(
    span: SpanRecord, service_map: dict[str, str]
) -> tuple[PeerCandidate, ...]:
    candidates: list[PeerCandidate] = []
    for field in ("peer.service", "service.peer.name"):
        value = span.attributes.get(field)
        if isinstance(value, str) and value:
            candidates.append(PeerCandidate(
                method="explicit_peer", service=value, field_path=f"attributes.{field}"
            ))
    # On SERVER spans rpc.service and the qualified operation identify the local
    # RPC service, not the remote caller. They are callee hints only.
    if span.kind == "CLIENT":
        rpc_service = span.attributes.get("rpc.service")
        if isinstance(rpc_service, str) and rpc_service in service_map:
            candidates.append(PeerCandidate(
                method="rpc_service_map", service=service_map[rpc_service],
                field_path="attributes.rpc.service",
            ))
        for qualified, service in service_map.items():
            if span.name.lstrip("/").startswith(qualified + "/"):
                candidates.append(PeerCandidate(
                    method="span_name_map", service=service, field_path="name"
                ))
    unique = {
        (candidate.method, candidate.service, candidate.field_path): candidate
        for candidate in candidates
    }
    return tuple(unique[key] for key in sorted(
        unique, key=lambda item: (PEER_METHOD_ORDER[item[0]], item[2], item[1])
    ))


def resolve_peer(span: SpanRecord, service_map: dict[str, str]) -> PeerResolution:
    candidates = peer_candidates(span, service_map)
    if not candidates:
        return PeerResolution(
            peer_service=None, method="unresolved", candidates=(),
            ambiguous=False, warnings=(),
        )
    chosen = candidates[0]
    ambiguous = len({candidate.service for candidate in candidates}) > 1
    warnings = (
        ("Peer resolution sources disagree; deterministic precedence was applied.",)
        if ambiguous else ()
    )
    return PeerResolution(
        peer_service=chosen.service, method=chosen.method, candidates=candidates,
        ambiguous=ambiguous, warnings=warnings,
    )


def rpc_edge_endpoints(service: str, span_kind: str, peer_service: str | None) -> tuple[str, str] | None:
    """The P1.1 directed edge orientation shared by span identity and scope."""
    if peer_service is None or span_kind not in {"CLIENT", "SERVER"}:
        return None
    return (service, peer_service) if span_kind == "CLIENT" else (peer_service, service)


def directed_rpc_edge_key(caller: str, callee: str, operation: str) -> str:
    payload = json.dumps(
        [caller, callee, operation], ensure_ascii=False, separators=(",", ":")
    )
    return "rpc-edge-" + hashlib.sha256(payload.encode()).hexdigest()[:20]


def rpc_edge_key(span: SpanRecord, peer_service: str | None) -> str | None:
    endpoints = rpc_edge_endpoints(span.service, span.kind, peer_service)
    return directed_rpc_edge_key(*endpoints, span.name) if endpoints is not None else None
