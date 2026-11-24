# P1.6a Deterministic Topology-Derived InvestigationScope

This build adds persisted investigation permissions from historically verified
typed RPC topology. It does not query neighbors, attribute faults, change the
planner/verify signal policy, or expand Live/Replay authorization.
Baseline: 812 passed, 1 skipped.

## Scope contracts and policy identity

`models/scope.py` defines four frozen, versioned contracts:

- ScopeExpansionPolicy: rule=original_seed_one_hop_rpc, rule_version=1,
  max_admitted_neighbors=8 by default, max_service_name_length=256 by default.
  Its SP-* hashes the full canonical structured policy. The supported hard
  ceilings are 64 admitted neighbors and 1024 characters per topology endpoint.
- ScopeExpansionRecord: discovered_service, from_seed_service, directed
  caller/callee, hop=1, rule/version, one canonical O-trace witness, one canonical
  A-* witness and a support_set_digest.
- ScopeDerivationBasis: policy_id, acquisition_state_digest, resolution_digest
  and topology_witness_digest, with an SB-* structural identity.
- InvestigationScope: policy, basis, original seed set, bounded expansions,
  overflow count/digest, and excluded-identifier count/digest.

DEFAULT_SCOPE_POLICY defines the default once. Changing either policy parameter
changes SP-* even when a small input produces the same admitted services. Policy
is persisted with the scope and reused on later refreshes. It never enters T-*.

SX-* binds the semantic discovered service, original seed, directed caller/callee,
hop and rule/version. It excludes witnesses, evidence counts and display prose.
SIS-* binds policy ID, original seeds and the ordered SX-* list. It excludes
derivation provenance, supporting-span counts and overflow metadata. Adding a
duplicate witness or another acquisition for the same topology leaves SX/SIS
unchanged while changing SB as appropriate. Changing SP intentionally changes SIS.

`admitted_services` is computed from original seeds plus bounded expansion
records; `discovered_neighbor_count` is computed from admissions plus overflow.
These are not independently mutable lists. JSON stores seeds and expansions,
which reconstruct these properties and all structural IDs on validation.

## Exact one-hop eligibility

Let S0 be the immutable IncidentInput.seed_services set. Candidate facts come
from completed trace TaskSpecs whose service scope intersects S0 and whose
half-open acquisition window overlaps the incident window. There is no
connectivity-plan or checkout-specific selection in this scope path.

An admitted topology witness must be a typed O-trace rpc_outcome from a verified
native attempt resolution, inside the incident window, with a resolved,
unambiguous peer and a valid directed rpc_edge_key. Exactly one endpoint must
be in S0; the other must be distinct and outside S0. Both endpoint strings must
be nonblank and within the policy name-length bound. Existing P1.1 resolved
service identities are retained exactly: no new lowercasing, trimming, alias
guessing or service inference is added.

One hop always uses S0. Admitting payment never makes payment another expansion
origin. A checkout->payment edge permits payment; payment->accounting cannot
permit accounting when the original seed is checkout. Edges between two original
seeds and self-loops create no expansion. Multi-service TaskSpecs can be tested
synthetically, but this build does not create or authorize new such queries.

Outcome is not an eligibility condition. Success, gRPC UNKNOWN, epistemic
unknown and failure can all provide topology if peer identity is resolved.
Admission is a permission, not failure evidence, a causal conclusion, a query,
or a completeness claim. E-* records, metric labels, logs, hypotheses and LLM
prose never enter this derivation.

## P1.1 orientation reuse

The existing orientation in observations/entity.py was factored into
rpc_edge_endpoints: CLIENT means local->peer; SERVER means peer->local.
Both rpc_edge_key used by Observation production and scope derivation call this
same helper. directed_rpc_edge_key retains the original JSON encoding and
20-hex SHA-256 prefix over [caller, callee, operation]. Scope verifies the typed
correlation key against that helper; it does not implement another orientation.

Unresolved SERVER caller never expands. Existing peer resolution, including
SERVER rejection of callee-only rpc.service/name hints as caller evidence,
is unchanged. Existing O-trace serialized goldens and CLIENT/SERVER edge-key
regressions pass without changed expected IDs.

## Neutral native resolution and shared refresh

