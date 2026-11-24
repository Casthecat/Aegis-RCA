"""A cached archive cannot establish repeat-execution consistency."""
import pytest

from aegis_rca.evaluation import stage_c
from aegis_rca.evaluation.scoring import Label
from aegis_rca.evaluation.stage_c_models import BenchmarkRunResult
from aegis_rca.evaluation.stage_c_scoring import score_result, score_run
from test_benchmark_admission import approved


def consistency(score):
    return next(d for d in score.dimensions if d.name == "deterministic_consistency")


@pytest.mark.parametrize("copy_archive", [False, True])
async def test_identical_archive_is_not_a_reproducibility_observation(tmp_path, copy_archive):
    root, candidate_id, _, _ = approved(tmp_path)
    run = await stage_c.run_candidate(root, candidate_id)
    comparison = BenchmarkRunResult.model_validate_json(run.model_dump_json()) if copy_archive else run
    result = score_result(run, Label(answerability="symptom_only"), "0" * 64, comparison=comparison)
    dimension = consistency(result)
    assert dimension.status == "UNSCORED" and dimension.value is None
    assert "identical archive" in dimension.definition


@pytest.mark.parametrize("use_directory", [False, True])
async def test_score_run_rejects_same_archive_path_before_label_access(tmp_path, use_directory):
    root, candidate_id, _, _ = approved(tmp_path)
    output = tmp_path / "experiment"
    run = await stage_c.run_candidate(root, candidate_id, output=output)
    path = output / "runs" / run.run_key_id / "run.json"
    before = path.read_bytes()
    comparison = path.parent if use_directory else path.parent / "." / "run.json"
    with pytest.raises(ValueError, match="separate rerun archive"):
        score_run(path, tmp_path / "labels-not-read", tmp_path / "score.json", comparison_path=comparison)
    assert path.read_bytes() == before
    assert not (tmp_path / "score.json").exists()


async def test_distinct_same_trial_executions_can_establish_consistency(tmp_path, monkeypatch):
    root, candidate_id, _, _ = approved(tmp_path)
    ticks = iter((10.0, 10.125, 20.0, 20.25))
    monkeypatch.setattr(stage_c, "perf_counter", lambda: next(ticks))
    run = await stage_c.run_candidate(root, candidate_id)
    repeated = await stage_c.run_candidate(root, candidate_id)
    assert run.run_key_id == repeated.run_key_id
    assert run.semantic_result_id == repeated.semantic_result_id
    assert run.archive_sha256 != repeated.archive_sha256
    dimension = consistency(score_result(run, Label(answerability="symptom_only"), "0" * 64,
        comparison=repeated))
    assert dimension.status == "SCORED" and dimension.value == 1


async def test_score_run_directory_and_archive_path_resolve_the_same_immutable_run(tmp_path):
    root, candidate_id, _, _ = approved(tmp_path)
    experiment = tmp_path / "experiment"
    run = await stage_c.run_candidate(root, candidate_id, output=experiment)
    directory = experiment / "runs" / run.run_key_id
    before = (directory / "run.json").read_bytes()
    labels = tmp_path / "labels"
    labels.mkdir()
    (labels / f"{candidate_id}.json").write_text(Label(answerability="symptom_only").model_dump_json())
    output = experiment / "scores" / "result.json"
    score = score_run(directory, labels, output)
    assert score == score_run(directory / "run.json", labels, output)
    assert (directory / "run.json").read_bytes() == before
