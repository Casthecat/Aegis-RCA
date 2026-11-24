"""Explicit, conservative counter arithmetic on a barrier-preserving timeline."""

from datetime import timezone
import math

from ..models.incident import TimeWindow
from ..models.metric_observation import (
    ClosedSampleInterval, CounterDeltaSpec, CounterDeltaValue, IntervalRateSpec,
    IntervalRateValue, MetricDerivationDiagnostic, MetricDerivationExclusion,
    MetricDerivationSpec, MetricDerivedFactProvenance, MetricObservationBatch,
    MetricObservationContext, MetricObservationQuality, MetricObservationRecord,
)
from ..models.telemetry import MetricResult
from .metrics import PreparedMetricSeries, _digest, _observe_prepared, _prepare_series


def _scope_window(spec, window):
    selected = window if spec.sample_scope == "requested_window" else spec.scope_window
    if selected is None:
        return None
    return TimeWindow(start=selected.start.astimezone(timezone.utc), end=selected.end.astimezone(timezone.utc))


def _diagnostic(spec, scope_window, code, exclusions=()):
    exclusions = tuple(sorted(exclusions, key=lambda item: (
        item.timestamp if item.timestamp is not None else item.interval.start,
        item.reason,
        item.interval.end if item.interval is not None else item.timestamp,
    )))
    record = MetricDerivationDiagnostic(id="pending", code=code, spec=spec,
                                        scope_window=scope_window, exclusions=exclusions)
    identity = {"version": 1, "diagnostic": record.model_dump(exclude={"id"})}
    return record.model_copy(update={"id": "D-metric-" + _digest(identity)})


def _derived_id(record):
    payload = record.model_dump(exclude={"id", "summary", "quality"})
    payload["provenance"].pop("source_locators")
    payload["provenance"].pop("exclusions_ref")
    return "O-metric-" + _digest({"identity_version": 1, "fact": payload})


def _segments(prepared: PreparedMetricSeries, spec, scope_window):
    """Walk the source timeline, never an adjacency list of usable points."""
    segments, current, exclusions = [], [], []
    eligible = 0
    for point in prepared.timeline:
        if scope_window is not None and not scope_window.start <= point.timestamp < scope_window.end:
            exclusions.append(MetricDerivationExclusion(reason="outside_scope", timestamp=point.timestamp))
            continue
        reasons = list(point.barriers)
        if point.value is not None and point.value < 0:
            reasons.append("negative_counter")
        if reasons:
            exclusions.extend(MetricDerivationExclusion(reason=reason, timestamp=point.timestamp) for reason in reasons)
            if current:
                segments.append(tuple(current))
                current = []
            continue
        eligible += 1
        if current:
            previous = current[-1]
            interval = ClosedSampleInterval(start=previous.timestamp, end=point.timestamp)
            seconds = (point.timestamp - previous.timestamp).total_seconds()
            pair_reasons = []
            if point.value < previous.value:
                pair_reasons.append("visible_reset")
            if spec.max_gap_seconds is not None and seconds > spec.max_gap_seconds:
                pair_reasons.append("max_gap_exceeded")
            if pair_reasons:
                exclusions.extend(MetricDerivationExclusion(reason=reason, interval=interval) for reason in pair_reasons)
                segments.append(tuple(current))
                current = []
        current.append(point)
    if current:
        segments.append(tuple(current))
    return segments, exclusions, eligible


def _value(spec, points):
    first, last = points[0], points[-1]
    interval = ClosedSampleInterval(start=first.timestamp, end=last.timestamp)
    delta = last.value - first.value
    if isinstance(spec, CounterDeltaSpec):
        if not math.isfinite(delta):
            return None
        return CounterDeltaValue(interval=interval, start_value=first.value,
                                 end_value=last.value, delta=delta, used_sample_count=len(points))
    # IntervalRateSpec's contract and the caller both enforce exactly two points.
    if len(points) != 2:
        raise ValueError("local_adjacent_endpoint requires exactly two adjacent points")
    seconds = (last.timestamp - first.timestamp).total_seconds()
    rate = delta / seconds
    if not math.isfinite(rate):
        return None
    return IntervalRateValue(interval=interval, start_value=first.value, end_value=last.value,
                             delta=delta, elapsed_seconds=seconds, rate=rate)


def _derived_summary(value):
    if isinstance(value, CounterDeltaValue):
        return f"Visible counter endpoint difference={value.delta:g} over the recorded sample interval."
    return f"Local adjacent endpoint rate={value.rate:g} per second over {value.elapsed_seconds:g} seconds."


