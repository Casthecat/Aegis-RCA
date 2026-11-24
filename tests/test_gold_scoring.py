"""BSP-v2 consumes frozen blinded Gold, never private injection truth or first HS."""
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from aegis_rca.evaluation import stage_c
from aegis_rca.evaluation.__main__ import main
from aegis_rca.evaluation.gold import (BenchmarkGoldLabelV2, DEFAULT_GOLD_RUBRIC,
    ExpectedDiagnosticClaim, GoldReviewerProvenance)
from aegis_rca.evaluation.gold_scoring import (BenchmarkScoringProtocolV2,
    BenchmarkScoreResultV2, score_gold_result, score_gold_run)
from aegis_rca.evaluation.scoring import Label
from aegis_rca.evaluation.stage_c_models import BenchmarkScoringProtocol, ExperimentSpec
from aegis_rca.evaluation.stage_c_scoring import score_result, score_run
from aegis_rca.models.telemetry import MetricResult
from aegis_rca.tools.snapshot import TelemetrySnapshot
from test_benchmark_admission import approved
from test_hypothesis_portfolio import scenario
from test_stage_c import dimensions


def gold(run, *, answerability="mechanism_identifiable", edges=(("checkout", "payment"),), exhaustive=False):
    now = datetime(2026, 9, 18, tzinfo=timezone.utc)
    return BenchmarkGoldLabelV2(candidate_id=run.admission.candidate_id,
        snapshot_sha256=run.admission.snapshot_sha256, bundle_sha256=run.admission.bundle_sha256,
        sanitizer_ruleset=run.admission.ruleset, gold_diagnostic_rubric_id=DEFAULT_GOLD_RUBRIC.id,
        answerability=answerability, expected_claims=tuple(sorted((ExpectedDiagnosticClaim(
            failure_mode="rpc_connection_establishment", caller=a, callee=b) for a, b in edges), key=lambda c: c.id)),
        claim_labels_exhaustive=exhaustive, capture_policy_id="CP-" + "1"*64,
        query_policy_id="QP-" + "2"*64, frozen_at=now,
        reviewer=GoldReviewerProvenance(reviewer_id="blinded-reviewer", mode="independent", reviewed_at=now,
            approved_telemetry_reviewed=True, injection_truth_sealed=True, stage_c_results_unseen=True,
            prior_injection_knowledge=False, coverage_reviewed=True,
            coverage_sufficient=answerability != "insufficient_evidence", empty_set_justified=answerability == "no_incident",
            successful_path_and_no_incident_symptoms=answerability == "no_incident"))


def two_supported_snapshot():
    incident, backend = scenario()
    return TelemetrySnapshot(incident=incident, provenance="synthetic_wire_fixture",
        traces=backend.materials["trace", ("frontend",)], logs=backend.materials["log", ("frontend",)],
        metrics=MetricResult(), rpc_service_map={})


def test_bsp_v2_is_new_structural_scoring_protocol():
    old, new = BenchmarkScoringProtocol(), BenchmarkScoringProtocolV2()
    assert old.id != new.id
    assert old.label_schema == "legacy_Label_v1_nonexhaustive"
    assert new.gold_diagnostic_rubric_id == DEFAULT_GOLD_RUBRIC.id
    assert BenchmarkScoringProtocolV2.model_validate_json(new.model_dump_json()) == new
    assert new.model_copy(update={"revision": "audit-v2"}).id != new.id


async def test_no_incident_empty_target_set_requires_successful_symptom_free_path(tmp_path):
    root, cid, _, _ = approved(tmp_path)
    run = await stage_c.run_candidate(root, cid)
    label = gold(run, answerability="no_incident", edges=(), exhaustive=True)
    payload = label.model_dump()
    payload["reviewer"]["successful_path_and_no_incident_symptoms"] = False
    with pytest.raises(ValidationError):
        BenchmarkGoldLabelV2.model_validate(payload)


@pytest.mark.parametrize("exhaustive", [False, True])
async def test_multiclaim_precision_requires_exhaustiveness(tmp_path, exhaustive):
    root, cid, _, _ = approved(tmp_path, two_supported_snapshot())
    run = await stage_c.run_candidate(root, cid)
    label = gold(run, edges=(("frontend", "catalog"),), exhaustive=exhaustive)
    score = score_gold_result(run, label, "a"*64)
    d = dimensions(score)
    assert len(run.semantic.report.claims) == 2
    # Known positive is not the first display hypothesis.
    assert run.semantic.report.hypotheses[0].claim.startswith("frontend could not establish its auth")
    assert d["supported_claim_recall"].value == d["known_edge_recall"].value == 1
    assert d["mechanism_correctness"].value == 1
    if exhaustive:
        assert d["claim_precision"].value == d["edge_precision"].value == 0.5
        assert d["false_supported_claims"].value == 1
    else:
        assert all(d[key].status == "UNSCORED" for key in ("claim_precision", "edge_precision", "false_supported_claims"))
    assert BenchmarkScoreResultV2.model_validate_json(score.model_dump_json()) == score
    assert d["root_cause_overclaim"].value == 0


