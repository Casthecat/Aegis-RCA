# P1.5 TaskSpec / AttemptRecord identity

P1.4-3 now adds current persisted verification on top of this acquisition
baseline. See [persisted-verification.md](persisted-verification.md) for the
current refresh, binding and consumer contracts. The P1.5 validation sections
below remain historical milestone records.

P1.6a adds a separate topology-derived permission scope without changing T/A
identity or issuing neighbor queries. Its neutral exact native resolver is
shared with connectivity persistence; see [investigation-scope.md](investigation-scope.md).

This build establishes acquisition identity and provenance. Task intent,
materialized TaskSpec, Attempt identity and Check identity are separate. It does
not add retries, CheckResult persistence, coverage adapters or Snapshot V2.

## Intent and materialization boundary

`InvestigationTask` remains the planner/policy output: signal, services, window,
goal and compatibility alias. `make_task` and planner code do not read backend
settings or construct T-* identities.

`graph.admit_batch(state, runtime)` invokes `task_materialization.materialize_task`
before deduplication, reservation and dispatch. The resulting frozen TaskSpec
contains the concrete request semantics. Workers receive a centrally reserved
A-* and send a transient `MaterializedTask` to the existing backend.query API.
This request executes canonical services and the UTC window while preserving the
original intent separately in compatibility artifacts.

| Backend/request | Materialized acquisition values |
| --- | --- |
| Fixture | fixture.select.v1 template; exact signal/services/window |
| Replay | replay.select.v1 template; exact signal/services/window |
| Live metric query | prometheus.query_range.v1; configured expression, step and result limit |
| Live trace search | jaeger.search.v3; result limit |
| Live log search | opensearch.search.v1; result limit, actual adapter index, timestamp/filter/sort field, service exact-filter field, server timeout in milliseconds |
| Unconfigured Live metric/knowledge | explicit unconfigured template, with unused query/step/limit omitted |
| Generic query-only backend | tool.query.v1 and the physical intent fields accepted by that protocol |

No operation filter is fabricated: the current Live search does not pass one.
No backend URL, filesystem path, check/hypothesis ID, goal, result or coverage
appears in TaskSpec identity. Transport controls are not query semantics; the
OpenSearch server timeout is included because it is an actual request-body
parameter. The log source decoder's unused fields are not query filters.

Live executes the frozen expression/step/limit even if mutable backend.settings
is replaced after admission. If the log adapter's index/query-field/server-timeout
configuration drifts, it returns an error before issuing a different physical
query under the old T-*. The attempt retains its original TaskSpec and failure.

Direct backend.query compatibility remains available. Live resolves its request
through the same materializer before HTTP work when the caller supplies only an
old InvestigationTask. The standalone `collect.capture` path was inspected: it
remains a raw Snapshot V1 capture command, not an investigation graph execution;
it does not pretend to produce investigation AttemptRecords. Capture-attempt
artifact persistence is not added in this build. Snapshot serialization is
unchanged.

## TaskSpec contract and T-*

TaskSpec (schema version 1) contains:

- signal, sorted unique services and canonical UTC TimeWindow;
- query_template_id;
- applicable query_expression, result_limit, metric_step_seconds, LogQueryScope;
- optional display alias, excluded from identity.

`TaskSpec.id = T-<SHA-256(canonical semantic payload)>`, with the full digest.
Signal/service/window/query/template/step/limit changes change T-* when they are
used acquisition parameters. Alias/goal/LLM wording does not. Defaults are values
in the resolved contract, not references to mutable settings. No retry policy is
part of this identity. There is no dependency on CheckSpec identity.

The canonical hash encoder lives in the neutral `aegis_rca.models.identity`
module, shared by acquisition and verification without an acquisition-to-check
model import. Its algorithm and C/CR/P/PR/T/A identity inputs are unchanged.
TaskSpec and AttemptRecord expose computed read-only
`.id` properties; their ordinary JSON payloads round-trip and recompute the same
IDs. Saved artifact rows also include an explicit ID for convenient inspection.

