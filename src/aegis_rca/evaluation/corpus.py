"""Freeze reviewed corpus membership and independent-episode QA before evaluation.

This is private operator tooling. The separate execution loader deliberately reads
only its execution projection, hash receipt, and access ledger.
"""
from collections import Counter
from pathlib import Path

from pydantic import Field, StrictBool, model_validator

from ..models.identity import structural_id
from ..models.incident import Contract
from . import benchmark
from .corpus_models import (CorpusFreezeSpec, CorpusFreezeReceipt, CorpusQASummary,
    ExecutionCorpusManifest, ExecutionCorpusMember, HOLDOUTS, HoldoutAccessAssessment,
    HoldoutAccessEvent, MAX_EPISODES, PrivateCorpusManifest, PrivateCorpusMember,
    PrivateEpisodeReference, SplitName)
from .corpus_recording import (CorpusProductionRecord, InjectionTruth, QUERY_POLICY,
    collection_quality, validate_corpus_incident)
from .gold import BenchmarkGoldLabelV2, DEFAULT_GOLD_RUBRIC
from .gold_scoring import BenchmarkScoringProtocolV2
from .sanitization import canonical_bytes, digest
from .stage_c import admission_reference, publish, read_contract


class CorpusEpisodeInput(Contract):
    """Private operator input, including attempts that never yielded candidates."""
    truth: InjectionTruth | None = None
    production: CorpusProductionRecord
    gold: BenchmarkGoldLabelV2 | None = None
    split: SplitName | None = None
    synthetic: StrictBool = False

    @model_validator(mode="after")
    def linkage(self):
        if self.truth is None:
            if self.production.status not in {"recording_failed", "excluded"} or self.gold is not None or self.split is not None:
                raise ValueError("only early failed/excluded attempts may lack injection truth")
            return self
        if self.truth.private_episode_id != self.production.private_episode_id:
            raise ValueError("episode and production ledger identities differ")
        for name in ("capture_window", "source_snapshot_sha256", "capture_policy_id",
                "query_policy_id", "sanitizer_ruleset", "seed_services"):
            if getattr(self.truth, name) != getattr(self.production, name):
                raise ValueError(f"private production and truth disagree on {name}")
        if self.truth.candidate_id not in (None, self.production.candidate_id):
            raise ValueError("private candidate references disagree")
        if self.gold and self.gold.candidate_id != self.production.candidate_id:
            raise ValueError("Gold and production candidate identities differ")
        if self.split is not None and self.gold is None:
            raise ValueError("split assignment requires frozen diagnostic Gold")
        if self.synthetic and self.split not in (None, "SMOKE"):
            raise ValueError("synthetic data may only enter the SMOKE split")
        if self.split == "SMOKE" and not self.synthetic:
            raise ValueError("SMOKE is synthetic harness validation only")
        return self


class CorpusFreezeRequest(Contract):
    spec: CorpusFreezeSpec
    episodes: tuple[CorpusEpisodeInput, ...] = Field(min_length=1, max_length=MAX_EPISODES)


class FrozenCorpusBundle(Contract):
    execution: ExecutionCorpusManifest
    private: PrivateCorpusManifest
    qa: CorpusQASummary


def _sha(contract):
    return digest(canonical_bytes(contract.model_dump(mode="json")))


def _telemetry_fingerprint(snapshot):
    # Window/incident metadata must not turn a duplicate captured slice into an
    # independent episode. Actual record timestamps remain in the fingerprint.
    payload = snapshot.model_dump(mode="json", include={"traces", "logs", "metrics", "rpc_service_map", "collection_errors"})
    return digest(canonical_bytes(payload))


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _validate_gold(episode, admitted, spec):
    gold = episode.gold
    _require(gold is not None, "frozen Gold required")
    _require(gold.review_packet_id is not None, "blinded review packet is required before Gold freeze")
    _require(gold.frozen_at <= spec.frozen_at, "Gold must freeze before the corpus split")
    _require(gold.reviewer.reviewed_at >= episode.production.created_at, "Gold review predates its episode")
    for label_field, value in (("snapshot_sha256", admitted.snapshot_sha256),
            ("bundle_sha256", admitted.bundle_sha256), ("sanitizer_ruleset", spec.sanitizer_ruleset),
            ("capture_policy_id", spec.capture_policy_id), ("query_policy_id", spec.query_policy_id),
            ("gold_diagnostic_rubric_id", spec.gold_diagnostic_rubric_id)):
        _require(getattr(gold, label_field) == value, f"Gold {label_field} does not bind the admitted candidate/policy")


