# P2.2 operator pilot: 3–4 independent episodes

This is a procedure to execute deliberately after the tooling build. **No pilot
episode has been recorded.** ENV_READY exposed independently observed upstream
checkout and product-catalog cgroup OOM failures before capture. The DRP-v2
procedure below uses `artifacts/p22-pilot-03`; preserve both
`artifacts/p22-pilot-01` and `artifacts/p22-pilot-02` unchanged rather than
rewriting their old profiles or policies. Do not start bulk Phase-A recording from this
runbook. Do not run B0/B1/B2, engineering Aegis replay or an Aegis report before
the answerability review and Gold/split freeze. Collection doctor/catalog checks
are system-independent and are permitted.

Commands below run in PowerShell from the repository root. Paths use the new
private pilot workspace. Its existing protocol-only initialization may be reused
after the read-only validation below; never overwrite a mismatched or completed
output. Keep
this workspace out of version control. The operator/custodian may see private
truth. Reviewer B must receive only the blind packet, not this control-specific
runbook, family plan, console recording log or operator folders.

## 1. Establish roles and storage before capture

Use separate OS accounts or containers for the custodian, answerability reviewer,
developer and execution harness. Grant Reviewer B access only to its blind packet
directory. Keep `recordings/private`, quarantine, family plans and Gold outside
the execution harness mount. The trusted admission component needs the candidate
store and the existing `review/` audit, scan and decision files to enforce the
unchanged approval implementation. Those files are private admission metadata,
not public receipts. Only the resulting admitted snapshot and safe hashes pass
to investigation execution. The current CLI performs admission and investigation
in one process; this phase boundary is tested in code, not a separate OS sandbox.
Restrict that process to admission metadata plus the execution projection,
freeze receipt and checkpoint/access directory. Do not give it InjectionTruth or
Gold access, and do not replace admission with a copy-and-trust path.

Seal holdout **telemetry, labels and identities**, not just labels. Do not place a
holdout packet on a developer-visible drive. Have an administrator verify the
actual filesystem ACLs/mounts before declaring a split sealed; these commands do
not install access controls or monitor filesystem reads. If one person performs
all roles, declare the label review `weak_blind`, acknowledge prior injection
knowledge, seal private truth before reviewing anonymous telemetry, and do not
call this independent double review.

```powershell
$aegisPython = '.\.venv\Scripts\python.exe'
$pilotRoot = 'artifacts/p22-pilot-03'
$recordingsRoot = Join-Path $pilotRoot 'recordings'
$protocolRoot = Join-Path $pilotRoot 'protocol'
$blindRoot = Join-Path $pilotRoot 'reviewer-b'
$goldRoot = Join-Path $pilotRoot 'gold'
$corpusRoot = Join-Path $pilotRoot 'frozen-corpus'
$operator = Read-Host 'Private QA operator identity'
$seedJustification = 'The checkout business-path incident or alert is the entry context; the injected target does not determine the seed.'
if (Test-Path -LiteralPath $protocolRoot) {
@'
import sys
from pathlib import Path
from aegis_rca.evaluation.corpus_recording import PILOT_CAPTURE_POLICY, QUERY_POLICY
from aegis_rca.evaluation.demo_environment import PILOT_DEMO_RESOURCE_PROFILE
from aegis_rca.evaluation.gold import DEFAULT_GOLD_RUBRIC
from aegis_rca.evaluation.gold_scoring import BenchmarkScoringProtocolV2
from aegis_rca.evaluation.sanitization import canonical_bytes
from aegis_rca.evaluation.stage_c import read_contract

root = Path(sys.argv[1])
expected = {
    'capture-policy.json': PILOT_CAPTURE_POLICY,
    'query-policy.json': QUERY_POLICY,
    'demo-resource-profile.json': PILOT_DEMO_RESOURCE_PROFILE,
    'rubric.json': DEFAULT_GOLD_RUBRIC,
    'scoring-protocol.json': BenchmarkScoringProtocolV2(),
}
if {path.name for path in root.iterdir()} != set(expected):
    raise SystemExit('Protocol directory must contain exactly the five frozen artifacts')
for name, contract in expected.items():
    path = root / name
    if (read_contract(path, type(contract)) != contract
            or path.read_bytes() != canonical_bytes(contract.model_dump(mode='json'))):
        raise SystemExit(f'Protocol mismatch: {name}; preserve it and choose a new workspace')
print('Existing frozen protocol verified; no files changed')
'@ | & $aegisPython - $protocolRoot
    if ($LASTEXITCODE -ne 0) { throw 'Existing protocol rejected; do not overwrite it' }
} else {
    & $aegisPython -m aegis_rca.evaluation corpus init-pilot --output $protocolRoot
    if ($LASTEXITCODE -ne 0) { throw 'Use a new protocol destination or inspect the error' }
}
$capturePolicy = Join-Path $protocolRoot 'capture-policy.json'
$resourceProfile = Join-Path $protocolRoot 'demo-resource-profile.json'
$demoCompose = 'artifacts/demo-compose.json'
$demoEnvironmentReceipt = 'artifacts/demo-compose.provenance.json'
```

