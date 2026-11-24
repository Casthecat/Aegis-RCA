# P1.7a — Generic dependency failure hypotheses

This stage verifies a **failure mode**, not an ultimate root cause. The single
implemented kind is `dependency_connectivity`. The runtime claim is “caller
could not establish its callee dependency.” `underlying_cause` remains `None`.

## 1. Changed files and literal audit

New production modules: `src/aegis_rca/hypotheses.py` (native candidates) and
`src/aegis_rca/service_scope.py` (bounded logical triage).

Changed production files: `models/hypothesis.py`, `policy.py`, `graph.py`,
`runtime.py`, `report.py`, `verification/catalog.py`, `verification/aggregate.py`,
`verification/connectivity.py`, and `verification/persisted.py`, all under
`src/aegis_rca/`.

New regression file: `tests/test_dependency_hypotheses.py`. Existing expectations
were migrated in `test_acquisition_identity.py`, `test_acquisition_partitions.py`,
`test_api.py`, `test_check_aggregation.py`, `test_check_reason_contracts.py`,
`test_connectivity_wiring.py`, `test_integration.py`, `test_investigation_scope.py`,
`test_persisted_verification.py`, and `test_workflow.py` under `tests/`.
Documentation: this file and the evaluation example migration notice.

The pre-edit inspection covered hypothesis contracts, policy, catalog, aggregate,
persisted verification, graph triage/hypothesize/verify, report, state, scoring,
fixtures, CLI/API, and connectivity compatibility entry points. Literals were
classified before changing their uses:

- Executable demo semantics: checkout-only triage, fixed caller/peer evidence
  selection, `H-connectivity` truth branching, payment-specific plan mode, and
  fixed report wording were replaced by the parameterized path.
- Compatibility entry points: `connectivity_plan(window)` and default arguments
  on the pure `evaluate_connectivity` function retain the original sample pair.
  Both delegate to the generic factory. Runtime always passes explicit endpoints.
- Fixture/configuration examples: raw synthetic telemetry, CLI collection's
  sample service default, configured RPC service map, Snapshot V1 map defaults,
  and the P1.6a explanatory topology comment remain unchanged. They are data or
  acquisition configuration, not diagnostic policy.
- Legacy evidence vocabulary: `checkout_errors_elevated` remains for wire
  compatibility/background evidence; it is not a required diagnostic check.
- Protocol/failure vocabulary: `CLIENT`, `SERVER`, `UNAVAILABLE`,
  `connection_refused`, and `dns_resolution_failed` remain reusable predicates.
- Archived evaluation examples retain their historical report/label pair.
  New runs require the label migration described below, not a policy alias.

No production edits were made to state, observation/normalization, metric
derivation, acquisition identity/store, task materialization, planner model/provider,
Live backend, Snapshot V1/V2, recorded replay, CLI/API, scorer, or benchmark runner.

## 2. HypothesisSpec

`HypothesisSpec` is a frozen, extra-forbidding Pydantic contract:

```python
schema_version = 1
semantic_version = "1"
kind = "dependency_connectivity"
caller: str
callee: str
window: TimeWindow
failure_class = "rpc_connection_establishment"
operation: str | None = None
```

Endpoints must be valid bounded service identifiers and must differ. The window
is valid, timezone aware and normalized to UTC. An explicit operation must be
nonblank. No confidence, prose, evidence references, acquisition references or
assessment status is part of this contract.

## 3. HS identity

`HS-*` uses the unchanged `models.identity.structural_id` canonical encoding and
SHA-256 helper over the structured fields above. `operation=None` is omitted
from the identity payload; an actual operation enters it. Presentation changes
cannot change HS; endpoints, window or a constrained operation can.

For the fixture window `[2026-09-08T14:30:00Z, 2026-09-08T14:40:00Z)`:

- checkout → payment:
  `HS-b97a36c2415b4dfb801632ecd40ca61a7e8e67242adb432f4b9c2c6d158fbfe6`
