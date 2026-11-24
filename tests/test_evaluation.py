"""Synthetic unit cases for scorer behavior, never a measured RCA benchmark."""
from datetime import datetime, timedelta, timezone
import json

import pytest

from aegis_rca.evaluation import EpisodeSpec, HumanReview, Label, aggregate, scan_leakage, score_episode, validate_splits
from aegis_rca.evaluation.__main__ import main
from aegis_rca.models.evidence import Evidence, EvidenceSource, Observation
from aegis_rca.models.hypothesis import Hypothesis, IncidentReport, ReportClaim
from aegis_rca.models.incident import TimeWindow


START = datetime(2026, 9, 9, tzinfo=timezone.utc)
WINDOW = TimeWindow(start=START, end=START + timedelta(minutes=5))
LABEL = Label(answerability="mechanism_identifiable", failure_mode="connection_failure")


def record(id="e1", service="checkout", timestamp=START):
    return Evidence(id=id, signal="log", service=service, timestamp=timestamp,
                    observation=Observation(code="connection_refused", peer_service="payment"),
                    excerpt="upstream refused the connection", source=EvidenceSource(backend="opensearch", locator="index/doc"))


def report(status="SUPPORTED", mechanism="connection_failure", hypothesis_status="supported", citations=("e1",)):
    return IncidentReport(status=status, diagnosis_level="failure_mode" if status == "SUPPORTED" else "undetermined",
                          summary="A prose summary is never parsed as an answer.", rounds=2, tool_calls=3, termination_reason="done",
                          claims=(ReportClaim(text="connection observation", evidence_ids=citations),),
                          hypotheses=(Hypothesis(id="h1", claim="candidate mechanism", affected_service="checkout",
                                                 failure_mode=mechanism, status=hypothesis_status, supporting_evidence_ids=citations),))


def score(candidate=None, label=LABEL, evidence=None, **kwargs):
    return score_episode(candidate or report(), [record()] if evidence is None else evidence, label, WINDOW, ("checkout",), **kwargs)


def episode(id="one", group="group-1", split="dev", parent=None):
    return EpisodeSpec(id=id, group=group, split=split, parent_episode=parent, report="public/report.json",
                       evidence="public/evidence.json", label="private/label.json", window=WINDOW, services=("checkout",))


def test_mechanism_match_uses_structured_supported_first_hypothesis_only():
    assert score()["mechanism_exact_match"] is True
    wrong = report(mechanism="process_crash").model_copy(update={"summary": "connection_failure"})
    assert score(wrong)["mechanism_exact_match"] is False
    assert score(report(hypothesis_status="candidate"))["mechanism_exact_match"] is False
    assert score(report(status="INCONCLUSIVE", hypothesis_status="uncertain"))["mechanism_exact_match"] is False
    later = report().hypotheses[0]
    ranking = wrong.model_copy(update={"hypotheses": (wrong.hypotheses[0], later)})
    assert score(ranking)["mechanism_exact_match"] is False


def test_semantic_human_review_remains_separate_from_match_and_citations():
    row = score(human_review=HumanReview(reviewer="reviewer-1", checked_claims=2, unsupported_claims=1, semantic_mechanism_correct=False))
    metrics = aggregate([row])
    assert metrics["mechanism_exact_match"]["value"] == 1
    assert metrics["structural_citation_validity"]["value"] == 1
    assert metrics["human_unsupported_claim_rate"]["value"] == 0.5
    assert metrics["human_semantic_mechanism_correct"]["value"] == 0


@pytest.mark.parametrize("evidence", [[], [record(service="payment")], [record(timestamp=WINDOW.end)], [record(timestamp=START - timedelta(seconds=1))]])
def test_citation_must_exist_and_be_in_scope(evidence):
    row = score(evidence=evidence)
    assert row["structural_citations"] == {"k": 0, "n": 1}
    assert row["hypothesis_structural_citations"] == {"k": 0, "n": 1}
    assert row["mechanism_exact_match"] is True  # Mechanical label match is not semantic support.


def test_uncited_claims_are_reported_and_zero_citation_denominator_is_na():
    row = score(report(citations=()))
    assert row["uncited_claims"] == 1
    assert aggregate([row])["structural_citation_validity"]["display"] == "N/A"


def test_duplicate_evidence_ids_reject_ambiguous_registry():
    with pytest.raises(ValueError, match="duplicate evidence"):
        score(evidence=[record(), record(service="payment")])


def test_coverage_selective_accuracy_and_false_positives_have_separate_denominators():
    rows = [score(), score(report(status="INCONCLUSIVE", hypothesis_status="uncertain")),
            score(label=Label(answerability="no_incident")),
            score(report(status="INCONCLUSIVE", hypothesis_status="uncertain"), label=Label(answerability="symptom_only"))]
    metrics = aggregate(rows)
    assert metrics["mechanism_exact_match"]["display"] == "1/2"
    assert metrics["conclusive_coverage"]["display"] == "1/2"
    assert metrics["selective_exact_match"]["display"] == "1/1"
    assert metrics["healthy_false_positive"]["display"] == "1/1"
    assert metrics["symptom_only_structural_restraint"]["display"] == "1/1"
    assert metrics["human_unsupported_claim_rate"]["value"] is None


