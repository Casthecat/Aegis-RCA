# P2.2 corpus protocol and delivery report

P2.2 makes the repository pilot-ready. It adds collection protocol, private truth,
blinded diagnostic Gold, corpus/split QA, and BSP-v2 tooling. It does not record
the Phase-A corpus or add investigation capabilities. No pilot episode was
recorded as part of this implementation.

The governing distinction is:

- **InjectionTruth** records what the operator changed and observed about the experiment.
- **BenchmarkGoldLabelV2** records what a human reviewer concludes the approved telemetry justifies under a frozen diagnostic rubric.
- **AdmittedTelemetry** is the approved V1 snapshot that Stage C may observe.

Neither an injection name nor an Aegis result determines Gold. The order is
approval, blinded answerability review, Gold freeze, split freeze, then declared
Stage C execution and performance interpretation. The [pilot runbook](pilot-runbook.md)
contains operator commands and the hard stops.

The subsequent [environment provenance hardening](demo-resource-profile.md)
binds the pilot's explicit DemoResourceProfile into CapturePolicy schema v2.
The current DRP-v2 provides checkout and product-catalog headroom after separate
direct upstream OOM observations; it changes profile data and CP identity only.
The original P2.2 delivery counts below are historical; the patch report records
its separate verification. No recording, injection, Gold label or corpus was
created during ENV_READY or this hardening patch.

## 1. Files changed

New evaluation modules provide `corpus_recording`, `gold`, `gold_scoring`,
`corpus_models`, `corpus`, `corpus_execution`, `corpus_production`,
`capture_freeze`, and `corpus_cli`. The existing
recorder gains policy-controlled and no-injection modes. Evaluation CLI dispatch,
Stage C scoring dispatch, and summary schema support integrate BSP-v2 while
preserving BSP-v1. New corpus, Gold, recording and scoring regression tests cover
the boundary. The final implementation audit lists the exact changed files and
test counts in [p22-file-audit.json](../artifacts/p22-file-audit.json).
The seven modified files are the recorder, evaluation CLI dispatcher,
`stage_c.py`, `stage_c_cli.py`, `stage_c_scoring.py`, `stage_c_summary.py`,
and the roadmap. The sixteen added files are the nine modules above, five test
modules (`test_corpus`, `test_corpus_recording`, `test_corpus_workflow`,
`test_gold_review`, `test_gold_scoring`), and this protocol/runbook pair.

## 2. Independent GoldDiagnosticRubric and GDR identity

`GoldDiagnosticRubric` is frozen/versioned; `GDR-*` hashes its diagnostic
definition, evidence rule, answerability and exhaustiveness rules, operation
policy and root-cause policy. The initial target is
`rpc_connection_establishment`: an identified caller attempted to invoke a
dependency but could not establish a usable RPC connection to that callee during
the incident window. This differs from an application returning an error after
the connection was established.

The reviewer considers endpoint attribution, timing, transport diagnostics,
corroboration, conflicting evidence and coverage. An attributable connection
refusal or name-resolution failure can establish this target. Availability
status alone can be ambiguous; topology alone is not evidence of failure.
These are diagnostic definitions, not aliases for a CheckSpec, SUPPORT result,
report, model answer or implementation path. P2.3 implementation changes do not
change existing Gold when GDR semantics remain unchanged.

The frozen v1 rubric identity is
`GDR-4f39dcda468d6319c95f36d6379fcfc786dd6ee4bbde79bc6d4bae8036497b20`.

## 3. Private InjectionTruth

`InjectionTruth` binds a private episode UUID, optional approved candidate UUID,
opaque scenario family and trial index, experiment/demo revision, supported
control and variant, private target/intention, original capture window/hash,
policy IDs, source recording identity, lifecycle timestamps, effectiveness,
readiness/recovery attestations, collection quality/errors and private notes.

The only implemented injection mechanisms are the already validated
`paymentUnreachable` and `paymentFailure`. Explicit no-injection records
`mechanism=none` and no effectiveness assertion. It is not a healthy diagnostic
label. Stage C execution and scoring do not accept InjectionTruth.

## 4. GoldLabelV2 and ExpectedDiagnosticClaim

Gold is keyed by canonical approved candidate UUID and binds admitted snapshot
and bundle hashes, sanitizer ruleset, GDR, capture/query policy IDs, blinded
packet identity, reviewer provenance, freeze time and private notes.