- frontend → catalog:
  `HS-21db5fdd7eba7296f2a3966bcc82beedb3e2a7730c042fbfaee193e33974ca2e`

## 4. Exact candidate admission

The completion boundary resolves one native trace generation from immutable
attempt-owned partitions. Candidate sources must agree with current persisted
TaskSpec, AttemptRecord and execution namespace, and have a verified binding.
Scope is derived first from that same generation, then freshness checked.

A candidate requires an `rpc_outcome` with local service equal to the proposed
caller, CLIENT span kind, resolved/unambiguous peer equal to the callee, gRPC
status name UNAVAILABLE, and an in-window timestamp. The caller must belong to
**original seeds**; the distinct callee must belong to current admitted services.
The candidate is admitted only when the generic plan's actual native trace check
returns SUPPORT. This reuses the verifier rather than a second status classifier.

Without persisted scope only original seeds are authorized. Stale scope is
rejected. Raw peer strings, historical archive scope and LLM claims cannot grant
permission. Two original seeds may form a candidate without a scope expansion.
The scope admission rule “exactly one seed endpoint” is not a hypothesis rule.
SERVER topology, successful RPCs and gRPC UNKNOWN may permit topology expansion
but cannot independently produce this failure candidate. An admitted neighbor
never becomes an original-seed caller; explicit neighbor acquisition also cannot
authorize hop-two hypothesis generation.

## 5–6. Generic factory and CheckSpecs

```python
dependency_connectivity_plan(caller, callee, window, operation=None)
```

It constructs exactly two required support checks:

1. Trace `RpcExists`: scope/local service caller, CLIENT, peer callee, resolved
   and unambiguous peer, `rpc_status_names=("UNAVAILABLE",)`, optional operation.
2. Log `LegacyEvidenceExists`: scope/service caller, peer callee,
   `codes=("connection_refused", "dns_resolution_failed")`.

Both are caller-side checks. Callee traces/logs and metrics are not required.
Operation constrains the RPC check only; the existing legacy log contract has no
operation matcher. Runtime candidates currently leave operation unconstrained.

## 7–8. Plan and aggregate migration

The one supported aggregate failure mode is now
`rpc_connection_establishment`; `payment_connection_failure` is no longer an
executable mode. The small allowlist mechanism and contradiction behavior remain
unchanged. `catalog_key="dependency_connectivity"` is display metadata.

The default two-check plan migrated intentionally:

```text
old P-12a468e597d8558cd6dfd9ed088efc87611d6712082ab15ac8443ddd1ca6410c
new P-b8779552044abd90c1fcadc81aa14f098c382efa5839e52d8db48f9b473e3410
```

The default CheckSpec IDs are unchanged:

```text
C-34f3b28fee067136807c099221e6ea5689865b5589b7d0eb5685c123f99d678d
C-9ccc9797df659e559be397588ed8a5fe6a0a7ddec6f9d74e7a106b1ab6a2ef4a
```

Scoring already uses exact structured label matching and was not redesigned.
For newly generated reports, migrate applicable private failure-mode labels to
`rpc_connection_establishment`, retaining the scenario's caller/callee edge and
human review requirements. Do not pass private scenario/fault names to policy.
Historical reports and their labels can remain paired in their original version;
do not compare a new generalized report against an old payment-specific label
and interpret the resulting exact-match miss as a telemetry regression.

## 9. Generic triage contract

Logical seeds are nonempty, sorted and deduplicated deterministically. Raw input
has at most 64 service entries (before deduplication), matching the existing
scope-policy upper service-count bound. Each identifier is a string with a
nonblank stripped form and at most the existing default scope name bound of 256
characters. Identifiers are preserved exactly; leading/trailing whitespace is
not silently rewritten into another telemetry service. The incident's nested
window is revalidated and requires aware timestamps with start < end.

The bounds live in `service_scope.py`; `IncidentInput` and Snapshot wire schemas
were not changed. Invalid runtime inputs produce `invalid_scope` with zero
queries; invalid HTTP payloads may be rejected earlier by existing Pydantic
request validation. Backend capability is separate: Live still requires its
existing single-seed physical acquisition contract.

