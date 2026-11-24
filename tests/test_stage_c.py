"""Stage C uses temporary fully reviewed synthetic candidates, never real private labels."""
import json
import socket
from pathlib import Path
from uuid import uuid4
import pytest
from pydantic import ValidationError

from aegis_rca.evaluation import benchmark
from aegis_rca.evaluation.__main__ import main
from aegis_rca.evaluation.sanitization import canonical_bytes, digest
from aegis_rca.evaluation.stage_c_models import (VARIANTS, BenchmarkBudgetSpec, BenchmarkExecutionProtocol,
    BenchmarkScoringProtocol, BenchmarkRunResult, BenchmarkVariantSpec, ExperimentSpec, TrialSpec)
from aegis_rca.evaluation import stage_c
from aegis_rca.evaluation.stage_c_scoring import score_run, score_result, root_cause_overclaim
from aegis_rca.evaluation.scoring import Label
from aegis_rca.models.execution import DEFAULT_EXECUTION_POLICY
from aegis_rca.models.telemetry import TraceResult, LogResult, MetricResult
from aegis_rca.tools.snapshot import TelemetrySnapshot
from test_benchmark_admission import approved
from test_sanitization import exported, decision_file
from test_hypothesis_portfolio import scenario


def multi_snapshot():
    incident, backend = scenario(log_edges=(("frontend", "catalog"),))
    return TelemetrySnapshot(incident=incident, provenance="synthetic_wire_fixture",
        traces=backend.materials["trace", ("frontend",)], logs=backend.materials["log", ("frontend",)],
        metrics=MetricResult(), rpc_service_map={})


def dimensions(score):
    return {d.name: d for d in score.dimensions}


def rearchive(run, **updates):
    payload = {**run.model_dump(mode="json", exclude={"archive_sha256"}), **updates}
    return BenchmarkRunResult.model_validate({**payload, "archive_sha256": digest(canonical_bytes(payload))})


def test_variant_registry_is_structural_and_budget_is_separate():
    assert len({v.id for v in VARIANTS.values()}) == 3
    assert VARIANTS["B2"].runtime_policy == DEFAULT_EXECUTION_POLICY
    assert VARIANTS["B2"].runtime_policy.model_dump_json() == DEFAULT_EXECUTION_POLICY.model_dump_json()
    for v in VARIANTS.values():
        renamed = v.model_copy(update={"display_key": "renamed", "display_name": "different prose"})
        assert renamed.id == v.id
        assert "budget" not in v.model_dump()
        assert BenchmarkVariantSpec.model_validate_json(v.model_dump_json()) == v
        with pytest.raises(ValidationError):
            v.llm = "on"
    budget = BenchmarkBudgetSpec()
    assert (budget.max_rounds, budget.max_tool_calls, budget.tool_timeout_seconds, budget.global_deadline_seconds) == (3, 6, 5, 120)
    assert BenchmarkScoringProtocol(revision="rescore-v2").id != BenchmarkScoringProtocol().id
    assert BenchmarkExecutionProtocol().id.startswith("BEP-")


@pytest.mark.parametrize("trial", [1, -1, True, "0"])
def test_only_deterministic_trial_zero(trial):
    with pytest.raises(ValidationError):
        TrialSpec(trial_index=trial)


@pytest.mark.parametrize("candidate", ["checkout", "../quarantine", "snapshot.json", str(uuid4()).upper()])
async def test_formal_input_requires_canonical_opaque_id(tmp_path, candidate, monkeypatch):
    monkeypatch.setattr(stage_c, "ReplayBackend", lambda *_: pytest.fail("invalid candidate executed"))
    with pytest.raises((ValidationError, ValueError)):
        await stage_c.run_candidate(tmp_path, candidate)


@pytest.mark.parametrize("option", ["--snapshot", "--episode", "--review", "--labels-root"])
def test_formal_cli_has_no_arbitrary_snapshot_or_private_input(tmp_path, option):
    with pytest.raises(SystemExit) as exc:
        main(["run", "--candidate", str(uuid4()), "--output", str(tmp_path), option, "secret"])
    assert exc.value.code == 2