## Execution namespace and ownership

The outer execution boundary supplies one execution_id to initial_state:

- CLI mints one run UUID, reuses it for the default output directory and passes
  it to initial_state. A custom output directory is not an identity input.
- API reuses its existing job UUID as execution_id; no second investigation UUID
  is minted.
- Python callers may supply execution_id to initial_state; otherwise that state
  factory mints it once for the new investigation.

Runner, admission, workers and backends never mint another execution ID. All
attempts in the state must belong to its namespace. New independent executions
get new IDs even over identical input. Saved IncidentState includes execution_id;
JSON restoration preserves it and existing A-* identities. A completed saved
state can be evaluated with fresh Dependencies without executing retries.

This is not a durable checkpoint scheduler or a new resume command. API restart
handling still marks interrupted jobs as before; it does not silently rerun them.

## Attempt identity and completion contract

`AttemptIdentity` contains execution_id, task_spec_id and a strict positive
ordinal. Its ID is:

```text
A-<SHA-256(canonical({execution_id, task_spec_id, ordinal}))>
```

Ordinal is per T-* within the execution. `admit_batch` is the sole allocation
owner. Current execution permits one attempt per T-* and therefore allocates
ordinal 1 only. The contract can represent ordinal 2 with the same T-* and a
different A-* for future bounded retry policies; no current path allocates a
retry. Result contents, status, timestamps and per-attempt randomness do not
participate in A-*.

`AttemptRecord` is immutable and versioned. It adds original tool_status
(ok/empty/error/timeout), backend kind, worker timeout budget, structured
error_class, truncation, coverage_complete, pre_capture_omissions, admitted O/E
IDs, independent binding states, digests and warnings. It is finalized once after
the query. No intermediate mutable AttemptRecord is overwritten.

Structured error classes distinguish backend_error, invalid_result, tool_timeout
and global_deadline, with none for ordinary success/empty results. Warning prose
is never parsed into status. Backend-returned ToolResult status is retained
verbatim. Locally generated failure results are marked by classification and do
not receive a digest claiming an original backend ToolResult.

The Pydantic AttemptRecord contract enforces the following complete matrix on
construction and persisted JSON restoration, including when error_class defaults
to none. All other combinations are rejected.

| tool_status | Permitted error_class |
| --- | --- |
| ok | none |
| empty | none |
| error | backend_error, invalid_result |
| timeout | tool_timeout, global_deadline |

Workers already classify backend-returned error/timeout as backend_error/tool_timeout
before finalization. Invalid results become error/invalid_result; other backend
exceptions become error/backend_error; worker timeouts become timeout/tool_timeout.
The runner finalizes global deadlines as timeout/global_deadline. No exception to
the matrix or change to ToolResult status vocabulary is needed.

## Digests and admitted fact binding

`tool_result_digest` hashes the validated original ToolResult projection before
scope filtering. It includes that projection's status, Evidence and metadata.
It is **not** a digest of all raw acquisition material. Invalid payloads,
orchestration timeouts and deadline-generated results have no original-result
digest.

`source_material_digest` hashes the scoped native trace input actually passed to
observe_spans: normalized SpanRecords, RPC service map, observation backend name
and synthetic marker. It does not claim to identify an original HTTP response,
all signals, all raw acquisition bytes, or a complete content-addressed store.

Trace completion uses Fixture's independent raw synthetic source, Replay's
snapshot source, or Live's captured source through the existing trace-source
capability. A successful/empty trace ToolResult can bind O-trace IDs independently
of any E-trace projection. E-* IDs are never converted into O-* IDs.

Binding states are explicit:

- observation_binding=bound: the trace source was available and the listed O-*
  facts were derived from it, with its source digest;
- observation_binding=unknown: no native binding was established; currently this
  includes metric/log signals and unavailable/error trace sources;
