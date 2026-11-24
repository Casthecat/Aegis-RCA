# P1.4-3 Persisted Verification + Acquisition Binding

P1.6a now extends the same persistence boundary with an independent bounded
InvestigationScope; see [investigation-scope.md](investigation-scope.md).
`runtime.refresh_acquisition_state` resolves one neutral native generation and
passes it to connectivity refresh and scope derivation. Exact binding semantics,
VBS/VBE identities and connectivity consumers below are unchanged. Native trace
source/resolution helpers now live in trace_acquisition.py.

P1.4-3 replaces consumer-side connectivity evaluation with one current persisted
bundle, refreshed at acquisition completion. The baseline was 767 passed, 1 skipped.
No retries, P1.6, Snapshot V2, new predicates or metric extensions are introduced.

## Persisted contract and storage

`verification/persisted.py` defines `PersistedConnectivityVerification`:

- schema_version=1;
- state_basis: StateVerificationBasis, computed VBS-* identity;
- evaluation_basis: ResolvedEvaluationBasis, computed VBE-* identity;
- evaluation: ConnectivityVerification containing trace_check, log_check and
  their PlanVerificationResult.

IncidentState.connectivity_verification is a single nullable, non-reducer field.
It is never passed to merge_records. Each refresh replaces the entire bundle;
there are no independently mergeable CR/PR lists. Ordinary Pydantic JSON
round-trips recompute all IDs. CLI/API save the current bundle as
connectivity_verification.json (JSON null if no refresh occurred).

Only CURRENT verification is retained. Historical T-*/A-* acquisition provenance
remains available, but old verification generations are NOT guaranteed
reconstructible from IncidentState alone. Raw spans/Observation source material
are not duplicated into state. Historical verification persistence is deferred.

## VBS: authoritative state freshness

`state_verification_basis(state)` reads IncidentState only. It binds the current
catalog plan ID and incident window, sorted relevant TaskSpec IDs, and sorted
references pairing each relevant A-* with a semantic fingerprint of:

- task_spec_id, tool_status, truncated, coverage_complete, pre_capture_omissions;
- observation_binding, evidence_binding;
- source_material_digest, tool_result_digest;
- admitted_observation_ids and admitted_evidence_ids.

A-* already binds execution ID, T-* and ordinal. The TaskSpec ID binds its full
canonical acquisition semantics. No hashing or identity inputs for T/A changed.

Relevant attempts are selected deterministically by authoritative TaskSpec,
never tool-call alias: trace/log signal matching a connectivity check, service
intersection with checkout and overlapping half-open check/acquisition windows.
Metric attempts and obviously unrelated service/window attempts are excluded.
Partial overlapping acquisitions can supply positive scoped facts; existing
evaluator coverage applicability still determines which views enter coverage_used.
The adapter does not widen a partial acquisition to the incident window.

VBS also binds relevant current E-log payloads: all checkout log records in the
incident window and all records referenced by relevant log attempts. Including
historically referenced records outside the check window ensures that any
supplied attempt projection is fingerprinted. Each reference preserves the E-ID
and hashes the existing verifier semantic_payload: signal, service, timestamp,
and structured observation (code/peer). E-* itself remains a legacy citation ID.
Replacing semantic content under the same E-ID invalidates VBS.

Warnings, excerpts, source locators, hypothesis prose, citation ordering and
unrelated metric changes are excluded. Deterministic collection ordering removes
input ordering effects. Relevant authoritative state changes invalidate VBS.

## VBE: actual resolved evaluator inputs

Constructed only by refresh, VBE binds sorted actual supplied O-trace IDs,
supplied E-log ID/semantic fingerprint references and per-attempt resolution.
Each resolution carries the exact CheckCoverageView supplied to evaluation;
its identity excludes warning prose via identity_payload().

The closed resolution vocabulary is verified, source_unavailable,
source_digest_mismatch, observation_id_mismatch, evidence_missing, binding_unknown.
Failed resolution supplies no facts from that attempt and fact_set_complete=False.
The VBE supplied fact-ID sets must equal the unions of its per-attempt views.