@pytest.mark.parametrize("damage", ["pending", "rejected", "stale", "mutated", "v2"])
async def test_unapproved_or_changed_candidate_rejected_before_execution(tmp_path, monkeypatch, damage):
    root, cid, _, _ = exported(tmp_path)
    if damage != "pending":
        benchmark.review_candidate(root, cid, decision_file(root, cid,
            decision="reject" if damage == "rejected" else "approve"))
    if damage in {"mutated", "v2"}:
        path = root / "candidates" / cid / "snapshot.json"
        if damage == "mutated":
            path.write_bytes(path.read_bytes() + b"\n")
        else:
            path.write_bytes(canonical_bytes({"schema_version": 2}))
    if damage == "stale":
        path = root / "review" / cid / "decision.json"
        content = json.loads(path.read_bytes())
        path.write_bytes(canonical_bytes({**content, "scan_sha256": "0"*64}))
    monkeypatch.setattr(stage_c, "ReplayBackend", lambda *_: pytest.fail("unapproved candidate executed"))
    with pytest.raises((ValueError, OSError)):
        await stage_c.run_candidate(root, cid)


async def test_exact_admitted_object_no_reread_private_access_or_network(tmp_path, monkeypatch):
    root, cid, _, _ = approved(tmp_path)
    original_open = benchmark.open_approved_candidate
    original_backend = stage_c.ReplayBackend
    original_path_open = Path.open
    captured = []
    phase = [False]
    def admit(*args):
        candidate = original_open(*args)
        captured.append(candidate)
        # Mutate disk AFTER admission: execution must still consume the exact object.
        (root / "candidates" / cid / "snapshot.json").write_bytes(b"{}")
        phase[0] = True
        return candidate
    def backend(snapshot):
        assert snapshot is captured[0].snapshot
        return original_backend(snapshot)
    def checked_open(path, *args, **kwargs):
        if phase[0] and path.resolve().is_relative_to(root.resolve()):
            pytest.fail("execution reread recording/private roots")
        return original_path_open(path, *args, **kwargs)
    def network(*args, **kwargs):
        pytest.fail("formal deterministic execution attempted network")
    import aegis_rca.tools.live as live
    monkeypatch.setattr(benchmark, "open_approved_candidate", admit)
    monkeypatch.setattr(stage_c, "ReplayBackend", backend)
    monkeypatch.setattr(Path, "open", checked_open)
    monkeypatch.setattr(socket.socket, "connect", network)
    monkeypatch.setattr(live.LiveBackend, "__init__", network)
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    result = await stage_c.run_candidate(root, cid)
    assert result.semantic.report.status == "SUPPORTED"
    assert result.admission.snapshot_sha256 == captured[0].snapshot_sha256
    assert result.semantic.usage.network_calls == result.semantic.usage.llm_calls == result.semantic.usage.llm_cost == 0
    serialized = result.model_dump_json()
    assert "review/" not in serialized and "unseen-control-793" not in serialized


@pytest.mark.parametrize("variant,calls,status", [("B0", 2, "INCONCLUSIVE"), ("B1", 3, "SUPPORTED"), ("B2", 3, "SUPPORTED")])
async def test_frozen_single_edge_matrix(tmp_path, variant, calls, status):
    root, cid, _, _ = approved(tmp_path)
    result = await stage_c.run_candidate(root, cid, variant)
    assert result.semantic.usage.tool_calls == calls
    assert result.semantic.report.status == status
    assert result.semantic.report.underlying_cause is None
    assert result.budget == BenchmarkBudgetSpec()
    assert [t.signal for t in result.semantic.attempted_tasks] == (["metric", "trace"] if variant == "B0" else ["metric", "trace", "log"])
    if variant == "B0":
        assert "variant_did_not_schedule_required_intent" in result.semantic.failure_categories
    assert result.semantic.usage.llm_calls == 0


