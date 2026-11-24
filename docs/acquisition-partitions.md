# P1.6b — Multi-Service Acquisition + Per-Attempt Telemetry Partitions

P1.6b permits explicitly requested, scope-authorized trace/log acquisitions for a
neighbor. The existing planner still schedules only the original seed. Raw
material belongs to an orchestration completion, independently of mutable backend
capture buffers. This document is the implementation review and P1.6c handoff.

## 1. Files changed

New implementation files:

- `src/aegis_rca/models/acquisition_result.py`
- `src/aegis_rca/acquisition_store.py`
- `src/aegis_rca/tools/projection.py`

Updated implementation files:

- `src/aegis_rca/graph.py`: preflight authorization/capability, acquisition worker,
  coherence validation, terminal commit.
- `src/aegis_rca/runtime.py`: owned store, completion finalization, partition-backed refresh.
- `src/aegis_rca/runner.py`: recover terminal completions from the same store.
- `src/aegis_rca/task_materialization.py`: permission and capability gates.
- `src/aegis_rca/trace_acquisition.py`: exact A-partition resolution; no buffer fallback.
- `src/aegis_rca/tools/live.py`: local material projection, expanded trace/log, V1 export guard.
- `src/aegis_rca/tools/replay.py`: V1 acquisition envelope with unchanged query projection.
- `src/aegis_rca/fixtures.py`: independent raw fixture products and explicit injected materials.

Tests/documentation:

- New `tests/test_acquisition_partitions.py` and this document.
- Updated `tests/test_acquisition_identity.py`, `tests/test_connectivity_wiring.py`,
  `tests/test_persisted_verification.py`, `tests/test_investigation_scope.py`.
  Existing tests that simulated missing source by mutating backend buffers now
  simulate missing/corrupt partitions. Synthetic scope test setup commits an actual
  acquisition. Existing identity expectations were not rewritten.

## 2. AcquisitionResult contract

`AcquisitionResult` is a frozen, extra-forbidden Pydantic contract containing
`signal`, legacy `projection: ToolResult`, typed optional `material`, optional
`source: SourceMetadata`, and `projection_origin`.

Material types are selected by signal, including JSON round trips of empty
results: trace → `TraceResult`, log → `LogResult`, metric → `MetricResult`.
Source metadata captures the RPC service map, source backend name, and synthetic
flag. The backend receives `MaterializedTask`; it receives no A-ID, check,
hypothesis, or scope-expansion identity.

## 3. Projection/material coherence

- Successful material-aware telemetry requires matching typed raw material and
  source metadata, even when the raw result has zero records.
- Error/timeout cannot carry material or source metadata.
- Normalized projections preserve the material's truncation and coverage fields.
  `ok` means nonempty diagnostic Evidence; `empty` means empty diagnostic Evidence.
  Neither implies the number of raw records.
- The worker independently recomputes a normalized projection using this request,
  material, and metadata. Any discrepancy becomes `error / invalid_result` with
  no partition. Returned models are deeply revalidated before binding.
- Default Fixture products retain their preexisting, hand-authored synthetic
  Evidence and quality conventions through `projection_origin="fixture"`.
  Their raw fixtures are supplied independently, never reconstructed from
  Evidence. Injected materials use the ordinary normalized projection contract.
  The worker permits fixture origin only for FixtureBackend implementations.
- Existing query-only custom backends retain an explicit `legacy` compatibility
  path: ToolResult-only completion, no raw partition, no native binding. This is
  not treated as an empty acquisition. A backend implementing `acquire` cannot
  select legacy origin to evade the raw-material contract.
- Knowledge retrieval remains projection-only and unconfigured.

`ToolResult` and all existing public state/identity schemas remain unchanged.

## 4. Orchestration-owned partition store

`Dependencies.acquisitions` owns an `AcquisitionStore`. Its only authoritative
mapping is A-ID → serialized `CommittedAcquisition`. The backend returns material;
only orchestration creates an A-ID and commits it. IncidentState contains no raw
partitions or backend objects.