`init-pilot` writes five artifacts: the frozen pilot capture/query policies,
GDR, BSP-v2 and explicit DemoResourceProfile. It starts zero recordings and
assigns zero labels. The patch initializes only these protocol artifacts in
`artifacts/p22-pilot-03/protocol`; Demo preparation/startup and recording remain
pending operator actions. The reuse branch above checks all five contracts and
their exact canonical bytes without changing any file. CapturePolicy schema v2 requires the resource profile's
`DRP-*` identity, which participates in its `CP-*` identity. A policy without
this binding is rejected; it cannot inherit the new environment implicitly.
Default pilot timing is warmup
30s, capture 60s, ingest wait 15s, cooldown 30s. Limits are max_results 100, metric
step 15s, HTTP response limit 2,000,000 bytes and timeout 5s. Review these before
any episode; do not change them after seeing an Aegis answer.

Declare opaque family IDs once and retain them for subsequent independent trials
of each family. The private meaning belongs only to the custodian. This initial
pilot can remain DEV after Gold freeze; no unseen-family claim follows from it.

```powershell
$familyUnreachable = [guid]::NewGuid().ToString()
$familyFailure = [guid]::NewGuid().ToString()
$familyControl = [guid]::NewGuid().ToString()
$privatePlan = @{
    declared_at = [DateTimeOffset]::UtcNow.ToString('o')
    purpose = 'Three-episode collection-quality pilot; no Aegis performance used'
    family_unreachable = $familyUnreachable
    family_failure = $familyFailure
    family_control = $familyControl
    trial_index = 1
    planned_real_multi_edge = 0
} | ConvertTo-Json
New-Item -ItemType File -Path (Join-Path $pilotRoot 'private-family-plan.json') -Value $privatePlan -ErrorAction Stop | Out-Null
```

Future replicate holdout rules must declare DEV and REPLICATE_HOLDOUT trial
indices **before** capture. Do not retroactively choose the successful or hardest
trials as holdout. Phase-A family holdout may contain zero positive families.

## 2. Start the pinned environment and verify real workload

These are operator-invoked environment commands, not benchmark requirements.
Stage C itself remains offline and does not require Docker.

```powershell
docker info
if ($LASTEXITCODE -ne 0) { throw 'Docker Server must be available before the pilot' }
& $aegisPython scripts/prepare_demo.py
if ($LASTEXITCODE -ne 0) { throw 'Pinned source preparation failed' }
& $aegisPython scripts/demo.py prepare --resource-profile $resourceProfile
if ($LASTEXITCODE -ne 0) { throw 'Explicit environment preparation failed' }
& $aegisPython scripts/demo.py up
if ($LASTEXITCODE -ne 0) { throw 'Resolve environment readiness before recording' }
& $aegisPython scripts/demo.py status
```

The scripts pin OpenTelemetry Demo 3.0.0 and manage only the dedicated
`aegis-rca-demo` Compose project. They copy flag configuration into
`artifacts/demo-flags`; recording never changes upstream source files. Do not
replace pinned application image tags with `latest`.

The application remains **OpenTelemetry Demo 3.0.0**. The pilot's frozen Aegis
DemoResourceProfile v2 overrides exactly **checkout and product-catalog**: each
has container memory **128 MiB** and `GOMEMLIMIT=96MiB`. Canonical profile values
are 134217728 and 100663296 bytes for each service. No other service resources
change.

