# P1.7b — TaskIntent and missing-check-driven scheduling

P1.7b separates WHY an acquisition is requested (SchedulingNeed), WHAT logical
data is requested (TaskIntent), HOW a backend obtains it (TaskSpec), and THIS
execution (AttemptRecord). Only P1.7b is implemented; the single selected-HS
verification facade from P1.7a remains.

## 1. Files changed

New production files:

- `src/aegis_rca/models/scheduling.py`: frozen contracts, bounds and monotonic reducers.
- `src/aegis_rca/scheduling.py`: logical requests, check mapping, reconciliation and limitations.

Modified production files:

- `src/aegis_rca/state.py`: intent ledger, mapping limitations and request union.
- `src/aegis_rca/graph.py`: survey/planner, physical admission, required-check scheduling;
  strip internal recorded selector from worker payload because it is already in T.
- `src/aegis_rca/runtime.py`: admission recovery journal and post-merge reconciliation.
- `src/aegis_rca/runner.py`: deadline recovery of ledger and all reserved attempts.
- `src/aegis_rca/report.py`: acquisition limitations without changing pure check reasons.
- `src/aegis_rca/cli.py`, `src/aegis_rca/api.py`: additive ledger/limitation sidecar artifacts.

New tests: `tests/test_scheduling.py`. Updated tests:
`test_acquisition_identity.py`, `test_acquisition_partitions.py`, `test_api.py`,
`test_connectivity_wiring.py`, `test_dependency_hypotheses.py`, `test_integration.py`,
`test_investigation_scope.py`, `test_persisted_verification.py`,
`test_recorded_replay.py`, and `test_workflow.py`, all under `tests/`.
This document records the new contracts and intentional behavior migrations.

Exact file-hash audit: `artifacts/p17b-file-audit.json` against the captured
pre-edit baseline. No Observation, normalization, metric derivation, check
evaluator/aggregator, HS factory, identity encoder, Live backend, TaskSpec,
AttemptRecord, Snapshot V1/V2, recorded-replay backend or benchmark/scorer
implementation was changed.

## 2. TaskIntent and TI identity

```python
class TaskIntent(Contract):
    schema_version: Literal[1] = 1
    signal: Literal["metric", "trace", "log"]
    services: tuple[str, ...]
    window: TimeWindow
```

Frozen and extra-forbidding; services are sorted/deduplicated, nonempty, bounded
to 64 input entries with nonblank identifiers of at most 256 characters. These
reuse the P1.7a service bounds. Windows are aware, ordered and normalized to UTC.
TI hashes exactly these semantics using the unchanged neutral structural helper.

No check/HS ID, consumer purpose, goal, origin, priority, backend, T/A ID, alias or
Replay selector enters TI. Physical selection stays in TaskSpec. The current
logical contract deliberately does not introduce future query parameters.

For the default checkout incident window, new identity goldens are:

```text
metric TI-19817adc54092b7c1d65259692f65d68d910654f0b214200be59ac4f7bc27969
trace  TI-913a0d896f7cd311925e86b009055715fc06896d2b5dc007e80c252af60afb01
log    TI-ffcc5a09df51afe36391955cd49b019ebd6794602da691f77982a5545733f686
```

## 3. SchedulingNeed

Each need contains `intent_id`, canonical `requested_for_check_ids`, canonical
`requested_for_hypothesis_ids`, and a closed origin: `initial_survey`,
`required_check`, `llm`, or `programmatic`. Consumer sets are sorted/deduplicated
and bounded to 256 references. Needs sharing TI/origin union their consumers;
up to four origin groups can accompany one intent. Neither need aggregation nor
display goal changes logical or physical identity.

Two hypotheses requesting log(frontend,W) converge on one TI and one admitted
execution. Check consumers of an already acquired exact TI can also be added
without another acquisition. Consumers are preserved even when the corresponding
check has already become SUPPORT.

## 4. Intent ledger and transition rules

`IncidentState.intent_ledger` is a bounded, per-run authority: at most 256 records.
Every record embeds the TaskIntent and aggregated needs. Status is one of:

- `pending`: T and reserved A required; committed A absent; blocking reason absent.
- `completed`: linked terminal A returned ok/empty; committed A equals reserved A.
- `failed`: linked terminal A returned error/timeout; committed A equals reserved A.
- `unavailable`: stable pre-execution capability/replay failure; no T or A.
- `ambiguous_replay_selection`: replay ambiguity; no T or A.

Records optionally carry the validated recorded selector. Contract validators
reject inconsistent status/reason/linkage combinations. Reconciliation validates
the exact TI/T signal, services and window, recorded selector, current execution
and ordinal-one reservation identity, then the terminal A/T relationship.

The dedicated `transition` validator permits pending → completed/failed and
explicitly selected ambiguous → pending. Completed/failed/unavailable cannot
become a new pending execution. Consumer references can grow while disposition
and execution linkage remain fixed. The dedicated reducer revalidates both sides,
unions consumers and safely joins repeated ancestor snapshots from nested graph
state propagation; this is not permission to roll back a terminal record. It can
join ambiguous → pending → terminal across a subgraph boundary with validated
explicit-selector linkage. Conflicting terminal states/links are rejected.

Unknown check mappings use a separate bounded `SchedulingLimitation` collection;
they do not invent a query or a fictitious TI just to attach a disposition.

## 5. Exact pending ownership boundary

Planner/scheduler returns ephemeral `ScheduledAcquisitionRequest` values carrying
TaskIntent, needs, goal and optional recorded selector. Request creation does not
write a pending ledger row or consume budget. TI association is explicit and
never inferred from `query-{signal}` aliases.

Admission authorizes current scope, checks capability, materializes T, applies TI
and independent physical T deduplication, and clips by remaining call/round
budget. Only a successfully reserved physical A gets a pending record linked to
its T and reserved A. The ledger bound is checked before reservation mutation;
reservation and admission journal publication occur without an await. Clipped
requests are absent from the ledger and remain logically not requested.

The admission journal in Dependencies recovers subgraph writes interrupted by a
global deadline. It is not another evaluator or worker-written truth store.
This stage preserves the existing in-process execution guarantee; it does not
claim durable cross-process exactly-once recovery.

## 6. Blocking reasons

The closed scheduling vocabulary is:

```text
unsupported_capability
replay_unavailable
replay_ambiguous
unsupported_check_acquisition_mapping
```

The last code is also the sole mapping-limitation reason. Typed exception classes
select capability/replay dispositions; no exception prose is parsed or exposed.
Budget, outside-scope and stale-scope are not unavailability reasons. Attempt
failure remains a failed row plus linked A/error_class, with no pre-execution
blocking reason. CheckReason and PlanReason vocabularies were not changed.

## 7. Exact logical completion semantics

Reuse requires equal TI semantics, including exact services and exact window.
A completed multi-service log/trace intent does not cover a narrower intent;
a broader time window does not implicitly cover a smaller one. There is no
generic service-superset coverage relation. Physical T dedup is separately
preserved: an identical materialized T can link its existing reservation/A,
without charging or creating another execution.

Completed describes acquisition completion, not check or hypothesis truth.
Successful empty raw acquisition and successful legacy projection-only acquisition
both complete the intent, even if no usable native fact exists. Failed acquisition
is not reinterpreted as absent telemetry or a negative fact.

## 8. Explicit CheckSpec mapping

`check_intent` recognizes only:

- RpcExists with trace scope → TI(trace, exact check services, exact check window).
- LegacyEvidenceExists with log scope → TI(log, exact check services, exact check window).

It uses predicate types and checks scope agreement. It does not parse names,
descriptions or reasons. Metric predicates have no diagnostic acquisition mapping
in P1.7b. An unknown mapping yields a structured check/HS limitation and no query.

## 9. UNKNOWN-aware scheduling

For required checks, SUPPORT and CONTRADICT never request telemetry. For all other
verdicts (or missing results), map the check and consult the exact TI ledger.
Pending/completed/failed/unavailable/ambiguous entries suppress automatic repeat
requests. A new authorized TI may be proposed; physical admission still decides
whether it can reserve budget. UNKNOWN alone never triggers a retry, and pure
CheckReason text does not participate in scheduling.