The store serializes at commit and deserializes fresh reader copies. Although
existing raw models contain mutable nested dictionaries, mutating the backend's
input, a returned partition, or a compatibility completion view cannot mutate
committed bytes. This is an in-memory boundary, not durable storage.

## 5. Exact terminal commit mechanism

`CommittedAcquisition` holds the finalized AttemptRecord, ToolCallRecord,
admitted Evidence, and optional partition. Its validator requires matching A/T,
signal, status, coverage, warnings and evidence references. A native bound
completion requires its raw partition; failures cannot have one.

`AcquisitionStore.commit` validates/serializes the whole envelope, takes a lock,
rejects an existing A-ID with `AlreadyCommitted`, and performs exactly one map
assignment. There is no `put(partition)` API and no independently mutable
completion dictionary. `Dependencies.completions` is a derived compatibility view.

## 6. Why partition/completion cannot race

Before the one assignment, neither the partition nor terminal completion is
published. After it, both are present in one envelope. Validation failure publishes
neither. The lock covers duplicate detection and publication. A timeout and success
competing for the same A-ID cannot both commit.

Worker finalization and commit contain no `await`. Deadline recovery uses the same
store and also has no await between selecting unfinished attempts and committing
their terminal outcomes. Backend threads receive no commit handle.

Regressions count the authoritative map writes, race success against timeout from
two threads, check nested mutation isolation, and reject a late successful commit
after timeout. The compiled graph also exercises two explicit payment workers and
collision-free compatibility call IDs.

## 7. TelemetryPartition and content digest

The frozen runtime partition contains `attempt_id`, `task_spec_id`, `signal`, typed
raw `material`, and `source`. It does not duplicate AttemptRecord status/coverage
authority. Raw result quality fields remain part of the returned raw value.

`content_digest` applies the unchanged neutral `structural_id("sha256-", payload)`
encoder to signal, material, and source metadata, excluding A-ID/T-ID. Mapping
order is canonical; raw sequence order is retained. Content equality and execution
identity remain separate. No TP-ID or disk schema is introduced. The digest is a
computed property rather than a second writable field.

## 8. Exact attempt-specific trace resolution

Resolution obtains exactly `partition_for(attempt.id)`, verifies its A/T/signal,
filters its spans using TaskSpec services/window, recomputes the preexisting
source-material digest, then requires exact equality of produced and historical
admitted O-ID sets. Missing partition/reader, mismatched digest, or differing O-ID
set supplies no facts. There is no global source fallback.

Historical attempts are never rewritten. A later payment acquisition, overlapping
Jaeger document, or mutated Live trace buffer cannot rebind checkout's attempt.
The old source helper remains available for initial binding and direct source
inspection, not as a fallback in historical resolution.

## 9. Live projection path

`LiveBackend.acquire` fetches into a local typed value M, calls the pure
`project_material(task, M, source)`, and returns both products. Compatibility
buffers are updated only after these products have been formed. The worker binds
from the returned M. Neither projection nor binding reads `Live.snapshot()`.

Trace and log normalization algorithms are unchanged. The existing log normalizer
does not use its spans argument, so the pure helper supplies an empty tuple rather
than accessing mutable trace buffers. Metrics retain the existing empty diagnostic
projection while preserving their raw series.

## 10. Expanded authorization

Every pending task passes authorization before materialization/reservation.
`current_investigation_scope` verifies freshness. If scope exists, the task's
services must be contained in its admitted services. If absent, only original
incident seeds are allowed. Requested time windows must remain within the incident.
Unauthorized requests raise `UnauthorizedAcquisition`; stale scope raises loudly.

Raw service names, catalog entries, hypotheses, and LLM text confer no permission.
No new service discovery query or automatic scheduling was added.

## 11. Capability rejection

