"""Pure positive checks over typed observations; no telemetry or inference IO."""

import operator

from ..models.check import (
    CategoryPresent, CheckCoverageView, CheckResult, CheckSpec, CounterDeltaCompare,
    IntervalRateCompare, LegacyEvidenceExists, MetricSummaryExists, RpcExists,
)
from ..models.metric_observation import (
    CategoryPresenceValue, CounterDeltaValue, IntervalRateValue, MetricObservationRecord,
    WindowSummaryValue,
)
from ..models.observation import ObservationRecord
from ._shared import closes, covers, deduplicate, prepare_coverage, result


_COMPARATORS = {"eq": operator.eq, "ne": operator.ne, "lt": operator.lt,
                "lte": operator.le, "gt": operator.gt, "gte": operator.ge}


def _rpc_matches(predicate: RpcExists, record: ObservationRecord) -> bool | None:
    value, quality = record.value, record.quality
    decisions: list[bool | None] = [record.service == predicate.local_service]
    for field in ("span_kind", "operation", "rpc_system"):
        expected = getattr(predicate, field)
        actual = getattr(value, field)
        if expected is not None:
            decisions.append(None if actual is None else actual == expected)
    if predicate.otlp_status is not None:
        decisions.append(None if value.otlp_status == "UNKNOWN" and predicate.otlp_status != "UNKNOWN"
                         else value.otlp_status == predicate.otlp_status)
    if predicate.outcomes:
        # Acquisition completeness cannot resolve uncertainty inside a fact.
        # Explicitly requested unresolved states still have positive membership.
        decisions.append(True if value.outcome in predicate.outcomes else
                         None if value.outcome in {"unknown", "inconsistent"} else False)
    if predicate.rpc_status_names:
        # Protocol UNKNOWN is concrete gRPC code 2, unlike normalized OTLP UNKNOWN.
        membership = {source.name in predicate.rpc_status_names for source in value.rpc_status_sources}
        decisions.append(None if value.rpc_status_name is None or len(membership) > 1 else
                         value.rpc_status_name in predicate.rpc_status_names)
    if predicate.peer_service is not None:
        decisions.append(None if value.peer_service is None or quality.ambiguous
                         or quality.peer_resolution_method == "unresolved" else
                         value.peer_service == predicate.peer_service)
    if predicate.require_resolved_peer and (value.peer_service is None
                                            or quality.peer_resolution_method == "unresolved"):
        decisions.append(None)
    if predicate.require_unambiguous_peer and quality.ambiguous:
        decisions.append(None)
    if False in decisions:
        return False
    return None if None in decisions else True


def _inside_metric_window(record: MetricObservationRecord, spec: CheckSpec) -> bool:
    value, window = record.value, spec.scope.window
    if isinstance(value, WindowSummaryValue):
        start, end = value.observed_sample_extent.first, value.observed_sample_extent.last
    elif isinstance(value, (CounterDeltaValue, IntervalRateValue)):
        start, end = value.interval.start, value.interval.end
    else:
        return True  # Category admission requires exact acquisition scope instead.
    return window.start <= start <= end < window.end