def test_all_abstentions_have_na_selective_accuracy_and_missing_latency():
    metrics = aggregate([score(report(status="INCONCLUSIVE", hypothesis_status="uncertain"))])
    assert metrics["selective_exact_match"]["display"] == "N/A"
    assert metrics["conclusive_coverage"]["value"] == 0
    assert metrics["latency_ms"]["median"] is None
    assert metrics["tool_calls"]["total"] == 3
    assert aggregate([])["mechanism_exact_match"]["display"] == "N/A"


def test_latency_reports_observed_values_without_filling_missing_samples():
    metrics = aggregate([score(latency_ms=12), score(), score(latency_ms=24)])
    assert metrics["latency_ms"] == {"n": 2, "total": 36, "median": 18, "p95": 24}
    with pytest.raises(ValueError, match="latency"):
        score(latency_ms=float("inf"))


def test_label_answerability_and_human_annotation_counts_are_validated():
    with pytest.raises(ValueError, match="failure_mode"):
        Label(answerability="mechanism_identifiable")
    with pytest.raises(ValueError, match="failure_mode"):
        Label(answerability="no_incident", failure_mode="crash")
    with pytest.raises(ValueError, match="unsupported_claims"):
        HumanReview(reviewer="one", checked_claims=1, unsupported_claims=2)
    label = Label(answerability="symptom_only", observed_edge=("checkout", "payment"), origin_service="checkout")
    assert score(label=label)["localization"].startswith("not_scored")


@pytest.mark.parametrize("records", [
    [episode(), episode(id="two", split="test")],
    [episode(), episode(id="two", group="group-2", split="test", parent="one")],
    [episode(id="variant-a", parent="parent"), episode(id="variant-b", group="group-2", parent="parent", split="validation")],
])
def test_same_group_or_parent_never_crosses_splits(records):
    with pytest.raises(ValueError, match="split leakage"):
        validate_splits(records)
    with pytest.raises(ValueError, match="split leakage"):
        validate_splits(list(reversed(records)))


def test_split_guard_accepts_same_split_derivatives_and_rejects_duplicate_ids():
    validate_splits([episode(), episode(id="two", parent="one")])
    with pytest.raises(ValueError, match="duplicate episode"):
        validate_splits([episode(), episode()])


def test_recursive_leakage_scanner_checks_paths_text_and_supplied_answer_names():
    payload = {"public/episode/injection.json": {}, "spans": [{"attributes": {"feature_flag.key": "hidden"}}],
               "body": "paymentServiceUnreachable was enabled", "context": "checkout address rewritten"}
    hits = scan_leakage(payload, injected_flags=["paymentServiceUnreachable"], scenario_answers=["checkout address rewritten"])
    reasons = {reason for hit in hits for reason in hit["reasons"]}
    assert reasons == {"injection_metadata", "injected_flag", "scenario_answer"}
    assert any("injection.json" in hit["path"] for hit in hits)
    assert len(scan_leakage(payload, injected_flags=["paymentServiceUnreachable"], max_hits=2)) == 2
    assert scan_leakage({"body": "upstream connection refused"}) == []


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def make_manifest(tmp_path):
    spec = episode()
    write_json(tmp_path / spec.report, report().model_dump(mode="json"))
    write_json(tmp_path / spec.evidence, [record().model_dump(mode="json")])
    write_json(tmp_path / spec.label, LABEL.model_dump(mode="json"))
    manifest_path = tmp_path / "manifest.json"
    write_json(manifest_path, {"episodes": [spec.model_dump(mode="json")]})
    return manifest_path


def test_cli_reads_existing_reports_and_writes_split_results_only(tmp_path):
    manifest = make_manifest(tmp_path)
    output = tmp_path / "scores.json"
    main(["score", str(manifest), "--output", str(output)])
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert saved["by_split"]["dev"]["mechanism_exact_match"]["display"] == "1/1"
    assert saved["by_split"]["test"]["mechanism_exact_match"]["display"] == "N/A"
    assert "not a benchmark" in saved["notice"]
    assert saved["requires_leakage_review"] is False


def test_cli_does_not_overwrite_input_or_launch_missing_investigation(tmp_path):
    manifest = make_manifest(tmp_path)
    original = manifest.read_bytes()
    with pytest.raises(SystemExit):
        main(["score", str(manifest), "--output", str(manifest)])
    assert manifest.read_bytes() == original
    (tmp_path / "public/report.json").unlink()
    with pytest.raises(SystemExit):
        main(["score", str(manifest), "--output", str(tmp_path / "new.json")])
    assert not (tmp_path / "new.json").exists()
