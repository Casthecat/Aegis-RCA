"""Human Gold judgments are independent of injection controls and Aegis results."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from aegis_rca.evaluation import gold, stage_c
from aegis_rca.evaluation.corpus_recording import NEUTRAL_QUERY, PILOT_CAPTURE_POLICY
from aegis_rca.evaluation.gold import (BenchmarkGoldLabelV2, ExpectedDiagnosticClaim,
    GoldJudgment, GoldReviewerProvenance, DEFAULT_GOLD_RUBRIC)
from aegis_rca.evaluation.sanitization import canonical_bytes
from aegis_rca.models.incident import TimeWindow
from test_benchmark_admission import approved
from test_sanitization import fixture


NOW = datetime(2026, 9, 20, tzinfo=timezone.utc)


def neutral_snapshot():
    snapshot = fixture()
    window = TimeWindow(start=snapshot.incident.window.start,
        end=snapshot.incident.window.start+timedelta(seconds=PILOT_CAPTURE_POLICY.duration_seconds))
    return snapshot.model_copy(update={"incident": snapshot.incident.model_copy(update={"query": NEUTRAL_QUERY, "window":window})})


def reviewer(**updates):
    return GoldReviewerProvenance(**dict(dict(reviewer_id="blinded-human", mode="independent",
        reviewed_at=NOW+timedelta(minutes=1), approved_telemetry_reviewed=True,
        injection_truth_sealed=True, stage_c_results_unseen=True, prior_injection_knowledge=False,
        coverage_reviewed=True, coverage_sufficient=True), **updates))


def judgment(**updates):
    return GoldJudgment(**dict(dict(answerability="mechanism_identifiable",
        expected_claims=(ExpectedDiagnosticClaim(caller="checkout", callee="payment"),),
        claim_labels_exhaustive=False, reviewer=reviewer()), **updates))


def review_packet(tmp_path):
    root, cid, _, _ = approved(tmp_path, neutral_snapshot())
    packet = gold.prepare_gold_review(root, cid, PILOT_CAPTURE_POLICY, tmp_path / "blind", prepared_at=NOW)
    return root, cid, packet


def test_packet_has_only_approved_telemetry_rubric_and_coverage(tmp_path, monkeypatch):
    root, cid, _, _ = approved(tmp_path, neutral_snapshot())
    forbidden = (tmp_path / "sealed-truth", tmp_path / "stage-c-results")
    original = Path.open
    def guarded(path, *args, **kwargs):
        assert not any(path.resolve().is_relative_to(p.resolve()) for p in forbidden)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", guarded)
    monkeypatch.setattr(stage_c, "run_investigation", lambda *_: pytest.fail("Gold reviewer ran Aegis"))
    packet = gold.prepare_gold_review(root, cid, PILOT_CAPTURE_POLICY, tmp_path / "blind", prepared_at=NOW)
    payload = b"".join(p.read_bytes() for p in (tmp_path / "blind").iterdir())
    assert b"unseen-control-793" not in payload
    assert b"scenario_family_id" not in payload and b"control_flag" not in payload
    assert packet.trace_truncated is True and packet.trace_records == 1
    assert packet.trace_coverage_complete is None
    assert packet.capture_policy == PILOT_CAPTURE_POLICY
    template = json.loads((tmp_path / "blind" / "decision.template.json").read_bytes())
    assert template["answerability"] is None
    assert template["reviewer"]["approved_telemetry_reviewed"] is False
    with pytest.raises(ValidationError):
        GoldJudgment.model_validate(template)


def test_human_gold_freezes_only_after_packet_review_and_cannot_overwrite(tmp_path):
    root, cid, packet = review_packet(tmp_path)
    decision = tmp_path / "decision.json"
    decision.write_bytes(canonical_bytes(judgment().model_dump(mode="json")))
    output = tmp_path / "gold" / f"{cid}.json"
    label = gold.freeze_gold(root, tmp_path / "blind", decision, output, frozen_at=NOW+timedelta(minutes=2))
    assert label.review_packet_id == packet.id
    assert label.gold_diagnostic_rubric_id == DEFAULT_GOLD_RUBRIC.id
    assert label.expected_claims[0].callee == "payment"
    assert label.claim_labels_exhaustive is False
    assert BenchmarkGoldLabelV2.model_validate_json(output.read_bytes()) == label
    with pytest.raises(FileExistsError):
        gold.freeze_gold(root, tmp_path / "blind", decision, output, frozen_at=NOW+timedelta(minutes=2))
    assert "control_flag" not in label.model_dump()
    with pytest.raises(ValidationError):
        BenchmarkGoldLabelV2.model_validate({**label.model_dump(), "control_flag": "paymentUnreachable"})


def test_insufficient_healthy_coverage_requires_human_insufficient_label(tmp_path):
    root, cid, packet = review_packet(tmp_path)
    # No operator/injection metadata is an input to this decision. Actual admitted
    # truncation prevents a reviewer from asserting complete healthy coverage.
    decision = tmp_path / "decision.json"
    healthy = judgment(answerability="no_incident", expected_claims=(), claim_labels_exhaustive=True,
        reviewer=reviewer(empty_set_justified=True, successful_path_and_no_incident_symptoms=True))
    decision.write_bytes(canonical_bytes(healthy.model_dump(mode="json")))
    output = tmp_path / "gold" / f"{cid}.json"
    with pytest.raises(ValueError, match="incomplete observed coverage"):
        gold.freeze_gold(root, tmp_path / "blind", decision, output, frozen_at=NOW+timedelta(minutes=2))
    insufficient = judgment(answerability="insufficient_evidence", expected_claims=(),
        reviewer=reviewer(coverage_sufficient=False))
    decision.write_bytes(canonical_bytes(insufficient.model_dump(mode="json")))
    label = gold.freeze_gold(root, tmp_path / "blind", decision, output, frozen_at=NOW+timedelta(minutes=2))
    assert label.answerability == "insufficient_evidence" and label.expected_claims == ()


@pytest.mark.parametrize("update", [dict(stage_c_results_unseen=False), dict(injection_truth_sealed=False),
    dict(approved_telemetry_reviewed=False), dict(coverage_reviewed=False), dict(prior_injection_knowledge=True)])
def test_blind_provenance_cannot_assert_missing_review_or_independence(update):
    with pytest.raises(ValidationError):
        reviewer(**update)
    assert reviewer(mode="weak_blind", prior_injection_knowledge=True).mode == "weak_blind"


def test_gold_review_requires_current_approval_and_post_packet_judgment(tmp_path):
    root, cid, packet = review_packet(tmp_path)
    decision = tmp_path / "decision.json"
    decision.write_bytes(canonical_bytes(judgment(reviewer=reviewer(reviewed_at=NOW-timedelta(seconds=1))).model_dump(mode="json")))
    output = tmp_path / "gold" / f"{cid}.json"
    with pytest.raises(ValueError, match="follow approved packet"):
        gold.freeze_gold(root, tmp_path / "blind", decision, output, frozen_at=NOW+timedelta(minutes=2))
    decision.write_bytes(canonical_bytes(judgment().model_dump(mode="json")))
    (root / "candidates" / cid / "snapshot.json").write_bytes(b"{}")
    with pytest.raises(ValueError):
        gold.freeze_gold(root, tmp_path / "blind", decision, output, frozen_at=NOW+timedelta(minutes=2))
    assert not output.exists()


def test_claim_is_bound_mechanism_edge_without_parallel_arrays():
    claim = ExpectedDiagnosticClaim(caller="checkout", callee="payment")
    assert claim.id != ExpectedDiagnosticClaim(caller="frontend", callee="catalog").id
    with pytest.raises(ValidationError):
        ExpectedDiagnosticClaim(caller="checkout", callee="checkout")
    with pytest.raises(ValidationError):
        ExpectedDiagnosticClaim(caller="checkout", callee="payment", operation="Charge")
    with pytest.raises(ValidationError):
        GoldJudgment.model_validate({**judgment().model_dump(), "expected_edges": [("checkout", "payment")]})


def test_no_incident_never_defaults_from_empty_claims():
    with pytest.raises(ValidationError):
        judgment(answerability="no_incident", expected_claims=())
    with pytest.raises(ValidationError):
        judgment(answerability="mechanism_identifiable", expected_claims=())
    with pytest.raises(ValidationError):
        judgment(answerability="root_cause_identifiable")


def test_explicit_incomplete_acquisition_cannot_be_frozen_as_no_incident(tmp_path):
    snapshot = neutral_snapshot()
    snapshot = snapshot.model_copy(update={"traces":snapshot.traces.model_copy(update={
        "truncated":False, "coverage_complete":False})})
    root, cid, _, _ = approved(tmp_path, snapshot)
    gold.prepare_gold_review(root, cid, PILOT_CAPTURE_POLICY, tmp_path / "blind", prepared_at=NOW)
    decision = tmp_path / "decision.json"
    decision.write_bytes(canonical_bytes(judgment(answerability="no_incident", expected_claims=(),
        claim_labels_exhaustive=True, reviewer=reviewer(empty_set_justified=True, successful_path_and_no_incident_symptoms=True)).model_dump(mode="json")))
    with pytest.raises(ValueError, match="incomplete observed coverage"):
        gold.freeze_gold(root, tmp_path / "blind", decision, tmp_path / "gold" / f"{cid}.json",
            frozen_at=NOW+timedelta(minutes=2))
