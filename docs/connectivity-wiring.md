# P1.4-2 connectivity wiring

This document records the P1.4-2 milestone. P1.4-3 supersedes the runtime
re-evaluation and no-coverage wiring described below; see
[persisted-verification.md](persisted-verification.md). Predicate semantics,
catalog identities and external behavior remain unchanged.

The workflow now uses one structured connectivity evaluator for deterministic
hypothesis status, verification control and report eligibility. P1.4-1 contracts,
predicate semantics and structural identity algorithms are unchanged.

## Canonical pure boundary

```python
from aegis_rca.verification.connectivity import evaluate_connectivity

result = evaluate_connectivity(
    incident.window,
    trace_observations=observations,
    legacy_log_evidence=logs,
)
# result.trace_check: CheckResult, native observation origin
# result.log_check: CheckResult, legacy_evidence origin
# result.plan_result: PlanVerificationResult
```

`ConnectivityVerification` is a frozen return contract containing those three
results. The helper builds `connectivity_plan(window)`, evaluates its RpcExists
and LegacyEvidenceExists checks, and calls the unchanged `aggregate_plan` for all
backend modes. It queries nothing, derives no metrics, parses no hypothesis prose
and does not select report citations.

The catalog still requires both checkout CLIENT → resolved/unambiguous payment
gRPC UNAVAILABLE and checkout → payment connection_refused/dns_resolution_failed
legacy log evidence. There is no generic-failure substitution, metric condition,
same-trace constraint, voting or success-as-contradiction behavior.

## Runtime admission and source ownership

`runtime.evaluate_runtime_connectivity(state, backend)` calls the same pure
helper after obtaining facts from an optional read-only `TraceSourceBackend`
capability. Its `trace_source()` returns `TraceObservationSource`: the owned
TraceResult, RPC service map, observation backend name and synthetic marker.

| Backend | Source read by trace_source | Observation context |
| --- | --- | --- |
| Fixture | Independent hand-authored `demo_traces()` or explicitly supplied `traces=` | fixture, synthetic=True, fixture service map |
| Replay | Actual `snapshot.traces` and `snapshot.rpc_service_map` | jaeger, synthetic marker from snapshot provenance |
| Live | Already captured `self.traces` and configured `settings.rpc_service_map` | jaeger, synthetic=False |

All paths pass the actual SpanRecords through existing `observe_spans`. No E-T1
field, body, code or ID is used to reconstruct a span or O-trace record. Existing
E-T1 remains separately available in the default fixture ToolResult for citation
compatibility. Custom fixture Evidence records do not implicitly supply raw
traces: callers pass a TraceResult independently. CLI/API connectivity and
missing-logs scenarios explicitly supply the independent source fixture; the
empty scenario supplies no spans. Existing scenario names, responses and command
options remain unchanged.

Runtime only exposes spans after a corresponding trace task has completed with
ToolCallRecord status `ok` or `empty`. It filters owned spans by the completed
task's actual service/time scope, then the canonical predicate applies the
incident scope. Failed, timed-out or unattempted tasks cannot expose preloaded or
stale raw data. A backend without the optional source capability supplies no
native trace observations and cannot gain support from legacy trace citations.

`empty` still means no diagnostic Evidence projection. It does not mean no raw
span, no RPC, or complete acquisition. A gRPC UNKNOWN raw observation can remain
available when Replay/Live return an empty legacy trace projection. A custom
synthetic fixture can likewise supply a positive raw span without E-T1.

There is no ToolResult-to-CheckCoverageView adapter. No acquisition/query IDs or
completeness are fabricated. Physical task IDs remain `query-<signal>` and only
participate in the existing dispatch/completion accounting. Required positive
facts can SUPPORT; missing positive facts remain UNKNOWN. Nothing is added to
IncidentState, and the backend/raw spans are never stored in state.

## Hypothesis, control and report consumers

`hypothesize` passes the structured result to `form_hypotheses`. The registered
H-connectivity/payment_connection_failure hypothesis keeps its claim text,
missing-check text and current statuses, with support determined only by
PlanVerificationResult. Application/saturation behavior and provisional LLM
proposal handling remain as before. Free-form claims are not executable.

`verify(state, runtime)` preserves the branch order:

1. Supported plan → finalize, even if the final available call supplied support.
2. Exhausted budget → stop_budget.
3. Log unattempted and RPC CheckResult SUPPORT → one targeted query-log follow-up.
4. Otherwise log unattempted → existing replan behavior.
5. Trace unattempted → existing trace follow-up.
6. Metric unattempted → inspect remaining signal.
7. Otherwise → inconclusive.

No retries, rejection behavior, planner redesign or new physical queries are
introduced. Missing E-T1 does not suppress a needed log follow-up when a raw RPC
fact establishes support. Conversely E-T1 cannot cause follow-up/support when
native RpcExists is not established.

