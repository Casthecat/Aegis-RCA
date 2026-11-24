# P2.2 Demo environment provenance hardening

## Current revision: two-service DRP-v2

This evidence-driven profile-data revision retains the generic schema-v1
DemoResourceProfile mechanism and CapturePolicy schema v2. It changes only the
default frozen profile data and the resulting capture identity. It does not
prepare or start Docker, collect telemetry, inject a fault, run Stage C or begin
P2.3.

The application remains **OpenTelemetry Demo 3.0.0**. Two services now have
independently observed upstream resource failures, both before any pilot capture:

1. Checkout used upstream `20M` / `GOMEMLIMIT=16MiB` and reported
   `ExitCode=137`, `OOMKilled=true` after approximately 11 minutes. Under DRP-v1,
   checkout subsequently ran healthy with live `HostConfig.Memory=134217728`
   and `GOMEMLIMIT=100663296B`.
2. Product-catalog retained upstream `20M` / `GOMEMLIMIT=16MiB`. Its fresh current
   container reported `StartedAt=2026-09-19T18:58:44.684288111Z`,
   `RestartCount=0`, `Status=exited`, and `OOMKilled=true`. The prior absence of
   a Docker-events result does not override this direct current-container
   `State.OOMKilled=true` evidence.

The frozen profile `pilot-checkout-product-catalog-headroom-v2` therefore
overrides **exactly checkout and product-catalog**. Each receives
`memory_limit_bytes=134217728` (128 MiB) and `gomemlimit_bytes=100663296`
(96 MiB). All other services retain their resolved upstream resource semantics;
all non-resource fields are preserved by profile application. This provides
headroom for establishing a healthy baseline. It is experiment-environment
identity, not a benchmark treatment or proof that ENV_READY will now pass.
No inference is made that other services with a 20M limit are unstable.

`DEMO_RESOURCE_PROFILE_V1` preserves the original checkout-only profile and
identity; `DEMO_RESOURCE_PROFILE_V2` is the new explicit profile, and
`PILOT_DEMO_RESOURCE_PROFILE` selects v2 for new initialization. Neither old
profile nor old policy artifacts are migrated or overwritten.

- Old DRP: `DRP-9847588deaaf02889737ffb35c7fe46bdf4ede547a8387941d0e669c897ca48b`.
- New DRP: `DRP-1cc5f2abd6914a39268b4e60b4108500b996fee7637d7f87c5b578513ebd6791`.
- Old pilot-02 CP: `CP-f8f78a35a67b279df75d5ad099b8fa64a00842853f9e4b45354b21b0c6c83b4e`.
- New pilot-03 CP: `CP-348a6b0f3260f77ca5dc3dff2aa057ab9fef15aad165bef7e489076623612d67`.

The revision preserves `artifacts/p22-pilot-01` and `artifacts/p22-pilot-02`
unchanged. Only the five protocol artifacts are initialized in
`artifacts/p22-pilot-03/protocol`. The [active runbook](pilot-runbook.md) points
to pilot-03. The existing recorder gate still requires the exact bound DRP,
pinned Demo version and prepared Compose bytes; a pilot-02 prepared environment
cannot satisfy the new capture policy. Human health validation remains separate.
If any resource profile changes after collection starts, freeze a new profile,
capture policy and capture/corpus version rather than silently changing the
environment.

This revision changes six files: `src/aegis_rca/evaluation/demo_environment.py`
(frozen profile data only), `tests/test_demo_environment.py`,
`tests/test_corpus_recording.py` (two-service fixture), and the three documents
`docs/demo-resource-profile.md`, `docs/corpus-protocol.md`, `docs/pilot-runbook.md`.
It adds `tests/test_demo_resource_revision.py`. The generic profile schema,
application/validation functions, recorder, fault controls, CapturePolicy schema,
and Stage C implementations are unchanged.

Baseline: **1251 passed, 1 skipped**. The targeted environment/corpus/recording
suite passed **110 tests in 131.60s**; see
[p22-drp-v2-targeted.log](../artifacts/p22-drp-v2-targeted.log).
Full pytest passed **1258 tests, 1 skipped in 813.79s** (7 additional passing
cases); see [p22-drp-v2-full.log](../artifacts/p22-drp-v2-full.log). The remaining
skip is the existing Windows symlink privilege case. Existing Stage C tests used
temporary synthetic fixtures; no pilot/performance Stage C experiment was run.

[p22-drp-v2-audit.json](../artifacts/p22-drp-v2-audit.json) records **159 protected
existing files byte-identical** and all **15 pre-existing operator/source
artifacts unchanged**, including both old pilot workspaces. BV-B0/B1/B2, BB,
BEP, BSP-v1, BSP-v2 and GDR identities match their prior declarations exactly.
The actual pilot-03 CLI initialization reported `recordings_started=0`, and
the runbook's read-only protocol validation passed without rewriting files.