## 10. One compatibility facade

`connectivity_verification` remains the one persisted verification slot. It now
contains its `hypothesis_spec`; its intrinsic validator derives the generic plan
from that subject and verifies C/P/PR relationships. VBS relevant attempts and
log evidence selection use the same parameterized caller, callee, window/codes.
Bound log IDs still participate in integrity checks; unrelated telemetry cannot
satisfy the actual predicate. The current accessor also checks subject permission
against fresh scope.

Candidates are ephemeral and sorted by `(caller, callee, HS id)`. P1.7a selects
the first candidate for the single slot; this is deterministic selection, not a
causal ranking. It does not assess every candidate or prefer the candidate with
more supporting logs. Multi-subject assessment/persistence is deferred.

`Hypothesis` is the runtime/display projection of that subject and evaluation;
its ID is HS. There is no independent H-connectivity evaluator. Existing LLM
proposals remain provisional and cannot create this deterministic subject or
grant scope. If no eligible native candidate exists, the slot is `None` and the
deterministic hypothesis projection is empty. Source-unavailable, healthy and
empty runs therefore no longer persist an unconditional demo hypothesis.

Existing saved P1.4/P1.6 connectivity bundles without `hypothesis_spec` cannot be
silently read as current bundles. Regenerate verification from retained recorded
acquisitions. Old report/H identifiers remain historical display artifacts.
Snapshot V1/V2 raw acquisition archives themselves are unchanged and replayable.

## 11–12. Report meaning and root-cause non-claim

Supported output uses the template “frontend could not establish its catalog
dependency.” `diagnosis_level="failure_mode"`, `underlying_cause=None` and
explicit limitations remain. The next checks state that why the dependency was
unreachable remains unresolved and invite inspection of both sides. No callee
process fault, bad configuration, feature flag, network component failure or
ultimate root cause is asserted.

E-* citation selection is parameterized by caller, callee, evidence code and
incident window. Citation selection is a compatibility projection, never a
second truth evaluator. The default still selects E-T1/E-L1.

## 13–14. Generalized and default regressions

The synthetic frontend/catalog fixture uses explicit raw CLIENT UNAVAILABLE,
frontend connection-refused logs with peer catalog, and an empty background
metric acquisition. It produces HS(frontend,catalog), a supported generic plan
and parameterized failure-mode report. It also round-trips through Snapshot V2
recorded acquisition replay: HS/P/O semantics persist while replay gets new T/A
identities. All queries remain on original seeds.

Default checkout/payment remains **2 rounds / 3 calls / SUPPORTED / E-T1,E-L1**,
failure_mode diagnosis and unresolved underlying cause. There is no automatic
payment query. Negative regressions cover SERVER-only verified facts, successful
CLIENT, gRPC UNKNOWN, ambiguous/missing peer, outside-window facts, self edges,
stale/absent scope, unavailable binding, two original seeds, hop-two callers and
invalid/bounded triage.

## 15–16. Validation

Targeted regression run: **607 passed in 34.58s** (`artifacts/p17a-targeted.log`).
Final full suite: **989 passed, 1 skipped in 37.69s** (`artifacts/p17a-full.log`),
up from the baseline of 954 passed, 1 skipped. The skip remains the pre-existing
Windows symlink test in `tests/test_sanitization.py`. Both runs use the project
virtual environment and a fresh project-local `--basetemp`.

The targeted run covers dependency hypotheses, check aggregation/reason contracts,
connectivity wiring/persistence, investigation scope, acquisition identity and
partitions, workflow/integration/API, trace/metric observation and derivation
goldens, Snapshot V2, recorded replay and benchmark admission. The final full run
also covers Snapshot V1/live replay, normalization, sanitization, scoring, CLI,
reasoning and the remaining contracts. `artifacts/p17a-file-audit.json` lists the
exact 20 modified and 4 added source/test/documentation files against the captured
pre-edit hashes, plus 15 unchanged protected implementation files.

## 17. Identity and artifact migration ledger