## 10. Initial survey

The bounded initial survey is metric(original canonical seeds,W), then
trace(original canonical seeds,W). There is no initial log, per-seed fanout or
admitted-neighbor expansion. Metric is background compatibility acquisition,
never a required diagnostic check. Reservation ordering is metric, trace, log,
with deterministic service/window/TI ordering within a signal. Serialized record
collections retain their own canonical ID order, not wall-clock completion order.

## 11–12. Planner, scheduler and LLM admission

Planner owns the initial survey and optional logical proposals. The deterministic
verify/scheduler derives follow-ups from the selected persisted HS's actual
CheckSpecs and CheckResults. Required requests precede optional work and are
ordered independently of confidence or evidence counts.

LLM contracts are unchanged: signal/services/goal, no TI IDs. Deterministic code
creates TI. Allowed services remain original seeds. During the survey an LLM may
contribute to those exact survey intents but cannot replace them, add log, or
cause per-seed fanout. Out-of-scope proposals are rejected before admission and
do not poison the ledger. Current-scope freshness errors remain invariant errors.

The normal graph does not add an optional LLM round after a no-HS survey: it stops
as required. The planner boundary can admit optional logical proposals when
explicitly invoked with no required or survey work; this does not authorize
automatic neighbor investigation.

## 13. Capability and Replay preflight

Unsupported capability produces unavailable/unsupported_capability with no saved
T, no A and no tool-call charge. Live's single-seed physical constraint remains.
Replay V2 no candidate produces unavailable/replay_unavailable. Multiple matching
historical acquisitions produce ambiguous_replay_selection/replay_ambiguous.
Neither loops nor becomes negative evidence.

Programmatic explicit selection validates exact signal/services/window through
the existing Replay backend before ambiguity can progress to pending. The selector
changes T-replay, not TI. Conflicting selectors for one TI in a batch are rejected
before reservation. Invalid explicit selection is rejected without poisoning or
overwriting the TI disposition. Once completed/failed, even a different selector
cannot create a second execution for the same TI.

For explicit selection, worker constructs MaterializedTask using only ordinary
InvestigationTask fields plus the already validated TaskSpec. The recorded
selector lives in TaskSpec/ledger, avoiding an extra-field validation failure at
the worker boundary. No Replay selection algorithm was changed.

## 14. Attempt failure and reconciliation

Workers publish the existing physical completion and raw partition; they never
write the intent ledger. After authoritative worker results merge, one reconciler
uses linked AttemptRecords to move pending to completed or failed. Error,
invalid_result and timeout receive no retry A. Global-deadline recovery merges
the admission journal and terminalizes reserved unfinished attempts, including a
reservation interrupted before dispatch, then reconciles before reporting.

## 15. Budget and termination

Only physical reservation consumes the existing tool budget. TI, need and blocked
disposition creation do not. Max rounds/calls clip executable candidates without
creating orphan pending rows. Supported findings arriving on the final allowed
call remain usable. Otherwise budget exhaustion stays a control decision, not
a per-intent unavailable reason or a negative finding.

With no new required intent, verify terminates `INCONCLUSIVE` using the control
reason `no_admissible_new_intent`. Completed-but-UNKNOWN, failed, unavailable and
ambiguous acquisitions cannot create infinite follow-up loops.

## 16. Provenance and artifacts

```text
C / HS → SchedulingNeed → TI → materialized T → reserved/committed A
                                              → raw partition/facts → CR
```

The ledger is the authoritative cross-layer linkage. Existing TaskSpec/Attempt
identities are unchanged. CLI/API add `intent_ledger.json` and
`scheduling_limitations.json` beside the existing artifacts. The ledger sidecar
includes TI IDs, embedded logical semantics, consumers, disposition and T/A links.
Snapshot V1/V2 wire schemas and historical permission rules remain unchanged;
historical Snapshot scope is never current authorization. Replay execution builds
a fresh current ledger from its own admissions.

## 17. Default and generalized supported regression