Each `ExpectedDiagnosticClaim` binds `failure_mode`, `caller` and `callee` in one
record. GDR-v1 leaves operation unconstrained; operation-specific expected claims
are not silently introduced. Claim identity uses `GC-*`; label identity uses
`GL-*`. Expected claims have deterministic ordering. There are no independently
maintained parallel mechanism/edge truth arrays. Edge metrics derive their sets
from these claims.

## 5. Four answerability states

- `mechanism_identifiable`: approved evidence justifies at least one target diagnostic claim.
- `symptom_only`: evidence supports incident symptoms but does not justify a target mechanism claim.
- `no_incident`: adequate successful business-path evidence supports no incident symptoms and a justified exhaustive empty target set.
- `insufficient_evidence`: evidence/coverage cannot support those determinations.

There is no `root_cause_identifiable` state. Multiple expected claims do not form
a separate answerability class. Private injection effectiveness cannot upgrade
`insufficient_evidence` to an identifiable mechanism.

## 6. Claim exhaustiveness

With `claim_labels_exhaustive=false`, expected claims are known positives.
Recall can be scored; additional runtime claims are not automatically false.
Claim/edge precision and extra-claim false-support remain UNSCORED.

With `true`, the reviewer asserts the complete expected target-claim set under
the GDR for this approved episode. Only this permits set precision and
false-supported claim counts. A single injected control does not justify an
exhaustive label. Non-mechanism answerability restraint is reported separately
from set precision, so an epistemic overclaim indicator is not presented as a
known extra-edge false-positive count.

## 7. Blinded review workflow

Reviewer A may inspect private injection/production facts to assess timing,
effectiveness, collection stability and recovery. Reviewer B receives only the
approved candidate, GDR and snapshot-derived coverage metadata, together with
the frozen collection policy. The review-package function has no InjectionTruth
or Stage C result input. The initial decision template is deliberately invalid;
it does not preselect an answerability decision or mark review complete.

Reviewer B must not see injection truth or B0/B1/B2/report output before Gold
freeze. A single-person procedure is **weak-blind**, not independent double
review: seal truth, review anonymized approved telemetry, record prior knowledge
honestly, freeze Gold, then unseal for production QA reconciliation. Unblinding
cannot upgrade diagnostic truth merely because the operator knows the fault.

## 8. Controlled lifecycle

ENV_READY → STABILIZE → optional INJECT → fixed WARMUP/onset confirmation →
CAPTURE → fixed INGEST_WAIT → RESTORE in finally → COOLDOWN/recovery QA →
SANITIZE → SCAN → HUMAN full-snapshot/leakage REVIEW → APPROVE or REJECT →
BLINDED ANSWERABILITY REVIEW → GOLD FREEZE → SPLIT FREEZE → later checkpoint.

Human approval and Gold are distinct gates. Restoring flags after capture is
not approval. A successfully written snapshot is not a successful experiment if
recovery or source QA failed. Failed attempts remain in the private ledger.

## 9. Episode independence

Corpus inclusion requires restored previous faults, pre-health and recovery
health QA, elapsed frozen cooldown, the declared capture duration and a distinct
quarantine recording. Capture windows may not overlap. Source hashes, candidate
snapshot hashes and telemetry fingerprints must be distinct. Fingerprints omit
incident/window metadata, so changing a snapshot wrapper cannot turn the same
captured records into an independent episode. One long faulty recording cannot
be sliced into several independent corpus trials.

Repeats from one scenario family are within-family robustness observations, not
additional mechanism families.

## 10. Neutral query policy

The exact Phase-A query is:

> Investigate service degradation during this incident window.

`QP-*` freezes this wording and the incident-entry seed policy. Corpus QA and
review packaging reject alternate or fault-hinted query text. The recorder uses
the neutral query from the start. It does not rewrite old snapshots or alter
`control-metadata-v1` sanitization to rescue hinted historical candidates.

## 11. Seed policy

The Phase-A entry seed is `checkout`, representing the business incident/alert
context. The operator records a private justification. Knowing that an injection
targets payment does not authorize using payment as an incident seed. This
corpus policy does not change the general production runtime's service rules.

## 12. Pilot capture/truncation policy

