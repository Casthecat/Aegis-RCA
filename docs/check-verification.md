# P1.4-1: deterministic check results

This is a pure library over supplied, admitted facts. An Observation describes
what happened or which value was returned. A CheckResult describes what those
facts establish for one explicit structured check. Neither a check nor plan
aggregation determines root cause.

Nothing here queries telemetry, reads files, derives metrics, invokes an LLM,
updates IncidentState, or changes the running investigation workflow. All existing
Observation, Evidence, snapshot, report and benchmark contracts remain intact.

## Public API and contracts

```python
from aegis_rca.verification import (
    aggregate_plan, connectivity_plan, evaluate_check, evaluate_legacy_check,
)
```

`models/check.py` adds frozen, extra-field-forbidden contracts:

| Contract | Semantic fields |
| --- | --- |
| `CheckScope` | signal, sorted unique services, half-open UTC window, optional logical query identity and acquisition ID |
| `CheckSpec` | schema version 1, semantic version, scope, discriminated predicate, relation, requirement |
| `CheckCoverageView` | acquisition scope/identity, input origin, query status, truncation, completeness, pre-capture omissions, admitted fact IDs, completeness of the admitted set |
| `CheckResult` | structural check ID, verdict, evaluator/input origin, sorted matched/relevant/incompatible IDs, actual coverage used, structured reason |
| `HypothesisVerificationPlan` | schema and semantic versions, failure mode, unique structural checks |
| `PlanVerificationResult` | structural plan ID, SUPPORTED/UNCERTAIN, sorted check-result IDs, structured reason |

Aliases, descriptions, summaries and coverage warnings are display-only. Logical
query/acquisition identities must identify selections/captures, not physical file
locations. They are supplied by the caller; the verifier does not invent them or
reconstruct omitted historical metadata. A scope without query identity can
admit intrinsically scoped positive trace/log facts but cannot be closed.

Only semantic version `"1"` is executed. Unknown check versions return UNKNOWN;
unknown plan/check versions cannot produce a SUPPORTED plan. The contradiction
relation is reserved and returns UNKNOWN in both evaluators.

`CheckResult.reason` is the closed `CheckReason` Literal vocabulary (18 emitted
native/legacy codes); `PlanVerificationResult.reason` uses `PlanReason` (10
aggregate codes). Arbitrary prose is rejected by Pydantic. CheckResult.summary
remains the free-form display field. This contract restriction does not change
the identity payload or digest for existing valid reason codes.

## Structural identity

`CheckSpec.id` is `C-` plus the full SHA-256 of its canonical semantic payload.
Changing a predicate, exact matcher, series label (including empty values), scope,
operator, threshold, interval, semantic version, relation or requirement changes
this ID. Alias/description changes do not. Callers cannot supply an arbitrary
`id` to CheckSpec. Whitelists and service sets are sorted/deduplicated; UTC
timestamps and finite float representations are canonical, including signed zero.

`CheckResult.id` is a `CR-` digest binding the check ID, verdict, sorted fact IDs,
coverage actually used, evaluator/input origin, schema version and reason. Display
summary and warning prose are excluded. No count is converted to confidence.
Facts retain their existing O-* or E-* identities; physical provenance locators
are not copied into results.

`HypothesisVerificationPlan.id` (`P-`) binds versions, failure mode and sorted
structural check IDs. `PlanVerificationResult.id` (`PR-`) binds the plan identity
and aggregation outcome/results. Primitive CheckResults are check-relative, not
hypothesis-relative. The four `id` properties are computed, read-only accessors;
they are not redundant JSON fields. JSON round trips deterministically recompute
them from the frozen payload. Result `check_id` and aggregate `plan_id` are
ordinary serialized references.

Equal semantic input IDs are deduplicated. Conflicting payloads sharing one fact
ID return UNKNOWN instead of choosing a copy by order. Distinct matching IDs are
all preserved. Duplicate check IDs in a plan are rejected. Conflicting results
for one check and conflicting metadata for one acquisition identity are rejected.

