"""Pure facts about frozen returned metrics. No filtering or diagnostic policy."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math

from ..models.incident import TimeWindow
from ..models.metric_observation import (
    CategoryMember, CategoryPresenceValue, MetricDescriptor, MetricDiagnostic,
    MetricFactProvenance, MetricObservationBatch, MetricObservationContext,
    MetricObservationQuality, MetricObservationRecord, MetricQueryProvenance,
    MetricSeriesKey, MetricSeriesSampling, ObservedSampleExtent, WindowSummaryValue,
)
from ..models.telemetry import MetricResult


def _canonical(value):
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat(timespec="microseconds")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite values cannot enter a metric fact identity")
        return {"float64": (0.0 if value == 0 else value).hex()}
    if isinstance(value, dict):
        return {key: _canonical(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    return value


def _digest(payload) -> str:
    content = json.dumps(_canonical(payload), sort_keys=True, ensure_ascii=False,
                         separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _fact_id(record: MetricObservationRecord) -> str:
    """Identity excludes display, physical locators and all batch metadata."""
    payload = record.model_dump(exclude={"id", "summary", "quality"})
    payload["provenance"].pop("source_locators")
    return "O-metric-" + _digest({"identity_version": 1, "fact": payload})


def _extent(stamps):
    return ObservedSampleExtent(first=stamps[0], last=stamps[-1]) if stamps else None


def _median(values):
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    low, high = ordered[middle - 1:middle + 1]
    # Sum before halving subnormals; halve first only when the sum overflows.
    total = low + high
    value = total / 2 if math.isfinite(total) else low / 2 + high / 2
    return 0.0 if value == 0 else value


def _summary_text(value: WindowSummaryValue) -> str:
    return (f"Returned samples: n={value.usable_sample_count}; first={value.first:g}; "
            f"last={value.last:g}; min={value.min:g}; max={value.max:g}; "
            f"sample median={value.sample_median:g}.")


@dataclass(frozen=True)
class PreparedMetricSample:
    timestamp: datetime
    value: float | None
    barriers: tuple[str, ...] = ()


@dataclass(frozen=True)
class PreparedMetricSeries:
    key: MetricSeriesKey
    timeline: tuple[PreparedMetricSample, ...]
    sampling: MetricSeriesSampling

    @property
    def usable(self) -> tuple[tuple[datetime, float], ...]:
        return tuple((sample.timestamp, sample.value) for sample in self.timeline if sample.value is not None)


def _prepare_series(result: MetricResult) -> tuple[PreparedMetricSeries, ...]:
    """Single source normalization; invalid timestamps remain in the timeline.

    Negative finite values stay usable here. Only explicit cumulative-counter
    derivation may treat them as barriers; generic P1.3a facts remain unchanged.
    """
    groups = defaultdict(list)
    for series in result.series:
        key = MetricSeriesKey(name=series.name, labels=tuple(series.labels.items()))
        groups[key].append(series)
    prepared = []
    for key in sorted(groups, key=lambda item: (item.name, item.labels)):
        series_group = groups[key]
        sources = tuple(sorted({series.source for series in series_group}))
        grouped = defaultdict(list)
        for series in series_group:
            for point in series.points:
                stamp = point.timestamp.astimezone(timezone.utc)
                grouped[stamp].append(0.0 if point.value == 0 else point.value)
        timeline, conflicts, nonfinite = [], [], []
        duplicates = 0
        for stamp, values in sorted(grouped.items()):
            tokens = {value.hex() if math.isfinite(value) else
                      "nan" if math.isnan(value) else "inf" if value > 0 else "-inf"
                      for value in values}
            duplicates += len(values) - len(tokens)
            finite_values = {value for value in values if math.isfinite(value)}
            barriers = []
            if len(finite_values) > 1:
                conflicts.append(stamp)
                barriers.append("conflicting_timestamp")
            if any(not math.isfinite(value) for value in values):
                nonfinite.append(stamp)
                barriers.append("nonfinite_timestamp")
            usable = len(tokens) == 1 and len(finite_values) == 1
            timeline.append(PreparedMetricSample(stamp, next(iter(finite_values)) if usable else None,
                                                 tuple(barriers)))
        sampling = MetricSeriesSampling(
            series=key, source_locators=sources,
            returned_sample_count=sum(len(values) for values in grouped.values()),
            unique_timestamp_count=len(grouped), usable_sample_count=sum(point.value is not None for point in timeline),
            duplicate_sample_count=duplicates, returned_sample_extent=_extent(sorted(grouped)),
            conflicting_timestamps=tuple(conflicts), nonfinite_timestamps=tuple(nonfinite),
        )
        prepared.append(PreparedMetricSeries(key, tuple(timeline), sampling))
    return tuple(prepared)


def observe_metrics(
    result: MetricResult,
    *,
    window: TimeWindow,
    context: MetricObservationContext | None = None,
) -> MetricObservationBatch:
    """Summarize ALL supplied samples; window is request provenance, not a filter.

    One result represents one logical query source. Repeated exact series keys
    are combined, with timestamp conflicts retained as unusable data. Category
    presence refers to returned series, including series with no usable points.
    """
    return _observe_prepared(result, window=window, context=context, prepared=_prepare_series(result))


def _observe_prepared(result, *, window, context, prepared) -> MetricObservationBatch:
    context = context or MetricObservationContext()
    groups = {item.key: item for item in prepared}
    descriptors = {binding.series: binding.descriptor for binding in context.descriptors}
    if descriptors.keys() - groups.keys():
        raise ValueError("descriptor binding does not match a returned series key")

    records, sampling, diagnostics = [], [], []
    status_codes = {"failed": "query_failed", "not_configured": "query_not_configured",
                    "unknown": "query_status_unknown"}
    if context.query_status in status_codes:
        diagnostics.append(MetricDiagnostic(code=status_codes[context.query_status]))
    if not groups:
        diagnostics.append(MetricDiagnostic(code="zero_returned_series"))

    for key in sorted(groups, key=lambda item: (item.name, item.labels)):
        item = groups[key]
        sources, used = item.sampling.source_locators, item.usable
        conflicts = item.sampling.conflicting_timestamps
        nonfinite = item.sampling.nonfinite_timestamps
        sampling.append(item.sampling)
        if not used:
            diagnostics.append(MetricDiagnostic(code="no_usable_samples", series=key))
            continue
        descriptor = descriptors.get(key, MetricDescriptor())
        issues = []
        if descriptor.instrument_type == "unknown" or descriptor.value_semantics == "unknown":
            issues.append("unknown_semantics")
        if conflicts:
            issues.append("conflicting_samples")
        if nonfinite:
            issues.append("nonfinite_samples")
        labels = dict(key.labels)
        if "__name__" in labels and labels["__name__"] != key.name:
            issues.append("metric_name_conflict")
        if key.name == "expression" and "__name__" not in labels:
            issues.append("expression_placeholder")
        values = [value for _, value in used]
        value = WindowSummaryValue(
            usable_sample_count=len(used), first=values[0], last=values[-1],
            min=min(values), max=max(values), sample_median=_median(values),
            observed_sample_extent=_extent([stamp for stamp, _ in used]),
        )
        semantic_context = key in descriptors or context.sample_time_basis != "unknown"
        record = MetricObservationRecord(
            id="pending", series=key, descriptor=descriptor, value=value,
            provenance=MetricFactProvenance(
                derivation="returned_sample_summary", input_digest=_digest(used),
                source_locators=sources,
                context_identity=context.contract if semantic_context else None,
                sample_time_basis=context.sample_time_basis,
            ), quality=MetricObservationQuality(issues=tuple(sorted(issues))),
            summary=_summary_text(value),
        )
        records.append(record.model_copy(update={"id": _fact_id(record)}))

    for label in context.category_labels:
        categories = defaultdict(list)
        for key in sorted(groups, key=lambda item: (item.name, item.labels)):
            labels = dict(key.labels)
            if label in labels:
                categories[labels[label]].append(key)
        if not categories:
            diagnostics.append(MetricDiagnostic(code="category_label_absent", label=label))
            continue
        members = tuple(CategoryMember(value=value, series=tuple(categories[value]))
                        for value in sorted(categories))
        value = CategoryPresenceValue(label=label, observed_values=tuple(sorted(categories)), members=members)
        member_keys = {key for member in members for key in member.series}
        sources = tuple(sorted({source for key in member_keys for source in groups[key].sampling.source_locators}))
        record = MetricObservationRecord(
            id="pending", value=value,
            provenance=MetricFactProvenance(
                derivation="returned_category_presence",
                input_digest=_digest([member.model_dump() for member in members]),
                source_locators=sources,
            ), summary=(f"Returned series contain values for label {label}: "
                        + ", ".join(value.observed_values))[:2000],
        )
        records.append(record.model_copy(update={"id": _fact_id(record)}))

    return MetricObservationBatch(
        observations=tuple(sorted(records, key=lambda item: item.id)),
        provenance=MetricQueryProvenance(
            requested_window=TimeWindow(start=window.start.astimezone(timezone.utc),
                                        end=window.end.astimezone(timezone.utc)),
            backend=context.backend, query=context.query, query_status=context.query_status,
            source_locators=tuple(sorted({series.source for series in result.series})),
            truncated=result.truncated, coverage_complete=result.coverage_complete,
            warnings=tuple(sorted(result.warnings)),
        ), context=context, sampling=tuple(sampling), diagnostics=tuple(diagnostics),
    )