`CapturePolicy` schema v2 has a structural `CP-*` identity and requires an
explicit `demo_resource_profile_id`. The bound frozen DemoResourceProfile has a
`DRP-*` identity covering its version, profile name, pinned Demo version and
canonical service overrides. Changes to the resource profile change DRP and CP;
old policies without the binding are rejected rather than silently upgraded.

The application remains OpenTelemetry Demo 3.0.0. Upstream checkout used `20M`
container memory and `GOMEMLIMIT=16MiB`; ENV_READY produced a directly observed
cgroup OOM (`ExitCode=137`, `OOMKilled=true`) after approximately 11 minutes,
before any pilot capture. Checkout then ran healthy under the checkout-only
DRP-v1 with live `HostConfig.Memory=134217728`, `GOMEMLIMIT=100663296B`.
Product-catalog retained upstream `20M` / `GOMEMLIMIT=16MiB` and later supplied
direct evidence from its fresh current container:
`StartedAt=2026-09-19T18:58:44.684288111Z`, `RestartCount=0`, `Status=exited`,
`OOMKilled=true`. The prior absence of a Docker-events result does not override
that current-container state.

DRP-v2 therefore overrides exactly checkout and product-catalog with 128 MiB
container memory and `GOMEMLIMIT=96MiB` each, canonicalized as 134217728 and
100663296 bytes. All other services retain upstream resolved resources. These
two observations do not establish that other 20M services are unstable. The
profile provides environment-stability headroom; it is not a benchmark
treatment or a guarantee of readiness. DRP-v1 remains unchanged and available.

`demo.py prepare --resource-profile ...` explicitly applies the profile to
resolved upstream Compose and publishes a provenance receipt binding pinned
Demo version, upstream source marker, DRP and generated Compose digest. Formal
recording rejects absent or inconsistent provenance before backend discovery or
fault mutation. The validated receipt is retained in the private episode
directory. This binding verifies prepared artifacts; operators must still bring
up the prepared environment and independently verify running service health.
Human readiness attestation cannot substitute for the structural validation.

The zero-recording `artifacts/p22-pilot-01` and `artifacts/p22-pilot-02` workspaces
are preserved unchanged. `artifacts/p22-pilot-03/protocol` contains the newly
initialized DRP-v2 and bound capture policy; no old policy is migrated. If resource settings
change after collection starts, create new DRP/CP identities and a new
capture/corpus version; do not silently mix environments under one version.

CapturePolicy also pins warmup,
capture duration, ingest wait, cooldown, result limits, metric step, HTTP byte
bound/timeout, health protocol and truncation treatment. The initial pilot uses
30 seconds warmup, a 60-second capture, 15 seconds ingest wait, 30 seconds
cooldown, max_results 100, metric step 15 seconds, response bound 2,000,000 bytes,
and request timeout 5 seconds. These are pilot inputs, not an assertion that
60 seconds is optimal or sufficient.

Pilot QA examines workload readiness, ingestion, record volume, collection
errors, unknown/incomplete coverage, truncation, limit pressure, source stability
and sanitizer behavior. Longer windows may increase truncation; limits must not
be enlarged episode by episode to obtain SUPPORT. Final Phase-A capture policy
is frozen only after human inspection of system-independent pilot QA. Aegis/B2
performance must not decide the window or limits.

## 13. Healthy-control semantics

`record_episode --no-injection` uses the same lock, backend discovery, operator
health checks, fixed-window metric/trace/log capture, ingest wait, restoration
verification, cooldown and recovery QA as injected episodes. It performs no flag
writes, including during restoration. It records `mechanism=none`, not Gold.

An empty injected-control field never proves absence of an operational failure.
No-incident Gold additionally requires sufficient admitted coverage, justified
successful business-path evidence and an exhaustive empty expected-claim set.
Natural failures must be labeled honestly or excluded as source instability.

## 14. Insufficient evidence

Natural collection failure, truncation, incomplete coverage or missing logs may
justify `insufficient_evidence` under the rubric. No code deletes just the log
record needed by an Aegis check. Any future evidence ablation must be explicitly
synthetic and confined to SMOKE/DEV, not presented as independent real collection.

## 15. Real multi-edge limitation

The corpus protocol fixes `planned_real_multi_edge=0`. Current fault controls do
not deliberately create a second dependency fault. Naturally observed additional
claims may be included only after independent Gold review. Synthetic multi-edge
fixtures remain SMOKE. If the real corpus has no multi-claim episode, it cannot
establish a real B1→B2 competing-hypothesis benefit.

