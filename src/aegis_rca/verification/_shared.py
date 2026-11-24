"""Internal deterministic admission and result construction for both boundaries."""

from ..models.check import CheckCoverageView, CheckReason, CheckResult, CheckScope, CheckSpec
from ..models.identity import structural_id
from ..models.evidence import Evidence
from ..models.metric_observation import MetricObservationRecord
from ..models.observation import ObservationRecord


def covers(view: CheckCoverageView, scope: CheckScope, input_kind: str, *, exact=False) -> bool:
    if view.input_kind != input_kind or view.signal != scope.signal:
        return False
    if scope.query_identity is not None and view.query_identity != scope.query_identity:
        return False
    if scope.acquisition_id is not None and view.acquisition_id != scope.acquisition_id:
        return False
    if exact:
        return view.services == scope.services and view.window == scope.window
    return (set(scope.services) <= set(view.services)
            and view.window.start <= scope.window.start
            and view.window.end >= scope.window.end)


def prepare_coverage(coverage: tuple[CheckCoverageView, ...]) -> tuple[CheckCoverageView, ...]:
    views = {}
    for view in coverage:
        if not isinstance(view, CheckCoverageView):
            raise TypeError("coverage must contain CheckCoverageView records")
        key = (view.input_kind, view.acquisition_id)
        payload = view.identity_payload()
        if key in views and views[key].identity_payload() != payload:
            raise ValueError("conflicting coverage for one acquisition identity")
        views[key] = view.model_copy(update={"warnings": ()})
    return tuple(views[key] for key in sorted(views))


def closes(scope: CheckScope, views: tuple[CheckCoverageView, ...],
           records: tuple[ObservationRecord | MetricObservationRecord | Evidence, ...]) -> bool:
    # The logical query identifies the intended selection, not just service/time.
    supplied = {record.id: record for record in records}

    def admitted_set_present(view):
        for identifier in view.admitted_ids:
            record = supplied.get(identifier)
            if record is None or record.signal != view.signal:
                return False
            legacy = isinstance(record, Evidence)
            if legacy != (view.input_kind == "legacy_evidence"):
                return False
            if isinstance(record, (ObservationRecord, Evidence)) and not (
                record.service in view.services
                and view.window.start <= record.timestamp < view.window.end
            ):
                return False
        return True

    input_kind = "legacy_evidence" if scope.signal == "log" else "observation"
    return scope.query_identity is not None and any(
        covers(view, scope, input_kind)
        and view.query_status == "succeeded" and view.coverage_complete is True
        and not view.truncated and view.pre_capture_omissions == "none"
        and view.fact_set_complete and admitted_set_present(view)
        for view in views
    )


def semantic_payload(record: ObservationRecord | MetricObservationRecord | Evidence) -> dict:
    if isinstance(record, ObservationRecord):
        return record.model_dump(include={"signal", "kind", "service", "timestamp", "value",
                                          "correlation", "quality"},
                                 exclude={"quality": {"warnings"}})
    if isinstance(record, MetricObservationRecord):
        return record.model_dump(include={"signal", "series", "descriptor", "value", "provenance"},
                                 exclude={"provenance": {"source_locators", "exclusions_ref"}})
    return record.model_dump(include={"signal", "service", "timestamp", "observation"})


def deduplicate(records):
    """Equal IDs have one vote (none); conflicting facts cannot be selected safely."""
    unique, fingerprints, conflicts = {}, {}, set()
    for record in records:
        fingerprint = structural_id("", semantic_payload(record))
        if record.id in fingerprints and fingerprints[record.id] != fingerprint:
            conflicts.add(record.id)
        fingerprints[record.id] = fingerprint
        unique[record.id] = record
    return tuple(unique[key] for key in sorted(unique)), tuple(sorted(conflicts))


def result(spec: CheckSpec, verdict: str, reason: CheckReason, *, relevant=(), matched=(),
           coverage=(), legacy=False) -> CheckResult:
    return CheckResult(
        check_id=spec.id, verdict=verdict, reason=reason,
        input_kind="legacy_evidence" if legacy else "observation",
        evaluator="legacy_compatibility_v1" if legacy else "observation_verifier_v1",
        relevant_ids=tuple(relevant), matched_ids=tuple(matched), coverage_used=tuple(coverage),
    )
