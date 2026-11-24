"""P2.2 operator CLI and execution-safe checkpoint integration; no live recording."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from uuid import uuid4

import pytest

from aegis_rca.evaluation import benchmark, corpus_execution, corpus_production, stage_c
from aegis_rca.evaluation.__main__ import main
from aegis_rca.evaluation.capture_freeze import freeze_capture_policy, CapturePolicyDecision
from aegis_rca.evaluation.corpus import freeze_corpus, inspect_corpus_qa, CorpusFreezeRequest, record_holdout_access
from aegis_rca.evaluation.corpus_models import HoldoutAccessEvent
from aegis_rca.evaluation.corpus_production import update_production
from aegis_rca.evaluation.corpus_recording import CapturePolicy, QUERY_POLICY, PILOT_CAPTURE_POLICY
from aegis_rca.evaluation.sanitization import canonical_bytes
from aegis_rca.evaluation.stage_c import publish
from test_corpus import episode, spec, altered, NOW


class LaterClock:
    @classmethod
    def now(cls, tz=None):
        return NOW+timedelta(days=2)


def test_init_pilot_cli_creates_only_frozen_protocols(tmp_path, capsys):
    main(["corpus", "init-pilot", "--output", str(tmp_path/"protocol")])
    result = json.loads(capsys.readouterr().out)
    assert result["recordings_started"] == 0
    assert result["capture_policy_id"].startswith("CP-")
    assert {p.name for p in (tmp_path/"protocol").iterdir()} == {
        "capture-policy.json", "query-policy.json", "demo-resource-profile.json", "rubric.json", "scoring-protocol.json"}
    assert result["demo_resource_profile_id"] == PILOT_CAPTURE_POLICY.demo_resource_profile_id
    assert not (tmp_path/"recordings").exists()
    with pytest.raises(SystemExit):
        main(["corpus", "execute", "--corpus", "unused", "--checkpoint", "invalid", "--output", "unused", "--snapshot", "forbidden"])


async def test_corpus_execution_reads_no_private_corpus_truth_or_gold(tmp_path, monkeypatch):
    root, item = episode(tmp_path)
    corpus = tmp_path/"corpus"
    frozen = freeze_corpus(root, spec(), (item,), corpus)
    monkeypatch.setattr(corpus_execution, "datetime", LaterClock)
    checkpoint = corpus_execution.declare_checkpoint(corpus, ("DEV",), "checkpoint-custodian")
    original = benchmark._read_bytes
    def guarded(path):
        path = Path(path)
        if path.resolve().is_relative_to(corpus.resolve()):
            relative = path.relative_to(corpus)
            assert path.name in {"execution.json", "freeze.json"} or relative.parts[0] in {"checkpoints", "access"}
        return original(path)
    monkeypatch.setattr(benchmark, "_read_bytes", guarded)
    result = await corpus_execution.execute_corpus(root, corpus, checkpoint.id, tmp_path/"experiment")
    assert result["corpus_id"] == frozen.execution.corpus_id
    assert result["experiment_summary"]["completed_trial_count"] == 3
    assert result["experiment_summary"]["by_variant"]["B0"]["paired_tool_calls"] == 2
    assert result["experiment_summary"]["by_variant"]["B2"]["paired_supported"] == 1
    assert result["scoring_protocol_id"] == frozen.execution.expected_scoring_protocol_id


async def test_frozen_corpus_hash_mismatch_stops_before_backend_construction(tmp_path, monkeypatch):
    root, item = episode(tmp_path)
    corpus = tmp_path/"corpus"
    freeze_corpus(root, spec(), (item,), corpus)
    monkeypatch.setattr(corpus_execution, "datetime", LaterClock)
    checkpoint = corpus_execution.declare_checkpoint(corpus, ("DEV",), "custodian")
    original = benchmark.open_approved_candidate
    def mismatched(*args):
        return original(*args).model_copy(update={"snapshot_sha256":"a"*64})
    monkeypatch.setattr(benchmark, "open_approved_candidate", mismatched)
    monkeypatch.setattr(stage_c, "ReplayBackend", lambda *_: pytest.fail("mismatched corpus executed"))
    result = await corpus_execution.execute_corpus(root, corpus, checkpoint.id, tmp_path/"experiment")
    assert result["experiment_summary"]["completed_trial_count"] == 0
    assert len(result["experiment_summary"]["harness_failures"]) == 3


async def test_human_holdout_access_blocks_checkpoint_execution(tmp_path, monkeypatch):
    root, item = episode(tmp_path, split="FAMILY_HOLDOUT")
    corpus = tmp_path/"corpus"
    freeze_corpus(root, spec(holdout_telemetry_labels_and_ids_sealed=True), (item,), corpus)
    monkeypatch.setattr(corpus_execution, "datetime", LaterClock)
    checkpoint = corpus_execution.declare_checkpoint(corpus, ("FAMILY_HOLDOUT",), "custodian")
    record_holdout_access(corpus, HoldoutAccessEvent(candidate_id=item.gold.candidate_id,
        at=LaterClock.now(), actor="developer", role="developer", material="telemetry", purpose="manual_inspection"))
    monkeypatch.setattr(stage_c, "ReplayBackend", lambda *_: pytest.fail("invalidated holdout executed"))
    with pytest.raises(ValueError, match="holdout_access_violation"):
        await corpus_execution.execute_corpus(root, corpus, checkpoint.id, tmp_path/"experiment")


@pytest.mark.parametrize("directory", ["candidates", "quarantine", "private"])
async def test_corpus_execution_output_cannot_mutate_recording_roots(tmp_path, monkeypatch, directory):
    root, item = episode(tmp_path)
    corpus = tmp_path/"corpus"
    freeze_corpus(root, spec(), (item,), corpus)
    monkeypatch.setattr(corpus_execution, "datetime", LaterClock)
    checkpoint = corpus_execution.declare_checkpoint(corpus, ("DEV",), "custodian")
    before = {p:p.read_bytes() for p in root.rglob("*") if p.is_file()}
    with pytest.raises(ValueError, match="outside immutable"):
        await corpus_execution.execute_corpus(root, corpus, checkpoint.id,
            root/directory/(item.gold.candidate_id if directory == "candidates" else item.production.private_episode_id))
    assert before == {p:p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert not (corpus/"access").exists()


def test_checkpoint_requires_frozen_split_and_declared_time(tmp_path, monkeypatch):
    root, item = episode(tmp_path)
    corpus = tmp_path/"corpus"
    freeze_corpus(root, spec(), (item,), corpus)
    class EarlyClock:
        @classmethod
        def now(cls, tz=None):
            return NOW
    monkeypatch.setattr(corpus_execution, "datetime", EarlyClock)
    with pytest.raises(ValueError, match="freeze must precede"):
        corpus_execution.declare_checkpoint(corpus, ("DEV",), "operator")
    monkeypatch.setattr(corpus_execution, "datetime", LaterClock)
    with pytest.raises(ValueError, match="absent"):
        corpus_execution.declare_checkpoint(corpus, ("FAMILY_HOLDOUT",), "operator")


def test_production_progress_reads_real_manual_gates_and_preserves_source(tmp_path, monkeypatch):
    # Keep nested immutable revision names within the Windows legacy path bound.
    root, item = episode(tmp_path.parent / ("ledger-"+uuid4().hex[:6]))
    episode_id, cid = item.production.private_episode_id, item.gold.candidate_id
    private = root/"private"/episode_id
    private.mkdir(parents=True, exist_ok=True)
    publish(private/"production.json", altered(item.production, status="quarantined", candidate_id=None).model_dump(mode="json"))
    publish(private/"injection-truth.json", altered(item.truth, candidate_id=None).model_dump(mode="json"))
    source = root/"quarantine"/episode_id/"snapshot.json"
    before = source.read_bytes()
    decision = root/"review"/cid/"decision.json"
    saved = decision.read_bytes()
    decision.unlink()
    monkeypatch.setattr(corpus_production, "datetime", LaterClock)
    assert update_production(root, episode_id, candidate_id=cid).status == "review_pending"
    assert not decision.exists()
    decision.write_bytes(saved)
    assert update_production(root, episode_id, candidate_id=cid).status == "approved"
    label = tmp_path/"gold"/f"{cid}.json"
    publish(label, item.gold.model_dump(mode="json"))
    assert update_production(root, episode_id, gold_path=label).status == "answerability_labeled"
    assert update_production(root, episode_id).status == "answerability_labeled"
    assert source.read_bytes() == before and decision.read_bytes() == saved
    assert len(tuple((private/"production-history").glob("*.json"))) >= 4


def test_pilot_capture_freeze_requires_quality_review_and_enough_episodes(tmp_path, monkeypatch):
    import aegis_rca.evaluation.capture_freeze as module
    root, item = episode(tmp_path)
    candidate_free = altered(item, gold=None, split=None,
        production=altered(item.production, status="quarantined"))
    qa = inspect_corpus_qa(root, spec(), (candidate_free,))
    qa_path = tmp_path/"qa.json"
    publish(qa_path, qa.model_dump(mode="json"))
    policy = CapturePolicy(policy_name="capture-policy-phase-a-v1",
        demo_resource_profile_id=PILOT_CAPTURE_POLICY.demo_resource_profile_id)
    policy_path = tmp_path/"proposed.json"
    publish(policy_path, policy.model_dump(mode="json"))
    human = CapturePolicyDecision(reviewer="operator", reviewed_at=NOW,
        collection_quality_reviewed=True, workload_and_source_stability_reviewed=True,
        ingestion_lag_and_limits_reviewed=True, sanitizer_leakage_reviewed=True, aegis_results_not_used=True)
    decision_path = tmp_path/"policy-decision.json"
    publish(decision_path, human.model_dump(mode="json"))
    monkeypatch.setattr(module, "datetime", LaterClock)
    with pytest.raises(ValueError):
        freeze_capture_policy(policy_path, qa_path, decision_path, tmp_path/"final-policy")
    assert not (tmp_path/"final-policy").exists()
    # A separate three-episode pilot QA snapshot is needed, not a change in Aegis outcome.
    _, second = episode(tmp_path, index=2)
    _, third = episode(tmp_path, index=3)
    candidates = tuple(altered(x, gold=None, split=None, production=altered(x.production,status="quarantined"))
                       for x in (item,second,third))
    three_qa = inspect_corpus_qa(root, spec(), candidates)
    publish(tmp_path/"three-qa.json", three_qa.model_dump(mode="json"))
    frozen = freeze_capture_policy(policy_path, tmp_path/"three-qa.json", decision_path, tmp_path/"final-policy")
    assert frozen.policy.id == policy.id and frozen.pilot_recorded_count == 3
    assert frozen.decision.aegis_results_not_used is True
    smoke = altered(three_qa, phase="smoke", real_recorded_count=0)
    publish(tmp_path/"smoke-qa.json", smoke.model_dump(mode="json"))
    with pytest.raises(ValueError, match="three real episodes"):
        freeze_capture_policy(policy_path, tmp_path/"smoke-qa.json", decision_path, tmp_path/"forbidden-freeze")


def test_private_qa_cli_never_runs_stage_c(tmp_path, monkeypatch, capsys):
    root, item = episode(tmp_path)
    item = altered(item, gold=None, split=None, production=altered(item.production, status="quarantined"))
    request = CorpusFreezeRequest(spec=spec(), episodes=(item,))
    request_path = tmp_path/"private-request.json"
    publish(request_path, request.model_dump(mode="json"))
    monkeypatch.setattr(stage_c, "ReplayBackend", lambda *_: pytest.fail("pilot QA ran Aegis"))
    main(["corpus", "qa", "--request", str(request_path), "--recordings-root", str(root), "--output", str(tmp_path/"qa.json")])
    result = json.loads(capsys.readouterr().out)
    assert result["attempted_episode_count"] == 1 and result["answerability_distribution"] == {}