## Coverage and admission

`coverage_complete=True` is insufficient by itself. An acquisition can close a
trace/log check only when all of the following hold:

1. Input origin and signal agree.
2. Acquisition services cover the check services and its window covers the check
   window.
3. The check declares a logical query identity and it exactly matches the view.
   If the check also binds an acquisition ID, that must match too.
4. Query status is `succeeded`, coverage is explicitly complete, truncation is
   false, and pre-capture omissions are explicitly `none`.
5. `fact_set_complete=True` explicitly attests a complete admitted set, every
   admitted ID is supplied, and those records agree with the acquisition's
   signal/origin and intrinsic service/time scope.

The admitted ID list binds metadata to actual facts. A successful backend query
does not automatically prove that the verifier received all admitted records.
Missing IDs, unknown historical omissions or unrelated coverage cannot close a
check. An explicitly complete empty admitted set is valid for an empty
acquisition. Existing ToolResult/metric batch flags are not automatically upgraded
into these stronger attestations; historical omission state stays unknown.

Trace and legacy log records have their own service and timestamp. Without a
query/acquisition constraint in the check, those fields admit a positive fact
directly. When either identity is specified, explicit matching-view ID membership
is also required. Acquisition scope may be a superset for these intrinsic facts.

Metric facts have no intrinsic local service. Even a positive metric predicate
requires matching-view ID membership and **exact acquisition/check service
sets**. A multi-service acquisition cannot identify which service produced a
particular metric fact. Numeric facts additionally require their entire observed
extent or closed sample interval to fit inside the half-open check window:
`check.start <= first <= last < check.end`. A sample at the check end is outside.
The acquisition window may cover a larger window; no clipping or recomputation
occurs. Category facts lack per-member service/time locations, so their acquisition
window must equal the check window as well.

An observed positive remains SUPPORT under truncated, incomplete, unknown, or
failed broader acquisition status when its fact admission is established. These
flags govern completeness, not whether a supplied observed fact existed.

## Implemented predicates and verdicts

| Predicate | Positive/evaluable behavior | Insufficiency |
| --- | --- | --- |
| `RpcExists` | Exact local service, optional CLIENT/SERVER, exact peer, outcome/status whitelists, OTLP status, operation and RPC system; positive SUPPORT; complete no-match NEUTRAL | Open no-match or potentially matching unresolved fields UNKNOWN |
| `MetricSummaryExists` | Exact lossless `MetricSeriesKey` and existing `WindowSummaryValue` produce SUPPORT, including unknown instrument semantics | Missing admitted usable summary UNKNOWN, even if query flags alone say complete |
| `CounterDeltaCompare` | Existing `CounterDeltaValue.delta`, exact comparator true SUPPORT / false NEUTRAL | No uniquely selected appropriate fact UNKNOWN |
| `IntervalRateCompare` | Existing `IntervalRateValue.rate`, exact comparator true SUPPORT / false NEUTRAL | No uniquely selected appropriate fact UNKNOWN |
| `CategoryPresent` | Explicit label/value membership in an admitted returned-series fact; present SUPPORT, absent from one returned-set fact NEUTRAL | No fact or ambiguous multiple nonmatching returned sets UNKNOWN |
| `LegacyEvidenceExists` | Separate compatibility API, explicit allowed structured log code and exact service/peer | Open no-match or unresolved potentially matching peer UNKNOWN |

Native evaluation accepts an explicitly typed tuple of trace and/or metric
ObservationRecords. It rejects Evidence and legacy predicates. No regex, raw
samples, arbitrary expressions, free-form hypothesis text or derived diagnostic
labels are executable input.

RPC exact-peer matching always requires a resolved, unambiguous peer. A missing
RPC status is unresolved. Explicit gRPC `UNKNOWN` is a known status distinct from
`UNAVAILABLE`. If multiple explicit status sources disagree about membership in
the requested whitelist, the matcher remains unresolved rather than trusting
field order. An independently known mismatch makes that record nonmatching; any
other fully matching record still establishes positive existence.

