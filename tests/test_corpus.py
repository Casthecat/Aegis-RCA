"""Temporary synthetic wire data exercises private corpus gates; no real corpus is recorded."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from aegis_rca.evaluation import benchmark
from aegis_rca.evaluation.capture_freeze import CapturePolicyDecision, CapturePolicyFreeze
from aegis_rca.evaluation.corpus import (CorpusEpisodeInput, freeze_corpus,
    inspect_corpus_qa, load_execution_corpus, record_holdout_access)
from aegis_rca.evaluation.corpus_models import (CorpusFreezeSpec, ExecutionCorpusManifest,
    HoldoutAccessEvent, ReplicateTrialRule)
from aegis_rca.evaluation.corpus_recording import (CaptureQuality, CorpusProductionRecord,
    HealthQA, InjectionTruth, LifecycleEvent, NEUTRAL_QUERY, PILOT_CAPTURE_POLICY, QUERY_POLICY, collection_quality)
from aegis_rca.evaluation.gold import (BenchmarkGoldLabelV2, DEFAULT_GOLD_RUBRIC,
    ExpectedDiagnosticClaim, GoldReviewerProvenance)
from aegis_rca.evaluation.gold_scoring import BenchmarkScoringProtocolV2
from aegis_rca.evaluation.sanitization import digest
from aegis_rca.models.incident import TimeWindow
from test_benchmark_admission import approved
from test_sanitization import fixture


NOW = datetime(2026, 9, 18, 12, tzinfo=timezone.utc)


def altered(value, **updates):
    return type(value).model_validate({**value.model_dump(), **updates})


def spec(**updates):
    return CorpusFreezeSpec(corpus_version="test-pilot-v1", frozen_at=NOW + timedelta(days=1),
        frozen_by="test-operator", capture_policy_id=PILOT_CAPTURE_POLICY.id,
        query_policy_id=QUERY_POLICY.id, gold_diagnostic_rubric_id=DEFAULT_GOLD_RUBRIC.id,
        scoring_protocol_id=BenchmarkScoringProtocolV2().id, performance_results_unseen=True, **updates)


def episode(tmp_path, *, index=1, family=None, split="DEV", mechanism="payment_unreachable",
            query=NEUTRAL_QUERY, start=None, snapshot=None, synthetic=False):
    start = start or NOW + timedelta(minutes=10*index)
    window = TimeWindow(start=start, end=start+timedelta(seconds=60))
    original = snapshot or fixture()
    incident = altered(original.incident, query=query, window=window)
    # This is a unit-test stub for recorded source bytes, never an actual recording
    # or a result that is interpreted as real performance.
    spans = tuple(altered(span, start=start) for span in original.traces.spans)
    logs = tuple(altered(log, timestamp=start) for log in original.logs.logs)
    snapshot = altered(original, incident=incident, provenance="synthetic_wire_fixture" if synthetic else "live_capture",
        traces=altered(original.traces, spans=spans), logs=altered(original.logs, logs=logs))
    root, cid, source, _ = approved(tmp_path, snapshot)
    admitted = benchmark.open_approved_candidate(root, cid)
    episode_id = source.parent.name
    health = HealthQA(at=start-timedelta(seconds=60), reviewer="operator", workload_ran=True,
        business_path_succeeded=True, no_residual_fault=True, services_healthy=True)
    recovery = altered(health, at=window.end+timedelta(seconds=45))
    quality = collection_quality(snapshot, PILOT_CAPTURE_POLICY)
    stages = [("ENV_READY", -61), ("STABILIZE", -60)]
    if mechanism != "none":
        stages.append(("INJECT", -31))
    stages += [("WARMUP", -30), ("CAPTURE", 0), ("INGEST_WAIT", 60), ("RESTORE", 75), ("COOLDOWN", 75)]
    lifecycle = tuple(LifecycleEvent(stage=stage, at=start+timedelta(seconds=offset)) for stage, offset in stages)
    flag, variant = {"payment_unreachable": ("paymentUnreachable", "on"),
        "payment_failure": ("paymentFailure", "100%"), "none": (None, None)}[mechanism]
    truth = InjectionTruth(private_episode_id=episode_id, candidate_id=cid,
        scenario_family_id=family or str(uuid4()), family_trial_index=index, mechanism=mechanism,
        control_flag=flag, variant=variant, private_target="secret-target",
        quarantine_recording_id=episode_id, capture_window=window,
        source_snapshot_sha256=digest(source.read_bytes()), capture_policy_id=PILOT_CAPTURE_POLICY.id,
        seed_justification="legitimate checkout alert context", previous_fault_restored=True,
        cooldown_completed=True, injection_effective=None if mechanism == "none" else True,
        pre_health=health, recovery_health=recovery, quality=quality, lifecycle=lifecycle)
    production = CorpusProductionRecord(private_episode_id=episode_id, candidate_id=cid,
        status="answerability_labeled", created_at=health.at, updated_at=recovery.at,
        capture_window=window, source_snapshot_sha256=truth.source_snapshot_sha256,
        capture_policy_id=PILOT_CAPTURE_POLICY.id, quality=quality)
    reviewer = GoldReviewerProvenance(reviewer_id="blinded-reviewer", mode="independent",
        reviewed_at=recovery.at+timedelta(seconds=60), approved_telemetry_reviewed=True,
        injection_truth_sealed=True, stage_c_results_unseen=True, prior_injection_knowledge=False,
        coverage_reviewed=True, coverage_sufficient=True)
    gold = BenchmarkGoldLabelV2(candidate_id=cid, snapshot_sha256=admitted.snapshot_sha256,
        bundle_sha256=admitted.bundle_sha256, capture_policy_id=PILOT_CAPTURE_POLICY.id,
        query_policy_id=QUERY_POLICY.id, review_packet_id="GRP-"+"1"*64,
        frozen_at=reviewer.reviewed_at+timedelta(seconds=60), reviewer=reviewer,
        answerability="mechanism_identifiable", expected_claims=(ExpectedDiagnosticClaim(caller="checkout", callee="payment"),),
        claim_labels_exhaustive=False)
    return root, CorpusEpisodeInput(truth=truth, production=production, gold=gold, split=split, synthetic=synthetic)


def test_freeze_is_private_and_execution_loader_never_reads_gold_or_truth(tmp_path, monkeypatch):
    root, item = episode(tmp_path)
    output = tmp_path / "corpus"
    bundle = freeze_corpus(root, spec(), (item,), output)
    public = (output / "execution.json").read_text()
    for secret in ("paymentUnreachable", "secret-target", "mechanism_identifiable",
            item.truth.scenario_family_id, item.truth.private_episode_id, "expected_claims", "gold/"):
        assert secret not in public
    before = {p: p.read_bytes() for p in output.rglob("*.json")}
    original_read = benchmark._read_bytes
    def safe_read(path):
        if path.resolve().is_relative_to(output.resolve()):
            assert path.name in {"execution.json", "freeze.json"}
        return original_read(path)
    monkeypatch.setattr(benchmark, "_read_bytes", safe_read)
    assert load_execution_corpus(output) == bundle.execution
    monkeypatch.setattr(benchmark, "_read_bytes", original_read)
    assert freeze_corpus(root, spec(), (item,), output) == bundle
    assert before == {p: p.read_bytes() for p in before}
    assert bundle.private.spec.benchmark_budget.id.startswith("BB-")
    assert bundle.private.spec.benchmark_execution_protocol.id.startswith("BEP-")


@pytest.mark.parametrize("damage", ["pending", "mutated", "gold_missing", "packet_missing", "stale_gold"])
def test_manual_approval_and_frozen_gold_are_required(tmp_path, damage):
    root, item = episode(tmp_path)
    if damage == "pending":
        (root / "review" / item.gold.candidate_id / "decision.json").unlink()
    elif damage == "mutated":
        path = root / "candidates" / item.gold.candidate_id / "snapshot.json"
        path.write_bytes(path.read_bytes()+b"\n")
    elif damage == "gold_missing":
        with pytest.raises(ValidationError):
            altered(item, gold=None)
        return
    elif damage == "packet_missing":
        item = altered(item, gold=altered(item.gold, review_packet_id=None))
    else:
        item = altered(item, gold=altered(item.gold, snapshot_sha256="0"*64))
    with pytest.raises((ValueError, OSError)):
        freeze_corpus(root, spec(), (item,), tmp_path/"bad")


def test_fault_hinted_query_rejected_without_rewriting_candidate(tmp_path):
    root, item = episode(tmp_path, query="Why is the payment dependency unreachable?")
    path = root/"candidates"/item.gold.candidate_id/"snapshot.json"
    before = path.read_bytes()
    with pytest.raises(ValueError, match="query_leakage"):
        freeze_corpus(root, spec(), (item,), tmp_path/"corpus")
    assert path.read_bytes() == before


@pytest.mark.parametrize("field,value", [("previous_fault_restored", False), ("cooldown_completed", False),
    ("injection_effective", False), ("pre_health", None), ("recovery_health", None)])
def test_episode_readiness_and_recovery_gates(tmp_path, field, value):
    root, item = episode(tmp_path)
    item = altered(item, truth=altered(item.truth, **{field: value}))
    with pytest.raises(ValueError):
        freeze_corpus(root, spec(), (item,), tmp_path/"corpus")


def test_nonoverlapping_capture_windows_required(tmp_path):
    root, first = episode(tmp_path)
    _, second = episode(tmp_path, index=2, start=first.truth.capture_window.start+timedelta(seconds=30))
    with pytest.raises(ValueError, match="overlapping_window"):
        freeze_corpus(root, spec(), (first, second), tmp_path/"corpus")


def test_duplicate_snapshot_slice_not_an_independent_episode(tmp_path):
    root, first = episode(tmp_path)
    _, second = episode(tmp_path, index=2, start=first.truth.capture_window.start)
    with pytest.raises(ValueError, match="duplicate_slice"):
        freeze_corpus(root, spec(), (first, second), tmp_path/"corpus")


def test_family_must_not_cross_dev_and_family_holdout(tmp_path):
    family = str(uuid4())
    root, first = episode(tmp_path, family=family)
    _, second = episode(tmp_path, family=family, index=2, split="FAMILY_HOLDOUT")
    with pytest.raises(ValueError, match="scenario family"):
        freeze_corpus(root, spec(), (first, second), tmp_path/"corpus")


def test_replicate_holdout_requires_predeclared_sealed_trial_rule(tmp_path):
    family = str(uuid4())
    root, first = episode(tmp_path, family=family)
    _, second = episode(tmp_path, family=family, index=2, split="REPLICATE_HOLDOUT")
    with pytest.raises(ValueError, match="predeclared"):
        freeze_corpus(root, spec(), (first, second), tmp_path/"missing")
    rule = ReplicateTrialRule(scenario_family_id=family, declared_at=NOW-timedelta(days=1),
        dev_trials=(1,), replicate_holdout_trials=(2,), telemetry_labels_and_ids_sealed=True)
    bundle = freeze_corpus(root, spec(replicate_trial_rules=(rule,), holdout_telemetry_labels_and_ids_sealed=True),
        (first, second), tmp_path/"corpus")
    assert bundle.qa.split_distribution == {"DEV": 1, "REPLICATE_HOLDOUT": 1}
    assert bundle.qa.unseen_family_positive_count == 0
    with pytest.raises(ValueError, match="sealed"):
        freeze_corpus(root, altered(spec(replicate_trial_rules=(rule,)), holdout_telemetry_labels_and_ids_sealed=False),
            (first, second), tmp_path/"unsealed")


def test_holdout_access_violation_remains_recorded_and_blocks_execution(tmp_path):
    root, item = episode(tmp_path, split="FAMILY_HOLDOUT")
    output = tmp_path/"corpus"
    freeze_corpus(root, spec(holdout_telemetry_labels_and_ids_sealed=True), (item,), output)
    machine = HoldoutAccessEvent(candidate_id=item.gold.candidate_id, at=spec().frozen_at,
        actor="offline-harness", role="stage_c_harness", material="telemetry",
        purpose="declared_checkpoint", checkpoint_id="checkpoint-1")
    assert record_holdout_access(output, machine).split_valid
    assert load_execution_corpus(output)
    human = altered(machine, actor="developer", role="developer", purpose="manual_inspection", checkpoint_id=None)
    result = record_holdout_access(output, human)
    assert not result.split_valid
    assert result.invalidated_candidate_ids == (item.gold.candidate_id,)
    with pytest.raises(ValueError, match="holdout_access_violation"):
        load_execution_corpus(output)
    assert len(list((output/"access").glob("*.json"))) == 2


def test_mixed_sanitizer_rulesets_rejected_by_execution_contract(tmp_path):
    root, item = episode(tmp_path)
    manifest = freeze_corpus(root, spec(), (item,), tmp_path/"corpus").execution
    payload = manifest.model_dump()
    payload["members"][0]["admission"]["ruleset"] = "new-unreviewed-ruleset"
    with pytest.raises(ValidationError, match="mixed sanitizer"):
        ExecutionCorpusManifest.model_validate(payload)


def test_all_attempted_and_rejected_episodes_are_counted(tmp_path):
    root, included = episode(tmp_path)
    _, rejected = episode(tmp_path, index=2)
    rejected = altered(rejected, gold=None, split=None,
        production=altered(rejected.production, status="review_rejected", reason_codes=("review_rejected",)))
    episode_id = str(uuid4())
    failed_truth = InjectionTruth(private_episode_id=episode_id, scenario_family_id=str(uuid4()),
        family_trial_index=1, mechanism="none", quarantine_recording_id=episode_id,
        capture_policy_id=PILOT_CAPTURE_POLICY.id, seed_justification="entry alert")
    failed_record = CorpusProductionRecord(private_episode_id=episode_id, status="recording_failed",
        reason_codes=("capture_failed",), created_at=NOW, updated_at=NOW,
        capture_policy_id=PILOT_CAPTURE_POLICY.id)
    failed = CorpusEpisodeInput(truth=failed_truth, production=failed_record)
    bundle = freeze_corpus(root, spec(), (included, rejected, failed), tmp_path/"corpus")
    assert bundle.qa.attempted_episode_count == 3
    assert bundle.qa.successfully_recorded_count == 2
    assert bundle.qa.approved_count == bundle.qa.included_count == 1
    assert bundle.qa.review_rejected_count == 1
    assert bundle.qa.rejection_reason_histogram == {"capture_failed": 1, "review_rejected": 1}
    assert len(bundle.private.attempted_episodes) == 3


def test_no_injection_control_can_remain_insufficient_never_auto_healthy(tmp_path):
    root, item = episode(tmp_path, mechanism="none")
    reviewer = altered(item.gold.reviewer, coverage_sufficient=False)
    gold = altered(item.gold, answerability="insufficient_evidence", expected_claims=(), reviewer=reviewer)
    item = altered(item, gold=gold)
    bundle = freeze_corpus(root, spec(), (item,), tmp_path/"corpus")
    assert bundle.qa.answerability_distribution == {"insufficient_evidence": 1}
    assert bundle.private.members[0].gold_label_id == gold.id


def test_partial_or_changed_corpus_never_silently_completes(tmp_path):
    root, item = episode(tmp_path)
    output = tmp_path/"corpus"
    output.mkdir()
    (output/"execution.json").write_text("{}")
    with pytest.raises((ValueError, OSError)):
        freeze_corpus(root, spec(), (item,), output)
    valid = tmp_path/"valid"
    freeze_corpus(root, spec(), (item,), valid)
    path = valid/"execution.json"
    path.write_bytes(path.read_bytes()+b"\n")
    with pytest.raises(ValueError, match="digest"):
        load_execution_corpus(valid)


def test_private_gold_mutation_prevents_frozen_corpus_reuse(tmp_path):
    root, item = episode(tmp_path)
    output = tmp_path/"corpus"
    freeze_corpus(root, spec(), (item,), output)
    path = output/"gold"/f"{item.gold.candidate_id}.json"
    path.write_bytes(path.read_bytes()+b"\n")
    with pytest.raises(ValueError, match="Gold archive digest"):
        freeze_corpus(root, spec(), (item,), output)


def test_declared_source_must_be_the_actual_sanitizer_source(tmp_path):
    root, first = episode(tmp_path)
    _, other = episode(tmp_path, index=2, start=first.truth.capture_window.start)
    # Same window and source semantics do not authorize swapping episode identity.
    truth = altered(first.truth, private_episode_id=other.truth.private_episode_id,
        quarantine_recording_id=other.truth.quarantine_recording_id,
        source_snapshot_sha256=other.truth.source_snapshot_sha256)
    production = altered(first.production, private_episode_id=truth.private_episode_id,
        source_snapshot_sha256=truth.source_snapshot_sha256)
    with pytest.raises(ValueError, match="does not derive"):
        freeze_corpus(root, spec(), (altered(first, truth=truth, production=production),), tmp_path/"corpus")


def test_next_episode_waits_for_previous_recovery_not_only_capture_end(tmp_path):
    root, first = episode(tmp_path)
    _, second = episode(tmp_path, index=2, start=first.truth.capture_window.end+timedelta(seconds=20))
    with pytest.raises(ValueError, match="previous recovery"):
        freeze_corpus(root, spec(), (first, second), tmp_path/"corpus")


def test_pilot_qa_can_count_early_failed_attempt_without_truth_or_gold(tmp_path):
    episode_id = str(uuid4())
    record = CorpusProductionRecord(private_episode_id=episode_id, status="recording_failed",
        reason_codes=("lock_unavailable",), created_at=NOW, updated_at=NOW,
        capture_policy_id=PILOT_CAPTURE_POLICY.id)
    item = CorpusEpisodeInput(production=record)
    qa = inspect_corpus_qa(tmp_path/"recordings", spec(), (item,))
    assert qa.attempted_episode_count == 1
    assert qa.successfully_recorded_count == qa.approved_count == qa.included_count == 0
    assert qa.answerability_distribution == {}
    assert qa.rejection_reason_histogram == {"lock_unavailable": 1}
    assert not (tmp_path/"recordings").exists()


def test_early_failure_without_truth_is_persisted_in_frozen_attempt_ledger(tmp_path):
    root, included = episode(tmp_path)
    record = CorpusProductionRecord(private_episode_id=str(uuid4()), status="recording_failed",
        reason_codes=("capture_failed",), created_at=NOW, updated_at=NOW,
        capture_policy_id=PILOT_CAPTURE_POLICY.id)
    output = tmp_path/"corpus"
    bundle = freeze_corpus(root, spec(), (included, CorpusEpisodeInput(production=record)), output)
    assert bundle.qa.attempted_episode_count == 2
    ref = next(row for row in bundle.private.attempted_episodes if row.private_episode_id == record.private_episode_id)
    assert ref.injection_truth_sha256 is None
    assert (output/"episodes"/record.private_episode_id/"production.json").exists()
    assert not (output/"episodes"/record.private_episode_id/"injection.json").exists()


def test_phase_a_requires_policy_frozen_after_pilot_quality_review():
    policy = altered(PILOT_CAPTURE_POLICY, policy_name="capture-policy-phase-a-v1")
    payload = {**spec().model_dump(), "phase": "phase_a", "capture_policy": policy,
        "capture_policy_id": policy.id}
    with pytest.raises(ValidationError, match="after pilot QA"):
        CorpusFreezeSpec.model_validate(payload)
    decision = CapturePolicyDecision(reviewer="operator", reviewed_at=NOW-timedelta(days=2),
        collection_quality_reviewed=True, workload_and_source_stability_reviewed=True,
        ingestion_lag_and_limits_reviewed=True, sanitizer_leakage_reviewed=True, aegis_results_not_used=True)
    frozen = CapturePolicyFreeze(policy=policy, pilot_qa_sha256="a"*64, pilot_attempted_count=4,
        pilot_recorded_count=3, decision=decision, frozen_at=NOW-timedelta(days=1))
    valid = CorpusFreezeSpec.model_validate({**payload, "phase_a_capture_freeze": frozen})
    assert valid.phase_a_capture_freeze.id.startswith("CPF-")
    with pytest.raises(ValidationError, match="pilot and SMOKE"):
        altered(valid, phase="pilot")


def test_phase_a_policy_freeze_cannot_postdate_corpus_freeze():
    policy = altered(PILOT_CAPTURE_POLICY, policy_name="capture-policy-phase-a-v1")
    decision = CapturePolicyDecision(reviewer="operator", reviewed_at=NOW,
        collection_quality_reviewed=True, workload_and_source_stability_reviewed=True,
        ingestion_lag_and_limits_reviewed=True, sanitizer_leakage_reviewed=True, aegis_results_not_used=True)
    frozen = CapturePolicyFreeze(policy=policy, pilot_qa_sha256="b"*64, pilot_attempted_count=3,
        pilot_recorded_count=3, decision=decision, frozen_at=spec().frozen_at)
    with pytest.raises(ValidationError, match="precede corpus"):
        altered(spec(), phase="phase_a", capture_policy=policy, capture_policy_id=policy.id,
            phase_a_capture_freeze=frozen)


@pytest.mark.parametrize("damage", ["missing_lifecycle", "short_warmup", "short_ingest", "short_cooldown", "false_quality"])
def test_frozen_capture_timing_and_quality_are_checked_against_source(tmp_path, damage):
    root, item = episode(tmp_path)
    truth = item.truth
    if damage == "missing_lifecycle":
        truth = altered(truth, lifecycle=())
    elif damage in {"short_warmup", "short_ingest"}:
        changes = {"short_warmup": ("WARMUP", truth.capture_window.start-timedelta(seconds=1)),
            "short_ingest": ("INGEST_WAIT", truth.capture_window.end+timedelta(seconds=14))}
        stage, at = changes[damage]
        truth = altered(truth, lifecycle=tuple(altered(event, at=at) if event.stage == stage else event for event in truth.lifecycle))
    elif damage == "short_cooldown":
        truth = altered(truth, recovery_health=altered(truth.recovery_health, at=truth.capture_window.end+timedelta(seconds=30)))
    else:
        truth = altered(truth, quality=CaptureQuality())
    with pytest.raises(ValueError, match="lifecycle|policy_violation|quality"):
        freeze_corpus(root, spec(), (altered(item, truth=truth),), tmp_path/"corpus")


def test_smoke_qa_does_not_count_synthetic_recordings_as_real_pilot(tmp_path):
    root, item = episode(tmp_path, split="SMOKE", synthetic=True)
    qa = inspect_corpus_qa(root, spec(phase="smoke"), (item,))
    assert qa.phase == "smoke"
    assert qa.successfully_recorded_count == 1
    assert qa.real_recorded_count == 0
    # Calling the QA phase pilot cannot turn the same bytes into a live episode.
    pilot = inspect_corpus_qa(root, spec(), (item,))
    assert pilot.phase == "pilot" and pilot.real_recorded_count == 0
    with pytest.raises(ValidationError, match="SMOKE QA"):
        altered(qa, real_recorded_count=1)


@pytest.mark.parametrize("damage", ["source_bytes", "source_window", "provenance", "quality"])
def test_pre_gold_pilot_qa_validates_all_recorded_source_claims(tmp_path, damage):
    root, item = episode(tmp_path)
    item = altered(item, gold=None, split=None,
        production=altered(item.production, status="review_rejected", reason_codes=("review_rejected",)))
    if damage == "source_bytes":
        path = root/"quarantine"/item.production.private_episode_id/"snapshot.json"
        path.write_bytes(path.read_bytes()+b"\n")
    elif damage == "source_window":
        window = altered(item.production.capture_window, end=item.production.capture_window.end+timedelta(seconds=1))
        item = altered(item, truth=altered(item.truth, capture_window=window),
            production=altered(item.production, capture_window=window))
    elif damage == "provenance":
        item = altered(item, synthetic=True)
    else:
        item = altered(item, production=altered(item.production, quality=CaptureQuality()))
    with pytest.raises(ValueError, match="QA"):
        inspect_corpus_qa(root, spec(), (item,))


def test_rejected_but_real_recorded_episode_remains_pilot_qa_context(tmp_path):
    root, item = episode(tmp_path)
    item = altered(item, gold=None, split=None,
        production=altered(item.production, status="review_rejected", reason_codes=("review_rejected",)))
    qa = inspect_corpus_qa(root, spec(), (item,))
    assert qa.phase == "pilot"
    assert qa.real_recorded_count == qa.successfully_recorded_count == 1
    assert qa.approved_count == 0
    assert qa.review_rejected_count == 1


def test_corpus_freeze_requires_explicit_unseen_results_attestation():
    payload = spec().model_dump()
    del payload["performance_results_unseen"]
    with pytest.raises(ValidationError, match="performance_results_unseen"):
        CorpusFreezeSpec.model_validate(payload)
    assert spec().holdout_telemetry_labels_and_ids_sealed is False


def test_replicate_rule_requires_explicit_sealing_attestation():
    with pytest.raises(ValidationError, match="telemetry_labels_and_ids_sealed"):
        ReplicateTrialRule(scenario_family_id=str(uuid4()), declared_at=NOW,
            dev_trials=(1,), replicate_holdout_trials=(2,))


def test_holdout_membership_requires_explicit_sealing_at_freeze(tmp_path):
    root, item = episode(tmp_path, split="FAMILY_HOLDOUT")
    with pytest.raises(ValueError, match="must remain sealed"):
        freeze_corpus(root, spec(), (item,), tmp_path/"corpus")
