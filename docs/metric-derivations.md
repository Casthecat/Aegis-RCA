# P1.3b-1: counter delta and local adjacent interval rate

This additive API preserves P1.3a. `observe_metrics` still emits only summaries
and optional category presence, never filters frozen samples, and keeps its
existing IDs and wire representation. Derivations do not enter legacy Evidence,
the graph, verifier, reports or benchmark execution.

```python
from aegis_rca.models.metric_observation import CounterDeltaSpec, IntervalRateSpec
from aegis_rca.observations import derive_metrics

# context must be an explicitly supplied, versioned instrumentation contract.
# series_key must exactly match a returned series and a descriptor binding.
batch = derive_metrics(
    snapshot.metrics,
    window=snapshot.incident.window,
    context=context,
    specs=(
        CounterDeltaSpec(series=series_key, sample_scope="returned_usable"),
        IntervalRateSpec(series=series_key, sample_scope="returned_usable"),
    ),
)
```

Signature: `derive_metrics(result, *, window, context=None, specs=())` returns
`MetricObservationBatch`. With empty specs it is exactly `observe_metrics` for
the same inputs, including serialization. Descriptor/context alone never
requests a derivation. Specs are frozen typed contracts, not PromQL strings.
Duplicate/reordered specs are canonicalized. P1.3a facts remain present with
specs; new facts and typed diagnostics are additive.

## Authority and specs

Each derivation requires an exact `MetricSeriesKey`, an explicit descriptor
binding with `instrument_type=counter` and
`value_semantics=cumulative_counter`, and a versioned `MetricContextIdentity`.
Gauge, unknown, histogram and already-rate outputs are inapplicable. Names and
PromQL text never confer authority. Metadata is not fetched or inferred.

Both specs carry schema version 1, exact series, sample scope, optional scope
window, and an optional finite positive `max_gap_seconds` (boolean is rejected).
CounterDeltaSpec supports `adjacent_pairs` (default) and
`single_monotonic_segment`. IntervalRateSpec only supports `adjacent_pairs`.
Malformed specs raise validation errors. Missing authority, incompatible
descriptors and absent requested series produce derivation diagnostics.
Existing P1.3a validation of unmatched/duplicate context bindings is unchanged.

## One normalization, preserving continuity barriers

Both APIs consume `_prepare_series` in `observations/metrics.py`. Its frozen
`PreparedMetricSeries` holds the canonical key, a complete ordered timeline,
and P1.3a sampling provenance. Each `PreparedMetricSample` retains its UTC
timestamp, a finite value or `None`, and conflict/non-finite barrier reasons.
Duplicate timestamp/value pairs are deduplicated; contradictory timestamps
are not assigned an arbitrary value. Negative finite samples remain valid for
P1.3a summaries and become barriers only in counter derivation.

The derivation walks this timeline, not the filtered usable list. Thus
`valid(t0), invalid(t1), valid(t2)` never creates a t0-to-t2 interval.
Non-finite, conflicting and negative-counter timestamps split continuity.
Visible decreases and excessive gaps split between consecutive eligible points.
A subsequent original adjacent pair may still be valid.

## Sample scope

| Scope | Derivation input |
| --- | --- |
| `returned_usable` | Entire frozen normalized timeline, including barriers; no request-window filtering |
| `requested_window` | Timeline timestamps in the supplied request window `[start,end)` |
| `explicit_window` | Timeline timestamps in the spec's required `scope_window`, `[start,end)` |

Only explicit scope accepts a window on the spec. Filtering is limited to
derivation. Out-of-scope timestamps are audited as exclusions. Actual fact
intervals are `ClosedSampleInterval(start,end)` with two distinct real sample
endpoints; they are never replaced by request-window bounds.

## Reset and gap rules

A decrease between consecutive eligible points, including a decrease to zero,
is a visible reset boundary. No negative delta is emitted, no post-reset value
is added back, and no reset is repaired or crossed. Every derived fact records
`reset_visibility=visible_decreases_only`; hidden resets remain undetectable.

With an explicit maximum gap, `dt > max_gap_seconds` is a barrier; equality is
allowed. No cadence is inferred. With no maximum, adjacent-pair delta/rate are
allowed and provenance says `gap_policy=unspecified`. This does not certify
continuous observation. Segment delta requires an explicit maximum even if a
particular segment happens to contain only two points.

## Arithmetic