def _validate_inclusion(episode, admitted, spec, root):
    truth, production = episode.truth, episode.production
    _require(production.status in {"approved", "answerability_labeled", "included_dev",
        "included_replicate_holdout", "included_family_holdout"}, "manual approval/Gold lifecycle is incomplete")
    _require(not set(production.reason_codes) & {"structural_leakage", "sanitizer_destructive",
        "source_unstable", "review_rejected", "injection_not_effective", "window_policy_violation",
        "overlapping_window", "query_leakage", "duplicate_slice", "health_qa_failed", "recovery_failed",
        "capture_failed", "holdout_access_violation"}, "production exclusion blocks corpus membership")
    for field in ("capture_policy_id", "query_policy_id", "sanitizer_ruleset"):
        _require(getattr(truth, field) == getattr(spec, field), f"mixed {field} in frozen corpus")
    _require(admitted.ruleset == spec.sanitizer_ruleset, "mixed sanitizer rulesets")
    _require(truth.previous_fault_restored and truth.cooldown_completed, "episode independence requires restore and cooldown QA")
    _require(truth.pre_health is not None and truth.pre_health.passed, "workload/business-path pre-health QA required")
    _require(truth.recovery_health is not None and truth.recovery_health.passed, "recovery health QA required")
    _require(truth.capture_window is not None, "capture window missing")
    window = truth.capture_window
    if spec.phase == "phase_a":
        _require(spec.phase_a_capture_freeze.frozen_at < window.start,
            "Phase-A capture must follow the pilot-based capture-policy freeze")
    _require(truth.pre_health.at <= window.start, "pre-health QA must precede capture")
    _require((truth.recovery_health.at - window.end).total_seconds() >= spec.capture_policy.cooldown_seconds,
        "cooldown QA occurred before the frozen cooldown elapsed")
    _require((window.end - window.start).total_seconds() == spec.capture_policy.duration_seconds,
        "window_policy_violation: duration differs from frozen capture policy")
    _require(truth.recovery_health.at <= spec.frozen_at and production.updated_at <= spec.frozen_at,
        "corpus freeze predates completed production QA")
    _require(admitted.snapshot.incident.window == window, "candidate window differs from its independent source episode")
    expected_stages = ("ENV_READY", "STABILIZE", *(("INJECT",) if truth.mechanism != "none" else ()),
        "WARMUP", "CAPTURE", "INGEST_WAIT", "RESTORE", "COOLDOWN")
    capture_events = tuple(event for event in truth.lifecycle if event.stage in set(expected_stages) | {"INJECT"})
    _require(tuple(event.stage for event in capture_events) == expected_stages,
        "recorded capture lifecycle is incomplete or out of order")
    times = {event.stage: event.at for event in capture_events}
    _require((window.start-times["WARMUP"]).total_seconds() >= spec.capture_policy.warmup_seconds,
        "window_policy_violation: fixed warmup did not elapse")
    _require(window.start <= times["CAPTURE"] <= window.end and times["INGEST_WAIT"] >= window.end,
        "window_policy_violation: capture/ingest timestamps disagree with window")
    _require((times["RESTORE"]-times["INGEST_WAIT"]).total_seconds() >= spec.capture_policy.ingest_wait_seconds,
        "window_policy_violation: fixed ingest wait did not elapse")
    _require((truth.recovery_health.at-times["COOLDOWN"]).total_seconds() >= spec.capture_policy.cooldown_seconds,
        "window_policy_violation: fixed cooldown did not elapse")
    validate_corpus_incident(admitted.snapshot.incident)
    _require(episode.synthetic == (admitted.snapshot.provenance == "synthetic_wire_fixture"),
        "synthetic telemetry must be explicitly restricted to SMOKE")
    _require((spec.phase == "smoke") == episode.synthetic, "SMOKE and real corpus versions must remain separate")
    if truth.mechanism != "none":
        _require(truth.injection_effective is True, "injection_not_effective: exclude failed fault production")
    source_path = benchmark._safe(root, "quarantine", truth.quarantine_recording_id, "snapshot.json")
    source_bytes = benchmark._read_bytes(source_path)
    _require(digest(source_bytes) == truth.source_snapshot_sha256,
        "quarantine source hash differs from production QA")
    original = benchmark._benchmark_snapshot(benchmark._json(source_bytes))
    _require(truth.quality == production.quality == collection_quality(original, spec.capture_policy),
        "collection quality must describe the actual source under the frozen capture policy")
    # This operator phase is permitted private mapping access. Admission does not
    # need source identity, whereas corpus episode-independence certification does.
    audit = benchmark._json(benchmark._read_bytes(benchmark._safe(root, "review", admitted.candidate_id, "audit.json")))
    _require(audit.get("source_episode") == truth.quarantine_recording_id
        and audit.get("source_sha256") == truth.source_snapshot_sha256
        and audit.get("candidate_id") == admitted.candidate_id
        and audit.get("bundle_sha256") == admitted.bundle_sha256,
        "candidate does not derive from the declared independent quarantine episode")
    _validate_gold(episode, admitted, spec)


