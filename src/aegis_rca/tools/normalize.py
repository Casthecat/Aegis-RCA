import hashlib
import re

from ..models.evidence import Evidence, EvidenceSource, Observation
from ..models.observation import ObservationRecord
from ..models.telemetry import LogRecord, SpanRecord
from ..observations.entity import resolve_peer
from ..observations.trace import observe_spans


def stable_id(signal: str, locator: str, content: str) -> str:
    return f"E-{signal}-" + hashlib.sha256(f"{locator}\n{content}".encode()).hexdigest()[:20]


def span_peer(span: SpanRecord, service_map: dict[str, str]) -> str | None:
    return resolve_peer(span, service_map).peer_service


def project_legacy_rpc_evidence(
    observations: tuple[ObservationRecord, ...],
) -> tuple[Evidence, ...]:
    records = []
    for record in observations:
        if record.value.span_kind != "CLIENT" or record.value.rpc_status_name != "UNAVAILABLE":
            continue
        peer = record.value.peer_service
        excerpt = (
            f"{record.service} client RPC {record.value.operation}: "
            f"gRPC UNAVAILABLE; peer={peer or 'unknown'}"
        )
        locator = record.provenance.locator
        trace_id = record.correlation.trace_id
        span_id = record.correlation.span_id
        records.append(Evidence(
            id=stable_id("trace", f"{locator}#{trace_id}/{span_id}", excerpt),
            signal="trace", service=record.service, timestamp=record.timestamp,
            observation=Observation(code="rpc_unavailable", peer_service=peer),
            excerpt=excerpt[:2000],
            source=EvidenceSource(
                backend="jaeger", locator=locator, synthetic=record.provenance.synthetic
            ),
            correlation_group=trace_id,
        ))
    return tuple(records)


def normalize_spans(spans: tuple[SpanRecord, ...], service_map: dict[str, str]) -> tuple[Evidence, ...]:
    return project_legacy_rpc_evidence(observe_spans(spans, service_map))


def normalize_logs(logs: tuple[LogRecord, ...], spans: tuple[SpanRecord, ...],
                   service_map: dict[str, str]) -> tuple[Evidence, ...]:
    records = []
    for log in logs:
        lower = log.body.lower()
        code = None
        if "connection refused" in lower or "actively refused" in lower:
            code = "connection_refused"
        elif any(pattern in lower for pattern in ("no such host", "name resolution failed",
                 "temporary failure in name resolution", "dns resolution failed", "enotfound", "eai_again")):
            code = "dns_resolution_failed"
        if code is None:
            continue
        peer = log.attributes.get("peer.service")
        rpc_service = log.attributes.get("rpc.service")
        if not peer and isinstance(rpc_service, str):
            peer = service_map.get(rpc_service)
        if not isinstance(peer, str):
            peer = None
        # Require the connection error itself to name its endpoint. Merely mentioning
        # a service somewhere in a log is not enough to assign it the failed peer.
        mentioned = {service for service in service_map.values()
                     if re.search(r"(?:dial(?: tcp)?|connect(?:ion)?(?: to)?|lookup)\s+"
                                  + re.escape(service) + r"(?=[:.\s/]|$)", lower)}
        if peer is None and len(mentioned) == 1:
            peer = next(iter(mentioned))
        records.append(Evidence(
            id=stable_id("log", f"{log.source}#{log.id}", log.body), signal="log", service=log.service,
            timestamp=log.timestamp, observation=Observation(code=code, peer_service=peer),
            excerpt=log.body[:2000], source=EvidenceSource(backend="opensearch", locator=log.source),
            correlation_group=log.trace_id,
        ))
    return tuple(records)