The upstream checkout `20M` / `GOMEMLIMIT=16MiB` configuration produced a directly
observed `ExitCode=137`, `OOMKilled=true` event after approximately 11 minutes
during ENV_READY, before any pilot capture. Checkout subsequently ran healthy
under DRP-v1 with live `HostConfig.Memory=134217728` and
`GOMEMLIMIT=100663296B`. Product-catalog retained its upstream `20M` /
`GOMEMLIMIT=16MiB` values and then supplied independent direct evidence from its
fresh current container: `StartedAt=2026-09-19T18:58:44.684288111Z`,
`RestartCount=0`, `Status=exited`, `OOMKilled=true`. A prior absent Docker-events
result does not override the current container's `State.OOMKilled=true`.
These observations do not establish that any other 20M service is unstable.
This profile provides headroom to establish a stable healthy baseline; it is
experiment-environment identity,
not a B0/B1/B2 intervention or a guarantee of health. The new DRP is
`DRP-1cc5f2abd6914a39268b4e60b4108500b996fee7637d7f87c5b578513ebd6791`;
the bound pilot CP is
`CP-348a6b0f3260f77ca5dc3dff2aa057ab9fef15aad165bef7e489076623612d67`.

Explicit preparation resolves the upstream Compose files, applies the frozen
profile, and writes `demo-compose.json` plus its provenance receipt. The receipt
binds the pinned application version, upstream source marker, profile identity
and generated Compose digest. Do not hand-edit generated Compose or receipt
files. `prepare` without `--resource-profile` retains upstream resource values
for engineering use, removes a stale profile receipt and does not produce a
formal-pilot-ready environment.

Before backend discovery or any fault mutation, formal recording validates the
prepared Compose/receipt against the CapturePolicy: missing provenance,
resource-profile mismatch, Compose digest mismatch or Demo version mismatch
blocks recording. It also saves the validated receipt privately with the
episode. This verifies prepared files, not that running containers were recreated
from them. After changing preparation, use `up`, inspect the running checkout
and product-catalog configuration and readiness, then perform the business-path
checks below. Human health attestation remains a separate gate.

If resource requirements change after collection begins, freeze a new DRP, a
new CapturePolicy and a new capture/corpus version. Never silently apply changed
resources to a continuing corpus. Preserve previous policies and receipts.

Prepare `config/local.json` from the backend example if necessary. Verify actual
metric names/log mapping, and choose the metric query from the real catalog.
Ensure its acquisition limit fields match `capture-policy.json`; the recorder
rejects a mismatch rather than silently changing limits.

```powershell
& $aegisPython -m aegis_rca.collect doctor --backends config/local.json --output (Join-Path $pilotRoot 'operator-catalog.json')
if ($LASTEXITCODE -ne 0) { throw 'Fix collection readiness before injecting' }
```

At `http://127.0.0.1:18080`, verify an actual successful checkout business path
and that the workload is running. A homepage response or a successful telemetry
catalog request is not sufficient. Independently check target-service health and
that previous controls are restored. The recorder asks for an explicit `CONFIRM`
attestation, which must reflect these actual checks.

## 3. Record the three pilot episodes sequentially

Do not run these commands as an unattended loop. Complete restoration, cooldown
and recovery health for each episode before starting the next. The recorder
acquires an exclusive flag-file lock. Do not manually delete a live recorder's
lock. If interrupted, inspect private audit and original flag bytes before any
manual recovery.

Episode 1, the validated connection-establishment control:

```powershell
& $aegisPython scripts/record_episode.py --backends config/local.json --flag paymentUnreachable --capture-policy $capturePolicy --demo-compose $demoCompose --demo-environment-receipt $demoEnvironmentReceipt --operator $operator --seed-justification $seedJustification --scenario-family-id $familyUnreachable --family-trial-index 1 --output $recordingsRoot
if ($LASTEXITCODE -ne 0) { throw 'Retain this failed attempt and inspect private QA before continuing' }
```

Episode 2, the other validated control, whose Gold must still be decided from
telemetry rather than its flag name:

```powershell
& $aegisPython scripts/record_episode.py --backends config/local.json --flag paymentFailure --capture-policy $capturePolicy --demo-compose $demoCompose --demo-environment-receipt $demoEnvironmentReceipt --operator $operator --seed-justification $seedJustification --scenario-family-id $familyFailure --family-trial-index 1 --output $recordingsRoot
if ($LASTEXITCODE -ne 0) { throw 'Retain this failed attempt and inspect private QA before continuing' }
```

Episode 3, explicit no-injection through the same recording pipeline:

```powershell
& $aegisPython scripts/record_episode.py --backends config/local.json --no-injection --capture-policy $capturePolicy --demo-compose $demoCompose --demo-environment-receipt $demoEnvironmentReceipt --operator $operator --seed-justification $seedJustification --scenario-family-id $familyControl --family-trial-index 1 --output $recordingsRoot
if ($LASTEXITCODE -ne 0) { throw 'Retain this failed attempt and inspect private QA before continuing' }
```

For injected episodes, confirm independent onset after the fixed warmup; do not
consult Aegis. Capture uses the exact neutral query and checkout seed. Collection
waits through the fixed window and ingest lag, then queries metric, trace and log
through the same LiveBackend. Original flags restore in `finally`, followed by
fixed cooldown and another actual business-path/health check. No-injection skips
all flag mutations, including restoration writes, but retains the lock,
readiness, collection, restoration verification, cooldown and health workflow.

Each recording attempt writes private `production.json`; once recording
parameters validate it also writes `injection-truth.json`. Early failures may
have only the production record;
successful capture writes one immutable `quarantine/<episode>/snapshot.json`.
The legacy private `injection.json`/original-flags audit is also retained. Failed
lock/readiness/onset/capture/recovery attempts remain part of the QA denominator.

An optional fourth independent control episode can use the same no-injection
command with `$familyControl` and trial index 2. Observe naturally insufficient
evidence or recovery behavior honestly. Do not remove evidence, inject a new
unsupported fault, shorten a window after examining diagnostic records, or use
the fourth episode to manufacture a missing category. No DNS, saturation,
timeout, extra-dependency or simultaneous multi-flag control is supported.

## 4. Sanitize, scan and obtain explicit human leakage approval

Repeat this procedure for each captured episode; retain rejected and failed
attempts. Copy the private episode UUID from that recording's console/audit.
Never use the flag name as a candidate ID or output directory.

```powershell
$episodeId = Read-Host 'Private episode UUID from the recorder'
New-Item -ItemType Directory -Path (Join-Path $recordingsRoot 'review') -Force | Out-Null
$termsFile = Join-Path $recordingsRoot ('review/terms-' + [guid]::NewGuid().ToString() + '.json')
$terms = @{ injected_flags = @('paymentUnreachable', 'paymentFailure'); scenario_answers = @() } | ConvertTo-Json
New-Item -ItemType File -Path $termsFile -Value $terms -ErrorAction Stop | Out-Null
$exportJson = & $aegisPython -m aegis_rca.evaluation sanitize --recordings-root $recordingsRoot --episode $episodeId --terms-file $termsFile
if ($LASTEXITCODE -ne 0) { throw 'Record sanitizer failure; do not manufacture a candidate' }
$candidateId = ($exportJson | ConvertFrom-Json).candidate_id
& $aegisPython -m aegis_rca.evaluation scan --recordings-root $recordingsRoot --candidate $candidateId
& $aegisPython -m aegis_rca.evaluation corpus production-update --recordings-root $recordingsRoot --episode $episodeId --candidate $candidateId
$reviewDir = Join-Path $recordingsRoot ('review/' + $candidateId)
$candidateDir = Join-Path $recordingsRoot ('candidates/' + $candidateId)
$decisionInput = Join-Path $reviewDir 'decision.input.json'
Copy-Item -LiteralPath (Join-Path $reviewDir 'decision.template.json') -Destination $decisionInput -ErrorAction Stop
notepad.exe $decisionInput
```

The leakage reviewer must inspect the **entire** sanitized snapshot, scan status,
hits and private sanitizer audit. Set `full_snapshot_reviewed=true` only after
actual review; assign each disposition honestly and retain the frozen hashes.
Complete zero-hit scanning still requires manual review. Scan truncation,
structural blockers, destructive/ambiguous removal or unresolved leakage cannot
be solved by marking everything non-leakage.