def _check_independence(episodes, admitted_by_id):
    windows, sources, recordings, candidates, fingerprints = [], set(), set(), set(), set()
    for episode in episodes:
        truth = episode.truth
        candidate = admitted_by_id[episode.gold.candidate_id]
        for value, seen in ((truth.quarantine_recording_id, recordings),
                (truth.source_snapshot_sha256, sources), (candidate.snapshot_sha256, candidates),
                (_telemetry_fingerprint(candidate.snapshot), fingerprints)):
            _require(value not in seen, "duplicate_slice: episodes must use distinct source recordings and telemetry")
            seen.add(value)
        windows.append(truth)
    windows.sort(key=lambda truth: truth.capture_window.start)
    for first, second in zip(windows, windows[1:]):
        _require(first.capture_window.end <= second.capture_window.start, "overlapping_window: episodes must not overlap")
        _require(first.recovery_health.at <= second.pre_health.at,
            "episode independence requires previous recovery before the next pre-health check")


def _validate_splits(episodes, spec):
    rules = {rule.scenario_family_id: rule for rule in spec.replicate_trial_rules}
    families = {}
    by_candidate = {episode.gold.candidate_id: episode for episode in episodes}
    for episode in episodes:
        families.setdefault(episode.truth.scenario_family_id, []).append(episode)
    for family_id, family in families.items():
        splits = {episode.split for episode in family}
        trials = [episode.truth.family_trial_index for episode in family]
        _require(len(trials) == len(set(trials)), "family trial indices must be unique")
        _require("FAMILY_HOLDOUT" not in splits or splits == {"FAMILY_HOLDOUT"},
            "scenario family cannot span DEV/replicate and FAMILY_HOLDOUT")
        if "REPLICATE_HOLDOUT" in splits:
            rule = rules.get(family_id)
            _require(rule is not None, "replicate holdout requires a predeclared sealed-trial rule")
            _require(rule.declared_at < min(episode.truth.capture_window.start for episode in family),
                "replicate rule must be declared before recording")
            for episode in family:
                allowed = rule.dev_trials if episode.split == "DEV" else rule.replicate_holdout_trials
                _require(episode.truth.family_trial_index in allowed, "episode violates predeclared replicate trial rule")
    if any(episode.split in HOLDOUTS for episode in episodes):
        _require(spec.holdout_telemetry_labels_and_ids_sealed, "holdout telemetry, labels and identities must remain sealed")
    for event in spec.holdout_access_events:
        episode = by_candidate.get(event.candidate_id)
        _require(episode is not None, "access event does not identify an included candidate")
        if episode.split not in HOLDOUTS:
            continue
        allowed = (event.role == "answerability_reviewer" and event.purpose == "blinded_review"
            and event.at <= episode.gold.frozen_at)
        allowed |= event.role == "qa_operator" and event.purpose == "qa_reconciliation" and event.at >= episode.gold.frozen_at
        _require(allowed, "holdout_access_violation: human tuning or pre-freeze execution invalidates holdout")