def evaluate_check(
    spec: CheckSpec, *,
    observations: tuple[ObservationRecord | MetricObservationRecord, ...] = (),
    coverage: tuple[CheckCoverageView, ...] = (),
) -> CheckResult:
    """Evaluate supplied immutable facts. Legacy Evidence has a separate API.

    Metric records lack service scope, so callers must explicitly admit their
    IDs through a matching coverage view, even for positive checks. Completeness
    flags are not required for a positive match or a selected numeric comparison.
    """
    if isinstance(spec.predicate, LegacyEvidenceExists):
        raise TypeError("LegacyEvidenceExists requires evaluate_legacy_check")
    if any(not isinstance(item, (ObservationRecord, MetricObservationRecord)) for item in observations):
        raise TypeError("native evaluator accepts typed observation records only")
    views = prepare_coverage(coverage)
    applicable = tuple(view for view in views if covers(view, spec.scope, "observation"))
    records, conflicts = deduplicate(observations)
    if conflicts:
        return result(spec, "UNKNOWN", "conflicting_fact_identity", relevant=conflicts)
    if spec.relation != "supports_hypothesis":
        return result(spec, "UNKNOWN", "unsupported_relation")
    if spec.semantic_version != "1":
        return result(spec, "UNKNOWN", "unsupported_check_version")
    predicate = spec.predicate
    if isinstance(predicate, RpcExists):
        selected = []
        used = []
        for record in records:
            if not isinstance(record, ObservationRecord):
                continue
            if not (record.service in spec.scope.services
                    and spec.scope.window.start <= record.timestamp < spec.scope.window.end):
                continue
            bindings = tuple(view for view in applicable if record.id in view.admitted_ids)
            if (spec.scope.query_identity is not None or spec.scope.acquisition_id is not None) and not bindings:
                continue
            selected.append(record)
            used.extend(bindings)
        decisions = [(record.id, _rpc_matches(predicate, record)) for record in selected]
        relevant = tuple(identifier for identifier, _ in decisions)
        matched = tuple(identifier for identifier, matches in decisions if matches is True)
        if matched:
            return result(spec, "SUPPORT", "positive_match", relevant=relevant,
                          matched=matched, coverage=used)
        if any(matches is None for _, matches in decisions):
            return result(spec, "UNKNOWN", "unresolved_rpc_fields", relevant=relevant, coverage=used)
        complete = closes(spec.scope, applicable, records)
        return result(spec, "NEUTRAL" if complete else "UNKNOWN",
                      "complete_no_match" if complete else "open_scope_no_match",
                      relevant=relevant, coverage=applicable)

    exact_category = isinstance(predicate, CategoryPresent)
    # Numeric extents locate time, but no metric record locates a service.
    # A multi-service acquisition cannot localize a fact to a narrower service.
    metric_views = tuple(view for view in applicable
                         if view.services == spec.scope.services
                         and covers(view, spec.scope, "observation", exact=exact_category))
    selected = []
    used = []
    for record in records:
        if not isinstance(record, MetricObservationRecord):
            continue
        if exact_category:
            if not isinstance(record.value, CategoryPresenceValue) or record.value.label != predicate.label:
                continue
        elif record.series != predicate.series:
            continue
        bindings = tuple(view for view in metric_views if record.id in view.admitted_ids)
        if bindings and _inside_metric_window(record, spec):
            selected.append(record)
            used.extend(bindings)
    relevant = tuple(record.id for record in selected)
    if isinstance(predicate, CategoryPresent):
        matched = tuple(record.id for record in selected if predicate.value in record.value.observed_values)
        if matched:
            return result(spec, "SUPPORT", "returned_category_present", relevant=relevant,
                          matched=matched, coverage=used)
        # Different returned sets must not silently substitute for each other.
        if len(selected) > 1:
            return result(spec, "UNKNOWN", "ambiguous_category_fact", relevant=relevant, coverage=used)
        return result(spec, "NEUTRAL" if selected else "UNKNOWN",
                      "returned_category_not_present" if selected else "missing_category_fact",
                      relevant=relevant, coverage=used)
    if isinstance(predicate, MetricSummaryExists):
        matched = tuple(record.id for record in selected if isinstance(record.value, WindowSummaryValue))
        if matched:
            return result(spec, "SUPPORT", "summary_present", relevant=relevant,
                          matched=matched, coverage=used)
        # Query completeness cannot establish the existence of a usable summary;
        # empty/nonfinite samples and unresolved derivations remain unknown.
        return result(spec, "UNKNOWN", "missing_summary_fact", relevant=relevant, coverage=used)

    expected_type = CounterDeltaValue if isinstance(predicate, CounterDeltaCompare) else IntervalRateValue
    candidates = [record for record in selected if isinstance(record.value, expected_type)
                  and (predicate.interval is None or record.value.interval == predicate.interval)]
    if len(candidates) != 1:
        return result(spec, "UNKNOWN", "ambiguous_metric_fact" if candidates else "missing_metric_fact",
                      relevant=relevant, coverage=used)
    fact = candidates[0]
    # Only consume the existing typed delta/rate. Never derive from endpoints.
    numeric_value = fact.value.delta if isinstance(predicate, CounterDeltaCompare) else fact.value.rate
    satisfied = _COMPARATORS[predicate.operator](numeric_value, predicate.threshold)
    return result(spec, "SUPPORT" if satisfied else "NEUTRAL",
                  "comparison_true" if satisfied else "comparison_false", relevant=relevant,
                  matched=(fact.id,) if satisfied else (), coverage=used)
