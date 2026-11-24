# P1.7c — Bounded multi-hypothesis verification

P1.7c replaces the single current hypothesis truth slot with a bounded current
portfolio and one generalized collection of persisted hypothesis verifications.
This completes the requested **core P1 semantic architecture**. It does not establish
real-world RCA accuracy, complete deployment validation, or begin P2.

## 1. Files changed

New runtime files:

- `src/aegis_rca/models/portfolio.py`: frozen admission policy, portfolio and two bases.
- `src/aegis_rca/portfolio.py`: refresh derivation and state-only freshness accessor.
- `src/aegis_rca/assessments.py`: assessment and consumer-scoped limitation views.

Changed runtime files:

- `src/aegis_rca/models/hypothesis.py`: assessment/report contracts and claim provenance.
- `src/aegis_rca/hypotheses.py`: expose candidate support/witness provenance at refresh.
- `src/aegis_rca/verification/persisted.py`: explicit-subject PHV contract and accessors.
- `src/aegis_rca/runtime.py`: shared resolution index and atomic current-generation refresh.
- `src/aegis_rca/state.py`: portfolio/PHV collection; separate provisional display list.
- `src/aegis_rca/scheduling.py`: aggregate required requests and consumers across all PHVs.
- `src/aegis_rca/graph.py`: multi-HS display, continuation and termination policy.
- `src/aegis_rca/policy.py`: all deterministic compatibility projections.
- `src/aegis_rca/report.py`: multiple claims, assessments and overflow summary.
- `src/aegis_rca/cli.py`, `src/aegis_rca/api.py`: current portfolio and PHV artifacts.

New tests: `tests/test_hypothesis_portfolio.py`, `tests/portfolio_helpers.py`.
The latter is a **test-only** singleton adapter; production freshness/refresh helpers
never use it or infer a selected subject.

Migrated existing tests: `test_acquisition_identity.py`, `test_acquisition_partitions.py`,
`test_api.py`, `test_connectivity_wiring.py`, `test_dependency_hypotheses.py`,
`test_integration.py`, `test_investigation_scope.py`, `test_metric_observations.py`,
`test_persisted_verification.py`, `test_recorded_replay.py`, `test_scheduling.py`,
`test_workflow.py`. These replace access to the removed singleton state slot,
pass explicit subjects, and update intentional report/control expectations.
Documentation: this file and `docs/roadmap.md`.

## 2. Admission policy / HP

`HypothesisAdmissionPolicy` is frozen, extra-forbidden and schema version 1.
It fixes `kind=dependency_connectivity`, `operation_mode=edge`, and a strict integer
`max_active_hypotheses`, default **4**, permitted range **1..16**. Schema version 1
defines deterministic ordering by `(caller, callee, HS-id)`; this is structural
ordering, with no causal ranking. Change policy explicitly at the refresh boundary
with `hypothesis_policy=HypothesisAdmissionPolicy(max_active_hypotheses=...)`.
Subsequent refreshes preserve the current policy.

HP hashes the complete policy payload through the unchanged neutral structural
hash helper. Budget, evidence/span counts, LLM confidence and display text are absent.
Default identity: `HP-0bb822253bd68d155233f773ed51a9e56957b72bd37ed67bfa3c4112e5d25d70`.

## 3. HPS semantic identity

HPS hashes only `{policy_id, active_hypothesis_ids}` with HS IDs in the canonical
active order. It excludes HSB, HRB, acquisition generations, witnesses, report
text and overflow contents. Equivalent new acquisitions can preserve HPS while
changing HSB and HRB. Existing HS identities and their operation=None omission
semantics are unchanged.

## 4. HSB state-only freshness

`HypothesisStateBasis` binds policy ID, canonical original seeds, incident window,
current scope semantic/basis IDs, and relevant original-seed trace T/A references.
Each acquisition fingerprint includes `task_spec_id`, `tool_status`,
`observation_binding`, `source_material_digest`, and admitted O IDs. The referenced
TaskSpec ID already binds physical query semantics.