## 16. Sanitizer/ruleset freeze

Formal Phase-A targets `control-metadata-v1`. Mixed sanitizer rulesets are
rejected within a frozen corpus. Sanitizer matching rules and scanner caps are
unchanged. A new leakage form requiring semantic changes requires a new ruleset,
re-sanitization, review and labeling, followed by a new corpus version.

## 17. Human approval enforcement

Corpus inclusion and Gold packaging use `open_approved_candidate` and revalidate
current immutable admission hashes. Missing/pending/rejected/stale approval
cannot be replaced with a production status string. Corpus tooling never marks
`full_snapshot_reviewed`, decides leakage dispositions or approves a candidate.
The separate Gold workflow accepts an explicit human decision only after this
admission gate.

## 18. CorpusProductionRecord

Every attempted recording has a private production ledger, including readiness,
lock, onset, capture and recovery failures. Closed statuses cover recording
failure, quarantine, sanitization/scan/review, approval, Gold labeling, inclusion
and exclusion. Closed reasons include structural leakage, destructive sanitizer,
insufficient telemetry, capture/source failure, review rejection, ineffective
injection, policy/overlap/query violations, duplicate slices and access violations.
Progress updates do not mutate original quarantine telemetry.

## 19. Execution and private manifests

`ExecutionCorpusManifest` exposes only corpus identity/revision, approved opaque
candidate IDs and admitted public hashes, split assignments, V1/sanitizer
requirements and capture/query/scoring protocol IDs. It contains no family/fault
names, expected claims, truth or private review paths. **Execution-safe does not
mean human-public**, particularly for sealed holdout IDs and telemetry.

`PrivateCorpusManifest` binds episode/candidate/family/trial mapping, private
truth and production references/digests, frozen Gold references/digests and split
protocol. QA and private manifests never enter Stage C execution. The execution
loader reads only its projection, receipt and access/checkpoint records.

## 20. Corpus and split identity

`CORP-*` binds the corpus protocol/revision, approved bytes, split mapping,
Gold/rubric and label digests, unchanged BV-B0/B1/B2, BB/BEP, and collection/query/
sanitizer policies. `ECP-*` identifies the execution projection. Freeze publishes
canonical artifacts with a completion receipt last; partial output is not a
completed corpus, and a valid completed version is not overwritten.

Changing membership, split, label semantics or bytes requires a new corpus
version. A performance-informed edit cannot retain the old experiment claim.

## 21. DEV and holdout discipline

- **SMOKE** contains synthetic harness checks, never real performance evidence.
- **DEV** is visible and may inform P2.3 engineering.
- **REPLICATE_HOLDOUT** contains independent episodes of known families. A family may span DEV and this split only under a trial rule declared before capture, with holdout telemetry, labels and identities sealed.
- **FAMILY_HOLDOUT** contains families unseen during runtime/predicate tuning. A family cannot also occur in DEV or replicate holdout. An empty positive family holdout is valid, but supports no unseen-family generalization claim.

Use separate custodian/reviewer/developer accounts and restricted directories or
mounts. Do not give developers the holdout telemetry merely because labels are
hidden. At a declared checkpoint the harness may access approved bytes without
human inspection. Access declarations are append-only and human tuning access
invalidates the affected split. The tooling validates declared events; it is not
an OS access monitor and cannot detect undeclared copying. Filesystem/process
isolation and honest operator declarations remain necessary.

Corpus freeze requires an explicit `performance_results_unseen=true` human
attestation. Holdout sealing defaults to false; holdout inclusion requires an
explicit sealing declaration, and each replicate trial rule requires its own
explicit seal assertion. These fields do not install filesystem ACLs.

## 22. Corpus QA summary

QA reports attempted/recorded/sanitize-rejected/scan-blocked/review-rejected/
approved/included counts, answerability and diagnostic-family distributions,
splits and policies, seeds, durations, truncation and collection errors, rejection
reasons, real multi-edge count, injection-effective-but-insufficient-evidence
count, and unseen-family positive count. All failed attempts must be supplied;
denominators must not omit inconvenient episodes. Pilot QA can run before Gold
or split freeze, including an all-failed pilot, without inventing labels.

## 23. BSP-v2 scoring

