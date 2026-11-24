# P1.6c — Snapshot V2 and Recorded Acquisition Replay

This is the acquisition archive/replay layer over P1.6b's existing runtime
ownership. It does not schedule neighbors, implement retries, or extend benchmark
admission. Historical records remain archival provenance; replay creates current
TaskSpecs, Attempts, partitions, Observations and permissions.

## 1. Changed files

New:

- `src/aegis_rca/models/recorded.py`
- `src/aegis_rca/tools/snapshot_v2.py`
- `src/aegis_rca/tools/recorded_replay.py`
- `tests/test_snapshot_v2.py`
- `tests/test_recorded_replay.py`
- `docs/snapshot-v2.md`

Updated:

- `src/aegis_rca/models/acquisition.py`: explicitly versioned recorded TaskSpec.
- `src/aegis_rca/models/acquisition_result.py`: optional transport lineage and its consistency checks.
- `src/aegis_rca/runtime.py`: carry lineage into the existing single completion envelope.
- `src/aegis_rca/task_materialization.py`: recorded availability/selection before materialization.
- `src/aegis_rca/graph.py`: execution namespace and lineage checks; timeout boundary correction described below.
- `src/aegis_rca/runner.py`: reject replay using the historical execution namespace.
- `src/aegis_rca/cli.py`, `src/aegis_rca/api.py`: authoritative investigation V2 export.
- `src/aegis_rca/evaluation/benchmark.py`: explicit V1-only guard, no scanner/sanitizer change.
- `tests/test_api.py`: expect and validate the additional V2 artifact.

## 2. Archival RecordedAcquisitionV2

`RecordedAcquisitionV2` is a distinct, extra-forbidden archival Pydantic type. Its
explicit fields are `schema_version=2`, AttemptRecord, ToolCallRecord,
`admitted_evidence`, optional TelemetryPartition, declared
`partition_content_digest`, and optional `recorded_from`. It does not subclass or
embed CommittedAcquisition. Export copies an explicit field allowlist; future
runtime-only fields cannot enter the archive through a blanket model dump.

Raw material is stored once per recorded A, inside its partition. Overlapping
Jaeger documents in different acquisitions remain separate historical products.

## 3. Snapshot V2 envelope

`SnapshotV2` contains version 2, original IncidentInput, historical execution ID,
provenance (`live_investigation`, `fixture_investigation`, or
`replay_investigation`), canonical TaskSpec table, archival acquisition rows,
optional historical InvestigationScope, declared replay manifest ID and declared
archive content digest. It contains no report, hypotheses, persisted connectivity
verification, export timestamp, filename, output path, or random snapshot UUID.

## 4. Replay manifest identity

The persisted `RM-*` binds historical execution namespace, incident seed/window
scope, canonical historical T identities, historical A identities, terminal
status, partition presence, replay-content identity, and recorded lineage.
TaskSpec aliases, incident query prose, attempt/call warnings, Evidence excerpts
and Evidence source locators do not enter this identity. Historical scope is an
audit artifact rather than current replay permission.

The replay-content identity hashes raw records, raw coverage/truncation, signal
and historical source metadata. Result-level raw warnings are excluded as display
diagnostics. Raw record fields, including source locators that participate in the
frozen historical source-material digest, remain included. Thus it deliberately
differs from the full partition content digest when only raw warnings change.
Selectors/T-replay bind RM, not the full archival digest.

## 5. Full archive-content digest

The persisted `sha256-*` declaration binds the full canonical typed archival
payload, excluding only its own field. Each row also declares and validates its
full partition content digest. Display changes alter the archive digest without
changing RM/T-replay; replay-relevant raw changes alter both. Canonical identity
uses the existing neutral structural encoder, unchanged by this patch.

These are integrity hashes, not signatures/authentication. Valid resealing of a
deliberately edited archive is distinct from detecting accidental mutation of its
declared content. Load verifies the stored declarations instead of merely exposing
computed properties.

## 6. Export consistency boundary

`export_snapshot_v2(state, dependencies)` validates execution state, exact terminal
Attempt sets, namespace agreement, included T references, reservation/spec
agreement, exact state/store attempt/projection/admitted-Evidence agreement,
duplicates, partition A/T/signal/content, and historical scope freshness/witnesses.
Started uncommitted attempts prevent export. Unstarted reservations may remain in
the TaskSpec table but are never manufactured into terminal archival A rows.

Export does not repair state, call a backend, mutate the store, or rebuild a
missing partition. Projection-only legacy telemetry with no raw partition cannot
be exported as a successful V2 raw acquisition. Custom producers must explicitly
declare provenance rather than being silently labeled as replay.

## 7. TaskSpec versioning

TaskSpec supports versions 1 and 2. Version 1 rejects a non-null recorded selector
and the recorded replay template; its serializer omits the new field entirely,
including nested serialization. Existing V1 wire bytes and T identities are
unchanged. Live, Fixture and aggregate Replay V1 still materialize version 1.

Version 2 requires `query_template_id="replay.select.recorded.v1"` and a selector.
It forbids physical-query overrides. The selector enters T's structural identity.
It is never encoded into PromQL or concatenated into a template name.

