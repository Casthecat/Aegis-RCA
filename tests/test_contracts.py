from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from aegis_rca.fixtures import demo_evidence, demo_incident, demo_traces
from aegis_rca.models.evidence import Observation
from aegis_rca.models.hypothesis import Verification
from aegis_rca.models.incident import TimeWindow
from aegis_rca.policy import form_hypotheses
from aegis_rca.observations import observe_spans
from aegis_rca.verification.connectivity import evaluate_connectivity
from aegis_rca.report import audit_and_report
from aegis_rca.state import initial_state, merge_records


def test_merge_is_idempotent_and_order_independent():
    metric, trace, log = demo_evidence()
    a = merge_records([metric, trace], [trace, log])
    b = merge_records([log, trace], [metric, trace])
    assert a == b
    assert len(a) == 3
    assert merge_records(a, a) == a
    assert merge_records(merge_records([metric], [trace]), [log]) == a


def test_conflicting_evidence_ids_fail_instead_of_overwriting():
    evidence = demo_evidence()[0]
    with pytest.raises(ValueError, match="Conflicting records"):
        merge_records([evidence], [evidence.model_copy(update={"excerpt": "different content"})])


def test_time_window_requires_timezone_and_positive_duration():
    now = datetime(2026, 9, 8, tzinfo=timezone.utc)
    with pytest.raises(ValidationError):
        TimeWindow(start=now, end=now)
    with pytest.raises(ValidationError):
        TimeWindow(start="2026-09-08T12:00:00", end="2026-09-08T12:05:00")


def test_missing_server_span_does_not_prove_connectivity_failure_or_reject_application():
    trace = demo_evidence()[1].model_copy(update={
        "observation": Observation(code="server_span_absent", peer_service="payment")})
    connectivity = evaluate_connectivity(demo_incident().window)
    state = initial_state(demo_incident())
    state["evidence"] = [trace]
    hypotheses = form_hypotheses(state)
    assert connectivity.plan_result.status == "UNCERTAIN"
    assert all(h.status == "uncertain" for h in hypotheses)


def test_duplicate_signal_and_wrong_peer_cannot_meet_verification_gate():
    metric, trace, log = demo_evidence()
    facts = observe_spans(demo_traces().spans, {"oteldemo.PaymentService": "payment"})
    assert evaluate_connectivity(demo_incident().window, trace_observations=facts * 2).plan_result.status == "UNCERTAIN"
    wrong_peer = log.model_copy(update={
        "observation": Observation(code="connection_refused", peer_service="shipping")})
    assert evaluate_connectivity(demo_incident().window, trace_observations=facts,
                                 legacy_log_evidence=(wrong_peer,)).plan_result.status == "UNCERTAIN"


def test_auditor_rejects_finalize_without_semantic_predicates():
    state = initial_state(demo_incident())
    state["verification"] = Verification(decision="finalize", reason="untrusted_planner_claim")
    state["evidence"] = [demo_evidence()[0]]
    report = audit_and_report(state)
    assert report.status == "INCONCLUSIVE"
    assert not report.claims


async def test_report_citations_resolve_and_do_not_claim_underlying_cause():
    from aegis_rca.fixtures import FixtureBackend
    from aegis_rca.runner import run_investigation
    from aegis_rca.runtime import Dependencies
    state, _ = await run_investigation(initial_state(demo_incident()), Dependencies(FixtureBackend()))
    report = audit_and_report(state)
    assert report.status == "SUPPORTED"
    assert report.diagnosis_level == "failure_mode"
    assert report.underlying_cause is None
    assert "crashed" not in report.summary.lower()
    assert {key for claim in report.claims for key in claim.evidence_ids} <= {
        e.id for e in state["evidence"]}