- evidence_binding=bound: the original ToolResult was valid and its accepted
  E-* projection set is recorded;
- evidence_binding=unknown: no original valid projection was obtained.

These states describe provenance linkage, not acquisition completeness. An empty
ID tuple is not proof of absence. ToolResult.empty can bind native O-trace facts.
Truncated=False never upgrades coverage_complete; pre_capture_omissions stays
unknown. P1.5 acquisition alone makes no fact_set_complete assertion. P1.4-3
now creates explicit CheckCoverageViews only after resolving each attempt binding.

P1.4-3 refresh checks the source digest and exact historical O-ID set before
admitting native facts. Consumers then read the current persisted verification
without backend access. Changed backend source cannot silently rebind a finished
attempt or change the last persisted result. No raw spans are duplicated into state.

## Admission, budgets and state authority

Admission resolves every pending intent, deduplicates by T-* and reserves A-*
centrally before parallel dispatch. Different display aliases or consumers of
one TaskSpec share one acquisition. Distinct T-* values are not deduplicated just
because their display aliases happen to match.

Ordinary calls retain ToolCallRecord.id=query-signal. Each compatibility record
now explicitly includes task_spec_id and attempt_id. In the unusual case of two
distinct T-* values reusing one display alias, the later projection uses A-* as
its collision-free record key while retaining the original intent alias in
ToolCallRecord.task.id. Normal workflow ordering and query-* IDs are unchanged.

An attempt consumes one backend invocation/budget unit; reservations prevent
overbooking before workers start. Workers reject duplicate dispatch of A-*.
Rounds remain batch rounds. No error, timeout, empty, successful or truncated
result schedules a retry. A future policy may retry timeout/error only explicitly
and within max_tool_calls; empty, success and truncated success never imply retry.

IncidentState gains execution_id, task_specs and attempt_records. Completed
AttemptRecords are authoritative; tool_calls are compatibility projections made
by the same finish_attempt helper. Validation checks namespace, TaskSpec scope,
one projection per attempt, status, admitted E IDs, coverage, truncation and
warnings. Divergent copies are rejected rather than resolved by choosing one.

Dependencies.attempts now maps A-* to immutable reservations; completions maps
A-* to worker updates. started_attempts distinguishes reserved work from queries
that actually started. These maps are transient recovery buffers, not another
independently mutable completed-attempt database.

On a global deadline the runner merges completed updates, then finalizes only
started, unfinished attempts as timeout/global_deadline. They retain the same
reserved A-* and T-*. It recovers TaskSpecs and compatibility projections together
and never starts replacement queries. Unstarted reservations are not fabricated
as completed backend calls.

CLI/API preserve existing report/evidence/tool-call artifacts and add execution.json,
task_specs.json and attempts.json. Artifact rows include explicit computed IDs;
for model reconstruction, validate the semantic payload after removing that
redundant id field. Complete IncidentState JSON can also be validated directly
with TypeAdapter(IncidentState). TelemetrySnapshot V1 is unchanged.

## Coverage mapping status

| Future CheckCoverageView input | Authoritative source |
| --- | --- |
| acquisition_id | A-* |
| query_identity | T-* |
| signal/services/window | Materialized TaskSpec |
| query status | Explicit future mapping of original ToolResult status |
| truncation/coverage | Preserved flags, without upgrades |
| pre-capture omissions | Unknown unless separately established |
| admitted IDs | O-* or E-* selected by input kind |
| fact_set_complete | Never inferred from binding state, status or empty IDs |

The table above records the original P1.5 design boundary. The P1.4-3 adapter is
now implemented: ok/empty map to succeeded, error/timeout to failed, and
fact_set_complete requires exact supplied binding equality. See the current
verification document for the executable mapping and its conservative limits.
Only one current verification generation is retained. Historical T/A provenance
remains, but old verification generations are not guaranteed reconstructible
from IncidentState alone because raw Observation source material is not copied
into it. Historical verification persistence is deferred.
P1.6, Snapshot V2, retry execution, metric extensions and native log observations
are not started.