def derive_metrics(
    result: MetricResult,
    *,
    window: TimeWindow,
    context: MetricObservationContext | None = None,
    specs: tuple[MetricDerivationSpec, ...] = (),
) -> MetricObservationBatch:
    """Return unchanged P1.3a facts plus only explicitly authorized derivations.

    Segment delta emits one fact per maximal legal segment, never a sum of
    disjoint regions. Local rate v1 is always adjacent-pair arithmetic.
    """
    context = context or MetricObservationContext()
    if any(not isinstance(spec, (CounterDeltaSpec, IntervalRateSpec)) for spec in specs):
        raise TypeError("specs must contain CounterDeltaSpec or IntervalRateSpec contracts")
    prepared = _prepare_series(result)
    batch = _observe_prepared(result, window=window, context=context, prepared=prepared)
    if not specs:
        return batch
    by_key = {item.key: item for item in prepared}
    descriptors = {binding.series: binding.descriptor for binding in context.descriptors}
    summaries = {record.series: record for record in batch.observations if record.series is not None}
    records, diagnostics = [], []
    # Spec order and duplicate requests do not change facts or diagnostics.
    ordered_specs = {_digest(spec.model_dump()): spec for spec in specs}
    for _, spec in sorted(ordered_specs.items()):
        scope_window = _scope_window(spec, window)
        item = by_key.get(spec.series)
        descriptor = descriptors.get(spec.series)
        reason = None
        if item is None:
            reason = "series_not_returned"
        elif descriptor is None or context.contract is None:
            reason = "missing_counter_authority"
        elif descriptor.instrument_type != "counter" or descriptor.value_semantics != "cumulative_counter":
            reason = "incompatible_descriptor"
        elif isinstance(spec, CounterDeltaSpec) and spec.mode == "single_monotonic_segment" and spec.max_gap_seconds is None:
            reason = "segment_gap_required"
        if reason is not None:
            diagnostics.append(_diagnostic(spec, scope_window, reason))
            continue

        segments, exclusions, eligible = _segments(item, spec, scope_window)
        derived = []
        for segment in segments:
            if len(segment) < 2:
                continue
            inputs = ((segment,) if spec.mode == "single_monotonic_segment"
                      else tuple((first, last) for first, last in zip(segment, segment[1:])))
            for points in inputs:
                value = _value(spec, points)
                if value is None:
                    exclusions.append(MetricDerivationExclusion(reason="numeric_overflow",
                        interval=ClosedSampleInterval(start=points[0].timestamp, end=points[-1].timestamp)))
                else:
                    derived.append((points, value))
        diagnostic = None
        if not derived or exclusions:
            code = ("insufficient_samples" if eligible < 2 else "no_legal_intervals") if not derived else "intervals_excluded"
            diagnostic = _diagnostic(spec, scope_window, code, exclusions)
            diagnostics.append(diagnostic)

        for points, value in derived:
            quality = [issue for issue in summaries[spec.series].quality.issues
                       if issue in {"expression_placeholder", "metric_name_conflict"}]
            if context.sample_time_basis == "unknown":
                quality.append("unknown_time_basis")
            elif context.sample_time_basis == "query_evaluation":
                quality.append("evaluation_timestamps_not_scrape_timestamps")
            provenance = MetricDerivedFactProvenance(
                derivation="counter_delta" if isinstance(spec, CounterDeltaSpec) else "local_interval_rate",
                input_digest=_digest([(point.timestamp, point.value) for point in points]),
                used_interval=value.interval, spec=spec, sample_scope=spec.sample_scope,
                scope_window=scope_window,
                gap_policy="unspecified" if spec.max_gap_seconds is None else "maximum_gap",
                max_gap_seconds=spec.max_gap_seconds, context_identity=context.contract,
                sample_time_basis=context.sample_time_basis, source_locators=item.sampling.source_locators,
                exclusions_ref=diagnostic.id if diagnostic is not None else None,
            )
            record = MetricObservationRecord(id="pending", series=spec.series, descriptor=descriptor,
                value=value, provenance=provenance,
                quality=MetricObservationQuality(issues=tuple(sorted(quality))), summary=_derived_summary(value))
            records.append(record.model_copy(update={"id": _derived_id(record)}))

    return batch.model_copy(update={
        "observations": tuple(sorted((*batch.observations, *records), key=lambda record: record.id)),
        "diagnostics": (*batch.diagnostics, *sorted(diagnostics, key=lambda diagnostic: diagnostic.id)),
    })