def _validate_qa_sources(root, episodes, spec):
    """Count actual recorded sources, even rejected ones, without assigning Gold."""
    recorded = {}
    for episode in episodes:
        production = episode.production
        if production.source_snapshot_sha256 is None:
            continue
        source = benchmark._safe(root, "quarantine", production.private_episode_id, "snapshot.json")
        source_bytes = benchmark._read_bytes(source)
        _require(digest(source_bytes) == production.source_snapshot_sha256,
            "QA source hash does not match the recorded quarantine bytes")
        snapshot = benchmark._benchmark_snapshot(benchmark._json(source_bytes))
        _require(snapshot.incident.window == production.capture_window, "QA source window mismatch")
        _require(episode.synthetic == (snapshot.provenance == "synthetic_wire_fixture"),
            "QA synthetic declaration does not match recorded source provenance")
        if spec.phase == "smoke":
            _require(episode.synthetic, "SMOKE QA cannot contain real recording sources")
        if production.capture_policy_id == spec.capture_policy_id:
            expected = collection_quality(snapshot, spec.capture_policy)
            if production.quality is not None:
                _require(production.quality == expected, "QA collection metadata differs from actual source quality")
            if episode.truth and episode.truth.quality is not None:
                _require(episode.truth.quality == expected, "QA injection record quality differs from actual source quality")
        recorded[production.private_episode_id] = not episode.synthetic
    return recorded


def _qa(episodes, admitted_by_id, corpus_id, spec, recorded_sources):
    records = [episode.production for episode in episodes]
    included = [episode for episode in episodes if episode.split is not None]
    labeled = [episode for episode in episodes if episode.gold is not None]
    def histogram(values):
        return dict(sorted(Counter(values).items()))
    return CorpusQASummary(phase=spec.phase, corpus_id=corpus_id, attempted_episode_count=len(episodes),
        successfully_recorded_count=len(recorded_sources), real_recorded_count=sum(recorded_sources.values()),
        sanitize_rejected_count=sum(bool(set(record.reason_codes) & {"sanitizer_destructive", "structural_leakage"}) for record in records),
        scan_blocked_count=sum(record.status == "scan_blocked" for record in records),
        review_rejected_count=sum(record.status == "review_rejected" or "review_rejected" in record.reason_codes for record in records),
        approved_count=len(admitted_by_id), included_count=len(included),
        answerability_distribution=histogram(episode.gold.answerability for episode in labeled),
        diagnostic_family_distribution=histogram(kind for episode in labeled for kind in set(claim.failure_mode for claim in episode.gold.expected_claims)),
        split_distribution=histogram(episode.split for episode in included),
        sanitizer_rulesets=tuple(sorted({record.sanitizer_ruleset for record in records})),
        capture_policy_ids=tuple(sorted({record.capture_policy_id for record in records})),
        query_policy_ids=tuple(sorted({record.query_policy_id for record in records})),
        seed_distribution=histogram(seed for record in records for seed in set(record.seed_services)),
        capture_duration_seconds_distribution=histogram(str((record.capture_window.end-record.capture_window.start).total_seconds())
            for record in records if record.capture_window is not None),
        truncated_signal_distribution=histogram(signal for record in records if record.quality for signal in record.quality.truncated_signals),
        collection_error_distribution=histogram(signal for episode in episodes for signal in set(
            episode.truth.collection_errors if episode.truth else
            episode.production.quality.collection_error_signals if episode.production.quality else ())),
        rejection_reason_histogram=histogram(reason for record in records for reason in record.reason_codes),
        real_multi_edge_count=sum(not episode.synthetic and len({(claim.caller, claim.callee)
            for claim in episode.gold.expected_claims}) > 1 for episode in included),
        injection_effective_but_insufficient_evidence_count=sum(episode.truth.injection_effective is True
            and episode.gold.answerability == "insufficient_evidence" for episode in labeled),
        unseen_family_positive_count=sum(episode.split == "FAMILY_HOLDOUT" and bool(episode.gold.expected_claims) for episode in included))


