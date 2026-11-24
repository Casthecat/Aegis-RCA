# P2.1 — Approved-only Stage C benchmark harness

This stage freezes architectural ablations of the current deterministic investigator.
It does not benchmark a historical log-RAG implementation, LLM hallucination, or
ultimate root-cause diagnosis. All primary variants run the same approved Snapshot
V1 bytes through ReplayBackend, offline, without LLM calls.

## 1. Files changed

New implementation:

- `src/aegis_rca/models/execution.py`: neutral frozen runtime execution policy.
- `src/aegis_rca/evaluation/stage_c_models.py`: variants, budgets, protocols, trial,
  admission/run/semantic/usage/manifest/failure contracts.
- `src/aegis_rca/evaluation/stage_c.py`: admission boundary, execution, immutable
  publication, reuse and paired experiment summary.
- `src/aegis_rca/evaluation/stage_c_scoring.py`: separate multi-claim scoring and
  paired scored dimensions.
- `src/aegis_rca/evaluation/stage_c_summary.py`: frozen/versioned bounded summary,
  explicit completion/paired denominators, distributions and comparison contracts.
- `src/aegis_rca/evaluation/stage_c_cli.py`: narrow formal CLI.
- `tests/test_stage_c.py`: synthetic admission, architecture, isolation, determinism,
  archive, scoring and paired-analysis regressions.
- `tests/test_stage_c_summary.py`: complete/partial/failed experiments, shared
  denominators, row references, signed deltas, distributions, pooled ratios,
  immutable deterministic summaries and bounded contract validation.
- `tests/test_stage_c_repeatability.py`: self-archive comparison rejection and
  independently executed same-trial consistency checks.

Modified implementation:

- `src/aegis_rca/runtime.py`: Dependencies carries the neutral policy; initial
  portfolio refresh uses its admission policy.
- `src/aegis_rca/graph.py`: required scheduling respects the neutral switch.
- `src/aegis_rca/evaluation/benchmark.py`: ApprovedCandidate additionally returns
  `snapshot_sha256` calculated from the bytes already loaded and validated.
  Existing admission_metadata output, scanner, sanitizer and approval rules stay unchanged.
- `src/aegis_rca/evaluation/__main__.py`: dispatches the four Stage C commands;
  legacy offline `score` remains available and unchanged.

Documentation: this file and `docs/roadmap.md`. No existing test/golden expectation
was changed to accommodate this stage.

The summary/repeatability hardening is confined to evaluation, tests and these
documents. It does not migrate runtime identities, the structural hash algorithm,
BV/BB/BEP/BSP declarations, BRK/BRS schemas or existing execution artifacts.

## 2. Exact frozen B0/B1/B2 definitions

- **B0 / survey_stop:** HP active limit 1; initial metric(original seeds) and
  trace(original seeds); required-check follow-up OFF. Acquired facts still pass
  through the current deterministic portfolio, verifier and report. LLM OFF.
- **B1 / single_hypothesis_check_driven:** HP active limit 1; same initial survey;
  required-check follow-up ON. Current deterministic verification/report; LLM OFF.
- **B2 / full_p1:** production HP default limit 4; same initial survey; required
  scheduling ON; current multi-HS evaluation, shared TI and continuation/report
  semantics. LLM OFF.

There is no stop-on-first-supported treatment. Structural active-HS ordering is
unchanged; no private label controls which edge receives the single-HS slot.

## 3. Scientific question: B0 → B1

This isolates missing-required-check acquisition after the initial survey. Both
variants admit one deterministic hypothesis and have exactly the same resource
budget. Unused B0 budget remains unused. This is not a comparison to one-shot
log-RAG or a historical LLM baseline.

## 4. Scientific question: B1 → B2

This isolates bounded competing-hypothesis evaluation with shared logical
acquisitions. Both use the same missing-check mechanism and budget. In the
regression with frontend→auth and frontend→catalog UNAVAILABLE, but only a catalog
connection-refused log, B1's structurally first auth hypothesis remains unresolved;
B2 preserves auth as unresolved and verifies catalog. Both use three physical
calls, including the same caller/window log intent shape. This is a diagnostic
coverage difference without a root-cause winner.