Submit the human's approve/reject decision, then check admission:

```powershell
& $aegisPython -m aegis_rca.evaluation review --recordings-root $recordingsRoot --candidate $candidateId --decision-file $decisionInput
& $aegisPython -m aegis_rca.evaluation corpus production-update --recordings-root $recordingsRoot --episode $episodeId --candidate $candidateId
& $aegisPython -m aegis_rca.evaluation admit --recordings-root $recordingsRoot --candidate $candidateId
if ($LASTEXITCODE -ne 0) { throw 'Candidate is not admitted; retain rejection and do not package Gold' }
```

`production-update` observes actual scan/manual decision state. It cannot approve
or label a candidate. If a production exclusion is warranted, record its closed
reason explicitly; for example:

```powershell
& $aegisPython -m aegis_rca.evaluation corpus production-update --recordings-root $recordingsRoot --episode $episodeId --exclude-reason source_unstable
```

The old engineering sanitization runbook includes technical Aegis replay. **Omit
that replay step here until Gold has been frozen** to avoid exposing Reviewer B
to current Aegis output. Snapshot structural validation and manual inspection do
not require a diagnostic run.

## 5. Package approved telemetry for Reviewer B

The custodian executes this command after admission. Pass only the resulting
packet directory to Reviewer B through the agreed restricted channel.

```powershell
$packetDir = Join-Path $blindRoot $candidateId
& $aegisPython -m aegis_rca.evaluation corpus review-package --recordings-root $recordingsRoot --candidate $candidateId --capture-policy $capturePolicy --output $packetDir
if ($LASTEXITCODE -ne 0) { throw 'Blind package failed; approval, neutral query or policy may be stale' }
```

The packet contains the admitted snapshot, GDR, record counts, truncation/
coverage/errors, collection policy and an invalid decision template. It contains
no InjectionTruth, scenario family or Aegis answer. Reviewer B should examine the
snapshot and GDR, then copy `decision.template.json` to `decision.json` and fill
it manually. Do not replace null answerability or false review attestations from
injection knowledge.

The four permitted outcomes are `mechanism_identifiable`, `symptom_only`,
`no_incident`, and `insufficient_evidence`. A mechanism claim must identify its
mechanism and directed caller/callee in one `expected_claims` record. Set
exhaustiveness only after a separate whole-candidate coverage judgment. An
injection-free episode with inadequate evidence is not an exhaustive healthy
label. A natural incident is not erased to make the control look healthy.

The reviewer sets `mode=independent` only if they had no injection knowledge; use
`weak_blind` otherwise and set `prior_injection_knowledge` honestly. Record the
actual UTC review time and assert approved telemetry/coverage reviewed, truth
sealed and Stage C results unseen only when true. For a justified no-incident
decision, explicitly attest sufficient coverage, empty-set justification and
successful path/no incident symptoms. No decision may assert verified ultimate
root cause.

## 6. Freeze Gold before reconciliation, split or performance

The custodian receives the human decision, validates its bindings against current
admission, and freezes a new immutable UUID-named Gold file:

```powershell
$goldFile = Join-Path $goldRoot ($candidateId + '.json')
& $aegisPython -m aegis_rca.evaluation corpus gold-freeze --recordings-root $recordingsRoot --packet $packetDir --decision (Join-Path $packetDir 'decision.json') --output $goldFile
if ($LASTEXITCODE -ne 0) { throw 'Gold decision or current admission did not validate' }
& $aegisPython -m aegis_rca.evaluation corpus production-update --recordings-root $recordingsRoot --episode $episodeId --candidate $candidateId --gold $goldFile
```

Only now may Reviewer A reconcile diagnostic Gold with injection QA. Ineffective
injection can exclude a production attempt; it cannot turn weak telemetry into
a positive Gold claim. Freeze errors do not authorize editing source bytes or
copying private fault fields into the decision.

## 7. Assemble the private pilot QA request, including every attempt

The following helper reads existing private progress records and already frozen
Gold. It creates a request, not labels or approval. Run it from the project root
after replacing `artifacts/p22-pilot-03` if you chose another directory. Initially
it leaves all splits unset; this is sufficient for collection QA. Include all
private attempt directories, including failed and rejected episodes.