def inspect_corpus_qa(recordings_root, spec, episodes):
    """Private pilot QA before Gold/split freeze, including an all-failed pilot.

    This creates no execution manifest and infers no diagnostic judgments. Its
    provisional identity binds the supplied inputs, not an executable corpus.
    """
    request = CorpusFreezeRequest(spec=spec, episodes=tuple(episodes))
    root = benchmark._root(Path(recordings_root))
    ids = [episode.production.private_episode_id for episode in request.episodes]
    _require(len(ids) == len(set(ids)), "one ledger entry per attempted episode")
    recorded_sources = _validate_qa_sources(root, request.episodes, request.spec)
    admitted_by_id = {}
    for episode in request.episodes:
        if episode.gold is not None or episode.production.status in {"approved", "answerability_labeled", "included_dev",
                "included_replicate_holdout", "included_family_holdout"}:
            candidate_id = episode.production.candidate_id
            _require(candidate_id is not None, "approved production record lacks candidate identity")
            admitted = benchmark.open_approved_candidate(root, candidate_id)
            admitted_by_id[candidate_id] = admitted
            if episode.gold is not None:
                _validate_gold(episode, admitted, request.spec)
    provisional_id = structural_id("CORP-", {"kind": "unfrozen_pilot_qa", **request.model_dump(mode="json")})
    return _qa(request.episodes, admitted_by_id, provisional_id, request.spec, recorded_sources)