async def test_multi_hs_intervention_has_real_diagnostic_difference_under_same_budget(tmp_path):
    root, cid, _, _ = approved(tmp_path, multi_snapshot())
    one = await stage_c.run_candidate(root, cid, "B1")
    many = await stage_c.run_candidate(root, cid, "B2")
    assert one.budget == many.budget
    assert one.semantic.usage.tool_calls == many.semantic.usage.tool_calls == 3
    assert len(one.semantic.portfolio.active) == 1 and len(many.semantic.portfolio.active) == 2
    assert one.semantic.report.status == "INCONCLUSIVE"  # auth lacks connection log
    assert many.semantic.report.status == "SUPPORTED"  # catalog has it
    assert many.semantic.report.claims[0].text == "frontend could not establish its catalog dependency."
    assert "hypothesis_overflow" in one.semantic.failure_categories
    assert [a.status for a in many.semantic.report.assessments] == ["UNRESOLVED", "SUPPORTED"]


async def test_same_trial_reproduces_BRS_and_attempt_namespaces_but_not_timing(tmp_path):
    root, cid, _, _ = approved(tmp_path)
    a = await stage_c.run_candidate(root, cid)
    b = await stage_c.run_candidate(root, cid)
    assert a.run_key_id == b.run_key_id == a.semantic.execution_id
    assert a.semantic_result_id == b.semantic_result_id
    assert a.semantic.attempts == b.semantic.attempts
    changed = rearchive(a, duration_ms=a.duration_ms + 1)
    assert changed.semantic_result_id == a.semantic_result_id and changed.archive_sha256 != a.archive_sha256
    score = score_result(a, Label(answerability="mechanism_identifiable", failure_mode="rpc_connection_establishment"), "0"*64, comparison=b)
    assert dimensions(score)["deterministic_consistency"].value == 1


async def test_complete_archive_reused_without_execution_or_overwrite(tmp_path, monkeypatch):
    root, cid, _, _ = approved(tmp_path)
    output = tmp_path / "experiment"
    first = await stage_c.run_candidate(root, cid, output=output)
    path = output / "runs" / first.run_key_id / "run.json"
    before = path.read_bytes()
    monkeypatch.setattr(stage_c, "_execute_admitted", lambda *_: pytest.fail("reexecuted complete trial"))
    second = await stage_c.run_candidate(root, cid, output=output)
    assert second == first and path.read_bytes() == before
    assert path.with_suffix(".sha256").read_text() == digest(before)
    assert stage_c.read_run(path) == first


@pytest.mark.parametrize("damage", ["empty_dir", "missing_receipt", "duration", "whitespace", "receipt"])
async def test_partial_and_corrupt_archives_are_not_complete(tmp_path, monkeypatch, damage):
    root, cid, _, _ = approved(tmp_path)
    output = tmp_path / "experiment"
    first = await stage_c.run_candidate(root, cid, output=output)
    path = output / "runs" / first.run_key_id / "run.json"
    if damage == "empty_dir":
        path.unlink()
        path.with_suffix(".sha256").unlink()
    elif damage == "missing_receipt":
        path.with_suffix(".sha256").unlink()
    elif damage == "duration":
        path.write_bytes(canonical_bytes({**json.loads(path.read_bytes()), "duration_ms": first.duration_ms + 1}))
    elif damage == "whitespace":
        path.write_bytes(path.read_bytes() + b"\n")
    else:
        path.with_suffix(".sha256").write_text("0"*64)
    monkeypatch.setattr(stage_c, "_execute_admitted", lambda *_: pytest.fail("silently replaced partial/corrupt trial"))
    with pytest.raises((ValueError, OSError)):
        await stage_c.run_candidate(root, cid, output=output)