Normalized outcome `unknown` and `inconsistent` are epistemically unresolved
when a concrete success/failure outcome is required. Likewise, normalized OTLP
`UNKNOWN` cannot disprove concrete OK/ERROR/UNSET. Complete acquisition does not
resolve either field: these cases stay UNKNOWN unless another required matcher
already proves the record irrelevant. Explicitly requesting the actual unresolved
outcome or OTLP UNKNOWN may SUPPORT. Concrete gRPC UNKNOWN remains protocol code
2 and is still a definite nonmatch to UNAVAILABLE.

For the paymentFailure-shaped checkout → payment CLIENT fact with OTLP ERROR,
gRPC UNKNOWN and outcome failure:

- UNAVAILABLE matcher: UNKNOWN with open acquisition; NEUTRAL with exact complete
  acquisition; never SUPPORT.
- Explicit generic outcome=failure matcher: SUPPORT.
- The connectivity catalog uses UNAVAILABLE, never generic failure.

Comparators are exactly `eq`, `ne`, `lt`, `lte`, `gt`, `gte`, with a supplied finite
numeric threshold. No tolerance, baseline, z score or dynamic threshold exists.
Both numeric predicates require an exact series. An optional ClosedSampleInterval
filters to exact endpoints. More than one remaining distinct candidate is UNKNOWN,
including two facts for the same interval with different contexts/specs. Equal
duplicate IDs do not create ambiguity. No first/latest/min/max selection occurs.
The verifier reads only the typed delta/rate; it does not calculate one from
endpoints. Zero delta with `gt 0` is NEUTRAL and establishes neither health nor
absence of traffic/failure.

NEUTRAL is an evaluable false positive matcher, not hypothesis contradiction.
UNKNOWN is insufficiency. Requirement (`required`/`optional`) never changes the
predicate's truth/evaluability. No shipped predicate emits CONTRADICT; it is
reserved for future explicitly incompatible predicates. Category NEUTRAL describes
only membership in the returned set, never global category absence.

## Legacy boundary and connectivity plan

`evaluate_legacy_check(spec, *, evidence=(), coverage=())` only accepts
`LegacyEvidenceExists`. The allowed code set is restricted by the contract to
`connection_refused` and `dns_resolution_failed`. It requires log signal and exact
service/peer/time; it never searches excerpt text or source paths. Results carry
`input_kind="legacy_evidence"`, `evaluator="legacy_compatibility_v1"` and original
E-* IDs. No fake Observation or E-to-O conversion exists.

```python
plan = connectivity_plan(window)
# supplied_trace_facts and supplied_log_evidence are caller-owned typed tuples.
results = tuple(
    evaluate_check(check, observations=supplied_trace_facts)
    if check.scope.signal == "trace"
    else evaluate_legacy_check(check, evidence=supplied_log_evidence)
    for check in plan.checks
)
verification = aggregate_plan(plan, results=results)
```

The catalog key is H-connectivity and failure mode is payment_connection_failure.
It contains exactly two required support checks:

1. checkout CLIENT → resolved/unambiguous payment, RPC UNAVAILABLE.
2. checkout → payment legacy log with connection_refused or dns_resolution_failed.

There is no metric requirement, same-trace/group requirement, independence
assumption, evidence-count voting, or success-as-contradiction condition.

Aggregation only supports the registered failure mode payment_connection_failure
and version 1. A structured plan needs at least one required support check; all
required support results must be SUPPORT, and no explicit CONTRADICT result or
reserved contradiction-relation check may exist. Results must belong to the
plan's structural check identities and correct native/legacy origin; a SUPPORT
result must reference matched facts. Optional non-support results do not alter
required predicate truth. Any explicit contradiction blocks support.

No plan, empty/optional-only plan, missing required result, unsupported version,
unknown/custom/LLM-only mode, application mode or saturation mode stays UNCERTAIN.
There is no REJECTED status. Supplying a failure-mode name or display claim does
not execute that claim; aggregation uses only the supplied structured plan.