## 8. RecordedAcquisitionSelector

The selector contains `replay_manifest_id`, canonical `recorded_attempt_id`, and
integrity reference `recorded_task_spec_id`. Selection verifies all three. The
backend provides `selector_for_attempt(A)` and `selector_for_task(T)`; the latter
requires exactly one replayable A. `RecordedTaskRequest` carries an explicit
selector alongside ordinary logical intent. The materialized request carries it
in TaskSpec v2.

## 9. Discovery and ambiguity

Logical discovery requires exact signal, canonical service set and acquisition
window. Zero replayable matches raises `RecordedAcquisitionUnavailable`; more
than one raises `AmbiguousRecordedAcquisition`. No ordinal/latest/largest-payload
heuristic is used. Different historical query parameters remain different T/A
candidates, including different PromQL. Even multiple A values for the same T
require explicit A selection. There is no narrower-window slicing.

## 10–11. Historical/current task and execution namespaces

T-history remains the historical physical identity. T-replay includes version 2,
the recorded replay template and selected RM/A/T references; it is different from
T-history. A-replay uses a new execution ID and references T-replay. The runner and
admission boundary reject reuse of the archive's historical execution namespace,
including runs that might otherwise return without executing tasks.

Historical AttemptRecords are never merged into current IncidentState. CLI creates
a fresh execution UUID as before; programmatic callers must likewise supply a new
namespace or use `initial_state`'s generated ID.

## 12–13. Lineage and the current partition

The neutral `RecordedSourceReference` adds `recorded_content_digest` to the
selector fields, with no path. RecordedReplayBackend copies selected raw material
and preserves historical source metadata exactly. It returns an ordinary
AcquisitionResult plus this lineage reference.

The existing orchestration lifecycle creates the current A/T partition and commits
it with one CommittedAcquisition. The worker validates lineage against its selected
TaskSpec and raw content before commit; inconsistent lineage becomes
`error / invalid_result` without a partition. Lineage belongs to the completion,
not the raw partition hash. Current and historical partition content digests can
therefore be identical while their A/T references differ. No replay-specific
terminal store or commit lifecycle exists.

## 14. Observation replay identity

Fixture and synthetic Live regressions verify identical historical/current raw
material, source metadata, source-material digest and O-trace IDs under unchanged
producer semantics, with distinct T/A identities. Current resolution still uses
the exact current A partition and the existing historical binding rules.

## 15. Structural integrity versus semantic drift

Load checks schemas, references, stored digests and the frozen P1.5 trace-source
digest recipe. It never runs current observe_spans, legacy projection, Check
evaluation or report policy to declare corruption. Tests replace producers and
verify that a structurally valid archive still loads, while a replay may derive
different O facts. Those differences are semantic drift.

Historical `tool_result_digest` is retained as provenance. The archive contains
only admitted Evidence and ToolCallRecord, not the complete original ToolResult;
the loader cannot reconstruct or independently verify that original digest. The
guarantee is raw acquisition replay fidelity, not byte-identical historical
ToolResult or identical hand-authored fixture E IDs.

## 16. Historical InvestigationScope

The archive retains historical SIS/SB/SX witnesses and validates their historical
references. A new replay state starts without that scope. Current checkout
replay must produce current trace Observations and refresh current permission
before an explicit payment replay is authorized. Historical scope is never copied
into current state by the loader/backend. Expanded payment acquisition remains
irrelevant to checkout VBS/VBE and cannot originate hop 2.

## 17. Failed and empty acquisitions

Historical error, invalid-result, tool-timeout and global-deadline rows remain
archived without partitions. They are unavailable for raw replay and fail before
current T/A reservation or budget consumption. Historical successful zero-record
partitions remain explicit and replayable. Failure is never replayed as fake empty
telemetry or as a new historical-timeout simulation.

## 18–20. V1, benchmark and capture compatibility

TelemetrySnapshot V1, its serializer, ReplayBackend V1 and standalone collect are
unchanged. `read_snapshot` remains V1-only. `load_recording` is the explicit
engineering loader that dispatches V1 versus V2. A V1 investigation may itself
produce a new honest V2 execution archive; this is not a fabricated historical
upgrade of the original V1 capture.

Benchmark quarantine export and admission parsing use an explicit V1-only guard.
V2 engineering archives are rejected. Sanitizer/scanner/review logic is unchanged.
`collect.capture` continues to write aggregate V1 without an execution namespace,
Attempt history or archival acquisition rows.

## 21. CLI/API artifacts and invocation

Every completed CLI/API investigation writes authoritative `snapshot.v2.json`
from IncidentState plus AcquisitionStore. CLI no longer exports investigation
snapshots from Live.snapshot(), and emits no competing `snapshot.json` V1 export.
Existing reports, execution/task/attempt/scope artifacts remain separate. CLI
topology/timeline diagnostics read archived partitions; their transient display
input is not a new authoritative union. API retains its existing local fixture
endpoints and stores the archive alongside other job artifacts.