## 5. Neutral production runtime boundary

`InvestigationExecutionPolicy` is frozen/versioned and contains only
`required_check_scheduling` and `hypothesis_policy`. No B0/B1/B2 names, benchmark
flag, label, expected edge or budget enters production graph decisions. B2's
runtime policy equals `DEFAULT_EXECUTION_POLICY`, including its serialized bytes.
Formal execution creates Dependencies with `reasoner=None`; optional production
reasoner behavior is unchanged. The runtime policy does not encode a benchmark
identity or a private scenario.

The explicit existing refresh policy override remains available; an existing
portfolio preserves its chosen policy across acquisition refreshes. Formal runs
start with fresh state so their initial bound comes from the selected neutral policy.

## 6. Proof B2 equals the production default

The registry's B2 neutral policy compares equal to `DEFAULT_EXECUTION_POLICY`
as a frozen model and as serialized model bytes. The regression
`test_variant_registry_is_structural_and_budget_is_separate` asserts both.
Production receives that policy through Dependencies and has no variant-key branch.
The summary and scoring hardening does not change this policy or any execution path.

## 7. BV / BB / BEP / BSP

BV hashes architectural semantics: schema, runtime mechanism switches/admission
bound, initial survey definition and LLM OFF. Display key/name are excluded. Budget
values are absent from BV. Registry IDs:

- B0: `BV-e2a36cc8655540d2eab82f663b1252ae6c3c76b27c1f3e80fbf6f9b82b418056`
- B1: `BV-45e4972f7a1c94c3feb9c6f9b546471334e37f562eb8f0731c534f7200fd82d1`
- B2: `BV-728e8e179311d1d519bf76d493cfda4110b4700877a916dc2dc50f9f9c379058`

`BenchmarkBudgetSpec` freezes 3 rounds, 6 calls, 5-second tool timeout and a
120-second global deadline, shared by every primary variant:
`BB-9e94e60d067ec0a721ff82e033f86da064c828d348b4980b0ab4ca003f110822`.

BEP binds approved Stage C, V1, variant/budget schemas, exact-object replay/no
reread/no Live/no LLM rules and implementation revision p2.1-v1:
`BEP-e73516d551d3ec56cb5becb81768d6980c7f853f3cc885fd704889c3f2e65250`.

BSP separately binds the score schema, legacy non-exhaustive Label expectation,
multi-claim metric definitions, extra-edge restraint and scoring revision:
`BSP-67b7d104807c65947098fe28efdc5dc971376aa3dafdd84ea03658e050f80daa`.
Scoring revisions do not enter BRK or BRS. A revision tag records scoring
provenance; it does not invent a different algorithm. Future algorithm/schema
changes must explicitly version their protocol definitions.

## 8. BRK / BRS / archive identities

`BenchmarkRunKey` binds canonical candidate UUID, exact admitted bundle/snapshot
hashes, BV, BB, BEP and TrialSpec. It identifies a trial, not its outcome or duration.

`BenchmarkSemanticResult` binds BRK, execution namespace, report/termination,
current portfolio/assessments/PHVs, ordered physical acquisitions, TI dispositions,
stable usage and structured failure categories. It excludes wall-clock timing and
the graph's elapsed-time event stream. No raw telemetry is duplicated.

`BenchmarkRunResult.archive_sha256` is the SHA-256 of the canonical complete
payload excluding that digest field itself. `run.sha256` additionally stores the
full SHA-256 of the exact `run.json` bytes, including the embedded digest. Readers
validate both, and require canonical JSON bytes. Thus whitespace, duration or
payload changes cannot silently pass byte validation. Same BRK/BRS can coexist
with a different archive digest when only timing differs.

## 9. Deterministic execution namespace

Primary TrialSpec requires integer `trial_index=0`, deterministic=True. Runtime
`execution_id` is exactly BRK. Re-executing the same trial in a fresh archive root
therefore reproduces A namespaces and other provenance IDs when the observed
execution outcome is the same. No random execution UUID enters BRS.

Reruns measure deterministic consistency; they are not independent statistical
episodes. Actual timeout/deadline outcome differences remain different semantic
results and are not normalized away. Timing itself is archival only.