- Adjacent delta is `end_value - start_value` for exactly one legal pair.
- Segment delta is the last minus first value of one maximal uninterrupted
  monotonic segment with at least two points. Every pair must satisfy the
  explicit gap limit. Emit one independent fact per such segment; never sum
  disconnected segments. The input digest binds intermediate points too.
- Local rate is `(end_value - start_value) / elapsed_seconds` for exactly two
  legal adjacent points. Its only kind is `local_adjacent_endpoint`; it is never
  a multi-point segment slope. Non-finite arithmetic is excluded, not emitted.

There is no interpolation, extrapolation, PromQL rate/increase behavior, reset
repair, inferred baseline or interpretation as a request count. A zero visible
delta describes unchanged returned endpoint values only. It does not prove
zero traffic, a complete measurement window, health or absence of hidden resets.
The record descriptor describes the input series; a rate is in that series'
declared units per second, or unspecified counter units per second if unit is
unknown. Known query-evaluation timestamps remain usable but carry
`evaluation_timestamps_not_scrape_timestamps`; unknown basis carries
`unknown_time_basis`. Neither note changes the formula.

## Provenance, exclusions and identity

P1.3a `MetricFactProvenance` is unchanged. Derived facts use
`MetricDerivedFactProvenance`, including derivation/version, exact used interval,
normalized used-input digest, typed spec, effective scope/window, gap policy,
maximum gap, reset visibility, context identity, sample time basis and locators.
The record validates agreement between value, interval, spec, descriptor and
provenance. Derived-only fields never appear in P1.3a serialized facts.

Typed exclusions record either a point timestamp or a closed pair interval:
conflict, non-finite, negative counter, visible reset, excessive gap, outside
scope, or numeric overflow. Exclusions are stored once per spec in
`batch.diagnostics`, avoiding quadratic repetition on many derived facts.
`provenance.exclusions_ref` resolves to that diagnostic's `D-metric-*` ID.
Diagnostics also distinguish missing authority, incompatible descriptors,
missing series, missing segment gap policy, insufficient samples and no legal
intervals. When no fact can be produced, the diagnostic still preserves barriers.

Derived IDs remain `O-metric-<sha256>`. They bind kind, exact series, typed value,
actual interval and samples, derivation/version, spec, relevant context and input
descriptor. Existing P1.3a identity version 1 is untouched. Canonical encoding
uses UTC timestamps, finite float64 encodings and normalized zero.

IDs exclude display/quality prose, physical locators, candidate identifiers,
query text/status, warnings, unrelated telemetry and batch completeness.
The requested window enters only for requested scope; the explicit window
enters for explicit scope. Audit references are excluded: adding a remote
barrier does not rename an unchanged legal adjacent pair. If a barrier changes
a segment's actual used points/interval, the fact naturally changes identity.

## The synthetic 321 / 6 case

With explicit synthetic counter authority for both exact series:

| Scope | Per-series intervals | Deltas | Local rates |
| --- | --- | --- | --- |
| returned_usable | +15→+30, +30→+45, +45→+60 | 0, 0, 0 | 0, 0, 0 per second |
| requested_window `[START,START+60s)` | +15→+30, +30→+45 | 0, 0 | 0, 0 per second |

Each pair spans 15 seconds. Requesting both specs for both series yields 12
derived facts in returned scope, or 8 in requested scope, plus unchanged P1.3a
facts. The test context is synthetic authority; it does not restore missing
historical metadata in a real recording. There are no 321/6 request counts,
327 total, 6/327 error fraction, no-traffic claim or complete-window zero claim.

## Compatibility checks and deferred work

Seven full P1.3a JSON hashes were captured before this patch (including IDs):
ordinary payment shape, category presence, explicit context, invalid samples,
duplicate series, empty result and no usable samples. Tests pin those hashes,
exercise counter barriers and scopes, and check unchanged legacy replay/RCA
and snapshot bytes. Existing trace and benchmark hygiene/admission tests remain
part of validation. No replay, adapter, snapshot, policy or benchmark code changed.

Category distribution, quantiles and baseline/window comparison remain deferred.
Future distributions must align exact actual intervals or source fact IDs.
Future quantiles must allow q in [0,1] and distinguish returned estimates from
locally computed statistics. Query coverage and sample extent alone cannot prove
full baseline-window measurement coverage. None of these future features is
implemented here.