CLI/API fixture constructors now supply the independent raw log/metric fixtures
already available in P1.6b, consistent with connectivity/missing-log/empty scenarios.
Their existing hand-authored Evidence and default external outcomes stay unchanged;
the V2 replay of all three CLI fixture scenarios is tested.

```powershell
.\.venv\Scripts\python.exe -m aegis_rca.cli --mode fixture --output artifacts/runs/history-example
.\.venv\Scripts\python.exe -m aegis_rca.cli --mode replay --snapshot artifacts/runs/history-example/snapshot.v2.json --output artifacts/runs/replay-example
```

Use new output directories. Programmatic explicit replay, after current scope has
authorized the requested service, can be submitted to the existing compiled
investigation graph:

```python
selector = backend.selector_for_attempt(historical_attempt_id)
task = RecordedTaskRequest(**logical_task.model_dump(), recorded_selection=selector)
state = await investigate_graph().ainvoke({**state, "pending_tasks": [task]}, context=dependencies)
```

There is no automatic neighbor scheduling or CLI ambiguity-resolution policy;
ambiguous archives require explicit programmatic A selection at this stage.

## 22. Deterministic and atomic serialization

TaskSpecs sort by T, acquisition rows by A, admitted Evidence/references by E.
Canonical JSON sorts nested mapping keys and contains no export-time random data.
Write→load→write and file movement preserve canonical bytes and both identities.
The engineering archive limit is 100 MB; V1 retains its existing 20 MB limit.

The writer fully validates and serializes before creating a final file. It writes
a unique same-directory temporary file, flushes/fsyncs it, then atomically creates
the target via an exclusive hard link and removes the temporary name. Existing
targets are never overwritten. Fsync/publication failure leaves no partial target.
Filesystems lacking hard-link publication fail explicitly; there is no non-atomic
fallback or claim of cross-filesystem/crash-proof distributed durability.

## 23–24. Validation

Final targeted result: **287 passed in 30.23s**.

Final full-suite result: **954 passed, 1 skipped in 43.38s**, compared with the
P1.6b baseline of 896 passed, 1 skipped. The two new modules add 58 test cases.
The unchanged skip is `tests/test_sanitization.py:387`: Windows symlink creation
requires OS privileges. Validation results are also saved in
`artifacts/p16c-validation.json`.

Targeted command (use a fresh basetemp):

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_snapshot_v2.py tests/test_recorded_replay.py tests/test_acquisition_partitions.py tests/test_acquisition_identity.py tests/test_acquisition_hardening.py tests/test_investigation_scope.py tests/test_persisted_verification.py tests/test_live_replay.py tests/test_api.py tests/test_output_safety.py tests/test_benchmark_admission.py -q -ra --basetemp artifacts/pytest-p16c-target-review
.\.venv\Scripts\python.exe -m pytest -q -ra --basetemp artifacts/pytest-p16c-full-review
```

## 25. Existing identity and compatibility goldens

The pre-change V1 wire string and T golden are hardcoded in the new regression,
including nested serialization. Existing O-trace/O-metric, T/A, C/CR/P/PR,
VBS/VBE/SX/SIS/SB expectations remain untouched. Existing default Fixture remains
SUPPORTED, 2 rounds, 3 calls, E-T1/E-L1; paymentFailure remains INCONCLUSIVE.
The V1 snapshot/collect/replay and benchmark suites remain compatibility coverage.

A separate fixed-execution comparison verified SP/SIS/SX/SB/VBS/VBE against the
pre-P1.6c values; results are in `artifacts/p16c-golden-results.json`.

Source hashes and V1 task bytes were captured before editing in
`artifacts/p16c-before.json` and `artifacts/p16c-task-v1-before.json`.
`artifacts/p16c-change-audit.json` records changed/new files. No Observation,
normalizer, metric derivation, identity encoder, scope derivation, Check, planner,
report policy or LLM logic was modified.

## 26. P1.7 handoff (not implemented)

Any future planner must handle typed unavailable/ambiguous acquisition failures,
make explicit recorded A choices, and preserve current permission plus historical
availability as separate gates. It must not infer permission from historical SIS
or raw service names, treat T-history as T-replay, or turn admitted neighbors into
automatic retries/multi-hop work without a separate approved design.

## 27. Deferred

P1.7 implementation, retries, benchmark V2 hygiene/admission, expanded metric
templates, narrower replay slices, raw deduplication, automatic scheduling,
multi-hop expansion, generalized root-cause policy, and durable runtime commit
storage remain deferred.

## 28. Scope deviations / compatibility correction

No new stage or ownership redesign was introduced. Full-suite validation exposed
a preexisting P1.6b Windows timing issue: this Python 3.12 runtime's event-loop
monotonic clock has 15.625 ms resolution. Its timer can cancel a backend before a
nominal deadline comparison detects expiry, while a 10 ms immediate-backend test
can be misclassified by that coarse comparison.

The worker now uses an inline asyncio timeout context with an explicit expired
flag and high-resolution perf_counter completion timing. This preserves immediate
backend execution and the existing single terminal commit, rejects suppressed
timeout cancellation, and avoids a coarse-clock false timeout. It is a narrow
compatibility correction discovered by the required full suite, not a new retry
or execution lifecycle.