## 10. Approved-only admission and no reread

The public formal execution API accepts `(recordings_root, canonical_candidate_uuid,
variant_key, trial_index, output)` and calls only `open_approved_candidate` for
telemetry admission. It accepts neither a snapshot path nor a TelemetrySnapshot
argument. The internal execution boundary receives the trusted admission result
and no filesystem roots or label arguments.

Runtime constructs `ReplayBackend(admitted.snapshot)` with the exact object parsed
from validated candidate bytes. No snapshot/candidate/review file is reopened after
admission. A multi-variant experiment admits each candidate once before execution
and passes that same in-memory object to all variants. The manifest freezes the
public admitted hash references, not source mappings or private review contents.

The admission implementation itself necessarily reads review/decision metadata
to enforce approval. That read is confined to admission; no such metadata or
paths pass into runtime. Pending, rejected, stale or mutated candidates fail before
any acquisition. No review is auto-approved to obtain a real smoke sample.

## 11. V1-only formal candidates

Existing V1 sanitizer/scanner/manual review are reused without rule changes.
Snapshot V2 is rejected by that existing admission path. Formal V1 replay creates
current T/A execution records; it does not fabricate historical T/A provenance.
Engineering V1/V2 replay remains separate and non-benchmark.

## 12. BenchmarkRunResult

The frozen/versioned archive contains the nested run key, admitted public hashes,
variant, budget, execution protocol, trial, semantic result and their IDs, plus
duration and archive digest. The semantic result contains report, bounded current
portfolio/PHVs (no raw telemetry), attempted TaskSpecs and AttemptRecords in
reservation order, intent records, counts and categories. Counts are validated
against acquisition/ledger records; claims must reference the exact supported
PHV set and its PR/CR identities.

All three variants use the actual current verifier. B0's unqueried log check stays
UNKNOWN, not a fabricated PHV or artificial failure. For invalid triage there is no
applicable portfolio (`null`) or PHV (`[]`); no missing internal object is invented.

## 13. BenchmarkScoreResult

Scoring is a separate frozen artifact with BSP, BRK/BRS, the run archive reference,
candidate UUID, a digest of private label bytes, and explicit dimensions. Its BS
identity changes with scoring protocol/label/result content. Label values and
private paths are not copied into the score artifact. Re-scoring writes another
score file and never modifies `run.json` or `run.sha256`.

The existing `score_episode` and legacy `python -m aegis_rca.evaluation score ...`
remain unchanged. Stage C never reads display `hypotheses[0]`; it resolves every
verified claim's HS through the current structured assessments.

## 14. Structured failure-analysis taxonomy

The closed vocabulary is:

- `no_hypothesis_candidate`
- `variant_did_not_schedule_required_intent`
- `required_acquisition_unavailable`
- `replay_selection_ambiguous`
- `acquisition_failed`
- `budget_exhausted`
- `budget_prevented_required_acquisition`
- `evidence_acquired_check_unknown`
- `hypothesis_overflow`
- `unsupported_check_acquisition_mapping`
- `invalid_scope`
- `llm_error`
- `harness_error`

Runtime categories come from control, ledger, CheckResults and AssessmentLimitations,
not report prose. Required-unavailable is restricted to required-check consumers;
generic failed acquisitions also appear in usage counts. Global deadline is a
resource stop, and admitted timeouts retain their actual AttemptRecord status.
`llm_error` cannot occur in this LLM-off protocol. `harness_error` belongs to a
separate immutable experiment-cell failure receipt when admission/execution/archive publication
cannot yield a valid run. Receipts expose closed reasons, not exception payloads.
An honest INCONCLUSIVE is a valid completed run, not a harness failure. A failed
single-cell `run` exits without a valid result; summary counts that manifest cell
as incomplete, without manufacturing execution truth.

## 15. Scoring dimensions and UNSCORED

Every score explicitly contains all dimensions, each with a value or UNSCORED:
mechanism correctness, known-edge recall, supported-claim recall, edge precision,
false-supported claims, abstention, answerability restraint, root-cause overclaim,
calls, rounds, duplicate logical intents, intent efficiency, unavailable/failed/
ambiguous acquisitions, timeouts, LLM calls/tokens/cost and deterministic consistency.