def freeze_corpus(recordings_root, spec, episodes, output):
    """Validate every human gate, then publish immutable private and safe projections."""
    request = CorpusFreezeRequest(spec=spec, episodes=tuple(episodes))
    spec, episodes = request.spec, request.episodes
    root, destination = benchmark._root(Path(recordings_root)), benchmark._root(Path(output))
    _require(not destination.is_relative_to(root) and not root.is_relative_to(destination),
        "corpus output and recordings roots must be separate")
    _require(spec.query_policy_id == QUERY_POLICY.id, "unknown neutral query policy")
    _require(spec.gold_diagnostic_rubric_id == DEFAULT_GOLD_RUBRIC.id, "unknown Gold diagnostic rubric")
    _require(spec.scoring_protocol_id == BenchmarkScoringProtocolV2().id, "P2.2 corpus requires the frozen BSP-v2 protocol")
    _require((spec.phase == "phase_a") == (spec.capture_policy.policy_name == "capture-policy-phase-a-v1"),
        "Phase-A requires its own capture policy freeze after pilot")
    ids = [episode.production.private_episode_id for episode in episodes]
    _require(len(ids) == len(set(ids)), "one ledger entry per attempted episode")
    candidates = [episode.production.candidate_id for episode in episodes if episode.production.candidate_id]
    _require(len(candidates) == len(set(candidates)), "one independent episode per candidate")
    recorded_sources = _validate_qa_sources(root, episodes, spec)
    admitted_by_id = {}
    for episode in episodes:
        if episode.gold is not None or episode.production.status in {"approved", "answerability_labeled", "included_dev",
                "included_replicate_holdout", "included_family_holdout"}:
            candidate_id = episode.production.candidate_id
            _require(candidate_id is not None, "approved production record lacks candidate identity")
            admitted = benchmark.open_approved_candidate(root, candidate_id)
            admitted_by_id[candidate_id] = admitted
            if episode.gold is not None:
                _validate_gold(episode, admitted, spec)
            if episode.split is not None:
                _validate_inclusion(episode, admitted, spec, root)
    included = [episode for episode in episodes if episode.split is not None]
    _require(bool(included), "cannot freeze an empty execution corpus")
    _check_independence(included, admitted_by_id)
    _validate_splits(included, spec)
    private_members = []
    for episode in sorted(included, key=lambda item: item.gold.candidate_id):
        truth, gold = episode.truth, episode.gold
        admitted = admitted_by_id[gold.candidate_id]
        private_members.append(PrivateCorpusMember(private_episode_id=truth.private_episode_id,
            candidate_id=gold.candidate_id, scenario_family_id=truth.scenario_family_id,
            family_trial_index=truth.family_trial_index, split=episode.split, admission=admission_reference(admitted),
            injection_truth_sha256=_sha(truth), production_record_sha256=_sha(episode.production),
            gold_label_id=gold.id, gold_label_sha256=_sha(gold), telemetry_fingerprint=_telemetry_fingerprint(admitted.snapshot),
            injection_truth_reference=f"episodes/{truth.private_episode_id}/injection.json",
            production_record_reference=f"episodes/{truth.private_episode_id}/production.json",
            gold_label_reference=f"gold/{gold.candidate_id}.json"))
    private = PrivateCorpusManifest(spec=spec, members=tuple(private_members),
        attempted_episodes=tuple(PrivateEpisodeReference(private_episode_id=episode.production.private_episode_id,
            injection_truth_sha256=_sha(episode.truth) if episode.truth else None,
            production_record_sha256=_sha(episode.production))
            for episode in sorted(episodes, key=lambda item: item.production.private_episode_id)))
    execution = ExecutionCorpusManifest(corpus_id=private.id, corpus_version=spec.corpus_version,
        frozen_at=spec.frozen_at, corpus_protocol_id=spec.corpus_protocol.id,
        capture_policy_id=spec.capture_policy_id, query_policy_id=spec.query_policy_id,
        expected_scoring_protocol_id=spec.scoring_protocol_id,
        members=tuple(ExecutionCorpusMember(admission=member.admission, split=member.split) for member in private.members))
    qa = _qa(episodes, admitted_by_id, private.id, spec, recorded_sources)
    receipt = CorpusFreezeReceipt(corpus_id=private.id, execution_sha256=_sha(execution), private_sha256=_sha(private), qa_sha256=_sha(qa))
    if destination.exists() and any(destination.iterdir()):
        previous = read_contract(benchmark._safe(destination, "freeze.json"), CorpusFreezeReceipt)
        _require(previous == receipt, "completed corpus version is immutable; use a new output/version")
        _require(load_execution_corpus(destination) == execution, "existing corpus projection differs")
        _require(digest(benchmark._read_bytes(benchmark._safe(destination, "private.json"))) == receipt.private_sha256,
            "private corpus archive digest mismatch")
        _require(digest(benchmark._read_bytes(benchmark._safe(destination, "qa.json"))) == receipt.qa_sha256,
            "corpus QA archive digest mismatch")
        for episode in episodes:
            for name, value in (("injection.json", episode.truth), ("production.json", episode.production)):
                if value is not None:
                    path = benchmark._safe(destination, "episodes", episode.production.private_episode_id, name)
                    _require(digest(benchmark._read_bytes(path)) == _sha(value), "private episode archive digest mismatch")
            if episode.gold is not None:
                path = benchmark._safe(destination, "gold", f"{episode.gold.candidate_id}.json")
                _require(digest(benchmark._read_bytes(path)) == _sha(episode.gold), "private Gold archive digest mismatch")
        return FrozenCorpusBundle(execution=execution, private=private, qa=qa)
    # Individual atomic files, completion receipt last. Partial output is never a
    # valid frozen corpus and is never silently overwritten on retry.
    for episode in episodes:
        for name, value in (("injection.json", episode.truth), ("production.json", episode.production)):
            if value is not None:
                publish(benchmark._safe(destination, "episodes", episode.production.private_episode_id, name), value.model_dump(mode="json"))
        if episode.gold is not None:
            publish(benchmark._safe(destination, "gold", f"{episode.gold.candidate_id}.json"), episode.gold.model_dump(mode="json"))
    for name, value in (("private.json", private), ("qa.json", qa), ("execution.json", execution), ("freeze.json", receipt)):
        publish(benchmark._safe(destination, name), value.model_dump(mode="json"))
    return FrozenCorpusBundle(execution=execution, private=private, qa=qa)