async def test_missing_known_positive_hurts_claim_recall_without_marking_extra_false(tmp_path):
    root, cid, _, _ = approved(tmp_path)
    run = await stage_c.run_candidate(root, cid)
    label = gold(run, edges=(("checkout", "payment"), ("checkout", "catalog")))
    d = dimensions(score_gold_result(run, label, "a"*64))
    assert d["supported_claim_recall"].value == 0.5
    assert (d["supported_claim_recall"].numerator, d["supported_claim_recall"].denominator) == (1, 2)
    assert d["claim_precision"].status == d["false_supported_claims"].status == "UNSCORED"


@pytest.mark.parametrize("answerability,exhaustive", [
    ("symptom_only", False), ("no_incident", True), ("insufficient_evidence", False)])
@pytest.mark.parametrize("variant", ["B0", "B2"])
async def test_nonpositive_answerability_restraint_separate_from_set_false_positives(tmp_path, answerability, exhaustive, variant):
    root, cid, _, _ = approved(tmp_path)
    run = await stage_c.run_candidate(root, cid, variant)
    label = gold(run, answerability=answerability, edges=(), exhaustive=exhaustive)
    d = dimensions(score_gold_result(run, label, "a"*64))
    supported = variant == "B2"
    assert d["answerability_restraint"].value == int(not supported)
    assert d["answerability_overclaim"].value == int(supported)
    assert d["supported_claim_recall"].status == "UNSCORED"
    if exhaustive:
        assert d["false_supported_claims"].value == int(supported)
    else:
        assert d["false_supported_claims"].status == "UNSCORED"


@pytest.mark.parametrize("field,value", [("candidate_id", str(uuid4())), ("snapshot_sha256", "0"*64),
    ("bundle_sha256", "0"*64), ("sanitizer_ruleset", "another-ruleset")])
async def test_score_rejects_gold_for_different_admission(tmp_path, field, value):
    root, cid, _, _ = approved(tmp_path)
    run = await stage_c.run_candidate(root, cid)
    with pytest.raises(ValueError, match="admitted candidate"):
        score_gold_result(run, gold(run).model_copy(update={field: value}), "a"*64)


async def test_bsp_gold_schema_mismatch_and_rubric_mismatch_rejected(tmp_path):
    root, cid, _, _ = approved(tmp_path)
    run = await stage_c.run_candidate(root, cid)
    label = gold(run)
    legacy = Label(answerability="no_incident")
    with pytest.raises(ValueError, match="requires"):
        score_gold_result(run, legacy, "a"*64)
    with pytest.raises(ValueError, match="requires"):
        score_result(run, label, "a"*64)
    with pytest.raises(ValueError, match="requires"):
        score_gold_result(run, label, "a"*64, BenchmarkScoringProtocol())
    with pytest.raises(ValueError, match="requires"):
        score_result(run, legacy, "a"*64, BenchmarkScoringProtocolV2())
    with pytest.raises(ValueError, match="rubric"):
        score_gold_result(run, label, "a"*64, BenchmarkScoringProtocolV2(gold_diagnostic_rubric_id="GDR-"+"0"*64))


@pytest.mark.parametrize("report_update", [{"underlying_cause": "private cause guess"},
    {"diagnosis_level": "root_cause"}])
async def test_root_cause_overclaim_uses_structured_fields_not_claim_wording(tmp_path, report_update):
    root, cid, _, _ = approved(tmp_path)
    run = await stage_c.run_candidate(root, cid)
    label = gold(run)
    # This direct scorer unit test deliberately bypasses report validation to
    # test the independent metric against an overclaiming external result.
    report = run.semantic.report.model_copy(update=report_update)
    forged = run.model_copy(update={"semantic": run.semantic.model_copy(update={"report": report})})
    assert dimensions(score_gold_result(forged, label, "a"*64))["root_cause_overclaim"].value == 1


async def test_v2_artifact_identity_and_complete_dimension_contract(tmp_path):
    root, cid, _, _ = approved(tmp_path)
    run = await stage_c.run_candidate(root, cid)
    label = gold(run)
    score = score_gold_result(run, label, "a"*64)
    assert score.id == BenchmarkScoreResultV2.model_validate_json(score.model_dump_json()).id
    with pytest.raises(ValidationError, match="explicitly"):
        BenchmarkScoreResultV2.model_validate({**score.model_dump(), "dimensions": score.dimensions[:-1]})
    with pytest.raises(ValidationError, match="protocol mismatch"):
        BenchmarkScoreResultV2.model_validate({**score.model_dump(), "scoring_protocol_id": BenchmarkScoringProtocol().id})
    annotated = label.model_copy(update={"private_notes": "private reviewer prose is never copied into score"})
    assert "private reviewer prose" not in score_gold_result(run, annotated, "b"*64).model_dump_json()