```powershell
$unseenConfirmation = Read-Host 'Type UNSEEN only if no Aegis results were inspected for these episodes'
if ($unseenConfirmation -cne 'UNSEEN') { throw 'Unseen-results attestation required' }
@'
from datetime import datetime, timezone
from pathlib import Path
from aegis_rca.evaluation.corpus import CorpusEpisodeInput, CorpusFreezeRequest
from aegis_rca.evaluation.corpus_models import CorpusFreezeSpec
from aegis_rca.evaluation.corpus_recording import CapturePolicy, CorpusProductionRecord, InjectionTruth, QUERY_POLICY
from aegis_rca.evaluation.gold import BenchmarkGoldLabelV2, DEFAULT_GOLD_RUBRIC
from aegis_rca.evaluation.gold_scoring import BenchmarkScoringProtocolV2
from aegis_rca.evaluation.stage_c import publish, read_contract

root = Path('artifacts/p22-pilot-03')
policy = read_contract(root / 'protocol/capture-policy.json', CapturePolicy)
episodes = []
for directory in sorted((root / 'recordings/private').iterdir()):
    if not directory.is_dir() or not (directory / 'production.json').is_file():
        continue
    production = read_contract(directory / 'production.json', CorpusProductionRecord)
    truth_path = directory / 'injection-truth.json'
    truth = read_contract(truth_path, InjectionTruth) if truth_path.exists() else None
    gold_path = root / 'gold' / f'{production.candidate_id}.json'
    gold = read_contract(gold_path, BenchmarkGoldLabelV2) if gold_path.is_file() else None
    episodes.append(CorpusEpisodeInput(truth=truth, production=production, gold=gold, split=None))
spec = CorpusFreezeSpec(corpus_version='pilot-collection-qa-v1', phase='pilot',
    performance_results_unseen=True,
    frozen_at=datetime.now(timezone.utc), frozen_by='REPLACE_WITH_QA_CUSTODIAN',
    capture_policy=policy, capture_policy_id=policy.id, query_policy_id=QUERY_POLICY.id,
    gold_diagnostic_rubric_id=DEFAULT_GOLD_RUBRIC.id,
    scoring_protocol_id=BenchmarkScoringProtocolV2().id)
request = CorpusFreezeRequest(spec=spec, episodes=tuple(episodes))
publish(root / 'pilot-qa-request.json', request.model_dump(mode='json'))
'@ | & $aegisPython -
if ($LASTEXITCODE -ne 0) { throw 'Private QA request failed; inspect ledger completeness' }

& $aegisPython -m aegis_rca.evaluation corpus qa --recordings-root $recordingsRoot --request (Join-Path $pilotRoot 'pilot-qa-request.json') --output (Join-Path $pilotRoot 'pilot-qa.json')
```

Use a new request/output name for a later QA observation. Do not overwrite the
previous observation or omit failed attempts to improve the apparent completion
rate. Inspect counts, signal record volume/private quality records, errors,
truncation, source stability, policy versions and sanitizer rejection reasons.
No Stage C result is required or allowed as input to capture-policy selection.

## 8. After pilot QA only: freeze the Phase-A capture policy

Prepare `proposed-phase-a-policy.json` from the pilot policy, with
`policy_name="capture-policy-phase-a-v1"`. Decide any timing/limit adjustment
from system-independent collection quality and document the rationale. The
decision file has this shape; replace the identity/time and fill each assertion
only after the relevant review actually occurred:

```json
{
  "reviewer": "ACTUAL_QA_REVIEWER",
  "reviewed_at": "ACTUAL_UTC_REVIEW_TIME",
  "collection_quality_reviewed": true,
  "workload_and_source_stability_reviewed": true,
  "ingestion_lag_and_limits_reviewed": true,
  "sanitizer_leakage_reviewed": true,
  "aegis_results_not_used": true,
  "private_notes": "Human rationale based on collection QA, including failed attempts."
}
```

```powershell
& $aegisPython -m aegis_rca.evaluation corpus capture-policy-freeze --policy (Join-Path $pilotRoot 'proposed-phase-a-policy.json') --pilot-qa (Join-Path $pilotRoot 'pilot-qa.json') --decision (Join-Path $pilotRoot 'capture-freeze-decision.json') --output (Join-Path $pilotRoot 'phase-a-policy-freeze')
```