Mechanism matching is structural, restricted to an attributable labeled edge (or
unique claim). Known-claim recall refers only to the known identifiable label,
not an exhaustive claim set. Abstention is observed independently of correctness;
restraint can be evaluated for symptom-only/no-incident labels. Intent efficiency
means unique physically attempted logical intents divided by physical calls,
not information gain. Deterministic consistency requires a separately executed
same-BRK comparison archive; one run or a cached reuse alone is insufficient.
Passing the same resolved archive path as `--comparison-run` is rejected before
label access. An identical archive copied to another path, or the same archive
object supplied directly to scoring, leaves this dimension UNSCORED. A distinct
archive is necessary but is not an attestation that execution actually occurred;
the comparison must come from an independently executed rerun. If such a rerun
produces identical complete archival bytes, the score conservatively stays UNSCORED.

No composite accuracy number is produced. Structural correctness does not prove
semantic entailment, and this matrix does not measure LLM hallucination.

## 16. Multi-claim scoring

Stage C selects every `verified_failure_mode` claim, resolves its hypothesis ID
through structured assessments, and compares HS caller/callee/failure_class with
permitted label semantics. It never selects a display winner or `hypotheses[0]`.
A missing supported claim can score zero known-edge recall while mechanism
correctness remains UNSCORED because there is no attributable claim to evaluate.
Claim counts and candidate-level rates are kept separate in the summary.

## 17. Non-exhaustive edge labels

P2.1 reads the existing Label schema. `observed_edge` identifies one known edge;
it does not enumerate every valid edge. Therefore extra-edge precision/overclaim
is UNSCORED for these labels. A different supported edge is not automatically
false. An explicit `no_incident` label can score every supported claim as overclaim.
Root-cause overclaim is independently checked from diagnosis_level/underlying_cause.

Legacy `payment_connection_failure` is mapped only in the private scoring adapter
to `rpc_connection_establishment`. This private spelling never enters execution.
`expected_edges`/`edge_labels_exhaustive` are not added in P2.1.

## 18. Paired analysis

The immutable experiment manifest freezes candidate IDs, public admitted hash
references, variants, trial, budget and BEP before execution. Summary validates
each run against that manifest; it does not treat directory existence as success.
Primary complete-case comparisons require valid runs for all requested variants
and identical candidate admission metadata. Candidate names alone are insufficient.

`ExperimentSummary` is a frozen `schema_version=1` contract. It allows at most
256 candidate IDs, three registered variants and 32 scoring protocols. Thus an
experiment has at most 768 requested trial cells. Candidate IDs, paired rows and
comparison rows have deterministic order. Contract validation checks counts,
rates, cost distributions and signed deltas against their constituent rows.
`summarize()` returns this model's JSON-compatible dictionary and does not modify
run, score or manifest artifacts. It is a post-execution view of the artifacts
present when read, not a transactional monitor: a concurrent publication can
temporarily appear incomplete until both archive members have been published.

Counts distinguish admission, attempted work and completed execution:

- `candidate_count` is the frozen requested candidate-set size.
- `admitted_candidate_count` counts public admission references in the manifest.
  Each variant has this same admitted count; it is not a re-admission during summary.
- `requested_trial_count` is candidate count times requested variant count.
- `attempted_trial_count` counts distinct cells with a valid run, a validated
  immutable harness-failure receipt, or an existing output at that cell's expected
  canonical BRK. Admission rejection with a failure receipt counts as attempted.
  A frozen manifest alone does not. A partial BRK directory counts as attempted
  but cannot count as completed; an unrelated output directory creates no trial.
- `completed_trial_count` counts valid BenchmarkRunResults. Honest INCONCLUSIVE
  results count as completed. Per-variant `valid` retains the same meaning as
  `completed` for compatibility.