`BenchmarkScoringProtocolV2` has its own BSP identity and consumes only
GoldLabelV2 after immutable Stage C execution. It matches all structured verified
failure-mode claims by mechanism and directed edge, using GDR's unconstrained
operation projection. It does not inspect `hypotheses[0]`, injection metadata or
report wording to determine truth.

Known-positive claim/edge recall works with non-exhaustive labels. Set precision
and false-support require exhaustive labels. Separate metrics represent
non-mechanism answerability restraint/overclaim, abstention and structural
root-cause overclaim. Resource, acquisition and determinism dimensions retain
P2.1's distinct meanings. Unsupported dimensions are explicitly UNSCORED.
`--label-schema gold-v2` and `legacy-v1` reject mismatched label schemas. BSP-v1
semantics remain available unchanged. Scoring never mutates run.json.

## 24. P2.1 integration

Formal execution still accepts only a currently approved canonical candidate UUID
and the exact admitted in-memory V1 object. V2 engineering archives remain
ineligible candidates. B0/B1/B2, BB/BEP, BRK/BRS/archive identities, deterministic
execution namespaces, replay-only behavior and paired complete-case summaries
remain unchanged. Corpus checkpoints add orchestration and admitted-hash binding;
they do not add scheduling mechanisms or grant runtime label access.

## 25. Pilot-ready runbook

See [pilot-runbook.md](pilot-runbook.md) for pinned Demo startup, three optional
operator-driven recordings, fixed QA, leakage review, blind packets, Gold freeze,
private request assembly, QA, capture-policy freeze, and later split/checkpoint
operations. Bulk recording and Stage C performance runs are not automatic side
effects of review or pilot initialization.

## 26. Targeted validation

Sections 26–28 retain the original P2.2 implementation validation. The subsequent
environment-provenance patch and its new test/audit results are recorded in
[demo-resource-profile.md](demo-resource-profile.md).

Recording compatibility and focused new recorder tests: **21 passed** at the
recording integration checkpoint. Final corpus/admission/scoring/Stage C combined
result: **317 passed, 1 skipped in 47.02s**. This run also covers legacy
evaluation, recorder compatibility and scheduling.
Log: [p22-targeted-final.log](../artifacts/p22-targeted-final.log).

## 27. Full pytest

Baseline entering P2.2: **1120 passed, 1 skipped**. Final result:
**1225 passed, 1 skipped in 97.93s**: 105 additional passing cases. The remaining
skip is the existing Windows symlink privilege case.
Log: [p22-full-final.log](../artifacts/p22-full-final.log).

## 28. P1/P2.1 compatibility

P2.2 does not change production predicates, observation contracts, scheduling,
reports, acquisition identities, runtime policy or B0/B1/B2 treatment semantics.
The final protected-file and identity audit records regression evidence. New
identities belong to corpus, rubric, claims, Gold, BSP-v2 and checkpoint artifacts;
existing P1/P2.1 identities are not intentionally migrated.

The audit confirms **112 protected files byte-identical**, including existing
tests, production runtime, sanitizer/admission and Stage C identity contracts.
Registered B0/B1/B2, BB, BEP and BSP-v1 identities match the P2.1 audit exactly;
B2's runtime policy still equals the production default structurally.

## 29. Pilot status

**Zero real pilot episodes, fault injections, Gold labels or corpus versions.**
Operator ENV_READY checks discovered checkout and product-catalog upstream OOM
events before any recording. The DRP-v2 profile-data revision addresses the two
observed failures without preparing/starting Docker or running the pilot.
P2.3 remains unstarted.
Synthetic temporary test bundles demonstrate contracts, not collection readiness
or real diagnostic performance. There is no automatically approved real corpus.
The next human action is an operator-led 3–4 episode pilot using the runbook.

## 30. P2.3 handoff and deferred work

After pilot QA, freeze `capture-policy-phase-a-v1`; bulk Phase-A recording is a
separate explicit task. Preserve independent diagnostic semantics and sealed
holdout access while developing P2.3. Phase-A can measure within-family
repeatability; absent family-holdout positives and real multi-edge episodes must
remain explicit limitations.

Deferred: full corpus recording, unsupported fault controls, native log
Observation, new predicates, metric diagnosis, retries, multi-hop, benchmark V2
admission, LLM treatments, deployment and P2.3 capability implementation.
