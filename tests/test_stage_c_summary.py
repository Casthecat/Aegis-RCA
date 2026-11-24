"""Stage C summaries compare approved synthetic candidates on explicit denominators."""
import copy
import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from aegis_rca.evaluation import benchmark, stage_c
from aegis_rca.evaluation.sanitization import canonical_bytes
from aegis_rca.evaluation.scoring import Label
from aegis_rca.evaluation.stage_c_models import ExperimentSpec, VARIANTS as REGISTRY
from aegis_rca.evaluation.stage_c_scoring import score_run
from aegis_rca.models.telemetry import LogResult, MetricResult
from aegis_rca.tools.snapshot import TelemetrySnapshot
from test_benchmark_admission import approved
from test_hypothesis_portfolio import scenario
from test_stage_c import multi_snapshot


VARIANTS = ("B0", "B1", "B2")


def rate(k, n):
    return {"k": k, "n": n, "value": k / n if n else None}


def stats(*values):
    ordered = sorted(values)
    n = len(values)
    median = ((ordered[(n - 1) // 2] + ordered[n // 2]) / 2) if n else None
    return {"n": n, "mean": sum(values) / n if n else None,
            "median": median, "total": sum(values)}


def assert_stats(actual, *values):
    assert {key: actual[key] for key in stats(*values)} == stats(*values)


def archived_bytes(output):
    return {p.relative_to(output).as_posix(): p.read_bytes()
            for p in output.rglob("*") if p.is_file()}


def metric(summary, name):
    protocol, = summary["scored_comparisons"]["protocols"]
    return next(item for item in protocol["metrics"] if item["name"] == name)


async def test_single_edge_summary_has_explicit_complete_case_cost_and_completion_rates(tmp_path):
    root, cid, _, _ = approved(tmp_path)
    summary = await stage_c.run_experiment(root, ExperimentSpec(candidate_ids=(cid,)), tmp_path / "experiment")
    assert summary["schema_version"] == 1
    assert summary["candidate_count"] == summary["admitted_candidate_count"] == 1
    assert summary["requested_trial_count"] == summary["attempted_trial_count"] == summary["completed_trial_count"] == 3
    assert summary["paired_candidate_ids"] == [cid]
    for variant, calls, rounds, inconclusive in (("B0", 2, 1, 1), ("B1", 3, 2, 0), ("B2", 3, 2, 0)):
        row = summary["by_variant"][variant]
        assert row["requested"] == row["admitted"] == row["attempted"] == row["completed"] == row["valid"] == row["paired_n"] == 1
        assert row["completion_rate"] == rate(1, 1)
        assert row["harness_error_rate"] == rate(0, 1)
        assert row["paired_inconclusive_rate"] == row["paired_abstention_rate"] == rate(inconclusive, 1)
        assert_stats(row["tool_calls"], calls)
        assert_stats(row["rounds"], rounds)
    assert summary["limitation_histogram"]["variant_did_not_schedule_required_intent"] == 1


async def test_summary_is_deterministic_read_only_and_round_trips_as_frozen_contract(tmp_path):
    from aegis_rca.evaluation.stage_c_summary import ExperimentSummary

    root, cid, _, _ = approved(tmp_path)
    output = tmp_path / "experiment"
    first = await stage_c.run_experiment(root, ExperimentSpec(candidate_ids=(cid,)), output)
    before = archived_bytes(output)
    second = stage_c.summarize(output)
    third = stage_c.summarize(output)
    assert canonical_bytes(first) == canonical_bytes(second) == canonical_bytes(third)
    model = ExperimentSummary.model_validate(first)
    assert ExperimentSummary.model_validate_json(model.model_dump_json()) == model
    assert model.model_dump(mode="json") == first
    with pytest.raises(ValidationError):
        model.schema_version = 2
    assert archived_bytes(output) == before


async def test_partial_harness_failure_stays_in_intention_to_test_denominator(tmp_path, monkeypatch):
    root, cid, _, _ = approved(tmp_path)
    original = stage_c._execute_admitted

    async def fail_b1(admitted, variant, *args):
        if variant.display_key == "B1":
            raise RuntimeError("private-exception-payload-must-never-appear")
        return await original(admitted, variant, *args)

    monkeypatch.setattr(stage_c, "_execute_admitted", fail_b1)
    summary = await stage_c.run_experiment(root, ExperimentSpec(candidate_ids=(cid,)), tmp_path / "experiment")
    assert summary["candidate_count"] == summary["admitted_candidate_count"] == 1
    assert summary["requested_trial_count"] == summary["attempted_trial_count"] == 3
    assert summary["completed_trial_count"] == 2
    assert summary["paired_candidate_ids"] == []
    assert summary["limitation_histogram"]["harness_error"] == 1
    assert "private-exception" not in json.dumps(summary)
    for variant in VARIANTS:
        row = summary["by_variant"][variant]
        assert row["requested"] == row["admitted"] == row["attempted"] == 1
        assert row["completed"] == row["valid"] == int(variant != "B1")
        assert row["completion_rate"] == rate(int(variant != "B1"), 1)
        assert row["harness_error_rate"] == rate(int(variant == "B1"), 1)
        assert row["paired_n"] == 0
        assert_stats(row["tool_calls"])
        assert_stats(row["rounds"])
        assert row["paired_inconclusive_rate"] == row["paired_abstention_rate"] == rate(0, 0)


def test_unattempted_manifest_cells_are_not_counted_as_harness_attempts(tmp_path):
    root, cid, _, _ = approved(tmp_path)
    admitted = benchmark.open_approved_candidate(root, cid)
    output = tmp_path / "experiment"
    stage_c.freeze_manifest(output, ExperimentSpec(candidate_ids=(cid,)), (stage_c.admission_reference(admitted),))
    summary = stage_c.summarize(output)
    assert summary["requested_trial_count"] == 3
    assert summary["attempted_trial_count"] == summary["completed_trial_count"] == 0
    assert summary["admitted_candidate_count"] == 1
    assert summary["harness_failures"] == []
    assert len(summary["incomplete_trials"]) == 3
    assert summary["limitation_histogram"].get("harness_error", 0) == 0
    for row in summary["by_variant"].values():
        assert row["requested"] == row["admitted"] == 1
        assert row["attempted"] == row["completed"] == 0
        assert row["completion_rate"] == rate(0, 1)
        assert row["harness_error_rate"] == rate(0, 1)
        assert_stats(row["tool_calls"])
        assert_stats(row["rounds"])


async def test_all_admissions_rejected_retains_zero_completion_and_empty_paired_metrics(tmp_path, monkeypatch):
    cid = str(uuid4())
    monkeypatch.setattr(stage_c, "_execute_admitted", lambda *_: pytest.fail("rejected candidate executed"))
    summary = await stage_c.run_experiment(tmp_path / "recordings", ExperimentSpec(candidate_ids=(cid,)), tmp_path / "experiment")
    assert summary["candidate_count"] == 1 and summary["admitted_candidate_count"] == 0
    assert summary["requested_trial_count"] == summary["attempted_trial_count"] == 3
    assert summary["completed_trial_count"] == 0
    assert summary["paired_candidate_ids"] == []
    assert summary["limitation_histogram"]["harness_error"] == 3
    for row in summary["by_variant"].values():
        assert row["requested"] == row["attempted"] == 1
        assert row["admitted"] == row["completed"] == row["paired_n"] == 0
        assert row["completion_rate"] == rate(0, 1)
        assert row["harness_error_rate"] == rate(1, 1)
        assert_stats(row["tool_calls"])
        assert_stats(row["rounds"])
        assert row["paired_abstention_rate"] == row["paired_inconclusive_rate"] == rate(0, 0)


async def test_global_complete_case_selection_does_not_average_different_candidates(tmp_path, monkeypatch):
    root, complete, _, _ = approved(tmp_path)
    _, partial, _, _ = approved(tmp_path, multi_snapshot())
    original = stage_c._execute_admitted

    async def omit_one_cell(admitted, variant, *args):
        if admitted.candidate_id == partial and variant.display_key == "B1":
            raise RuntimeError("synthetic harness failure")
        return await original(admitted, variant, *args)

    monkeypatch.setattr(stage_c, "_execute_admitted", omit_one_cell)
    summary = await stage_c.run_experiment(root, ExperimentSpec(candidate_ids=(complete, partial)), tmp_path / "experiment")
    assert summary["candidate_count"] == summary["admitted_candidate_count"] == 2
    assert summary["requested_trial_count"] == summary["attempted_trial_count"] == 6
    assert summary["completed_trial_count"] == 5
    assert summary["paired_candidate_ids"] == [complete]
    assert summary["by_variant"]["B1"]["completion_rate"] == rate(1, 2)
    assert summary["by_variant"]["B1"]["harness_error_rate"] == rate(1, 2)
    for variant, calls in (("B0", 2), ("B1", 3), ("B2", 3)):
        row = summary["by_variant"][variant]
        assert row["paired_n"] == 1
        assert_stats(row["tool_calls"], calls)
        assert row["valid"] == (1 if variant == "B1" else 2)


async def test_summary_contract_rejects_schema_count_and_bound_tampering(tmp_path):
    from aegis_rca.evaluation.stage_c_summary import ExperimentSummary

    root, cid, _, _ = approved(tmp_path)
    summary = await stage_c.run_experiment(root, ExperimentSpec(candidate_ids=(cid,)), tmp_path / "experiment")
    mutations = (
        {"schema_version": 2}, {"candidate_count": 257}, {"requested_trial_count": 769},
        {"completed_trial_count": 4}, {"admitted_candidate_count": 2},
        {"paired_candidate_ids": [cid, cid]}, {"unrecognized_diagnostic": "arbitrary prose"},
    )
    for update in mutations:
        with pytest.raises(ValidationError):
            ExperimentSummary.model_validate({**summary, **update})
    malformed = copy.deepcopy(summary)
    malformed["by_variant"]["B0"]["completion_rate"]["value"] = 0
    with pytest.raises(ValidationError):
        ExperimentSummary.model_validate(malformed)
    malformed = copy.deepcopy(summary)
    malformed["admitted_candidate_count"] = 0
    for row in malformed["by_variant"].values():
        row["admitted"] = 0
    with pytest.raises(ValidationError):
        ExperimentSummary.model_validate(malformed)
    malformed = copy.deepcopy(summary)
    malformed["incomplete_trials"] = [{"candidate_id": cid, "variant": "B0",
                                        "status": "missing_or_failed_or_invalid"}]
    with pytest.raises(ValidationError):
        ExperimentSummary.model_validate(malformed)


async def test_summary_rows_and_deltas_bind_same_candidate_semantics_and_current_run_refs(tmp_path):
    root, cid, _, _ = approved(tmp_path)
    output = tmp_path / "experiment"
    summary = await stage_c.run_experiment(root, ExperimentSpec(candidate_ids=(cid,)), output)
    row, = summary["paired_rows"]
    assert row["candidate_id"] == cid and list(row["by_variant"]) == list(VARIANTS)
    runs = {run.variant.display_key: run for run in
            (stage_c.read_run(p) for p in (output / "runs").glob("*/run.json"))}
    for variant, run in runs.items():
        cell = row["by_variant"][variant]
        assert cell["run_key_id"] == run.run_key_id
        assert cell["semantic_result_id"] == run.semantic_result_id
        assert cell["status"] == run.semantic.report.status
        assert cell["tool_calls"] == run.semantic.usage.tool_calls
        assert cell["rounds"] == run.semantic.usage.rounds
        assert cell["supported_claims"] == len(run.semantic.report.claims)
        assert cell["failure_categories"] == list(run.semantic.failure_categories)
    first, second = summary["deltas"]
    assert (first["from_variant"], first["to_variant"]) == ("B0", "B1")
    assert (second["from_variant"], second["to_variant"]) == ("B1", "B2")
    for delta, expected in ((first, 1), (second, 0)):
        assert delta["paired_n"] == 1
        differences = {"tool_calls": expected, "rounds": expected,
                       "supported_claims": expected, "supported": expected}
        assert delta["rows"] == [{"candidate_id": cid, **differences}]
        assert delta["means"] == differences


async def test_multi_hypothesis_summary_exposes_coverage_difference_without_extra_shared_calls(tmp_path):
    root, cid, _, _ = approved(tmp_path, multi_snapshot())
    summary = await stage_c.run_experiment(root, ExperimentSpec(candidate_ids=(cid,)), tmp_path / "experiment")
    row, = summary["paired_rows"]
    assert [row["by_variant"][v]["supported_claims"] for v in VARIANTS] == [0, 0, 1]
    assert [row["by_variant"][v]["tool_calls"] for v in VARIANTS] == [2, 3, 3]
    assert summary["by_variant"]["B1"]["paired_abstention_rate"] == rate(1, 1)
    assert summary["by_variant"]["B2"]["paired_abstention_rate"] == rate(0, 1)
    delta = next(d for d in summary["deltas"] if d["from_variant"] == "B1")
    expected = {"tool_calls": 0, "rounds": 0, "supported_claims": 1, "supported": 1}
    assert delta["means"] == expected
    assert delta["rows"] == [{"candidate_id": cid, **expected}]
    assert summary["limitation_histogram"]["hypothesis_overflow"] == 2
    for row in summary["by_variant"].values():
        assert row["llm_calls"] == row["llm_tokens"] == row["llm_cost"] == 0
        assert row["paired_root_cause_overclaim_rate"] == rate(0, 1)


def test_expected_partial_trial_directory_is_attempted_but_not_completed(tmp_path):
    root, cid, _, _ = approved(tmp_path)
    admitted = benchmark.open_approved_candidate(root, cid)
    output, manifest = stage_c.freeze_manifest(tmp_path / "experiment", ExperimentSpec(candidate_ids=(cid,)),
                                             (stage_c.admission_reference(admitted),))
    key = stage_c.run_key(admitted, REGISTRY["B1"], manifest.budget, manifest.execution_protocol, manifest.spec.trial)
    (output / "runs" / key.id).mkdir(parents=True)
    summary = stage_c.summarize(output)
    assert summary["requested_trial_count"] == 3
    assert summary["attempted_trial_count"] == 1 and summary["completed_trial_count"] == 0
    assert summary["invalid_archives"] == 1
    assert summary["by_variant"]["B1"]["attempted"] == 1
    assert summary["by_variant"]["B1"]["harness_error_rate"] == rate(1, 1)
    assert summary["limitation_histogram"] == {"harness_error": 1}
    assert summary["by_variant"]["B0"]["attempted"] == summary["by_variant"]["B2"]["attempted"] == 0
    assert summary["paired_rows"] == []
    for delta in summary["deltas"]:
        assert delta["paired_n"] == 0 and delta["rows"] == []
        assert all(value is None for value in delta["means"].values())


async def test_score_summary_exposes_sums_common_denominators_and_explicit_unscored_metrics(tmp_path):
    root, cid, _, _ = approved(tmp_path, multi_snapshot())
    output = tmp_path / "experiment"
    await stage_c.run_experiment(root, ExperimentSpec(candidate_ids=(cid,)), output)
    labels = tmp_path / "private-labels"
    labels.mkdir()
    (labels / f"{cid}.json").write_text(Label(answerability="mechanism_identifiable",
        failure_mode="rpc_connection_establishment", observed_edge=("frontend", "catalog")).model_dump_json())
    for path in (output / "runs").glob("*/run.json"):
        score_run(path, labels, output / "scores" / (path.parent.name + ".json"))
    before = archived_bytes(output)
    summary = stage_c.summarize(output)
    recall = metric(summary, "known_edge_recall")
    assert recall["paired_n"] == 1 and recall["eligible_candidate_ids"] == [cid]
    for variant, value in (("B0", 0), ("B1", 0), ("B2", 1)):
        cell = recall["by_variant"][variant]
        assert cell["status"] == "SCORED"
        assert cell["k"] == cell["total"] == cell["mean"] == cell["median"] == value
        assert cell["n"] == 1 and cell["any_positive_rate"] == rate(value, 1)
    for name in ("edge_precision", "false_supported_claims", "deterministic_consistency"):
        dimension = metric(summary, name)
        assert dimension["paired_n"] == 0 and dimension["eligible_candidate_ids"] == []
        for cell in dimension["by_variant"].values():
            assert cell["status"] == "UNSCORED" and cell["n"] == 0
            assert cell["mean"] is cell["median"] is None
    protocol, = summary["scored_comparisons"]["protocols"]
    for comparison, expected in zip(protocol["deltas"], (0, 1)):
        delta = next(d for d in comparison["metrics"] if d["name"] == "known_edge_recall")
        assert delta["paired_n"] == 1 and delta["mean"] == expected
        assert delta["rows"] == [{"candidate_id": cid, "value": expected}]
        unscored = next(d for d in comparison["metrics"] if d["name"] == "edge_precision")
        assert unscored["paired_n"] == 0 and unscored["mean"] is None and unscored["rows"] == []
    assert archived_bytes(output) == before


async def test_summary_statistics_and_pooled_ratios_aggregate_two_paired_candidates(tmp_path):
    root, first, _, _ = approved(tmp_path)
    incident, backend = scenario(edges=(("alpha", "auth"), ("beta", "storage")))
    snapshot = TelemetrySnapshot(incident=incident, provenance="synthetic_wire_fixture",
        traces=backend.materials["trace", ("alpha", "beta")], metrics=MetricResult(),
        logs=LogResult(logs=tuple(log for caller in incident.seed_services
                                for log in backend.materials["log", (caller,)].logs)))
    _, second, _, _ = approved(tmp_path, snapshot)
    output = tmp_path / "experiment"
    summary = await stage_c.run_experiment(root, ExperimentSpec(candidate_ids=(first, second), variants=("B1", "B2")), output)
    assert summary["paired_candidate_ids"] == sorted((first, second))
    assert_stats(summary["by_variant"]["B1"]["tool_calls"], 3, 3)
    assert_stats(summary["by_variant"]["B2"]["tool_calls"], 3, 4)
    delta, = summary["deltas"]
    assert delta["paired_n"] == 2
    assert delta["means"] == {"tool_calls": 0.5, "rounds": 0, "supported_claims": 0.5, "supported": 0}
    labels = tmp_path / "private-labels"
    labels.mkdir()
    for cid, edge in ((first, ("checkout", "payment")), (second, ("alpha", "auth"))):
        (labels / f"{cid}.json").write_text(Label(answerability="mechanism_identifiable",
            failure_mode="rpc_connection_establishment", observed_edge=edge).model_dump_json())
    for path in (output / "runs").glob("*/run.json"):
        score_run(path, labels, output / "scores" / (path.parent.name + ".json"))
    summary = stage_c.summarize(output)
    mechanism = metric(summary, "mechanism_correctness")
    assert mechanism["paired_n"] == 2
    for cell in mechanism["by_variant"].values():
        assert cell["n"] == cell["k"] == cell["pooled_k"] == cell["pooled_n"] == 2
        assert cell["mean"] == cell["pooled_value"] == 1
        assert cell["any_positive_rate"] == rate(2, 2)

