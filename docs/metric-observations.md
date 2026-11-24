# P1.3a: facts about returned metric samples

`observe_metrics` is a pure, opt-in API. It describes frozen telemetry, not
anomaly, health, failure mode, hypothesis support, or root cause. It never
consults runtime configuration, files, metadata endpoints, or live systems.

```python
from aegis_rca.models.metric_observation import MetricObservationContext
from aegis_rca.observations import observe_metrics

batch = observe_metrics(
    snapshot.metrics,
    window=snapshot.incident.window,
    context=MetricObservationContext(
        category_labels=("rpc_response_status_code",),
    ),
)
```

The input is one logical query result. The API does not gather or aggregate
independent backends. `ReplayBackend.query()` and its legacy metric Evidence
behavior are unchanged. Facts are not automatically passed to the graph,
policy, verifier, report, or LLM. Trace observations keep their existing API
and IDs. Snapshot v1, sanitized candidate files and admission are unchanged.

## Contracts and unknown semantics

Models follow the existing frozen, extra-forbidden `Contract` convention;
new nested collections use immutable tuples. `MetricObservationBatch` contains
observations, query provenance, explicit context, per-series sampling details,
and diagnostics. `observe_metrics(...)` still emits only `WindowSummaryValue`
and `CategoryPresenceValue`. The shared `MetricObservationRecord.value` union
also supports the opt-in derived P1.3b-1 types `CounterDeltaValue` and
`IntervalRateValue`.

`MetricDescriptor` separates instrumentation type (counter/gauge/histogram/
summary/unknown) from output semantics (cumulative_counter/gauge_value/rate/
histogram_bucket/quantile/unknown), with an optional unit. All are unknown by
default. `_total`, `_count` and `_seconds` never infer a type or unit.

`MetricObservationContext` is versioned input, not a discovery mechanism.
Optional descriptor bindings select exact `MetricSeriesKey` values. Bindings
require a `MetricContextIdentity(contract_id, source, version)`, where source is
a stable logical identifier, not a filesystem locator. Duplicate bindings and
bindings to absent series are rejected. A non-unknown `sample_time_basis` also
requires this identity. No semantic annotations are copied between series.
Caller declarations are preserved as declarations; the API does not verify
their truth against a live system. Even an explicitly declared counter still
gets only a returned-sample summary in P1.3a.

```python
from aegis_rca.models.metric_observation import (
    MetricContextIdentity, MetricDescriptor, MetricDescriptorBinding,
    MetricObservationContext, MetricSeriesKey,
)

item = snapshot.metrics.series[0]
context = MetricObservationContext(
    contract=MetricContextIdentity(
        contract_id="instrumentation-contract", source="reviewed-schema", version="1",
    ),
    descriptors=(MetricDescriptorBinding(
        series=MetricSeriesKey(name=item.name, labels=tuple(item.labels.items())),
        descriptor=MetricDescriptor(instrument_type="counter",
                                    value_semantics="cumulative_counter", unit="observations"),
    ),),
    sample_time_basis="query_evaluation",
)
```

This example requires an actual caller-owned contract. It is not permission to
guess the semantics of an old recording from its metric name.

## Returned samples and normalization

The entire frozen `MetricResult` is the input. `window` records the requested
window in batch provenance; it NEVER filters samples again. Points at either
endpoint or outside that window are retained. Observed extents are separate
from `TimeWindow` and may contain a single timestamp. Returned evaluation
timestamps must not be described as fresh scrape timestamps; the time basis
remains unknown unless supplied explicitly.

- Sort UTC timestamps; normalize negative zero to positive zero.
- Deduplicate identical timestamp/value pairs.
- Combine repeated exact series keys within this single result, and normalize
  their samples together. Different labels always remain separate series.
- Two different finite values at one timestamp make that timestamp conflicted.
- Any non-finite value makes its timestamp unusable, including when a finite
  value also exists there. Both reasons are retained when applicable.
- Record returned count, unique timestamp count, duplicate count, usable count,
  returned extent, conflicted timestamps and non-finite timestamps in sampling
  provenance. Do not turn unusable data into zero.
- Emit no numerical fact for zero usable points. One usable point is valid.