`current_hypothesis_portfolio(state)` revalidates the persisted contract, compares
the state-recomputed HSB and checks seed/scope/window permissions. It never reads a
backend, calls `observe_spans`, derives candidates or executes a check. A source
change alone does not stale a persisted portfolio. Acquisition changes require a
new refresh; consumers reject stale authoritative state in the meantime.

## 5. HRB resolved derivation provenance

`HypothesisResolutionBasis` is constructed at refresh only. Per seed trace attempt
it records the native resolution outcome and the count/digest of actual resolved
O IDs. It also binds eligible HS count and ordered-ID digest, active HS IDs,
candidate matched-O/(A,O)-witness digest, and overflow digest. Digest inputs are
canonical structured values, never display prose. Unverified resolutions cannot
contribute observations. The digest representation binds the actual IDs without
persisting an unbounded candidate/witness list.

HSB asks whether current authoritative state matches; HRB records what the refresh
resolved. Neither is folded into HPS. State-only access does not re-prove CLIENT
UNAVAILABLE from historical admission IDs.

## 6. Bounded portfolio and overflow

There is one current portfolio, no generation history. Active count must obey the
policy and absolute bound 16. Active subjects are unique and canonically ordered.
Overflow stores only count plus digest; no overflow candidate names or full HS
list are persisted. Overflow requires a full active set. Empty overflow uses the
canonical empty digest; HRB active/count/digest references must agree with portfolio.
Per-attempt basis references are bounded at 100, matching the existing execution
tool-call ceiling. Hypothesis bounds and tool/round budgets remain independent.

## 7. Candidate admission and operation collapse

The P1.7a generic native support check remains the semantic gate: verified typed
`rpc_outcome`, local original-seed caller, CLIENT, resolved/unambiguous peer,
gRPC UNAVAILABLE, distinct endpoints, in-window, callee authorized by the fresh
current scope (or original seeds when no scope exists).

SERVER-only topology, success and concrete gRPC UNKNOWN do not create candidates.
Two original seeds may form a candidate. An admitted/queried neighbor does not
become an original-seed caller. Runtime candidates always have `operation=None`:
failing operations on one edge collapse into one HS while witnesses affect HRB.
Scope orientation and one-hop admission rules are unchanged.

## 8. Shared resolution and authoritative refresh order

`ResolvedAcquisitionIndex` is ephemeral: `traces[A]` caches a resolved native trace;
`logs[A]` caches its validated admitted legacy Evidence/binding outcome. It is not
another persisted truth store. For each refresh:

1. Terminal acquisition updates have been merged and validated.
2. Resolve original-seed trace attempts once.
3. Derive the current InvestigationScope.
4. Derive the bounded portfolio and its HSB/HRB.
5. Compute the union of all active-HS-relevant attempts and resolve uncached entries.
6. Evaluate every PHV from that shared index and replace the whole collection.
7. Reconcile the TI ledger before downstream consumers run.

The existing global deadline recovery calls this same owner after committing final
timeout attempts. No per-HS repeated trace partition read/normalization is needed.

## 9. PersistedHypothesisVerification

Each frozen/versioned PHV contains a HypothesisSpec, subject-specific VBS and VBE,
and the existing two CheckResults plus PlanVerificationResult. It duplicates no
raw telemetry. The current collection must exactly equal the portfolio's active
HS set **and order**, with no duplicate or stale subject generation. State has no
independent CR/PR reducers: refresh replaces the complete tuple atomically.

## 10. Per-HS VBS/VBE

Both `relevant_attempts(state, subject)` and `state_verification_basis(state, subject)`
require an explicit subject. Selection uses the actual generic plan CheckScopes:
caller service, signal and overlapping half-open window, plus relevant legacy
Evidence fingerprints. VBE binds actual supplied O/E sets and per-attempt coverage.
No helper hashes the whole IncidentState. Shared caller attempts may contribute to
multiple PHVs; unrelated metrics and unrelated neighbor trace acquisitions do not
stale a dependency PHV. Unchanged VBS/VBE inputs retain their existing identities.

