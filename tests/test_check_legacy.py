"""The explicitly labelled legacy bridge remains a structured-code boundary."""

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from aegis_rca.models.check import CheckCoverageView, CheckScope, CheckSpec, LegacyEvidenceExists, RpcExists
from aegis_rca.models.evidence import Evidence, EvidenceSource, Observation
from aegis_rca.models.incident import TimeWindow
from aegis_rca.verification.evaluate import evaluate_check
from aegis_rca.verification.legacy_bridge import evaluate_legacy_check


START = datetime(2026, 9, 9, 12, tzinfo=timezone.utc)
WINDOW = TimeWindow(start=START, end=START + timedelta(minutes=1))


def check(*, query=None, acquisition=None, requirement="required", relation="supports_hypothesis"):
    return CheckSpec(
        scope=CheckScope(signal="log", services=("checkout",), window=WINDOW,
                         query_identity=query, acquisition_id=acquisition),
        predicate=LegacyEvidenceExists(service="checkout", peer_service="payment",
                                       codes=("connection_refused", "dns_resolution_failed")),
        requirement=requirement, relation=relation,
    )


def log(identifier="E-L1", *, code="connection_refused", peer="payment", service="checkout",
        signal="log", timestamp=START, excerpt="any body", group=None):
    return Evidence(id=identifier, signal=signal, service=service, timestamp=timestamp,
                    observation=Observation(code=code, peer_service=peer), excerpt=excerpt,
                    source=EvidenceSource(backend="fixture", locator="private/log"),
                    correlation_group=group)


def complete_view(*, ids=(), **updates):
    values = dict(signal="log", services=("checkout",), window=WINDOW,
                  query_identity="query-logs", acquisition_id="logs-1", input_kind="legacy_evidence",
                  query_status="succeeded", truncated=False, coverage_complete=True,
                  pre_capture_omissions="none", admitted_ids=ids, fact_set_complete=True)
    values.update(updates)
    return CheckCoverageView(**values)


@pytest.mark.parametrize("code", ["connection_refused", "dns_resolution_failed"])
def test_explicit_connectivity_codes_keep_legacy_origin_and_ids(code):
    result = evaluate_legacy_check(check(), evidence=(log(code=code),))
    assert result.verdict == "SUPPORT"
    assert result.input_kind == "legacy_evidence"
    assert result.evaluator == "legacy_compatibility_v1"
    assert result.matched_ids == result.relevant_ids == ("E-L1",)
    assert "O-" not in result.model_dump_json()


def test_bridge_and_native_boundaries_reject_other_input_kinds():
    native = CheckSpec(scope=CheckScope(signal="trace", services=("checkout",), window=WINDOW),
                       predicate=RpcExists(local_service="checkout"))
    with pytest.raises(TypeError, match="LegacyEvidenceExists only"):
        evaluate_legacy_check(native)
    with pytest.raises(TypeError, match="Evidence records only"):
        evaluate_legacy_check(check(), evidence=({"id": "E-L1"},))
    with pytest.raises(TypeError, match="evaluate_legacy_check"):
        evaluate_check(check())
    with pytest.raises(TypeError, match="typed observation records only"):
        evaluate_check(native, observations=(log(),))


@pytest.mark.parametrize("code", ["application_error", "rpc_unavailable", "cpu_saturated"])
def test_code_contract_does_not_expand_legacy_rules(code):
    with pytest.raises(ValidationError):
        LegacyEvidenceExists(service="checkout", peer_service="payment", codes=(code,))
    assert evaluate_legacy_check(check(), evidence=(log(code=code),)).verdict == "UNKNOWN"


@pytest.mark.parametrize("changes", [
    {"service": "frontend"}, {"peer": "inventory"}, {"signal": "trace"},
    {"timestamp": START - timedelta(microseconds=1)}, {"timestamp": WINDOW.end},
])
def test_exact_service_peer_signal_and_half_open_window(changes):
    assert evaluate_legacy_check(check(), evidence=(log(**changes),)).verdict == "UNKNOWN"


def test_bridge_never_reads_body_or_requires_correlation():
    a = log(excerpt="", group=None)
    b = log(excerpt="different unrelated words", group="unrelated-trace")
    left = evaluate_legacy_check(check(), evidence=(a,))
    right = evaluate_legacy_check(check(), evidence=(b,))
    assert left == right
    assert left.verdict == "SUPPORT"
    text_only = log(code="application_error", peer=None,
                    excerpt="checkout connection_refused payment dns_resolution_failed")
    assert evaluate_legacy_check(check(), evidence=(text_only,)).verdict == "UNKNOWN"


def test_input_order_duplicates_and_physical_locator_do_not_change_results():
    a, b = log(), log("E-L2", code="dns_resolution_failed")
    relocated = a.model_copy(update={"source": EvidenceSource(backend="fixture", locator="moved/log")})
    left = evaluate_legacy_check(check(), evidence=(a, b))
    right = evaluate_legacy_check(check(), evidence=(b, relocated, a))
    assert left == right
    assert left.id == right.id
    assert left.matched_ids == ("E-L1", "E-L2")