After permission checking, the whole batch is capability-checked before creating
any TaskSpec or reservation. Unsupported requests raise typed
`UnsupportedAcquisition`, are not silently dropped, create no T/A or
`not_executed` records, and consume no tool-call budget.

Live requires exactly one canonical service per physical request. Authorized
trace/log neighbors are supported. Replay V1 supports only its captured seed
scope/window. Expanded fixture trace/log requires an explicitly injected material
entry. Materialization and built-in acquisition entry points also apply capability
checks; authorization remains the orchestration responsibility.

## 12. Metric safety

Live's configured fixed PromQL is valid only for its existing seed semantics.
An authorized payment metric request still fails capability before T/A creation.
There is no expression replacement, relabeling, or expanded metric template.
Seed metrics receive an ordinary per-attempt raw partition.

## 13. Empty partition versus absent partition

Successful zero-record trace/log/metric acquisition commits an explicit empty typed
partition. Real spans may coexist with an `empty` Evidence projection and a valid
native binding. Backend errors, invalid acquisition products, tool timeouts, and
unfinished acquisitions at global deadline commit no partition. Recovery never
manufactures empty telemetry. Legacy projection-only compatibility is explicitly
unbound, as described in section 3.

## 14. Deadline and late completion

The worker retains bounded `asyncio.wait_for` acquisition and also checks elapsed
event-loop time after return. If custom code suppresses cancellation and returns
after the tool deadline, the result is discarded as `timeout / tool_timeout`.

Global deadline recovery merges already committed envelopes, finalizes only
started uncommitted reservations as `timeout / global_deadline`, merges their
TaskSpecs, and refreshes verification/scope from the owned partition reader before
reporting. Previously committed partitions survive. Late `to_thread` work can
finish physically but cannot publish a partition or overwrite its terminal attempt.
No retries or durable exactly-once execution claim is introduced.

## 15. Explicit Fixture neighbor acquisition

FixtureBackend accepts `materials={(signal, canonical_services): typed_result}`.
For example, an injected `("trace", ("payment",))` entry and corresponding log
entry enable explicitly authorized payment requests. A payment span inside the
checkout document alone is insufficient to enable this fixture capability.

The integration regression demonstrates the normal explicit workflow:

```python
deps = Dependencies(FixtureBackend(materials={
    ("trace", ("payment",)): payment_traces,
    ("log", ("payment",)): payment_logs,
}))
state, _ = await run_investigation(initial_state(demo_incident()), deps)
# The normal run has admitted payment but has queried checkout only.
tasks = [make_task(state["incident"], signal).model_copy(
    update={"services": ("payment",)}) for signal in ("trace", "log")]
state = await investigate_graph().ainvoke(
    {**state, "pending_tasks": tasks}, context=deps)
```

Use the same Dependencies while the runtime is alive. Default budget accommodates
this explicit third round: five total calls, distinct ordinal-1 A-IDs and T-IDs,
and separate raw partitions. This does not automatically regenerate a report or
change planner policy. No partition persistence/reload format is provided here.

## 16. Replay V1 compatibility

Replay query semantics and top-level TelemetrySnapshot remain unchanged. Its
acquisition wrapper packages the selected V1 material with its projection and
source metadata. Replay's transport T-ID is fresh execution provenance, not a
historical Live acquisition identity. Expanded payment requests fail even when a
payment span appears in a recorded checkout trace. No V2 selector is implemented.

## 17. Live V1 snapshot rule

With an investigation partition reader attached, `Live.snapshot()` raises if
there is more than one committed raw partition of any signal, or if a committed
partition's request services differ from the configured seed tuple. Thus V1 cannot
silently export the last of multiple acquisitions or mislabel a neighbor query.

Normal single-query-per-signal seed behavior keeps its existing V1 capture buffers
and serialization. Standalone `collect.capture` retains the existing seed-only
`query` path and V1 format. Capture/export was not turned into Snapshot V2.

## 18. External behavior compatibility