## 11. Assessment truth states

The closed `AssessmentStatus` vocabulary is `SUPPORTED`, `UNRESOLVED`,
`CONTRADICTED`. SUPPORTED follows the plan; CONTRADICTED requires explicit
contradiction reason and a CONTRADICT CheckResult. Current shipped predicates do
not emit CONTRADICT. All other current evaluations are UNRESOLVED. There is no
`unsupported`, failed, unavailable, empty or budget-exhausted truth status.

## 12. Consumer-scoped limitations

`AssessmentLimitation` has a closed reason, required check ID and optional TI ID:

- `required_acquisition_failed`
- `required_acquisition_unavailable`
- `replay_selection_ambiguous`
- `unsupported_check_acquisition_mapping`
- `budget_prevented_required_acquisition`

Existing TI limitations attach only when the row's SchedulingNeeds refer to that
HS or its required check. Mapping limitations likewise require matching HS/check
references. A failed initial metric remains an incident operational limitation;
it is not a trace/log hypothesis diagnostic limitation. These views do not mutate
the ledger or turn tool failure into contradiction.

## 13. Need aggregation and TI sharing

All active PHVs contribute requirements. Requests merge by unchanged TI identity,
unioning HS/check consumer references. Same caller/window log checks share one
TI/T/A. Distinct caller scopes have distinct TIs in the existing deterministic task
ordering. A completed TI can gain new consumers without another execution, even
when its evidence leaves one consumer UNKNOWN. COMPLETED describes acquisition,
not whether every consumer's check is SUPPORT.

## 14. Continue while another hypothesis is actionable

Verification first gathers required new authorized requests across all active
PHVs. If any remain and tool/round budget permits, investigation continues even
when another HS is supported. Otherwise exhausted budget wins, then any supported
HS permits finalize, otherwise the result is inconclusive/no admissible new intent.
Physical backend rejection is recorded by the existing admission path and is not
retried; generic request authorization does not claim live backend capability.

## 15. Budget behavior

The scheduler executes only the deterministically admitted subset. A clipped TI
has no fake pending/unavailable row. At report derivation, an unestablished required
check with no ledger disposition, an authorized mapped request, and a tool/round
budget stop gets a consumer-specific `budget_prevented_required_acquisition`.
Global deadline failure of an admitted attempt is an acquisition-failed limitation,
not a fictitious budget-clipped request. Current supported claims survive both
control outcomes. The default fixture remains two rounds and three calls.

## 16. Multiple report claims

Every supported active HS produces an ordered `verified_failure_mode` claim:
"{caller} could not establish its {callee} dependency." Any such claim means
report SUPPORTED / diagnosis_level failure_mode; otherwise INCONCLUSIVE /
undetermined. Multiple claims stay multiple edges. The old human Hypothesis model
is a derived display projection, never an independent evaluation path.

## 17. Unresolved assessments and overflow reporting

Reports include bounded structured current assessments with HS/caller/callee,
unestablished check IDs, consumer limitations and a next unresolved question.
Markdown displays these separately from supported claims. Overflow reporting
contains only count and policy limit. It neither names excluded candidates nor
asserts that their failure modes are established by a complete plan.

## 18. Claim provenance

Verified claims require `hypothesis_id=HS-*`, `plan_result_id=PR-*`, and nonempty
current `check_result_ids=CR-*`. `evidence_ids` remains E-only compatibility
citations. PHV CheckResults carry the O/E and coverage links onward to A/T and
the immutable acquisition partition / Snapshot V2. The default citation pair
remains E-T1/E-L1. Report prose is not evidence. Historical report inputs may
still deserialize as `legacy_display`; they cannot acquire the new verified claim
class without its structured provenance.