def _execution_projection(root):
    receipt = read_contract(benchmark._safe(root, "freeze.json"), CorpusFreezeReceipt)
    data = benchmark._read_bytes(benchmark._safe(root, "execution.json"))
    _require(digest(data) == receipt.execution_sha256, "execution corpus digest mismatch")
    manifest = ExecutionCorpusManifest.model_validate(benchmark._json(data))
    _require(manifest.corpus_id == receipt.corpus_id, "execution corpus identity mismatch")
    _require(data == canonical_bytes(manifest.model_dump(mode="json")), "execution corpus encoding must be canonical")
    return manifest


def validate_execution_access(output, manifest):
    root = benchmark._root(Path(output))
    access = benchmark._safe(root, "access")
    events = []
    if access.exists():
        paths = sorted(access.glob("*.json"))
        _require(len(paths) <= 1024, "access ledger bound exceeded")
        for path in paths:
            event = read_contract(path, HoldoutAccessEvent)
            _require(path.stem == structural_id("CA-", event.model_dump(mode="json")), "access ledger identity mismatch")
            events.append(event)
    members = {member.admission.candidate_id: member for member in manifest.members}
    invalid = set()
    for event in events:
        _require(event.candidate_id in members, "access event outside corpus")
        _require(event.at >= manifest.frozen_at, "post-freeze access cannot predate corpus freeze")
        if members[event.candidate_id].split in HOLDOUTS:
            if not (event.role == "stage_c_harness" and event.purpose == "declared_checkpoint"
                    and event.material == "telemetry"):
                invalid.add(event.candidate_id)
    return HoldoutAccessAssessment(corpus_id=manifest.corpus_id, split_valid=not invalid, events=tuple(events),
        invalidated_candidate_ids=tuple(sorted(invalid)),
        reason="holdout_access_violation" if invalid else "holdout_access_compliant")


def load_execution_corpus(output):
    """Execution-safe loader: never opens private manifest, QA, Gold or truth files."""
    root = benchmark._root(Path(output))
    manifest = _execution_projection(root)
    assessment = validate_execution_access(root, manifest)
    _require(assessment.split_valid, "holdout_access_violation: split invalidated; freeze a new corpus version")
    return manifest


def record_holdout_access(output, event):
    """Append-only human/machine access declaration; violations remain recorded."""
    root = benchmark._root(Path(output))
    manifest = _execution_projection(root)
    event = HoldoutAccessEvent.model_validate(event)
    _require(event.candidate_id in {member.admission.candidate_id for member in manifest.members}, "access event outside corpus")
    _require(event.at >= manifest.frozen_at, "post-freeze access cannot predate corpus freeze")
    path = benchmark._safe(root, "access", f"{structural_id('CA-', event.model_dump(mode='json'))}.json")
    if path.exists():
        _require(read_contract(path, HoldoutAccessEvent) == event, "access event digest conflict")
    else:
        publish(path, event.model_dump(mode="json"))
    return validate_execution_access(root, manifest)