The same IncidentState can produce different VBE identities if source resolution
succeeds on one refresh and fails on another. Its VBS can remain identical: VBS
describes authoritative state, while VBE records what was actually evaluated.

## Exact historical native binding

For each relevant trace A-* refresh resolves its T-* and reads the backend's
already-owned source in that TaskSpec's exact service/window scope. It computes
the unchanged P1.5 source digest before running observe_spans. Only a matching
digest permits observation production. The newly produced O-ID SET must then
equal the historical admitted_observation_ids SET exactly.

Both subset and superset fail. A failed attempt supplies zero native facts and
retains its original AttemptRecord unchanged. Another independently verified
attempt can still supply a positive fact. No E-to-O conversion occurs, and no
backend.query is issued during refresh or reporting.

## Attempt-specific legacy binding and coverage adapter

Log input comes only from authoritative Evidence whose E-ID was admitted by that
completed log attempt, whose signal is log, and whose service/time is within its
authoritative TaskSpec scope. All historical IDs required by the log projection
must be present. If any is unavailable, the entire attempt input is withheld.
An unrelated E-ID cannot substitute for a missing historical one. Each attempt
has its own view; one log attempt cannot borrow another's Evidence.

`attempt_coverage` maps:

- acquisition_id=A-* and query_identity=T-*;
- signal/services/window directly from TaskSpec;
- ok/empty to succeeded; error/timeout to failed;
- truncated and coverage_complete exactly from AttemptRecord, including None;
- pre_capture_omissions=unknown from current acquisition data;
- admitted_ids to exactly the facts supplied from that attempt;
- fact_set_complete=True only for a verified binding/projection.

Warnings never create not_configured or a completeness claim. ToolResult.empty
describes an empty legacy diagnostic projection and can still bind O-trace facts.

fact_set_complete means all recorded facts intended for this evaluator input
were supplied. It does not mean complete real-world telemetry acquisition.
A verified empty projection can be fact_set_complete=True. Even with explicit
coverage_complete=True, unknown pre-capture omissions prevent closed-world
absence, including with an exact query identity. Positive SUPPORT continues to
survive truncation and coverage_complete=None.

## Refresh ownership and consumers

Investigate subgraph: admit -> workers -> merge -> persist_verification -> END.
An empty admitted batch follows admit -> merge -> persist_verification -> END.
`graph.persist_verification` calls the combined acquisition persistence owner in
runtime.py, which supplies shared resolved traces to connectivity refresh.

Round 1 stores trace SUPPORT, log UNKNOWN, PR UNCERTAIN before hypothesize/verify.
Persisted trace SUPPORT drives the existing targeted log follow-up. Round 2
atomically replaces the bundle with both checks SUPPORT and PR SUPPORTED.

The runner's global-deadline recovery is the only other combined refresh caller. It first
merges recoverable worker completions, finalizes started unfinished attempts as
timeout/global_deadline, and merges the associated TaskSpecs/projections. It then
refreshes the bundle before audit/report. Existing deadline control semantics
are retained. No replacement query or retry is issued.

hypothesize, verify, form_hypotheses, finalize and audit_and_report now consume
persisted truth via current_connectivity_verification. The old live
evaluate_runtime_connectivity entry point is removed. Report/audit no longer
accept an independent connectivity result override or evaluate a log-only fallback.
Pure evaluate_connectivity remains available for pure tests and the refresh owner.

## Freshness and validation boundaries

`current_connectivity_verification` revalidates the bundle's intrinsic contract,
recomputes state-only VBS, requires exact VBS equality, then checks resolution
attempt references and coverage against authoritative state. It also verifies
that supplied E fingerprints remain present in state. It never accesses backend
source or evaluates predicates/aggregation.