Default Fixture remains SUPPORTED, two rounds, three calls, E-T1/E-L1. Its automatic
tasks all remain checkout-side. paymentFailure remains INCONCLUSIVE while payment
may be scope-admitted. Existing missing-log, wrong-peer, healthy replay,
sanitization/admission and bounded artifact tests remain in the full suite.

Explicit payment attempts resolve independently but do not change checkout
connectivity VBS/VBE or scope SIS/SB. Payment-to-accounting cannot become a second
hop origin. The whole store is not hashed into verification or scope identities.

## 19. Targeted validation

Run with a fresh project-local `--basetemp`:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_acquisition_partitions.py tests/test_acquisition_identity.py tests/test_acquisition_hardening.py tests/test_investigation_scope.py tests/test_persisted_verification.py tests/test_connectivity_wiring.py tests/test_live_replay.py tests/test_workflow.py -q -ra --basetemp artifacts/pytest-p16b-target-review
```

Final result: **251 passed in 27.94s**. The new partition module contributes
36 focused cases, including the compiled explicit-payment graph integration.

## 20. Full pytest

```powershell
.\.venv\Scripts\python.exe -m pytest -q -ra --basetemp artifacts/pytest-p16b-full-review
```

Final result: **896 passed, 1 skipped in 35.63s**, up from the P1.6a baseline of
860 passed, 1 skipped. The existing skip is
`tests/test_sanitization.py:387`, Windows symlink creation requiring OS privileges.

## 21. Identity and golden regressions

Existing identity algorithms, models, and golden expectations are unchanged:
O-trace/O-metric, T/A, C/CR/P/PR, VBS/VBE, SX/SIS/SB. For fixed execution
`p16a-golden`, the new regression additionally pins:

- SIS: `SIS-f3310713d2679b587989121d5c558cdcad6beb6a9cc7037b193e54f7404483b2`
- SB: `SB-5b3664c9665c1f70ef798895765f61b6283a72b401064fd04f4b4622c291b7c5`
- VBS: `VBS-d5be4bdb988d494af12ae2f343a631aede3f25bb863ad0cea96b17598a4fd143`
- VBE: `VBE-b01c93039a4ad22ae42e9d6aaa35ed83481107af290d21fc0e30e59bfe7b3de4`

Pre-change source hashes and fixed runtime IDs were saved under
`artifacts/p16b-before.json` and `artifacts/p16b-goldens-before.json`.
`artifacts/p16b-change-audit.json` lists changed/new files against that baseline.
Snapshot schema/serializer, collect, normalization, observations, identity models,
verification models/evaluators, benchmark, policy, report and LLM files were not
modified. V1 round-trip and benchmark tests remain authoritative compatibility tests.

## 22. P1.6c handoff — documentation only

A future `RecordedAcquisitionSelector` must select an explicitly recorded
historical TaskSpec/acquisition partition. Historical Live T-ID describes its
physical acquisition. A new Replay transport TaskSpec is a different identity and
must not masquerade as the historical Live T-ID. Selection must preserve the
historical partition content and source metadata needed for exact binding.

Any future recording format must preserve distinct acquisitions even when both
contain the same complete Jaeger trace document. This patch supplies runtime
ownership only; it implements no selector or recorded-partition format.

## 23. Deferred work

P1.6c implementation, Snapshot V2 disk format, Replay V2 selection, automatic
neighbor scheduling, P1.7, retries, expanded metric querying/templates, raw global
deduplication, and durable commit storage are deferred.

## 24. Scope deviations and compatibility choices

No new stage was started. No planner/verify/LLM scheduling policy was changed.
The explicit legacy query-only path and Fixture's hand-authored projection mode
preserve existing backend behavior without manufacturing raw provenance; they are
documented exceptions to normalized material projection, not native acquisitions
with missing material. Direct historical trace resolution now requires a committed
partition even for synthetic callers; relevant test setup was migrated accordingly.