At least three real successfully recorded pilot episodes under the frozen pilot
policy and the explicit human QA
decision are required. Failed attempts remain in the pilot QA ledger. The new
output contains `capture-policy.json` and its immutable `freeze.json` receipt.
Synthetic SMOKE QA cannot satisfy this gate.
The later Phase-A corpus spec embeds this receipt, which must predate Phase-A
recordings. Pilot candidates retain their pilot policy identity; do not relabel
them as if they had been recorded under the new policy.

**Stop here before bulk Phase-A recording.** A policy receipt is not authorization
to start unattended corpus production. Do not begin P2.3 capability work as part
of this runbook.

## 9. Optional later corpus freeze and declared checkpoint

These steps are for the later authorized evaluation stage. They are deliberately
separate from pilot review and do not run automatically. After Gold is frozen,
prepare a new `CorpusFreezeRequest`: retain every attempt, explicitly assign DEV
or a predeclared sealed holdout split to eligible rows, choose a new corpus
version, and use a freeze time after all Gold/production updates. Keep excluded
rows with `split=null`. Do not assign SMOKE to real captures or claim a family
holdout from within-family replicate trials.

For REPLICATE_HOLDOUT, include the family rule recorded before capture; for
FAMILY_HOLDOUT ensure no family member appeared in DEV or replicate holdout.
Include truthful pre-freeze access events and explicit seal declarations. The
holdout seal defaults to false; the custodian must verify actual access controls
before setting holdout_telemetry_labels_and_ids_sealed=true. Each replicate
trial rule also requires an explicit telemetry_labels_and_ids_sealed=true
attestation. Corpus freeze
rejects missing approval/Gold, overlapping windows, duplicate source/telemetry
slices, mixed policies and invalid splits.

```powershell
& $aegisPython -m aegis_rca.evaluation corpus freeze --recordings-root $recordingsRoot --request (Join-Path $pilotRoot 'corpus-freeze-request.json') --output $corpusRoot
& $aegisPython -m aegis_rca.evaluation corpus checkpoint --corpus $corpusRoot --split DEV --operator $operator
```

Save the returned `CPT-*` checkpoint ID. An authorized harness account, with no
private manifest/Gold/truth mount, can later execute the declared split:

```powershell
$checkpointId = Read-Host 'Previously declared CPT checkpoint ID'
$experimentRoot = Join-Path $pilotRoot 'stage-c-dev'
& $aegisPython -m aegis_rca.evaluation corpus execute --recordings-root $recordingsRoot --corpus $corpusRoot --checkpoint $checkpointId --output $experimentRoot
```

Score completed runs afterwards with a scoring account that has frozen Gold but
no InjectionTruth access. The formal scorer's schema switch is explicit:

```powershell
$runDir = Read-Host 'Completed BRK run directory'
$scoreOutput = Read-Host 'New score JSON path under the experiment scores directory'
& $aegisPython -m aegis_rca.evaluation score-run --run $runDir --labels-root $goldRoot --label-schema gold-v2 --output $scoreOutput
& $aegisPython -m aegis_rca.evaluation summarize --experiment $experimentRoot
```

Record any human holdout access with `corpus access --corpus ... --event ...`.
The event includes candidate UUID, actual UTC time, actor, role, material and
purpose. Developer manual inspection or tuning access invalidates the holdout;
the old corpus cannot continue to be presented as sealed. Do not delete an access
event to recover eligibility. Declare a new corpus version and interpretation.

## Recovery and operational limits

If the process is interrupted, retain original flags, private truth, production
history, partial artifacts and quarantine bytes for inspection. Restore the
dedicated project's flags and verify real recovery before another recording.
Do not modify global Docker settings, delete volumes or recover locks without
confirming ownership and the recorded restoration state.

No DNS-specific control, saturation, timeout, extra dependency, simultaneous
multi-flag or deliberate real multi-edge injection is implemented. Missing
evidence is an honest collection/review outcome. A real corpus with no multi-edge
case cannot measure real B1→B2 portfolio advantage. A corpus with no positive
FAMILY_HOLDOUT cannot establish unseen-family generalization. These limitations
must accompany later performance reports.