`finalize(state, runtime)` evaluates the same helper again and passes its result
to `audit_and_report`. The runner's global-deadline report path also passes this
same evaluation. Report eligibility requires the existing finalize/stop_budget
decision and the supported plan. A direct audit caller without a supplied
structured result receives conservative evaluation without native trace facts;
E-* evidence alone cannot certify RPC existence.

CheckResults are not persisted in this stage. Hypothesize, verify and report can
re-evaluate deterministically. Tests prove terminal verify/report have identical
PR-* IDs with unchanged state/backend inputs and that no telemetry query occurs
between them, including with backend.query and CheckCoverageView construction
blocked during repeated evaluation/audit.

## Citation compatibility and removed duplicate truth

Old `connectivity_ready(Evidence)` is removed. Graph, policy and report no longer
call independent trace/log Evidence boolean matchers for connectivity truth.

`select_evidence_citations` and `connectivity_citations` are explicitly named
artifact selectors. The latter retains old trace-then-refused-then-DNS citation
ordering for reports/hypotheses. Their output does not determine support; tests
can remove all selected citations without changing the semantic plan result.
Ordinary supported fixtures still cite E-T1/E-L1, and Replay/Live retain their
existing E-* projection IDs. ReportClaim.evidence_ids never receives O-trace,
CR-* or PR-* IDs. No report/scorer citation migration is attempted.

## Behavior and identity acceptance

| Scenario | Result |
| --- | --- |
| Default connectivity fixture | SUPPORTED, 2 rounds, 3 calls, same E-T1/E-L1 citations |
| Missing logs | Targeted log follow-up once; ultimately INCONCLUSIVE |
| Wrong peer | INCONCLUSIVE |
| Duplicate trace | Same support verdict, no vote effect |
| Healthy or empty Replay | INCONCLUSIVE; absence unproven |
| UNAVAILABLE plus connection log | SUPPORTED through Fixture/Replay/Live |
| paymentFailure-shaped gRPC UNKNOWN | RpcExists not SUPPORT; INCONCLUSIVE through all three paths |
| 321/6 metrics and zero deltas | Irrelevant to connectivity; no derivation in verification |
| Successful RPC on same edge | No contradiction; matching UNAVAILABLE can still support |

Before changing runtime code, complete report JSON, decision sequences and query
ordering were captured for connectivity, empty and missing-logs fixtures. All
three full-payload SHA-256 goldens pass after wiring, including hypothesis text,
statuses, limitations, rounds, termination reasons and E-* citation identities.

Existing P1.1 O-trace canonical JSON, P1.3a wire hashes, P1.3b-1 derived O-metric
wire hashes and P1.4-1 valid C/CR/P/PR golden tests remain unchanged. Snapshot
serialization and benchmark sanitizer/admission code are unchanged; a wiring
test also checks frozen snapshot bytes remain identical through investigation.

## Files and scope

Modified production files: fixtures.py, runtime.py, graph.py, policy.py, report.py,
runner.py, tools/replay.py, tools/live.py, cli.py and api.py. CLI/API changes only
provide independent fixture spans for their existing scenarios; runner changes
cover its existing deadline report path.

New production file: verification/connectivity.py.

Tests: new test_connectivity_wiring.py; test_workflow.py explicitly supplies raw
fixture spans in custom source scenarios; test_contracts.py passes real native
facts to the replacement structured API. Prior workflow assertions are preserved.
This document is new.

No scope deviation or aggregate-origin exception was needed. Acquisition-bound
coverage awaits a real TaskSpec/AttemptRecord design (P1.5 or a later explicit
adapter). Broader CheckResult/PlanVerificationResult state persistence belongs to
P1.4-3. Native log observations, absence/contradiction predicates, metric extensions,
planner/LLM/scorer redesign and formal benchmark work remain deferred.

## Validation record (2026-09-10)

- New wiring tests: **30 passed**.
- Targeted workflow/contracts/Live-Replay/integration/P1.4 tests: **297 passed in 5.81s**.
- Full pytest: **702 passed, 1 skipped in 11.05s** (baseline 672 passed, 1 skipped).
- The only skip remains tests/test_sanitization.py:387: native Windows symlink
  creation requires OS privileges.
- Full command: `.venv\Scripts\python.exe -m pytest -q -ra --basetemp <fresh-workspace-artifact-directory>`.
- Before/after hashes of 125 pre-existing source/test/recording/document/example
  files show only the 12 explicitly listed modified production/test files changed;
  113 remain byte-identical. The P1.4-1 implementation and tests, all Observation
  and metric derivation code, IncidentState, ToolResult/task contracts, snapshot
  code, benchmark sanitizer/admission and reasoning code are unchanged.

Stopped at P1.4-2 for review; no P1.4-3 work was started.