## 19. Root-cause non-claim

Runtime always emits `underlying_cause=None`. No caller/callee, shared upstream
cause, fault propagation, ultimate cause or causal winner is inferred. No
root-cause score/confidence or causal DAG was added.

## 20. Leading-hypothesis compatibility

`Verification.leading_hypothesis_id` is populated only for exactly one active
deterministic HS. For zero or multiple HS it is None, including when exactly one
of several is supported. No production report, scheduler or truth decision reads
this field. Structural claim order is not a ranking.

## 21. Connectivity facade and artifact migration

`IncidentState.connectivity_verification` is removed. The only authoritative store
is `hypothesis_verifications` paired with `hypothesis_portfolio`.
`PersistedConnectivityVerification` remains an import alias for the SAME PHV class.
`current_connectivity_verification(state, subject=...)` is explicit lookup;
without a subject it returns an evaluation only for a singleton, otherwise None
(or raises when `required=True`). The refresh compatibility helper also requires
an explicit subject and does not write state.

CLI/API emit `hypothesis_portfolio.json` and `hypothesis_verifications.json`.
The legacy `connectivity_verification.json` is a derived singleton projection or
JSON null, never a separately mutable authority. Structural ID properties are
recomputed from the artifact contracts, as in the existing scope artifacts.
Old state consumers must migrate to the collection; old singleton diagnostic
state must not be silently promoted to a portfolio. Replay acquisition material
through the current refresh path to obtain a current generation.

## 22. LLM and provisional display

Deterministic displayed hypotheses come from every active PHV; no `[:3]` truncation
applies. LLM suggestions are a separate list bounded to three, forced uncertain,
and collisions with deterministic HS IDs are discarded. They cannot mint portfolio
members, overwrite PHV status, alter ordering or authorize another service.

## 23. Replay and current-only persistence

Snapshot V2 wire schema and recorded selection behavior are unchanged. A replay
recomputes scope, portfolio, PHVs, assessments and report from its current replay
acquisitions. It does not load historical diagnosis. TI remains transport-independent;
historical T and replay T remain different. HPS can remain equal while HSB/HRB
change with current execution provenance.

Only the current portfolio/PHVs are persisted; no assessment event history was
added. Old generations may not be reconstructible from IncidentState alone when
their acquisition source material is no longer available. Preserve acquisition
archives separately when that reconstruction is needed.

## 24. Targeted validation

Targeted selection: **510 passed in 53.81s**; log: `artifacts/p17c-targeted.log`.
The selection covers portfolio, persistence, scheduling, report/control, generic
hypotheses, replay, Snapshot V2, workflow, API, scope, partitions and underlying
trace/metric/acquisition identity regressions.

New portfolio regressions cover shared log supporting both/one/neither HS;
different caller TIs; supported-while-actionable continuation; consumer-scoped
failed/unavailable/budget cases; overflow; state-only access guards; one trace
resolution per refresh; completed TI gaining consumers; multi-HS deadline recovery;
operation collapse; non-candidate topology; no hop-two; tampered/stale collections;
LLM separation; replay and report provenance contracts.

## 25. Full pytest

Full suite: **1060 passed, 1 skipped in 56.72s**; log: `artifacts/p17c-full.log`.
The baseline was 1036 passed, 1 skipped: 24 additional passing portfolio cases.
The existing skip is the Windows symlink privilege test, not a diagnostic or
replay failure. Local tests do not establish live deployment readiness.

Commands (PowerShell, repository root; use a fresh basetemp for each run):