## Validation and intentionally deferred work

Synthetic regression tests live in test_check_results.py, test_check_legacy.py
and test_check_aggregation.py. They cover scope binding and incorrect claimed ID
admission, partial positives, exact RPC fields and conflicting status sources,
numeric selection and exact comparisons, returned-category scope, deterministic
identity/order/deduplication, the compatibility boundary, nonvacuous aggregation,
and actual catalog evaluation with different correlation groups. IO and metric
producer calls are blocked while evaluating prepared facts in the purity test.

Existing trace canonical fixtures, P1.3a complete-wire hashes, workflow/report,
live/replay and benchmark suites remain unchanged. Additional P1.3b-1 derived
wire hashes cover existing delta/rate IDs and serialization.

This stage intentionally defers graph/state/report integration, absence and
correlated incompatibility predicates, generalized contradiction/rejection,
native log observations, distribution/quantile/baseline facts, planner/LLM/scorer
changes and a formal benchmark runner. There is no P1.4-2 wiring in this build.

## Build validation record (2026-09-10)

- New tests: **157 passed** across native results (82), legacy bridge (42), and
  aggregation/catalog (33), all included in the final full run.
- Full command: `.venv\Scripts\python.exe -m pytest -q -ra --basetemp <fresh-workspace-artifact-directory>`.
- Full result: **611 passed, 1 skipped in 6.93s**. The unchanged skip is
  `tests/test_sanitization.py:387`: native symlink creation requires OS privileges.
- Before/after SHA-256 comparison of 112 pre-existing Python/JSON/Markdown files
  under src, tests, data/recordings, docs and examples: **zero changes**. All 11
  files in this build are additions. Snapshot sources and existing tests were
  not rewritten.
- Existing workflow/live/replay tests and canonical P1.1/P1.3a golden tests pass
  unchanged. New P1.3b-1 full-wire hashes include original derived IDs. Existing
  sanitizer/admission suites pass with only the same native symlink skip.
- Independent final source review found no remaining must-fix findings.

New files: models/check.py; verification/__init__.py, _shared.py, evaluate.py,
legacy_bridge.py, aggregate.py, catalog.py; tests/test_check_results.py,
tests/test_check_legacy.py, tests/test_check_aggregation.py; this document.

There is no scope expansion. Conservative admission details are explicit:
fact-ID/full-admitted-set attestations; exact metric service sets and exact
category acquisition windows; UNKNOWN for conflicting RPC-status membership and
unsupported semantic versions. The additional private helper and split tests
keep the native and legacy boundaries reviewable without changing existing code.

## P1.4-1 review corrections (2026-09-10)

The review patch keeps epistemically unknown/inconsistent RPC outcomes and OTLP
UNKNOWN unresolved for concrete matchers, including complete acquisitions. An
independent known mismatch still wins; gRPC UNKNOWN remains concrete code 2.
Both result reason fields now use closed Literal vocabularies without changing
their existing valid identity payloads or human-readable CheckResult.summary.

- Focused P1.4 tests: **218 passed in 0.62s**.
- Full pytest: **672 passed, 1 skipped in 6.59s** (61 more passing tests).
- The skip remains native symlink privileges at tests/test_sanitization.py:387.
- Every emitted native/legacy and aggregate reason is covered by actual-output
  JSON round-trip tests. Arbitrary reason prose is rejected.
- Pre-hardening C/CR/P/PR golden IDs for valid results remain unchanged.
- Existing P1.1 O-trace canonical JSON and P1.3a/P1.3b-1 O-metric full-wire
  golden tests pass unchanged. No Observation or metric derivation file changed.
- Before/after hashes of 123 pre-patch files identify only the requested check
  contracts/evaluation/type annotations, focused tests and this document as
  changed; graph/state/policy/report/benchmark/planner/LLM remain untouched.
- Full copies of the four files requested for external review are supplied in
  [p14-1-review-fixes.md](p14-1-review-fixes.md).

No P1.4-2 integration or contradiction/absence predicate was added.