For fixed execution `p16a-golden`, P references cause these expected migrations:

```text
old PR-960cb97f75dfe8cc1b66fbaa93dc6cf0079be3b39a03d7f9510f47eef30ef379
new PR-b2544dbaa447410f81c9e39fa5ff464120a6ccbaf36b593b08f36cd436d75f0f
old VBS-d5be4bdb988d494af12ae2f343a631aede3f25bb863ad0cea96b17598a4fd143
new VBS-d7dd283b800bd0b4a4c6f504310b36860e28db8ccc7f5c52fe3c86618078dfdc
unchanged VBE-b01c93039a4ad22ae42e9d6aaa35ed83481107af290d21fc0e30e59bfe7b3de4
```

The single-check aggregation fixture also changes solely with the mode:

```text
old P-0d74b6e0eade41cb6155c402a8a13588c5867cf567e24419a4e5af3e3fc92104
new P-8e9922ae7db87d9c95daf78d38dd34edf3ac7a88096bf01a1d7e232a85fa9039
old PR-32381691b709249122b673d404ec6783dc7bd1386cc49a8efef1576683543b2b
new PR-e610014ff666ad212efa1295492fe22c7a6c3aefa716bc49557e391bf2689ca8
```

Its C/CR goldens remain unchanged. O-trace/O-metric, T/A, C/CR, VBE and P1.6
scope/partition/replay identity regressions retain their original values. The
hashing helper, canonical encoding and underlying historical payload semantics
are unchanged. A historical payment-mode P payload still hashes to its old ID;
it is simply no longer accepted as executable policy.

The report/control digest fixture migrates with HS projection, removal of demo
placeholder alternatives, generic wording and the no-candidate behavior:

```text
connectivity:
old bee760bbb5c88d76415a1063ec0800c66b9634146627b650757f23b4d6c108d9
new 4d187dba67e341e2fdfd4f8329c43443c92050a3f71a24be555016f802239cc6
empty:
old 6eda17c586af791adffa76cb9fc7f861676de9238d1e94caa619bee6acc88714
new 7d7564728d68f51aa82e6ec2766cf2147e01297d9f9082b17f8522806b16a078
missing_logs:
old fb55123a3fb2ddde9dc37573a81528847d748ebb949721479b01a579e6dff6c1
new 02545744dd285dccd4976cd5b27188642ae8a8de75db994bf38e0efa02b4d49c
```

## 18. P1.7b handoff — TaskIntent identity

No TaskIntent is implemented. Future TI-* must describe **what acquisition is
wanted**: signal, services, window and actual logical acquisition parameters.
Purpose, check IDs and hypothesis IDs must not enter TI identity. Put
`requested_for_check_ids` and `requested_for_hypothesis_ids` in separate
SchedulingNeed/consumer references. HS(frontend,catalog) and HS(frontend,auth)
must share one logical `log(frontend, window)` acquisition intent when parameters
are identical. Keep physical T and historical/replay lineage separate.

## 19. P1.7c handoff — assessment and persistence

No new assessment status model or multi-HS persisted map is implemented. Future
assessment should distinguish PROPOSED, UNRESOLVED, SUPPORTED and CONTRADICTED.
Do not introduce an `unsupported` truth state conflating missing support,
insufficient evidence and contradiction. Required acquisition unavailable is a
limitation/blocking reason, not a hypothesis truth value. The existing legacy
Hypothesis display status is not the future foundational assessment contract.
Replace the single selected-subject facade deliberately when multi-HS persistence
is introduced; do not create a second independent truth system beside it.

## 20. Deferred work and scope boundaries

No P1.7b/c, TaskIntent scheduling, automatic neighbor queries, retries, multi-hop,
native log observations, metric diagnostic plans, causal DAG, verified root cause,
benchmark V2, citation cosmetic migration or formal benchmark Stage C work was
implemented. Live multi-seed capability and scorer redesign remain deferred.
The intentional compatibility changes are the HS projection, nullable
no-candidate facade and documented plan/report/artifact migrations above.