def test_conflicting_payloads_for_one_identity_never_pick_a_match():
    result = evaluate_legacy_check(check(), evidence=(log(), log(peer="inventory")))
    assert result.verdict == "UNKNOWN"
    assert result.reason == "conflicting_fact_identity"


@pytest.mark.parametrize("updates", [
    {"truncated": True}, {"coverage_complete": False}, {"query_status": "failed"},
    {"pre_capture_omissions": "unknown"}, {"fact_set_complete": False},
])
def test_scoped_positive_survives_incomplete_coverage(updates):
    view = complete_view(ids=("E-L1",), **updates)
    result = evaluate_legacy_check(check(query="query-logs"), evidence=(log(),), coverage=(view,))
    assert result.verdict == "SUPPORT"
    assert result.coverage_used == (view,)


def test_scoped_positive_requires_its_admission_not_an_unrelated_view():
    spec = check(query="query-logs", acquisition="logs-1")
    assert evaluate_legacy_check(spec, evidence=(log(),)).verdict == "UNKNOWN"
    for view in (complete_view(), complete_view(ids=("E-L1",), query_identity="query-other"),
                 complete_view(ids=("E-L1",), acquisition_id="logs-other"),
                 complete_view(ids=("E-L1",), input_kind="observation")):
        assert evaluate_legacy_check(spec, evidence=(log(),), coverage=(view,)).verdict != "SUPPORT"
    admitted = complete_view(ids=("E-L1",))
    assert evaluate_legacy_check(spec, evidence=(log(),), coverage=(admitted,)).verdict == "SUPPORT"


@pytest.mark.parametrize("updates", [
    {"truncated": True}, {"coverage_complete": None}, {"query_status": "unknown"},
    {"pre_capture_omissions": "present"}, {"fact_set_complete": False},
    {"admitted_ids": ("E-MISSING",)}, {"query_identity": "different-query"},
    {"services": ("payment",)}, {"input_kind": "observation"}, {"signal": "trace"},
    {"window": TimeWindow(start=START + timedelta(seconds=1), end=WINDOW.end)},
])
def test_no_match_needs_all_scoped_completeness_conditions(updates):
    result = evaluate_legacy_check(check(query="query-logs"), coverage=(complete_view(**updates),))
    assert result.verdict == "UNKNOWN"


def test_complete_intended_scope_can_produce_only_neutral_no_match():
    spec = check(query="query-logs")
    assert evaluate_legacy_check(spec).verdict == "UNKNOWN"
    result = evaluate_legacy_check(spec, coverage=(complete_view(),))
    assert result.verdict == "NEUTRAL"
    assert result.reason == "complete_no_match"
    assert evaluate_legacy_check(check(), coverage=(complete_view(),)).verdict == "UNKNOWN"
    nonmatch = log(peer="inventory")
    result = evaluate_legacy_check(spec, evidence=(nonmatch,), coverage=(complete_view(ids=("E-L1",)),))
    assert result.verdict == "NEUTRAL"


def test_unresolved_peer_stays_unknown_even_in_complete_scope():
    result = evaluate_legacy_check(check(query="query-logs"), evidence=(log(peer=None),),
                                   coverage=(complete_view(ids=("E-L1",)),))
    assert result.verdict == "UNKNOWN"
    assert result.reason == "unresolved_legacy_peer"


@pytest.mark.parametrize("requirement", ["required", "optional"])
def test_requirement_never_changes_primitive_truth(requirement):
    assert evaluate_legacy_check(check(requirement=requirement), evidence=(log(),)).verdict == "SUPPORT"
    assert evaluate_legacy_check(check(requirement=requirement)).verdict == "UNKNOWN"


def test_reserved_contradiction_relation_does_not_invert_positive_match():
    result = evaluate_legacy_check(check(relation="contradicts_hypothesis"), evidence=(log(),))
    assert result.verdict == "UNKNOWN"
    assert result.reason == "unsupported_relation"


@pytest.mark.parametrize("updates", [
    {"signal": "trace"}, {"service": "frontend"},
    {"timestamp": START - timedelta(microseconds=1)}, {"timestamp": WINDOW.end},
])
def test_claimed_admission_must_agree_with_supplied_record_scope(updates):
    result = evaluate_legacy_check(
        check(query="query-logs"), evidence=(log(**updates),),
        coverage=(complete_view(ids=("E-L1",)),),
    )
    assert result.verdict == "UNKNOWN"
    assert result.reason == "open_scope_no_match"


def test_unknown_check_semantic_version_never_evaluates_as_version_one():
    future = check().model_copy(update={"semantic_version": "2"})
    result = evaluate_legacy_check(future, evidence=(log(),))
    assert result.verdict == "UNKNOWN"
    assert result.reason == "unsupported_check_version"
    assert result.matched_ids == ()


def test_closure_helper_independently_rejects_unrelated_acquisition():
    from aegis_rca.verification._shared import closes

    scope = check(query="query-logs").scope
    assert closes(scope, (complete_view(),), ())
    for view in (complete_view(query_identity="unrelated-query"),
                 complete_view(services=("payment",)),
                 complete_view(input_kind="observation"),
                 complete_view(signal="trace")):
        assert not closes(scope, (view,), ())
