"""Frozen metric facts and explicit input context, independent of diagnostics."""

from datetime import timezone
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, FiniteFloat, field_validator, model_validator

from .incident import Contract, TimeWindow


class MetricSeriesKey(Contract):
    """Lossless identity: recorded name plus sorted, exact label pairs."""

    name: str
    labels: tuple[tuple[str, str], ...] = ()

    @field_validator("labels")
    @classmethod
    def canonical_labels(cls, labels):
        if len({key for key, _ in labels}) != len(labels):
            raise ValueError("series label keys must be unique")
        return tuple(sorted(labels))


class MetricDescriptor(Contract):
    instrument_type: Literal["counter", "gauge", "histogram", "summary", "unknown"] = "unknown"
    value_semantics: Literal[
        "cumulative_counter", "gauge_value", "rate", "histogram_bucket", "quantile", "unknown"
    ] = "unknown"
    unit: str | None = Field(default=None, min_length=1)


class MetricContextIdentity(Contract):
    """Caller-declared logical contract identity, not a local file locator."""

    contract_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    version: str = Field(min_length=1)

    @field_validator("contract_id", "source", "version")
    @classmethod
    def nonblank_identity(cls, value):
        if not value.strip():
            raise ValueError("context identity components must not be blank")
        return value


class MetricDescriptorBinding(Contract):
    series: MetricSeriesKey
    descriptor: MetricDescriptor


SampleTimeBasis = Literal["unknown", "query_evaluation", "source_sample"]
QueryStatus = Literal["unknown", "succeeded", "failed", "not_configured"]


class MetricObservationContext(Contract):
    """Only supplied inputs may describe historical semantics; nothing is fetched."""

    schema_version: Literal[1] = 1
    contract: MetricContextIdentity | None = None
    descriptors: tuple[MetricDescriptorBinding, ...] = ()
    category_labels: tuple[str, ...] = ()
    sample_time_basis: SampleTimeBasis = "unknown"
    backend: str | None = None
    query: str | None = None
    query_status: QueryStatus = "unknown"

    @field_validator("descriptors")
    @classmethod
    def canonical_bindings(cls, bindings):
        return tuple(sorted(bindings, key=lambda item: (item.series.name, item.series.labels)))

    @field_validator("category_labels")
    @classmethod
    def canonical_categories(cls, labels):
        if any(not label for label in labels):
            raise ValueError("category label keys must not be empty")
        return tuple(sorted(set(labels)))

    @model_validator(mode="after")
    def explicit_semantics(self):
        if len({binding.series for binding in self.descriptors}) != len(self.descriptors):
            raise ValueError("only one descriptor may bind each exact series key")
        if (self.descriptors or self.sample_time_basis != "unknown") and self.contract is None:
            raise ValueError("descriptors and time semantics require a versioned context contract")
        return self


class ObservedSampleExtent(Contract):
    first: AwareDatetime
    last: AwareDatetime

    @model_validator(mode="after")
    def ordered(self):
        if self.first > self.last:
            raise ValueError("observed extent must be ordered; a single timestamp is valid")
        return self


class WindowSummaryValue(Contract):
    kind: Literal["window_summary"] = "window_summary"
    usable_sample_count: int = Field(ge=1)
    first: FiniteFloat
    last: FiniteFloat
    min: FiniteFloat
    max: FiniteFloat
    sample_median: FiniteFloat
    observed_sample_extent: ObservedSampleExtent


class CategoryMember(Contract):
    value: str
    series: tuple[MetricSeriesKey, ...] = Field(min_length=1)