trace_acquisition.py owns TraceObservationSource, trace_source_material,
trace_binding and resolve_trace_attempt. It imports no connectivity catalog,
CheckResult, VBS/VBE or InvestigationScope. The runtime re-exports the source
type/binding function for existing backend callers.

ResolvedTraceAttempt is an ephemeral frozen acquisition object containing the
authoritative AttemptRecord/TaskSpec, a closed resolution outcome and typed
Observation facts. The resolver retains P1.4-3 semantics: source digest must
match before observe_spans runs, then the produced O-ID set must equal the
historically admitted O-ID set EXACTLY. Subset/superset/digest mismatches or
unavailable source provide no facts. Historical AttemptRecords are never changed.

runtime.refresh_acquisition_state selects the union of connectivity-relevant
trace attempts and original-seed trace attempts, then resolves each A-* once.
The same resolved map feeds the unchanged connectivity refresh and the separate
scope derivation. No second source read or observe_spans call occurs for scope
at the same persistence boundary. The initial worker binding remains separate:
it records history that the later persistence boundary must verify.

The investigate graph keeps admit -> workers -> merge -> persist_verification
-> END. Its existing persist_verification node now atomically returns both whole
current values. Neither field merges generations. The runner's deadline path
also calls the combined owner after merging completed workers and finalizing
started unfinished attempts, before audit/report. No refresh occurs in
hypothesize, verify, report or LLM code.

Scope is acquisition-native: the test with original seed shipping admits
inventory from shipping->inventory while connectivity's VBE has no trace inputs.
Scope consumes ResolvedTraceAttempt, not PersistedConnectivityVerification.

## Semantic identity versus freshness

The bounded acquisition_state_digest hashes original seed set, incident window,
SP-* and a sorted digest input containing every relevant seed-side A-*/T-* and
its status, observation binding, source_material_digest and historical O-ID set.
It does not persist those unbounded input lists in the scope artifact.

resolution_digest commits to each relevant A-*, its exact binding outcome and
the resolved O-ID set. topology_witness_digest commits to eligible directed
edges and their exact deduplicated O/A witness sets used during admission.
Each admitted expansion also has its own support_set_digest for its selected
semantic edge. These hashes preserve provenance without unbounded witness lists.

A new A/O/source generation changes SB even if it still permits the identical
checkout->payment edge and therefore preserves SX/SIS. A source failure on an
explicit refresh also changes the resolved basis, even if authoritative state
did not change.

current_investigation_scope revalidates the scope model, compares original seeds
and recomputed state-only acquisition digest, and verifies each persisted
canonical witness belongs to its historical bound seed-side attempt. It never
queries a backend, resolves source or extracts topology. A stale artifact is an
invariant error. validate_execution_state now invokes this accessor as well as
the existing connectivity accessor.

Backend changes alone do not silently refresh scope. It records the last
explicit resolution generation, just as persisted connectivity does. Before
any acquisition/persist boundary the field is None. A boundary with no eligible
verified topology persists a seed-only scope; unsuccessful attempts do not
grant neighbors. No raw spans or CheckResults are duplicated in the scope.

Connectivity VBS/VBE do not hash scope, policy or its basis. Scope-only policy
or permission updates cannot invalidate unchanged connectivity input. Unrelated
payment-only acquisitions are not automatically scope seed-side or connectivity
inputs. Planner/verify still materialize/query only the original incident scope.

## Deterministic ordering and bounded storage

Candidate services are deduplicated and sorted by exact Python string lexical
order. The first max_admitted_neighbors are admitted. For a service with several
seed/direction edges, choose the lexicographically smallest semantic tuple
(neighbor, seed, caller, callee), then the smallest (O-ID, A-ID) witness for that
edge. Witness count never affects ranking, admission, identity or confidence.

Only admitted neighbors have persisted expansion records. Each record has one
O-ID, one A-ID and one support digest. Overflow is only count and digest of its
sorted distinct service set, with no preview/list. Endpoint names rejected by
the explicit identifier bound are represented by count/digest, not raw names.