Default checkout/payment and frontend/catalog both retain **2 rounds / 3 calls /
metric-trace-log / SUPPORTED**. Default citations remain E-T1/E-L1, diagnosis is
failure_mode, underlying cause is None. All automatically generated acquisitions
remain caller/original-seed work; no automatic payment/catalog query.

The two-original-seeds regression now injects explicit caller-only log material
for the narrower required check query. It does not rely on broad multi-service
acquisition equivalence or change the backend's physical rules.

## 18. Intentional behavior migrations

Healthy/successful RPC, gRPC UNKNOWN, empty trace and unavailable native-source
cases with no eligible HS stop after the initial metric+trace survey: normally
**1 round / 2 calls / INCONCLUSIVE**. Failed initial acquisitions also stop without
unrelated log work. Missing required logs still receive one acquisition and remain
unresolved if it completes empty or fails. Log-first LLM proposals no longer
replace the mandatory survey. Capability/replay admission errors are now
structured nonexecuted dispositions instead of unhandled workflow exceptions.

## 19–20. Validation

Targeted scheduling/workflow/persistence/replay/API regression run:
**422 passed in 43.02s** (`artifacts/p17b-targeted.log`).
Final full suite: **1036 passed, 1 skipped in 48.64s**
(`artifacts/p17b-full.log`), compared with baseline 989 passed, 1 skipped.
The skip is the existing Windows symlink test. The new scheduling suite adds
47 passing cases (`artifacts/p17b-scheduling-final.log`). All commands use the
project virtual environment and fresh project-local `--basetemp` directories.
The final full suite includes the last added reservation-order assertion and
API sidecar round-trip checks. The scheduling suite covers TI fields and goldens, multi-consumer union, pending
ownership, clipping, exact services/window reuse, failure/no retry, Replay
unavailable/ambiguity/explicit selection, scope freshness/rejection, LLM priority,
mapping limits, deadline reconciliation, bounded transitions, worker ownership,
physical dedup and end-to-end provenance.

The file audit records 17 modified and 4 added source/test/documentation files,
and verifies that all 28 listed protected implementation files are unchanged.

## 21. Identity and golden regressions

There is **no T/A/O/C/CR/HS/P/PR/VBS/VBE identity migration** in P1.7b. P1.7a's
generic plan and report identity choices remain. Existing scope/partition and
Snapshot/replay integrity tests remain applicable. New TI goldens are listed
above. The supported default report/control digest remains unchanged.

Two negative report/control digests migrate intentionally:

```text
empty (1 round, 2 calls, no log):
old 7d7564728d68f51aa82e6ec2766cf2147e01297d9f9082b17f8522806b16a078
new 193ba91b7a94090b3f28b388e2b5d840bbcb7f50de7951f997de9e502de7dd99
missing_logs (completed UNKNOWN log, no new admissible intent):
old 02545744dd285dccd4976cd5b27188642ae8a8de75db994bf38e0efa02b4d49c
new b4b826ec4235131541ec56a619da6e2562aead7b30693b4e679ab5b2fd43f7c1
```

This describes unchanged identity algorithms/payload goldens, not identical
execution artifacts for intentionally removed log queries in healthy cases.

## 22. P1.7c handoff

Runtime still evaluates one selected HS; there is no hypothesis_verifications
map or final assessment-status model. Future multiple HS/check consumers can use
the existing need union and TI ledger. P1.7c should change which required checks
contribute needs, not acquisition identity semantics. Keep missing acquisition as
a limitation, separate from PROPOSED/UNRESOLVED/SUPPORTED/CONTRADICTED truth states.

## 23. Deferred work and scope deviations

No retries, multi-hop, automatic neighbor queries, native log Observation, metric
diagnostic plans, generalized coverage relation, verified-root-cause logic or
P1.7c persistence was implemented. No backend physical capability expansion or
benchmark/scorer redesign was made. Additive runtime sidecars, selector payload
normalization and reserved-attempt deadline reconciliation are necessary parts of
the requested provenance/admission contract. Unknown mapping limitations are
stored separately because there is no honest executable TI to fabricate.