**Real pilot episodes: 0. Fault injections: 0. Gold labels: 0. Corpus: 0.**
P2.3 remains unstarted. The following sections retain the original DRP-v1
hardening report and its historical test results.

## Original DRP-v1 hardening report

This patch binds the prepared Demo resource environment to formal capture
identity. It does not collect a pilot episode or change investigation,
verification, sanitization, Stage C, Gold or scoring semantics.

## Files changed

New files:

- `src/aegis_rca/evaluation/demo_environment.py`: resource profile and prepared-environment contracts/validation.
- `tests/test_demo_environment.py`: isolated Compose transformation and provenance tests.
- `tests/test_recorder_environment.py`: recording gates, policy migration and protected identity regressions.
- `docs/demo-resource-profile.md`: this patch report.

Modified files:

- `scripts/demo.py`: explicit profile consumption and atomic preparation receipt.
- `scripts/record_episode.py`: formal environment gate and private receipt retention.
- `src/aegis_rca/evaluation/corpus_recording.py`: CapturePolicy v2 with required DRP binding.
- `src/aegis_rca/evaluation/corpus_cli.py`: fifth init-pilot artifact and profile ID output.
- `tests/test_corpus_recording.py`: explicit valid prepared-environment fixtures.
- `tests/test_corpus_workflow.py`: new policy binding and init artifact expectations.
- `docs/corpus-protocol.md` and `docs/pilot-runbook.md`: environment policy and operator workflow.

The new `artifacts/p22-pilot-02/protocol` contains only the five initialized
protocol artifacts. Existing pilot/source/generated-environment artifacts were
preserved; no generated Compose was hand-edited or prepared during this patch.

## Evidence and scope

During ENV_READY, before any pilot capture, upstream OpenTelemetry Demo 3.0.0
checkout ran with `deploy.resources.limits.memory=20M` and
`GOMEMLIMIT=16MiB`. The actual container subsequently reported `ExitCode=137`
and `OOMKilled=true` after approximately 11 minutes. These resource values came
from upstream Compose; Aegis preparation did not introduce them.

The application remains **OpenTelemetry Demo 3.0.0**. The frozen Aegis pilot
resource profile provides checkout headroom above that empirically failed
baseline: 128 MiB container memory and 96 MiB Go memory limit. This is a declared
environment-stability profile, not a benchmark treatment or a claim that health
has been established. At the time of DRP-v1, product-catalog had not produced an
observed OOM event; its health-check timeouts did not establish a resource cause,
so that revision left it unchanged. The subsequent direct evidence and DRP-v2
response are recorded above.

## Frozen resource contract

`aegis_rca.evaluation.demo_environment.DemoResourceProfile` is frozen and
versioned. Its schema-v1 fields are:

- `schema_version=1`.
- `profile_name="pilot-checkout-headroom-v1"` for the initial pilot profile.
- `otel_demo_version="3.0.0"`.
- `service_overrides`, a bounded tuple sorted by unique service name.

Each override binds `service`, `memory_limit_bytes` and `gomemlimit_bytes`.
Canonical units are positive integer bytes; the Go limit must leave headroom
below the container memory limit. The initial profile contains only:

```json
{
  "service": "checkout",
  "memory_limit_bytes": 134217728,
  "gomemlimit_bytes": 100663296
}
```

`DRP-*` uses the existing canonical structural hash of the complete profile
semantics. Changing either limit or the service override set changes DRP.
Reordering equivalent overrides does not change it. Display prose, injection
truth, Gold and runtime outputs do not enter this identity.

The initial pilot profile is
`DRP-9847588deaaf02889737ffb35c7fe46bdf4ede547a8387941d0e669c897ca48b`.

The generated Compose uses `"134217728"` for checkout's memory limit and
`"100663296B"` for `GOMEMLIMIT`. Profile application copies the resolved input
and changes only the declared memory-limit and Go-limit fields. Other resource
fields and all other services retain their resolved upstream values. Existing
Aegis project, flag-volume, restart, image-pin, network and port preparation
still applies independently; this patch does not introduce further changes to
checkout's non-resource configuration.

## Explicit preparation and receipt

`corpus init-pilot` now emits `demo-resource-profile.json` alongside the capture
and query policies, GDR and BSP-v2. It does not prepare Docker, start containers,
record telemetry or mutate fault flags.

The operator deliberately passes that artifact to:

```powershell
.\.venv\Scripts\python.exe scripts/demo.py prepare --resource-profile artifacts/p22-pilot-02/protocol/demo-resource-profile.json
```

Preparation resolves pinned upstream Compose, applies the profile, and publishes
`artifacts/demo-compose.json` and
`artifacts/demo-compose.provenance.json`. The receipt name follows the generated
Compose filename stem with `.provenance.json`. Preparation without a profile
retains upstream resource behavior for engineering use, removes a stale profile
receipt, and is not formally pilot-ready.
Do not hand-edit generated Compose to apply or adjust resource settings.