Per-variant completion and harness-error rates expose `{k,n,value}` using the
requested candidate count as denominator. Harness errors count the union of cells
with validated failure receipts and attempted cells lacking a valid completed
archive, so a partial/corrupt canonical output also counts as a harness error.
A cell present in both groups counts once; no receipt is fabricated. An
unattempted manifest-only cell has zero harness errors and remains incomplete.
Incomplete cells must be unique, requested by the manifest and reconcile with
completion counts. Completed counts cannot exceed admitted counts. Incomplete
trials, invalid archives and harness receipts remain in the intention-to-test
view. Repeated or conflicting artifacts do not add samples.

All primary paired statistics use ONE global complete-case subset: every compared
variant must have a valid run for a candidate with identical admitted bytes.
Tool-call and round distributions expose `{n,total,mean,median}` on that subset;
paired SUPPORTED, INCONCLUSIVE, abstention and structural root-cause-overclaim
rates use the same denominator. When it is empty, rate values and means/medians
are null, counts are zero, and no success rate is fabricated. Primary LLM usage is
explicitly zero. Variant and global limitation histograms count each category at
most once per observed candidate/variant cell, including `harness_error` receipts;
canonical partial/corrupt attempted cells also contribute `harness_error`. They
are not per-HS event counts and do not imply disjoint categories.

`paired_rows` records each paired candidate's BRK/BRS, report status, calls,
rounds, supported-claim count and structural failure categories for each variant.
When both relevant variants were requested, `deltas` contains B0→B1 and B1→B2 in
that order. Each row is signed **right minus left** on the same candidate for
calls, rounds, supported claims and the SUPPORTED indicator; means use those
same rows. A positive cost delta means more calls/rounds, not automatically an
improvement. Empty comparisons have no rows and null means. A two-variant
comparison still cannot substitute a different subset for another comparison in
the same requested experiment.

Optional score artifacts under `EXPERIMENT_ROOT/scores/` are grouped by BSP. Paired
score comparisons require the same complete-case candidates and same private label
digest across variants. Each metric additionally uses the shared subset scored
by all variants and exposes its own denominator. If B0 abstains, conditional
mechanism correctness may be UNSCORED while known-edge recall can still be zero.
No asymmetric hidden denominator is used to make a variant look better.

Each metric aggregate exposes `status`, `k`, `n`, `total`, `mean`, `median`, an
optional pooled ratio and `any_positive_rate`. Their meanings are distinct:

- `k=total` is the sum of candidate metric values, and `n` is the eligible paired
  candidate count. `mean=k/n` is the candidate macro mean. For count dimensions,
  k may exceed n; for fractional metrics, k need not be an integer.
- `pooled_k/pooled_n` pools the original per-run numerator/denominator only when
  all eligible score dimensions explicitly provide them. For mechanism ratios,
  this is a claim-level ratio and need not equal the candidate macro mean.
  Inapplicable pooled numerator, denominator and value are all null.
- `any_positive_rate` is the number of candidates with a metric value greater
  than zero divided by eligible candidate count. For `false_supported_claims`,
  it is an overclaim occurrence rate; total false claims remains a separate count.
  Positive has no universal "better" interpretation across metrics.

UNSCORED aggregates have `n=0`, `k=total=0` and null mean/median; a zero total with
no eligible denominator is not a zero-error result. Per-protocol metric deltas
also retain explicit eligible candidate rows and signed right-minus-left values.
They use the common BSP, label-digest and metric eligibility subset, never a
different pairwise denominator. No score files yields an empty protocol list,
not fabricated scores. More than 32 BSP groups is rejected instead of silently
truncating the summary.

## 19. Ground-truth isolation

Execution receives admitted public telemetry, architectural policy, budget and
execution/trial metadata only. The CLI run/experiment commands have no labels-root,
review-path, episode-ID or raw-snapshot option. Score-run reads a private
`LABELS_ROOT/<candidate_uuid>.json` only after validating a completed run. Source
episode/fault mappings are outside the execution and public run artifacts.

Tests spy on file access after admission and mutate the source file after the
validated object is returned. Runtime continues from the admitted object without
reopening recordings or private roots. CLI unexpected exceptions are sanitized.

## 20. Offline execution