class CategoryPresenceValue(Contract):
    kind: Literal["category_presence"] = "category_presence"
    scope: Literal["returned_series"] = "returned_series"
    label: str = Field(min_length=1)
    observed_values: tuple[str, ...] = Field(min_length=1)
    members: tuple[CategoryMember, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def consistent_members(self):
        if self.observed_values != tuple(sorted(set(self.observed_values))):
            raise ValueError("observed category values must be unique and sorted")
        if tuple(member.value for member in self.members) != self.observed_values:
            raise ValueError("each observed category must have corresponding members")
        return self


class ClosedSampleInterval(Contract):
    """Two actual sample endpoints, not a half-open query window."""

    start: AwareDatetime
    end: AwareDatetime

    @field_validator("start", "end")
    @classmethod
    def utc(cls, stamp):
        return stamp.astimezone(timezone.utc)

    @model_validator(mode="after")
    def ordered(self):
        if self.start >= self.end:
            raise ValueError("sample interval must have two distinct ordered endpoints")
        return self


SampleScope = Literal["returned_usable", "requested_window", "explicit_window"]


class _CounterDerivationSpec(Contract):
    schema_version: Literal[1] = 1
    series: MetricSeriesKey
    sample_scope: SampleScope = "returned_usable"
    scope_window: TimeWindow | None = None
    max_gap_seconds: FiniteFloat | None = Field(default=None, gt=0)

    @field_validator("max_gap_seconds", mode="before")
    @classmethod
    def numeric_gap(cls, value):
        if isinstance(value, bool):
            raise ValueError("max_gap_seconds must be a positive finite number, not bool")
        return value

    @field_validator("scope_window")
    @classmethod
    def utc_window(cls, window):
        if window is None:
            return None
        return TimeWindow(start=window.start.astimezone(timezone.utc), end=window.end.astimezone(timezone.utc))

    @model_validator(mode="after")
    def explicit_scope(self):
        if (self.sample_scope == "explicit_window") != (self.scope_window is not None):
            raise ValueError("only explicit_window requires and accepts scope_window")
        return self


class CounterDeltaSpec(_CounterDerivationSpec):
    kind: Literal["counter_delta"] = "counter_delta"
    mode: Literal["adjacent_pairs", "single_monotonic_segment"] = "adjacent_pairs"


class IntervalRateSpec(_CounterDerivationSpec):
    kind: Literal["interval_rate"] = "interval_rate"
    mode: Literal["adjacent_pairs"] = "adjacent_pairs"


MetricDerivationSpec = Annotated[CounterDeltaSpec | IntervalRateSpec, Field(discriminator="kind")]


class CounterDeltaValue(Contract):
    kind: Literal["counter_delta"] = "counter_delta"
    interval: ClosedSampleInterval
    start_value: FiniteFloat = Field(ge=0)
    end_value: FiniteFloat = Field(ge=0)
    delta: FiniteFloat = Field(ge=0)
    used_sample_count: int = Field(ge=2)

    @model_validator(mode="after")
    def endpoint_delta(self):
        if self.delta != self.end_value - self.start_value:
            raise ValueError("delta must equal the visible nonnegative endpoint difference")
        return self


class IntervalRateValue(Contract):
    kind: Literal["interval_rate"] = "interval_rate"
    rate_kind: Literal["local_adjacent_endpoint"] = "local_adjacent_endpoint"
    interval: ClosedSampleInterval
    start_value: FiniteFloat = Field(ge=0)
    end_value: FiniteFloat = Field(ge=0)
    delta: FiniteFloat = Field(ge=0)
    elapsed_seconds: FiniteFloat = Field(gt=0)
    rate: FiniteFloat = Field(ge=0)
    used_sample_count: Literal[2] = 2

    @model_validator(mode="after")
    def adjacent_arithmetic(self):
        if (self.delta != self.end_value - self.start_value
                or self.elapsed_seconds != (self.interval.end - self.interval.start).total_seconds()
                or self.rate != self.delta / self.elapsed_seconds):
            raise ValueError("local rate must equal one endpoint delta divided by its elapsed seconds")
        return self


MetricValue = Annotated[
    WindowSummaryValue | CategoryPresenceValue | CounterDeltaValue | IntervalRateValue,
    Field(discriminator="kind"),
]


class MetricFactProvenance(Contract):
    derivation: Literal["returned_sample_summary", "returned_category_presence"]
    derivation_version: Literal[1] = 1
    input_digest: str
    source_locators: tuple[str, ...] = ()
    context_identity: MetricContextIdentity | None = None
    sample_time_basis: SampleTimeBasis = "unknown"


class MetricDerivationExclusion(Contract):
    reason: Literal[
        "conflicting_timestamp", "nonfinite_timestamp", "negative_counter",
        "visible_reset", "max_gap_exceeded", "outside_scope", "numeric_overflow"
    ]
    timestamp: AwareDatetime | None = None
    interval: ClosedSampleInterval | None = None

    @model_validator(mode="after")
    def location(self):
        point_reason = self.reason in {
            "conflicting_timestamp", "nonfinite_timestamp", "negative_counter", "outside_scope"
        }
        if point_reason != (self.timestamp is not None) or point_reason == (self.interval is not None):
            raise ValueError("point exclusions require a timestamp; pair exclusions require an interval")
        return self


class MetricDerivedFactProvenance(Contract):
    derivation: Literal["counter_delta", "local_interval_rate"]
    derivation_version: Literal[1] = 1
    input_digest: str
    used_interval: ClosedSampleInterval
    spec: MetricDerivationSpec
    sample_scope: SampleScope
    scope_window: TimeWindow | None
    gap_policy: Literal["unspecified", "maximum_gap"]
    max_gap_seconds: FiniteFloat | None = Field(default=None, gt=0)
    reset_visibility: Literal["visible_decreases_only"] = "visible_decreases_only"
    context_identity: MetricContextIdentity
    sample_time_basis: SampleTimeBasis
    source_locators: tuple[str, ...] = ()
    # Exclusions are stored once per spec in batch.diagnostics. This audit link
    # is not a derivation input and is excluded from the fact's stable ID.
    exclusions_ref: str | None = None

    @model_validator(mode="after")
    def spec_consistency(self):
        expected_derivation = "counter_delta" if self.spec.kind == "counter_delta" else "local_interval_rate"
        if self.derivation != expected_derivation or self.sample_scope != self.spec.sample_scope:
            raise ValueError("derived provenance must match its explicit spec")
        if (self.sample_scope == "returned_usable") != (self.scope_window is None):
            raise ValueError("window scopes require an effective window; returned scope has none")
        if self.sample_scope == "explicit_window" and self.scope_window != self.spec.scope_window:
            raise ValueError("explicit scope window must match the spec")
        expected_gap = "unspecified" if self.spec.max_gap_seconds is None else "maximum_gap"
        if self.gap_policy != expected_gap or self.max_gap_seconds != self.spec.max_gap_seconds:
            raise ValueError("gap provenance must match the spec")
        if self.spec.mode == "single_monotonic_segment" and self.max_gap_seconds is None:
            raise ValueError("segment facts require an explicit maximum gap")
        return self


MetricProvenance = Annotated[
    MetricFactProvenance | MetricDerivedFactProvenance, Field(discriminator="derivation")
]


class MetricObservationQuality(Contract):
    issues: tuple[Literal[
        "unknown_semantics", "conflicting_samples", "nonfinite_samples",
        "expression_placeholder", "metric_name_conflict", "unknown_time_basis",
        "evaluation_timestamps_not_scrape_timestamps"
    ], ...] = ()


class MetricObservationRecord(Contract):
    id: str
    schema_version: Literal[1] = 1
    signal: Literal["metric"] = "metric"
    series: MetricSeriesKey | None = None
    descriptor: MetricDescriptor = Field(default_factory=MetricDescriptor)
    value: MetricValue
    provenance: MetricProvenance
    quality: MetricObservationQuality = Field(default_factory=MetricObservationQuality)
    summary: str = Field(max_length=2000)

    @model_validator(mode="after")
    def series_scope(self):
        if isinstance(self.value, CategoryPresenceValue) == (self.series is not None):
            raise ValueError("only category presence has no single series key")
        derivation = {
            "window_summary": "returned_sample_summary", "category_presence": "returned_category_presence",
            "counter_delta": "counter_delta", "interval_rate": "local_interval_rate",
        }[self.value.kind]
        if self.provenance.derivation != derivation:
            raise ValueError("value and provenance derivations must agree")
        if isinstance(self.provenance, MetricDerivedFactProvenance):
            if self.series != self.provenance.spec.series or self.value.interval != self.provenance.used_interval:
                raise ValueError("derived series and interval must match provenance")
            if self.descriptor.instrument_type != "counter" or self.descriptor.value_semantics != "cumulative_counter":
                raise ValueError("derived counter facts require cumulative counter semantics")
            if self.provenance.spec.mode == "adjacent_pairs" and self.value.used_sample_count != 2:
                raise ValueError("adjacent pair facts require exactly two used samples")
        return self


class MetricSeriesSampling(Contract):
    series: MetricSeriesKey
    source_locators: tuple[str, ...]
    returned_sample_count: int = Field(ge=0)
    unique_timestamp_count: int = Field(ge=0)
    usable_sample_count: int = Field(ge=0)
    duplicate_sample_count: int = Field(ge=0)
    returned_sample_extent: ObservedSampleExtent | None = None
    conflicting_timestamps: tuple[AwareDatetime, ...] = ()
    nonfinite_timestamps: tuple[AwareDatetime, ...] = ()


class MetricDiagnostic(Contract):
    code: Literal[
        "query_failed", "query_not_configured", "query_status_unknown",
        "zero_returned_series", "no_usable_samples", "category_label_absent"
    ]
    series: MetricSeriesKey | None = None
    label: str | None = None


class MetricDerivationDiagnostic(Contract):
    id: str
    code: Literal[
        "series_not_returned", "missing_counter_authority", "incompatible_descriptor",
        "segment_gap_required", "insufficient_samples", "no_legal_intervals", "intervals_excluded"
    ]
    spec: MetricDerivationSpec
    scope_window: TimeWindow | None
    exclusions: tuple[MetricDerivationExclusion, ...] = ()


class MetricQueryProvenance(Contract):
    requested_window: TimeWindow
    backend: str | None = None
    query: str | None = None
    query_status: QueryStatus = "unknown"
    source_locators: tuple[str, ...] = ()
    truncated: bool = False
    coverage_complete: bool | None = None
    warnings: tuple[str, ...] = ()
    # Old adapters may have removed samples; their original locations are unknown.
    pre_capture_omissions: Literal["unknown"] = "unknown"


class MetricObservationBatch(Contract):
    schema_version: Literal[1] = 1
    observations: tuple[MetricObservationRecord, ...] = ()
    provenance: MetricQueryProvenance
    context: MetricObservationContext
    sampling: tuple[MetricSeriesSampling, ...] = ()
    diagnostics: tuple[MetricDiagnostic | MetricDerivationDiagnostic, ...] = ()