`PreparedDemoEnvironment` schema v1 binds:

- pinned OTel Demo version;
- full DemoResourceProfile and its matching DRP identity;
- upstream source marker (repository, tag and archive URL), plus its SHA-256;
- upstream base and observability Compose SHA-256 hashes;
- resolved upstream Compose SHA-256;
- generated Compose byte SHA-256.

Its `DEP-*` identity covers this receipt. The marker records the pinned source;
these digests provide reproducibility provenance, not an external signature or
proof against a malicious operator replacing both data and receipt.

## Capture binding and recording gate

CapturePolicy schema v2 requires `demo_resource_profile_id`; no deserialization
default supplies it to an old artifact. It participates in `CP-*`, so both the
schema migration and any changed DRP produce a changed capture identity. The
empty `artifacts/p22-pilot-01` is superseded and preserved. Initialize a new pilot
workspace; do not rewrite or migrate the old policy artifacts in place.

The new default pilot CapturePolicy is
`CP-f8f78a35a67b279df75d5ad099b8fa64a00842853f9e4b45354b21b0c6c83b4e`.

Formal `record_episode --capture-policy ...` validates prepared provenance
before constructing LiveBackend, catalog discovery or changing any fault flag.
It rejects missing receipts, mismatched profile IDs, changed generated Compose
bytes, inconsistent resource fields and mismatched Demo versions. The CLI
supports explicit `--demo-compose` and `--demo-environment-receipt` paths; their
defaults are the two generated artifact paths above. A validated receipt is
copied to private `demo-environment.json` in the episode directory. A failed
readiness attempt remains a failed private production record; it creates no
quarantine recording or injected fault.

Validation establishes the **prepared file** binding. It does not inspect live
container cgroups or prove that existing containers were recreated. The operator
must bring up the prepared project, verify the actual running configuration,
exercise the successful checkout business path and independently attest health.
Human health/readiness attestation is required in addition to this structural
gate and cannot override a failed binding.

If a resource profile changes after collection begins, create a new profile ID,
new CapturePolicy and new capture/corpus version. Never silently modify the
environment associated with recorded episodes.

## Operator workflow and compatibility

The patch initializes only the five frozen protocol artifacts in
`artifacts/p22-pilot-02/protocol`; `p22-pilot-01` remains untouched. The
[pilot runbook](pilot-runbook.md) can reuse this initialization after read-only
validation of all five contracts and their exact canonical bytes. A mismatch
stops the workflow and is never repaired by overwriting an old policy. If the
protocol directory is absent, the runbook initializes a new one.

The operator then passes its explicit profile to preparation. Demo preparation,
startup, injection and recording remain future operator steps; they were not
executed as part of this patch. Protocol initialization does not establish
container readiness or authorize a pilot recording.

P1 runtime and predicates, B0/B1/B2 interventions and BV identities, BB, BEP,
BSP-v1, BSP-v2 and independent Gold semantics are preserved. The intentional
identity migration is DRP-bound CapturePolicy v2 and artifacts that structurally
bind that new CP. Existing source/candidate/Gold/corpus artifacts are not
rewritten. Fault controls remain exactly the two previously validated flags.

## Validation and stop point

The baseline before this patch was **1225 passed, 1 skipped**. Focused tests cover
resource field isolation, canonical identity changes, capture binding,
missing/stale/mismatched provenance, preservation of scoring/runtime semantics
and init/prepare without recording or injection. The combined targeted suite
passed **197 tests in 110.23s**; see
[p22-environment-targeted.log](../artifacts/p22-environment-targeted.log).
Full pytest passed **1251 tests, 1 skipped in 266.31s**, adding 26 passing cases
over the 1225-pass baseline. The skip remains the existing Windows symlink
privilege case. See [p22-environment-full.log](../artifacts/p22-environment-full.log).

[p22-environment-audit.json](../artifacts/p22-environment-audit.json) records
before/after file hashes and protocol identities. **153 protected existing files**
are byte-identical, including production graph/runtime/check contracts,
sanitizer/admission, Stage C, Gold and scoring implementations. The exact existing
B0/B1/B2, BB, BEP, BSP-v1, BSP-v2 and GDR identity declarations remain unchanged.
All nine pre-existing source/prepared-environment/pilot-policy artifact files
covered by the audit remain byte-identical.

**Real pilot episodes: 0. Fault injections: 0. Gold labels: 0. Corpus: 0.**
No pilot/performance B0/B1/B2 experiment was started; pytest used temporary
synthetic fixtures only. P2.3 remains unstarted. This patch stops at
environment-provenance tooling; the pilot remains an operator task.