Formal code constructs ReplayBackend internally, with no backend-injection or
LiveBackend option, and no model provider. No Docker or OTel service is required.
Ambient LangSmith tracing is explicitly disabled for the formal run so an
environment setting cannot add remote tracing calls. Tests prohibit socket
connections and LiveBackend construction with ambient tracing enabled. Primary
LLM calls/tokens/cost and network usage are zero.

## 21. Immutable/atomic storage and reuse

`experiment.json` is immutable. `runs/<BRK>/run.json` is reusable only when its
contract, requested BRK, BRS, embedded digest, canonical bytes and full byte
receipt validate. It is not overwritten to refresh duration. The publication
primitive fsyncs a temporary file and links it exclusively into place; existing
targets are never replaced. `run.sha256` is published first, with run.json last;
missing either member is partial, not complete.

An interrupted or corrupt output is rejected and preserved for inspection. This
stage does not automatically repair it or retry a failed experiment cell. Use a
new experiment root for an explicitly requested rerun; same deterministic trial
semantics still give the same BRK. A concurrent publisher can lose an exclusive
publication race and report failure; this is not a distributed job queue.
Formal output roots must be outside recordings_root so result publication cannot
add files to an immutable candidate, quarantine or review directory.

## 22. CLI surface

Replace placeholders with an actually approved canonical candidate UUID. Existing
local unapproved candidates cannot be used as smoke inputs.

```powershell
python -m aegis_rca.evaluation run --recordings-root data/recordings --candidate <APPROVED_UUID> --variant B2 --trial 0 --output artifacts/formal-single
python -m aegis_rca.evaluation experiment --recordings-root data/recordings --spec spec.json --output artifacts/formal-matrix
python -m aegis_rca.evaluation score-run --run artifacts/formal-matrix/runs/<BRK>/run.json --labels-root <PRIVATE_LABELS_ROOT> --output artifacts/formal-matrix/scores/<BRK>.json
python -m aegis_rca.evaluation summarize --experiment artifacts/formal-matrix
```

`score-run --run` accepts either the containing `runs/<BRK>` directory or its
`run.json` file. Both resolve to the same validated archive and required
`run.sha256` receipt; neither permits an arbitrary telemetry snapshot.

`spec.json` contains `{"candidate_ids":["<APPROVED_UUID>"],"variants":["B0","B1","B2"]}`;
optional trial is `{"deterministic":true,"trial_index":0}`. The parser forbids extra
fields, noncanonical UUIDs, duplicate candidates/variants and other trial indices.
A one-off `run` freezes a single-cell manifest; use `experiment` for a matrix,
rather than appending new cells to an already frozen single-run directory.

For a new score revision, use `score-run --scoring-revision <revision>` with a new
output file. `--comparison-run <separate-rerun-directory-or-run.json>` enables
same-BRK deterministic consistency scoring from an independently executed rerun.
Self-comparison is rejected and identical copied archives remain UNSCORED. A
changed duration is not a semantic inconsistency. Formal run/experiment never
accepts `--snapshot`.

## 23. Targeted validation

**347 passed, 1 skipped in 41.71s** (`artifacts/p21-revised-targeted-final.log`). Selection:
Stage C, benchmark admission, sanitization, legacy evaluation, scheduling,
portfolio, workflow, connectivity wiring, recorded replay and Snapshot V2.
The included summary/repeatability checks cover both two-candidate
cost statistics and the distinction between cached reuse and a new execution.

```powershell
$p21Temp = 'artifacts/t21-' + [guid]::NewGuid().ToString('N').Substring(0,12)
.\.venv\Scripts\python.exe -m pytest tests/test_stage_c.py tests/test_stage_c_summary.py tests/test_stage_c_repeatability.py tests/test_benchmark_admission.py tests/test_sanitization.py tests/test_evaluation.py tests/test_scheduling.py tests/test_hypothesis_portfolio.py tests/test_workflow.py tests/test_connectivity_wiring.py tests/test_recorded_replay.py tests/test_snapshot_v2.py -q --tb=short --basetemp $p21Temp
```

The short basetemp avoids the Windows path-length limit in existing deeply nested
approval-test fixtures; the final run uses the unmodified admission helpers.

## 24. Full pytest