For fixed original seed input, stored size is bounded by O(M*L) service-name
characters plus O(M) fixed-size IDs/digests, where M<=64 and L<=1024 (defaults
M=8, L=256). Each expansion has four bounded endpoint/name fields. The basis
has four fixed-size ID/digest fields and no variable witness/attempt collection.
Original seeds are not silently truncated; their existing input size is an
additional term independent of peer fan-out. Integer counts use logarithmic
digits, as usual for bounded-summary counts. Temporary derivation work may
process the full acquired input; the persisted artifact does not retain it.

Tests compare 100 versus 2,000 peers with M=2: both have exactly two records and
their JSON sizes differ by fewer than 10 characters, due to count digits. The
2,000-peer scope remains under 3,000 characters. A single edge supported by 2,000
spans still produces one expansion and a scope under 2,000 characters.

## External behavior and verification

Fixture connectivity remains SUPPORTED, 2 rounds, 3 calls, E-T1/E-L1. Existing
missing-log, wrong-peer, healthy replay, Live/Replay, report/control golden and
no-retry tests remain in the suite. PaymentFailure gRPC UNKNOWN can admit
payment as a permission while connectivity and the final report remain
INCONCLUSIVE. Success edges likewise grant no failure support.

The only new persisted application output is investigation_scope.json in CLI/API,
alongside the existing artifacts. Scope admission creates no TaskSpec or Attempt.
Historical T/A provenance remains authoritative for materialized/attempted
services; those lists are not duplicated into InvestigationScope.

Before implementation, fixed execution p16a-golden captured VBS/VBE, runtime
trace/log CR, PR and all three T/A IDs. Literal regressions now require the same
values after the resolver refactor. Existing pure C/CR/P/PR and O/T/A goldens
also remain unchanged. No identity algorithm was altered to accommodate scope.

Targeted suite: test_investigation_scope.py, test_persisted_verification.py,
test_observations.py, test_connectivity_wiring.py, test_acquisition_identity.py,
test_acquisition_hardening.py, test_check_reason_contracts.py, test_workflow.py,
test_live_replay.py and test_api.py. Result: **299 passed in 18.69s**.
Full pytest: **856 passed, 1 skipped in 23.66s**, a net addition of 44 tests.
The unchanged skip is tests/test_sanitization.py:387, Windows symlink creation
requiring OS privileges. Commands use the local venv
and a fresh artifacts/pytest-p16a-* basetemp.

## Deferred P1.6b/c blockers

Historical T-* is physical acquisition identity. A recorded Live request with
query_template_id=jaeger.search.v3 and a newly materialized Replay request with
query_template_id=replay.select.v1 intentionally have different T-* values.
Future V2 replay must select the recorded acquisition TaskSpec/T-* from snapshot
provenance; it must not require fresh replay transport T-* == historical Live
T-*. Metric selection must remain especially strict and may require explicit
selection when multiple recorded metric TaskSpecs exist. This build does not
design or implement that selection protocol.

Future Live per-attempt partition ownership needs the real orchestration-owned
A-* context. P1.6b must decide how that identity reaches raw telemetry storage,
such as execution context or orchestration-owned partition capture. LiveBackend
must not mint another backend-local attempt ID. This context/partition design
is not implemented here.

Also deferred: automatic payment/shipping queries, Live multi-service
authorization/partitions, Replay expanded authorization, metric expansion,
Snapshot V2, capture migration, benchmark V2 and P1.7 policy.

## Files and scope

Added models/scope.py, investigation_scope.py, trace_acquisition.py,
tests/test_investigation_scope.py and this document.

Modified runtime.py, state.py, graph.py, runner.py, cli.py, api.py,
observations/entity.py; tests/test_persisted_verification.py,
tests/test_acquisition_identity.py, tests/test_api.py; and the persisted
verification/acquisition milestone documentation.

SHA-256 scope audit: 12 existing files changed, 5 files added and 119 existing
source/test/document/recording files are byte-identical. All 8 verification
files, all 10 existing model files, all 5 tools files (including Live/Replay and
Snapshot V1) and all 31 recording JSON files are unchanged. The entity helper
refactor preserves edge encoding and existing Observation goldens.

No scope deviation. Factoring the shared orientation and native resolver was
required to preserve one interpretation and avoid duplicate resolution. The
identifier-length policy is an explicit bounding parameter; it never truncates
or rewrites service identities. Scope and verification remain independent.