async def test_scoring_is_multiclaim_nonexhaustive_and_does_not_mutate_run(tmp_path):
    snapshot = multi_snapshot()
    # Both edges are supported while the private label identifies only catalog.
    incident, backend = scenario()
    snapshot = snapshot.model_copy(update={"logs": backend.materials["log", ("frontend",)]})
    root, cid, _, _ = approved(tmp_path, snapshot)
    result = await stage_c.run_candidate(root, cid, output=tmp_path / "experiment")
    path = tmp_path / "experiment" / "runs" / result.run_key_id / "run.json"
    before = path.read_bytes()
    labels = tmp_path / "private-labels"
    labels.mkdir()
    label = Label(answerability="mechanism_identifiable", failure_mode="rpc_connection_establishment", observed_edge=("frontend", "catalog"))
    (labels / f"{cid}.json").write_text(label.model_dump_json())
    score = score_run(path, labels, tmp_path / "score.json")
    d = dimensions(score)
    assert d["known_edge_recall"].value == d["mechanism_correctness"].value == 1
    assert d["edge_precision"].status == d["false_supported_claims"].status == "UNSCORED"
    assert d["deterministic_consistency"].status == "UNSCORED"
    assert d["root_cause_overclaim"].value == 0
    assert len(result.semantic.report.claims) == 2
    assert result.semantic.report.hypotheses[0].claim.startswith("frontend could not establish its auth")
    rescored = score_run(path, labels, tmp_path / "score-v2.json", protocol=BenchmarkScoringProtocol(revision="audit-v2"))
    assert rescored.scoring_protocol_id != score.scoring_protocol_id
    assert path.read_bytes() == before
    assert root_cause_overclaim(result.semantic.report.model_copy(update={"underlying_cause": "invented cause"}))
    healthy = score_result(result, Label(answerability="no_incident"), "1"*64)
    assert dimensions(healthy)["false_supported_claims"].value == 2
    assert "rpc_connection_establishment" not in score.model_dump_json()  # no private label values copied


async def test_paired_summary_keeps_execution_failure_in_requested_denominator(tmp_path, monkeypatch):
    root, cid, _, _ = approved(tmp_path)
    original = stage_c._execute_admitted
    async def fail_one(admitted, variant, *args):
        if variant.display_key == "B1":
            raise RuntimeError("private-token-must-not-escape")
        return await original(admitted, variant, *args)
    monkeypatch.setattr(stage_c, "_execute_admitted", fail_one)
    summary = await stage_c.run_experiment(root, ExperimentSpec(candidate_ids=(cid,)), tmp_path / "experiment")
    assert summary["paired_candidate_ids"] == []
    assert [summary["by_variant"][v]["valid"] for v in ("B0", "B1", "B2")] == [1, 0, 1]
    assert all(r["requested"] == 1 and r["paired_n"] == 0 for r in summary["by_variant"].values())
    assert len(summary["incomplete_trials"]) == 1
    failure, = list((tmp_path / "experiment" / "failures").rglob("*.json"))
    assert "private-token" not in failure.read_text() and "harness_error" in failure.read_text()
    with pytest.raises(ValueError, match="immutable"):
        stage_c.freeze_manifest(tmp_path / "experiment", ExperimentSpec(candidate_ids=(cid,), variants=("B0",)))


def test_exclusive_atomic_writer_never_overwrites(tmp_path, monkeypatch):
    path = tmp_path / "run.json"
    stage_c.publish(path, {"complete": True})
    before = path.read_bytes()
    with pytest.raises(FileExistsError):
        stage_c.publish(path, {"complete": False})
    assert path.read_bytes() == before and not list(tmp_path.glob(".stage-c-*"))


def test_formal_cli_smoke_and_summary(tmp_path, capsys):
    root, cid, _, _ = approved(tmp_path)
    main(["run", "--recordings-root", str(root), "--candidate", cid, "--variant", "B0", "--output", str(tmp_path / "experiment")])
    assert json.loads(capsys.readouterr().out)["status"] == "INCONCLUSIVE"
    main(["summarize", "--experiment", str(tmp_path / "experiment")])
    assert json.loads(capsys.readouterr().out)["by_variant"]["B0"]["paired_n"] == 1