```powershell
$p17cTemp = 'artifacts/pytest-p17c-targeted-' + [guid]::NewGuid().ToString('N')
.\.venv\Scripts\python.exe -m pytest tests/test_hypothesis_portfolio.py tests/test_persisted_verification.py tests/test_scheduling.py tests/test_connectivity_wiring.py tests/test_dependency_hypotheses.py tests/test_recorded_replay.py tests/test_snapshot_v2.py tests/test_workflow.py tests/test_api.py tests/test_investigation_scope.py tests/test_acquisition_partitions.py tests/test_observations.py tests/test_metric_observations.py tests/test_acquisition_identity.py tests/test_acquisition_hardening.py -q --tb=short --basetemp $p17cTemp
$p17cTemp = 'artifacts/pytest-p17c-full-' + [guid]::NewGuid().ToString('N')
.\.venv\Scripts\python.exe -m pytest -q --tb=short --basetemp $p17cTemp
```

## 26. Identity and golden migrations

No canonical hash implementation, old CheckSpec/plan semantics, TaskIntent/TaskSpec/
AttemptRecord identity, Observation semantics, scope identity or Snapshot V2
content encoding changed. Existing underlying HS/C/P/CR/PR/TI/T/A/O/SX/SIS/SB/VBS/VBE/
RM golden expectations are retained. HP/HSB/HRB/HPS are new identities only.

Intentional migrations:

- The old multiple-candidate test no longer selects an arbitrary single HS.
  It asserts all active PHVs and separate supported/unresolved assessments.
- Report payloads add claim provenance, assessments, provisional display and
  overflow. CR/PR provenance can depend on execution identity; report/control
  digest tests now use `execution_id="p17c-report-golden"`.
- A final call reaching tool/round budget has control reason `budget_exhausted`
  even when the report contains a supported failure mode.
- Connectivity report/control digest:
  `4d187dba67e341e2fdfd4f8329c43443c92050a3f71a24be555016f802239cc6`
  → `bb17b176a5d0e9e6cbd448e839f5b4f631e498e2bbc0d7c25a7d89e118544c3d`.
- Empty report/control digest:
  `193ba91b7a94090b3f28b388e2b5d840bbcb7f50de7951f997de9e502de7dd99`
  → `cb316cd270281448ef74e8b7ed3ca26025d06852a40df3f2257fb4055a3171ae`.
- Missing-log report/control digest:
  `b4b826ec4235131541ec56a619da6e2562aead7b30693b4e679ab5b2fd43f7c1`
  → `d6541e4acb8ffb5e89cb1ad2ed6bad8f120e973fbfd50e45a143905e06f4354a`.

These are report/control payload migrations, not new values substituted into
underlying identity goldens. The metric coexistence test likewise uses a fixed
execution ID when comparing complete report payloads across equivalent runs.

## 27. P1 exit-criteria assessment

The requested core semantic chain is implemented and locally exercised: typed
trace/metric contracts and normalization; deterministic checks; T/A provenance;
state-only persisted verification; one-hop bounded scope; per-attempt acquisition;
Snapshot V2 recorded replay; generic dependency hypotheses; logical TI scheduling;
bounded multi-HS persistence and honest failure-mode reporting.

Single-HS checkout/payment and frontend/catalog fixtures retain 2 rounds / 3 calls /
SUPPORTED / failure_mode / unresolved ultimate cause. Healthy/no-HS remains one
round / two calls. Multi-HS tests demonstrate shared acquisitions without a winner.

This closes P1 semantic architecture, not every historical operational checkbox
in the roadmap. Docker/live readiness, real incident collection, empirical RCA
accuracy, production deployment and formal benchmark validation are not claimed
by this local patch. Existing historical P2 scaffold entries do not mean new P2
work was undertaken here.

## 28. Deferred work and scope boundaries

No retries, automatic neighbor queries, multi-hop, native log Observation, metric
diagnostic plan, causal DAG, verified root cause, benchmark V2 or assessment-history
event sourcing was implemented. Live physical multi-seed capability is unchanged.
The existing scorer still uses a single-label fixture convention; existing
single-HS fixtures remain scorable. Multi-claim scoring is explicitly deferred,
and runtime does not choose a winner to satisfy `hypotheses[0]`.

No scope expansion beyond P1.7c. Stop here; P2 requires a separate task.