Missing bundle is conservative None for direct report/audit and hypothesis calls;
report remains INCONCLUSIVE even if state contains legacy connection evidence.
Normal graph hypothesize/verify require the bundle and raise if absent. Stale
bundles always raise an invariant error, including direct report/audit; no
consumer silently refreshes. validate_execution_state also checks current bundle
freshness. Refresh deliberately validates acquisition state without trusting the
previous generation, which is expected to be stale after merging new attempts.

Changing backend source after persistence alone does not invalidate VBS or change
persisted truth. The last refresh remains authoritative until another explicit
refresh. This is not a continuously live backend view.

Intrinsic Pydantic validation checks catalog plan/check identities, check origins,
PR references to its own CRs, aggregation consistency, resolved-input unions,
and evaluation references/coverage against VBE. It does not pretend to know
external IncidentState. State-relative attempt existence, historical admission
equality and authoritative coverage validation belong to the accessor.

## Identity and behavior verification

Pure C/CR/P/PR goldens are retained, including native and legacy CheckResults.
The catalog and check/model hashing inputs are unchanged. O-trace/O-metric and
T/A algorithms remain byte-identical; existing golden tests are retained.

Runtime CR/PR IDs intentionally change when coverage_used carries real A/T
provenance. The new tests verify that equal persisted acquisition/fact inputs
produce equal runtime IDs, while the corresponding no-coverage CR/PR differ.
C and P remain identical in those comparisons. Different executions still get
different A-* namespaces and therefore can have different runtime CR/PR IDs.

The unchanged fixture/replay/live regressions verify fixture SUPPORTED with
2 rounds, 3 calls and E-T1/E-L1; missing log targeted once then INCONCLUSIVE;
wrong peer and healthy/empty replay INCONCLUSIVE; UNAVAILABLE plus connection log
SUPPORTED; paymentFailure gRPC UNKNOWN INCONCLUSIVE; metrics irrelevant;
no contradiction/rejection or retries. Existing complete report/control/citation
golden hashes remain unchanged. ReportClaim.evidence_ids still contains only E-*.

Targeted command uses tests/test_persisted_verification.py,
test_connectivity_wiring.py, test_acquisition_identity.py,
test_acquisition_hardening.py, test_check_results.py, test_check_legacy.py,
test_check_aggregation.py, test_check_reason_contracts.py, test_contracts.py,
test_workflow.py, test_api.py and test_live_replay.py.

Targeted result: **408 passed in 15.52s**. Full suite: **812 passed, 1 skipped in
19.38s**, a net addition of 45 cases. The only skip remains
tests/test_sanitization.py:387 (Windows symlink creation requires OS privileges).
Commands use .venv/Scripts/python.exe -m pytest -q -ra --basetemp
with a fresh project-local artifacts/pytest-p143-* directory.

## Scope and changed files

Added verification/persisted.py, tests/test_persisted_verification.py and this
document. Modified runtime.py, state.py, graph.py, runner.py, policy.py, report.py,
verification/connectivity.py, cli.py, api.py; migrated tests in
test_connectivity_wiring.py, test_acquisition_identity.py, test_contracts.py and
test_api.py; updated acquisition-identity.md and connectivity-wiring.md.

Before/after SHA-256 scope audit: 15 pre-existing files changed, 3 added and
113 pre-existing source/test/document/recording files remain byte-identical.
All 10 models files, all 5 observations files, the catalog, native evaluator,
legacy evaluator, aggregator and existing CheckResult reason/golden test file
are byte-identical. This includes the neutral structural identity helper and
acquisition/check models, in addition to the unchanged O/T/A golden assertions.

The pure connectivity wrapper gains optional coverage arguments; underlying
predicate evaluators and the catalog are unchanged. CLI/API add only the current
bundle artifact. Existing tests asserting no runtime coverage or consumer-side
re-evaluation were migrated to the newly required persistence contract; external
behavior and existing golden expectations were preserved.

No scope deviation: P1.6, retries, Snapshot V2, historical verification storage,
native log observations, new predicates, metric extensions, planner/LLM redesign,
scorer/citation migration and formal benchmark Stage C remain deferred.