async def test_gold_scoring_reads_only_run_and_gold_and_preserves_run_bytes(tmp_path, monkeypatch):
    root, cid, _, _ = approved(tmp_path)
    experiment = tmp_path / "experiment"
    run = await stage_c.run_candidate(root, cid, output=experiment)
    archive = experiment / "runs" / run.run_key_id / "run.json"
    before = archive.read_bytes()
    labels = tmp_path / "gold"
    labels.mkdir()
    label = gold(run)
    (labels / f"{cid}.json").write_text(label.model_dump_json(), encoding="utf-8")
    injection = tmp_path / "injection_truth"
    injection.mkdir()
    (injection / "private.json").write_text('{"control_flag":"private-secret-control"}')
    original_read = stage_c.benchmark._read_bytes
    reads = []
    def checked(path):
        resolved = Path(path).resolve()
        assert not resolved.is_relative_to(root.resolve())
        assert not resolved.is_relative_to(injection.resolve())
        reads.append(resolved)
        return original_read(path)
    monkeypatch.setattr(stage_c.benchmark, "_read_bytes", checked)
    output = experiment / "scores" / "v2.json"
    result = score_gold_run(archive.parent, labels, output)
    assert archive.read_bytes() == before
    assert result == score_gold_run(archive, labels, output)
    assert (labels / f"{cid}.json").resolve() in reads
    assert "private-secret-control" not in result.model_dump_json()
    assert result.gold_label_id == label.id
    with pytest.raises(ValidationError):
        score_run(archive, labels, experiment / "scores" / "wrong-v1.json")
    (labels / f"{cid}.json").write_text(Label(answerability="no_incident").model_dump_json())
    with pytest.raises(ValidationError):
        score_gold_run(archive, labels, experiment / "scores" / "wrong-v2.json")
    assert archive.read_bytes() == before


async def test_gold_summary_preserves_paired_metric_denominators_and_v1_scores(tmp_path):
    root, cid, _, _ = approved(tmp_path)
    output = tmp_path / "experiment"
    await stage_c.run_experiment(root, ExperimentSpec(candidate_ids=(cid,)), output)
    labels, legacy_labels = tmp_path / "gold", tmp_path / "legacy"
    labels.mkdir()
    legacy_labels.mkdir()
    runs = list((output / "runs").glob("*/run.json"))
    label = gold(stage_c.read_run(runs[0]), exhaustive=True)
    (labels / f"{cid}.json").write_text(label.model_dump_json())
    (legacy_labels / f"{cid}.json").write_text(Label(answerability="mechanism_identifiable",
        failure_mode="rpc_connection_establishment", observed_edge=("checkout", "payment")).model_dump_json())
    for path in runs:
        score_gold_run(path, labels, output / "scores" / (path.parent.name + "-v2.json"))
        score_run(path, legacy_labels, output / "scores" / (path.parent.name + "-v1.json"))
    summary = stage_c.summarize(output)
    assert summary["scored_comparisons"]["invalid_scores"] == 0
    protocols = {p["scoring_protocol_id"]: p for p in summary["scored_comparisons"]["protocols"]}
    v2 = protocols[BenchmarkScoringProtocolV2().id]
    v1 = protocols[BenchmarkScoringProtocol().id]
    assert len(v2["metrics"]) == 22 and len(v1["metrics"]) == 20
    assert v2["paired_scored_candidate_ids"] == v1["paired_scored_candidate_ids"] == [cid]
    recall = next(m for m in v2["metrics"] if m["name"] == "supported_claim_recall")
    assert recall["paired_n"] == 1
    assert [recall["by_variant"][v]["mean"] for v in ("B0", "B1", "B2")] == [0, 1, 1]
    # B0 has zero predicted claims: precision has no common defined denominator.
    precision = next(m for m in v2["metrics"] if m["name"] == "claim_precision")
    assert precision["paired_n"] == 0


def test_formal_cli_requires_explicit_gold_schema(tmp_path, capsys):
    import asyncio
    root, cid, _, _ = approved(tmp_path)
    output = tmp_path / "experiment"
    run = asyncio.run(stage_c.run_candidate(root, cid, output=output))
    labels = tmp_path / "gold"
    labels.mkdir()
    (labels / f"{cid}.json").write_text(gold(run).model_dump_json())
    args = ["score-run", "--run", str(output / "runs" / run.run_key_id), "--labels-root", str(labels)]
    main(args + ["--label-schema", "gold-v2", "--output", str(output / "scores" / "v2.json")])
    scored = json.loads(capsys.readouterr().out)
    assert scored["schema_version"] == 2
    assert scored["scoring_protocol_id"] == BenchmarkScoringProtocolV2().id
    with pytest.raises(SystemExit) as exc:
        main(args + ["--output", str(output / "scores" / "legacy.json")])
    assert exc.value.code == 2