async def test_paired_score_metrics_use_same_labels_BSP_and_candidate_denominators(tmp_path):
    root, cid, _, _ = approved(tmp_path)
    output = tmp_path / "experiment"
    await stage_c.run_experiment(root, ExperimentSpec(candidate_ids=(cid,)), output)
    labels = tmp_path / "labels"
    labels.mkdir()
    (labels / f"{cid}.json").write_text(Label(answerability="mechanism_identifiable",
        failure_mode="rpc_connection_establishment", observed_edge=("checkout", "payment")).model_dump_json())
    for path in (output / "runs").glob("*/run.json"):
        score_run(path, labels, output / "scores" / (path.parent.name + ".json"))
    summary = stage_c.summarize(output)
    comparison, = summary["scored_comparisons"]["protocols"]
    assert comparison["paired_scored_candidate_ids"] == [cid]
    recall = next(m for m in comparison["metrics"] if m["name"] == "known_edge_recall")
    assert recall["paired_n"] == 1
    assert [recall["by_variant"][v]["mean"] for v in ("B0", "B1", "B2")] == [0, 1, 1]
    precision = next(m for m in comparison["metrics"] if m["name"] == "edge_precision")
    assert precision["paired_n"] == 0 and all(x["mean"] is None for x in precision["by_variant"].values())
    first = next((output / "scores").glob("*.json"))
    duplicate = {**json.loads(first.read_bytes()), "private_label_sha256": "1"*64}
    (output / "scores" / "conflict.json").write_bytes(canonical_bytes(duplicate))
    summary = stage_c.summarize(output)
    assert summary["scored_comparisons"]["invalid_scores"] == 1
    assert all(p["paired_scored_candidate_ids"] == [] for p in summary["scored_comparisons"]["protocols"])


async def test_experiment_admits_each_candidate_once_for_all_variants(tmp_path, monkeypatch):
    root, cid, _, _ = approved(tmp_path)
    original = benchmark.open_approved_candidate
    objects = []
    def admit(*args):
        objects.append(original(*args))
        return objects[-1]
    original_backend = stage_c.ReplayBackend
    def backend(snapshot):
        assert snapshot is objects[0].snapshot
        return original_backend(snapshot)
    monkeypatch.setattr(benchmark, "open_approved_candidate", admit)
    monkeypatch.setattr(stage_c, "ReplayBackend", backend)
    summary = await stage_c.run_experiment(root, ExperimentSpec(candidate_ids=(cid,)), tmp_path / "experiment")
    assert len(objects) == 1 and summary["paired_candidate_ids"] == [cid]
    manifest = json.loads((tmp_path / "experiment" / "experiment.json").read_bytes())
    assert manifest["admissions"][0]["snapshot_sha256"] == objects[0].snapshot_sha256


async def test_manifest_rejects_different_admitted_bytes_even_for_same_UUID(tmp_path):
    root, cid, _, _ = approved(tmp_path)
    await stage_c.run_candidate(root, cid, output=tmp_path / "experiment")
    admitted = benchmark.open_approved_candidate(root, cid)
    wrong = stage_c.admission_reference(admitted).model_copy(update={"snapshot_sha256": "0"*64})
    with pytest.raises(ValueError, match="immutable"):
        stage_c.freeze_manifest(tmp_path / "experiment", ExperimentSpec(candidate_ids=(cid,), variants=("B2",)), (wrong,))
    original = {p.name: p.read_bytes() for p in (root / "candidates" / cid).iterdir()}
    with pytest.raises(ValueError, match="outside immutable"):
        await stage_c.run_candidate(root, cid, output=root / "candidates" / cid)
    assert {p.name: p.read_bytes() for p in (root / "candidates" / cid).iterdir()} == original


