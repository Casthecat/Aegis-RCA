"""Only observed service relationships; absent spans never prove absent calls."""

from collections import defaultdict
from collections.abc import Iterable

from aegis_rca.models.telemetry import SpanRecord

from .models import ObservedEdge, ObservedTopology


def _known_service(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and value.strip().lower() not in {
        "unknown", "unknown_service", "n/a", "null", "none",
    } and not value.strip().lower().startswith("unknown_service:")


def infer_topology(spans: Iterable[SpanRecord]) -> ObservedTopology:
    """Infer directed edges from same-trace parents or CLIENT ``peer.service``.

    A network host/address is not treated as a service identity. Reused span IDs
    in different traces do not match. Counts represent observed relationships,
    not the total request volume. ERROR counts reflect explicit span status.
    """
    indexed: dict[tuple[str, str], SpanRecord] = {}
    ambiguous: set[tuple[str, str]] = set()
    warnings = ["Topology reflects observed spans only; missing spans and sampling prevent completeness claims."]
    invalid_ids = 0
    for span in spans:
        if not span.trace_id.strip() or not span.span_id.strip():
            invalid_ids += 1
            continue
        key = (span.trace_id, span.span_id)
        if key in indexed and indexed[key] != span:
            ambiguous.add(key)
        else:
            indexed[key] = span
    for key in ambiguous:
        indexed.pop(key, None)
    if ambiguous:
        warnings.append(f"Excluded {len(ambiguous)} conflicting trace/span identity record(s).")
    if invalid_ids:
        warnings.append(f"Excluded {invalid_ids} span record(s) with empty identities.")

    # Key is caller, callee, trace, parent/client span, child span. CLIENT peer
    # metadata is merged into a matching parent edge so one call is not doubled.
    observations: dict[tuple[str, str, str, str, str], tuple[bool, set[str]]] = {}
    parent_edges: dict[tuple[str, str, str, str], list[tuple[str, str, str, str, str]]] = defaultdict(list)
    missing_parents = 0
    unknown_services = 0
    for child in indexed.values():
        if not child.parent_span_id:
            continue
        parent = indexed.get((child.trace_id, child.parent_span_id))
        if parent is None:
            missing_parents += 1
            continue
        if not _known_service(parent.service) or not _known_service(child.service):
            unknown_services += 1
            continue
        if parent.service == child.service:
            continue
        key = (parent.service, child.service, child.trace_id, parent.span_id, child.span_id)
        observations[key] = (parent.status.upper() == "ERROR" or child.status.upper() == "ERROR", {"parent_relationship"})
        parent_edges[key[:4]].append(key)

    for span in indexed.values():
        if span.kind != "CLIENT":
            continue
        peer = span.attributes.get("peer.service")
        if not _known_service(span.service) or not _known_service(peer):
            unknown_services += 1
            continue
        peer = peer.strip()
        if peer == span.service:
            continue
        prefix = (span.service, peer, span.trace_id, span.span_id)
        matching = parent_edges.get(prefix, [])
        if matching:
            for key in matching:
                observations[key][1].add("client_peer")
        else:
            observations[(*prefix, "")] = (span.status.upper() == "ERROR", {"client_peer"})

    grouped: dict[tuple[str, str], list[tuple[str, bool, set[str]]]] = defaultdict(list)
    for (caller, callee, trace_id, _, _), (error, support) in observations.items():
        grouped[(caller, callee)].append((trace_id, error, support))
    edges = tuple(
        ObservedEdge(
            caller=caller,
            callee=callee,
            trace_ids=tuple(sorted({trace for trace, _, _ in records})),
            observation_count=len(records),
            error_count=sum(error for _, error, _ in records),
            support=tuple(sorted(set().union(*(support for _, _, support in records)))),
        )
        for (caller, callee), records in sorted(grouped.items())
    )
    if missing_parents:
        warnings.append(f"{missing_parents} span(s) have unavailable parents; no edge inferred from their absence.")
    if unknown_services:
        warnings.append(f"{unknown_services} relationship candidate(s) lack an explicit known service identity.")
    return ObservedTopology(edges=edges, warnings=tuple(warnings))
