"""Median/MAD window comparisons and conservative counter rate conversion."""

from collections import defaultdict
from datetime import datetime
import math

from aegis_rca.models.incident import TimeWindow
from aegis_rca.models.telemetry import MetricPoint, MetricSeries

from .models import CounterRateResult, MetricComparison


def _points(series: MetricSeries) -> tuple[list[tuple[datetime, float | None]], list[str]]:
    """Collapse identical duplicates; conflicting timestamps remain missing data."""
    grouped: dict[datetime, list[float]] = defaultdict(list)
    for point in series.points:
        grouped[point.timestamp].append(point.value)
    warnings: list[str] = []
    result: list[tuple[datetime, float | None]] = []
    for timestamp, values in sorted(grouped.items()):
        if any(not math.isfinite(value) for value in values):
            warnings.append(f"Non-finite sample at {timestamp.isoformat()} excluded.")
            result.append((timestamp, None))
        elif any(value != values[0] for value in values[1:]):
            warnings.append(f"Conflicting samples at {timestamp.isoformat()} excluded.")
            result.append((timestamp, None))
        else:
            result.append((timestamp, values[0]))
    return result, warnings


def _safe_median(values: list[float]) -> float:
    # statistics.median can overflow when adding the two middle finite values.
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return ordered[middle - 1] / 2 + ordered[middle] / 2


def compare_metric(
    series: MetricSeries,
    baseline: TimeWindow,
    incident: TimeWindow,
    *,
    min_absolute_shift: float,
    min_samples: int = 3,
    z_threshold: float = 3.5,
) -> MetricComparison:
    """Compare [start, end) medians using baseline MAD.

    ``min_absolute_shift`` must be positive and expressed in this metric's units.
    It is a practical effect threshold, not an inferred error/traffic ratio.
    A flat baseline uses this threshold alone and has no statistical score.
    Scores above 100 are reported as capped; they are not probabilities.
    """
    if not math.isfinite(min_absolute_shift) or min_absolute_shift <= 0:
        raise ValueError("min_absolute_shift must be finite and positive")
    if isinstance(min_samples, bool) or not isinstance(min_samples, int) or min_samples < 2:
        raise ValueError("min_samples must be an integer of at least 2")
    if not math.isfinite(z_threshold) or not 0 < z_threshold <= 100:
        raise ValueError("z_threshold must be finite and between 0 and 100")
    if baseline.end > incident.start:
        raise ValueError("baseline must end before or at the incident start")

    points, warnings = _points(series)
    before = [value for time, value in points if value is not None and baseline.start <= time < baseline.end]
    during = [value for time, value in points if value is not None and incident.start <= time < incident.end]
    data = dict(
        name=series.name,
        labels=series.labels,
        source=series.source,
        baseline=baseline,
        incident=incident,
        baseline_count=len(before),
        incident_count=len(during),
        min_absolute_shift=min_absolute_shift,
        baseline_median=_safe_median(before) if before else None,
        incident_median=_safe_median(during) if during else None,
    )
    if len(before) < min_samples or len(during) < min_samples:
        warnings.append(f"Insufficient finite samples: need {min_samples} in each window; anomaly unknown.")
        return MetricComparison(**data, warnings=tuple(warnings))

    center = data["baseline_median"]
    incident_center = data["incident_median"]
    mad = _safe_median([abs(value - center) for value in before])
    shift = incident_center - center
    if not math.isfinite(mad) or not math.isfinite(shift):
        warnings.append("Numeric range exceeded during comparison; anomaly unknown.")
        return MetricComparison(**data, warnings=tuple(warnings))
    absolute_shift = abs(shift)
    direction = "increase" if shift > 0 else "decrease" if shift < 0 else "unchanged"
    if mad == 0:
        warnings.append("Baseline MAD is zero; only the absolute shift threshold is used, with no statistical score.")
        return MetricComparison(
            **data,
            baseline_mad=mad,
            absolute_shift=absolute_shift,
            anomalous=absolute_shift >= min_absolute_shift,
            direction=direction,
            warnings=tuple(warnings),
        )
    # Divide first, avoiding overflow when scaling a very large MAD.
    raw_score = (absolute_shift / mad) / 1.4826
    capped = raw_score > 100
    if capped:
        warnings.append("Robust score capped at 100; score is not a calibrated probability.")
    return MetricComparison(
        **data,
        baseline_mad=mad,
        absolute_shift=absolute_shift,
        robust_score=min(raw_score, 100),
        score_capped=capped,
        anomalous=absolute_shift >= min_absolute_shift and raw_score >= z_threshold,
        direction=direction,
        warnings=tuple(warnings),
    )


def counter_to_rate(series: MetricSeries, *, max_gap_seconds: float | None = None) -> CounterRateResult:
    """Derive per-second rates for finite, nonnegative, adjacent counter samples.

    Reset intervals are omitted: their exact increment cannot be recovered from
    two samples. Non-finite/conflicting/negative samples break continuity rather
    than silently bridging a missing measurement. Rates are stamped at interval
    end. Pass a gap limit appropriate for the scrape interval when needed.
    """
    if max_gap_seconds is not None and (not math.isfinite(max_gap_seconds) or max_gap_seconds <= 0):
        raise ValueError("max_gap_seconds must be finite and positive")
    points, warnings = _points(series)
    output: list[MetricPoint] = []
    resets = 0
    skipped = 0
    for (start, previous), (end, current) in zip(points, points[1:]):
        if previous is None or current is None or previous < 0 or current < 0:
            skipped += 1
            continue
        seconds = (end - start).total_seconds()
        if current < previous:
            resets += 1
            skipped += 1
            continue
        if max_gap_seconds is not None and seconds > max_gap_seconds:
            skipped += 1
            continue
        rate = (current - previous) / seconds
        if not math.isfinite(rate):
            skipped += 1
            continue
        output.append(MetricPoint(timestamp=end, value=rate))
    if resets:
        warnings.append(f"Omitted {resets} counter reset interval(s); exact rates across resets are unknown.")
    if skipped:
        warnings.append(f"Omitted {skipped} invalid, reset, or overlong interval(s).")
    if not output:
        warnings.append("No usable counter intervals; rates are unknown.")
    return CounterRateResult(
        series=MetricSeries(name=f"{series.name}_per_second", labels=series.labels, points=tuple(output), source=series.source),
        resets=resets,
        skipped_intervals=skipped,
        warnings=tuple(warnings),
    )