async def test_semantic_contract_rejects_fabricated_usage_and_missing_claims(tmp_path):
    root, cid, _, _ = approved(tmp_path)
    result = await stage_c.run_candidate(root, cid)
    from aegis_rca.evaluation.stage_c_models import BenchmarkSemanticResult
    for field in ("completed_intents", "failed_intents", "timeouts", "duplicate_intent_count"):
        payload = result.semantic.model_dump(mode="json")
        payload["usage"][field] += 1
        with pytest.raises(ValidationError):
            BenchmarkSemanticResult.model_validate(payload)
    payload = result.semantic.model_dump(mode="json")
    payload["report"]["claims"] = []
    with pytest.raises(ValidationError, match="exactly project"):
        BenchmarkSemanticResult.model_validate(payload)
    wrong_variant = VARIANTS["B1"]
    wrong_key = result.run_key.model_copy(update={"variant_id": wrong_variant.id})
    # Even a coherently recomputed archive must not misdeclare its observed HP treatment.
    with pytest.raises(ValidationError):
        rearchive(result, variant=wrong_variant.model_dump(mode="json"),
            run_key=wrong_key.model_dump(mode="json"), run_key_id=wrong_key.id)


async def test_healthy_candidate_is_honest_no_candidate_abstention(tmp_path):
    snapshot = multi_snapshot().model_copy(update={"traces": TraceResult(), "logs": LogResult()})
    root, cid, _, _ = approved(tmp_path, snapshot)
    result = await stage_c.run_candidate(root, cid)
    assert result.semantic.report.status == "INCONCLUSIVE" and result.semantic.usage.tool_calls == 2
    assert result.semantic.failure_categories == ("no_hypothesis_candidate",)
    score = score_result(result, Label(answerability="no_incident"), "0"*64)
    assert dimensions(score)["answerability_restraint"].value == 1
    assert dimensions(score)["false_supported_claims"].value == 0


def test_stage_c_cli_experiment_score_and_summary(tmp_path, capsys):
    root, cid, _, _ = approved(tmp_path)
    spec = tmp_path / "spec.json"
    spec.write_text(ExperimentSpec(candidate_ids=(cid,)).model_dump_json())
    output = tmp_path / "experiment"
    main(["experiment", "--spec", str(spec), "--recordings-root", str(root), "--output", str(output)])
    assert json.loads(capsys.readouterr().out)["paired_candidate_ids"] == [cid]
    run = next((output / "runs").glob("*/run.json"))
    labels = tmp_path / "labels"
    labels.mkdir()
    (labels / f"{cid}.json").write_text(Label(answerability="symptom_only").model_dump_json())
    main(["score-run", "--run", str(run), "--labels-root", str(labels), "--output", str(tmp_path / "score.json")])
    assert json.loads(capsys.readouterr().out)["scoring_protocol_id"].startswith("BSP-")


async def test_invalid_scope_and_failed_acquisition_categories_are_structural(tmp_path):
    snapshot = multi_snapshot()
    invalid = snapshot.model_copy(update={"incident": snapshot.incident.model_copy(update={"seed_services": (" ",)})})
    root, cid, _, _ = approved(tmp_path / "invalid", invalid)
    run = await stage_c.run_candidate(root, cid)
    assert "invalid_scope" in run.semantic.failure_categories and run.semantic.usage.tool_calls == 0
    failed = snapshot.model_copy(update={"collection_errors": {"log": "capture_error"}})
    root, cid, _, _ = approved(tmp_path / "failed", failed)
    run = await stage_c.run_candidate(root, cid)
    assert run.semantic.report.status == "INCONCLUSIVE" and run.semantic.usage.failed_intents == 1
    assert "acquisition_failed" in run.semantic.failure_categories
    assert all(a.status == "UNRESOLVED" for a in run.semantic.report.assessments)


def test_formal_cli_does_not_expose_unexpected_harness_exception(tmp_path, monkeypatch, capsys):
    import aegis_rca.evaluation.stage_c_cli as cli
    def broken(*args):
        raise ArithmeticError("private-label-root secret-api-key")
    monkeypatch.setattr(cli, "summarize", broken)
    with pytest.raises(SystemExit) as exc:
        main(["summarize", "--experiment", str(tmp_path)])
    assert exc.value.code == 2
    assert "secret-api-key" not in capsys.readouterr().err