**1120 passed, 1 skipped in 72.84s** (`artifacts/p21-revised-full.log`). Baseline:
1060 passed, 1 skipped; 60 additional passing Stage C cases (42 initial cases,
12 summary cases and 6 repeatability/directory-scoring cases). The sole platform
skip is Windows symlink creation privilege. It is not a failed benchmark or a
substitute for live telemetry validation.

```powershell
$p21Temp = 'artifacts/f21-' + [guid]::NewGuid().ToString('N').Substring(0,12)
.\.venv\Scripts\python.exe -m pytest -q --tb=short --basetemp $p21Temp
```

## 25. P1 regression status

Existing P1 expectations are retained, including default fixture 2 rounds / 3
calls / SUPPORTED / E-T1,E-L1 and unchanged underlying identity goldens. No
HS/C/P/CR/PR/TI/T/A/O/SX/SIS/SB/RM hash implementation or Snapshot V1/V2 schema
changed. Formal BRK-derived executions naturally have new A namespaces; this does
not migrate existing P1 identities for unchanged execution inputs.
`artifacts/p21-revised-file-audit.json` records the exact changed/added files and confirms
81 protected existing implementation/test/example files remain byte-identical.

## 26. Real and synthetic smoke results

Local admission preflight found three existing candidate directories; none passed
`open_approved_candidate`. No real formal benchmark trial was run, and no existing
approval, quarantine source or private label was modified. The preflight records
only UUID/admitted boolean in `artifacts/p21-revised-smoke.json`.

Temporary test bundles use the existing synthetic export/review helpers. Single
edge: B0 2 calls / INCONCLUSIVE; B1/B2 3 calls / SUPPORTED. Multi-edge: B1 one
unresolved active HS, B2 one unresolved plus one supported HS, each with 3 calls.
Healthy: 2 calls / INCONCLUSIVE / no candidate. These are harness validation
results, not empirical real-world RCA performance or statistical episodes.
`artifacts/p21-revised-smoke.json` records the fresh CLI experiment with two
synthetic approved candidates across all three variants: 2 admitted candidates,
6 requested/attempted/completed trials and 2 common paired candidates. The
SUPPORTED counts are B0 0/2, B1 1/2 and B2 2/2. Mean calls are 2, 3 and 3;
mean rounds are 1, 2 and 2. B0→B1 mean supported-claim delta is +0.5 at +1 call;
B1→B2 is +0.5 at zero extra calls. Root-cause overclaim and LLM usage are zero.
Non-exhaustive edge precision/extra-edge overclaim remains UNSCORED.

The smoke exercised `experiment`, directory-form `score-run`, `summarize`, cached
reuse and a separate `run` in a fresh root. Scoring/reuse preserved all run bytes;
the independent B2 rerun preserved BRK, BRS and A records while its duration/archive
digest differed. The public smoke artifact contains references and summary only,
not private label values. These synthetic observations validate the harness;
they are not estimates of production performance.

## 27. P2.2 handoff

The next stage may supply independently collected, manually approved V1 candidates
and private labels keyed by opaque candidate UUID, preserving episode independence
and split discipline. Before edge precision can be scored, define/version an
exhaustive expected-edge label contract and update BSP explicitly. Do not silently
reinterpret a legacy single observed_edge as exhaustive truth. Freeze corpus,
manifest and scoring protocol before interpreting performance comparisons.

This handoff is documentation only. No new fault corpus, automatic approval or
P2.2 implementation was started.

## 28. Deferred work and scope deviations

No new predicates, native log Observation, metric diagnosis, retries, multi-hop,
benchmark V2 admission, causal DAG, root-cause verification, live deployment or
LLM baseline is included. Legacy scorer behavior is unchanged. LLM experiments
need separate documented provider/prompt/decoding/usage protocols and no hidden
fallback; this stage implements none.

The additive admitted snapshot hash and exact full-byte receipt make the requested
identity/admission guarantees explicit. They do not alter sanitization, scanning,
human-review semantics or public candidate bytes. No scope expansion beyond P2.1.
The bounded-summary and repeatability follow-up adds no runtime mechanism or
identity migration and does not begin P2.2.