`WindowSummaryValue` records usable count, first, last, min, max, sample median,
and the extent of usable returned points. Median gives each usable timestamp
equal weight; it is not a time-weighted statistic or a baseline. Finite extreme
values are handled without overflowing the median. There is no reset handling,
rate, interpolation, resampling or boundary extrapolation.

## Series identity and fact IDs

`MetricSeriesKey` contains the exact recorded name and lexicographically sorted
label pairs. Case, whitespace, empty label values and all dimensions (including
`__name__`, job, instance, address and version) are retained. Empty and absent
labels remain distinct in this lossless recording identity; it does not claim
to implement every Prometheus selector equivalence. Repeated label keys are
rejected. A conflicting `__name__` is preserved with a quality issue. The
adapter's `expression` name without `__name__` is marked as a placeholder, not
claimed to be an original instrument name.

`O-metric-*` IDs use full SHA-256 with identity version 1, canonical JSON,
UTC microsecond timestamps and finite float64 hexadecimal representations.
IDs bind schema/signal, exact series or category membership, descriptor, typed
value, derivation/version, and the relevant normalized input digest. Summary
input digests include every usable timestamp/value, not just summary statistics.
Exact duplicate points do not change a fact. Excluded invalid points remain in
sampling provenance; they do not change the identity of unchanged usable facts.

Summary IDs also bind the supplied contract identity when a descriptor or
time basis uses it. Unused contracts and descriptors for other series do not
affect that summary. Category presence does not use instrumentation semantics.

IDs exclude display summaries, quality annotations, warning prose, physical
source locators, query status/text, requested window, coverage/truncation,
task/check/hypothesis IDs, candidate UUIDs and unrelated trace/log data.
Changing only the requested window changes the batch, not the returned-sample
fact. Changing actual inputs, relevant semantics or category membership changes
the corresponding fact ID. A fact ID is not a hash of the whole batch or file.

## Query diagnostics and category presence

Batch provenance retains backend/query when supplied, source locators,
requested window, warnings, query status, truncation and tri-state coverage.
`truncated=False` never implies complete coverage. Historical omissions before
freezing may be impossible to locate; `pre_capture_omissions` stays unknown.
Warning prose is preserved, not parsed into historical query status.

Callers with explicit capture information can pass query_status=`succeeded`,
`failed` or `not_configured`; otherwise it is `unknown`. Diagnostics distinguish
these states, zero returned series, and returned series with no usable samples.
Partial returned facts can coexist with a failed/incomplete query. None of these
states proves no traffic, no errors, or health.

Category selection is explicit through `context.category_labels`. Presence
records the sorted values of each selected label and their full member keys.
It refers to returned series, including series with no usable samples. Missing
labels do not invent categories; present empty strings remain empty strings.
There are no category weights, category counts, denominators or percentages.

## The 321 / 6 regression

The self-contained synthetic test uses
`rpc_client_call_duration_seconds_count`, service `checkout`, method
`oteldemo.PaymentService/Charge`, and four returned points for each status:

| Status | Values | First / last / min / max / sample median |
| --- | --- | --- |
| OK | 321, 321, 321, 321 | 321 |
| UNAVAILABLE | 6, 6, 6, 6 | 6 |

Both descriptors remain unknown, and both usable counts are four. OK has
additional address/port labels; the keys stay distinct. Explicit status label
selection adds presence `[OK, UNAVAILABLE]` with member keys. It does not yield
321 successes, 6 failures, 327 requests, a 6/327 error fraction, absence of
traffic, change after injection, or a root cause. Constant returned values do
not establish a complete or continuously observed business interval.

## Deferred to P1.3b

The P1.3a `observe_metrics` API still performs none of these derivations.
Explicit counter delta and local adjacent rate are now available through the
separate [P1.3b-1 derive_metrics API](metric-derivations.md), with barrier and gap
rules. Category distribution, quantiles, baseline/window comparisons, and
PromQL inference remain deferred. CheckResult, policy/verifier redesign, planner identity,
multi-service expansion, report/LLM changes and benchmark execution remain
outside this patch. Context capture/persistence is not added to snapshot v1.