## Changed files and verification

New: models/acquisition.py, task_materialization.py, tests/test_acquisition_identity.py
and this document.

Modified: graph.py, runtime.py, runner.py, state.py, models/evidence.py,
tools/live.py, cli.py, api.py and tests/test_api.py. Existing Fixture/Replay raw
sources, ToolResult, planner/policy, observations/metric derivation, P1.4 verifier,
report/scorer and snapshot/benchmark implementations are unchanged.

The API test now expects the three additive provenance artifacts and verifies
job_id=execution_id and explicit A-* references. Existing fixture report/control
goldens remain unchanged. Identity tests cover canonical scope, actual frozen
Live parameters, different ordinals with equal projection digests, namespaces,
provenance linkage, T-* deduplication, budgets, timeout/deadline recovery,
projection consistency and saved-state namespace preservation.

Validation on 2026-09-10:

- New identity/provenance tests: **39 passed**.
- Targeted identity/workflow/API/Live-Replay/P1.4 suite: **345 passed in 13.05s**.
- Full pytest: **741 passed, 1 skipped in 17.60s**, from baseline 702 passed,
  1 skipped. The unchanged skip is tests/test_sanitization.py:387, Windows native
  symlink creation requiring privileges.
- Command: `.venv\Scripts\python.exe -m pytest -q -ra --basetemp <fresh-project-local-directory>`.
- Before/after SHA-256 comparison of 128 pre-existing source/test/recording/docs
  files: the 9 listed existing files changed; **119 remain byte-identical**.
  There are 4 additions including this document.
- Existing fixture connectivity/missing-log/empty complete report and control
  goldens pass. PaymentFailure remains INCONCLUSIVE, default connectivity remains
  SUPPORTED with two rounds, three calls and E-T1/E-L1 citations.
- Existing O-trace canonical JSON, O-metric/P1.3 full-wire hashes and P1.4-1
  C/CR/P/PR golden identities pass unchanged. Snapshot V1 bytes and benchmark
  sanitizer/admission behavior remain unchanged.

Stopped after P1.5 for review. No P1.4-3, P1.6, Snapshot V2 or retry execution was
started. Standalone capture-attempt persistence remains outside this investigation
execution migration, as described above.

### P1.5 review hardening (2026-09-10)

AttemptRecord now rejects inconsistent status/error pairs at the contract
boundary. The structural encoder was moved verbatim to models/identity.py;
models/check.py, models/acquisition.py, runtime.py and verification/_shared.py
import that neutral helper. Worker/backend/deadline classifications already
matched the matrix and required no runtime behavior changes.

Added tests/test_acquisition_hardening.py and strengthened the existing attempt
execution tests. The 26 new cases cover all 20 status/error combinations,
omitted error_class, JSON validation, fixed pre-migration T/A identities and a
mixed canonical-encoding golden (UTC, microseconds, signed zero, float hex,
Unicode, sorted dictionaries and tuple/list equivalence).

- Targeted: **313 passed in 11.68s**, running test_acquisition_hardening.py,
  test_acquisition_identity.py, test_check_results.py, test_check_reason_contracts.py,
  test_check_legacy.py, test_check_aggregation.py and test_connectivity_wiring.py.
- Full pytest: **767 passed, 1 skipped in 17.69s**. The existing Windows symlink
  privilege skip is unchanged.
- Existing C/CR (native and legacy)/P/PR literal goldens and new pre-migration
  T/A goldens pass without changing any expected existing identity. Observation
  goldens and workflow/control regressions remain green in the full suite.
- Scope audit: 6 existing source/test/document files changed, 2 files added;
  the other 89 pre-existing source/test/document files are byte-identical.

P1.5 valid execution behavior is unchanged. P1.4-3 and all deferred architecture
work remain unimplemented.
