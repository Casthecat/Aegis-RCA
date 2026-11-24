"""Explicit migration boundary for existing structured log connectivity evidence.

No text matching, provenance conversion, trace correlation, or causal inference
is performed here. The existing Evidence identities remain visible in results.
"""

from ..models.check import CheckCoverageView, CheckResult, CheckSpec, LegacyEvidenceExists
from ..models.evidence import Evidence
from ._shared import closes, covers, deduplicate, prepare_coverage, result


def _matches(predicate: LegacyEvidenceExists, record: Evidence) -> bool | None:
    if record.service != predicate.service or record.observation.code not in predicate.codes:
        return False
    if record.observation.peer_service is None:
        return None
    return record.observation.peer_service == predicate.peer_service


def evaluate_legacy_check(
    spec: CheckSpec, *, evidence: tuple[Evidence, ...] = (),
    coverage: tuple[CheckCoverageView, ...] = (),
) -> CheckResult:
    """Match admitted structured log codes without reinterpreting legacy content.

    Positive records have their own service and half-open timestamp scope. A
    requested logical query/acquisition additionally requires explicit admission
    through its coverage view. Negative results require complete acquisition and
    complete fact admission; a potentially relevant unresolved peer stays unknown.
    """
    if not isinstance(spec.predicate, LegacyEvidenceExists):
        raise TypeError("legacy bridge accepts LegacyEvidenceExists only")
    if any(not isinstance(item, Evidence) for item in evidence):
        raise TypeError("legacy bridge accepts Evidence records only")
    views = prepare_coverage(coverage)
    applicable = tuple(view for view in views if covers(view, spec.scope, "legacy_evidence"))
    records, conflicts = deduplicate(evidence)
    if conflicts:
        return result(spec, "UNKNOWN", "conflicting_fact_identity", relevant=conflicts, legacy=True)
    if spec.relation != "supports_hypothesis":
        return result(spec, "UNKNOWN", "unsupported_relation", legacy=True)
    if spec.semantic_version != "1":
        return result(spec, "UNKNOWN", "unsupported_check_version", legacy=True)
    selected = []
    used = []
    for record in records:
        if not (record.signal == "log" and record.service in spec.scope.services
                and spec.scope.window.start <= record.timestamp < spec.scope.window.end):
            continue
        bindings = tuple(view for view in applicable if record.id in view.admitted_ids)
        if (spec.scope.query_identity is not None or spec.scope.acquisition_id is not None) and not bindings:
            continue
        selected.append(record)
        used.extend(bindings)
    decisions = [(record.id, _matches(spec.predicate, record)) for record in selected]
    relevant = tuple(identifier for identifier, _ in decisions)
    matched = tuple(identifier for identifier, match in decisions if match is True)
    if matched:
        return result(spec, "SUPPORT", "positive_match", relevant=relevant,
                      matched=matched, coverage=used, legacy=True)
    if any(match is None for _, match in decisions):
        return result(spec, "UNKNOWN", "unresolved_legacy_peer", relevant=relevant,
                      coverage=used, legacy=True)
    complete = closes(spec.scope, applicable, records)
    return result(spec, "NEUTRAL" if complete else "UNKNOWN",
                  "complete_no_match" if complete else "open_scope_no_match",
                  relevant=relevant, coverage=applicable, legacy=True)
