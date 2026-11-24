"""Explicit plans aggregate positive checks without diagnosis or vote counts."""

from datetime import datetime, timedelta, timezone
from typing import get_args

import pytest
from pydantic import ValidationError

from aegis_rca.models.check import (
    CheckResult,
    CheckScope,
    CheckSpec,
    HypothesisVerificationPlan,
    LegacyEvidenceExists,
    PlanReason,
    PlanVerificationResult,
    RpcExists,
)
from aegis_rca.models.incident import TimeWindow
from aegis_rca.models.telemetry import LogRecord, SpanRecord
from aegis_rca.observations import observe_spans
from aegis_rca.tools.normalize import normalize_logs
from aegis_rca.verification.aggregate import aggregate_plan
from aegis_rca.verification.catalog import connectivity_plan
from aegis_rca.verification.evaluate import evaluate_check
from aegis_rca.verification.legacy_bridge import evaluate_legacy_check


def _window():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return TimeWindow(start=start, end=start + timedelta(minutes=1))


def _check(**updates):
    return CheckSpec(
        scope=CheckScope(signal="trace", services=("checkout",), window=_window()),
        predicate=RpcExists(local_service="checkout", rpc_status_names=("UNAVAILABLE",)),
        **updates,
    )


def _result(check, verdict="SUPPORT", *, matched_ids=None, **updates):
    legacy = isinstance(check.predicate, LegacyEvidenceExists)
    ids = matched_ids if matched_ids is not None else (("E-log-1",) if legacy else ("O-trace-1",))
    return CheckResult(
        check_id=check.id,
        verdict=verdict,
        input_kind="legacy_evidence" if legacy else "observation",
        evaluator="legacy_compatibility_v1" if legacy else "observation_verifier_v1",
        matched_ids=ids if verdict == "SUPPORT" else (),
        relevant_ids=ids,
        reason="positive_match" if verdict == "SUPPORT" else "open_scope_no_match",
        **updates,
    )


def _plan(checks, **updates):
    return HypothesisVerificationPlan(failure_mode="rpc_connection_establishment", checks=checks, **updates)


def test_catalog_preserves_exact_connectivity_matchers_and_no_correlation():
    plan = connectivity_plan(_window())
    assert plan.catalog_key == "dependency_connectivity"
    assert plan.failure_mode == "rpc_connection_establishment"
    assert len(plan.checks) == 2
    assert all(check.requirement == "required" for check in plan.checks)
    assert all(check.relation == "supports_hypothesis" for check in plan.checks)
    assert {check.scope.signal for check in plan.checks} == {"trace", "log"}
    assert all(check.scope.services == ("checkout",) for check in plan.checks)
    rpc = next(check.predicate for check in plan.checks if isinstance(check.predicate, RpcExists))
    assert rpc.local_service == "checkout"
    assert rpc.span_kind == "CLIENT"
    assert rpc.peer_service == "payment"
    assert rpc.require_resolved_peer and rpc.require_unambiguous_peer
    assert rpc.rpc_status_names == ("UNAVAILABLE",)
    assert rpc.outcomes == ()
    legacy = next(check.predicate for check in plan.checks if isinstance(check.predicate, LegacyEvidenceExists))
    assert legacy.service == "checkout" and legacy.peer_service == "payment"
    assert legacy.codes == ("connection_refused", "dns_resolution_failed")
    assert "correlation" not in plan.model_dump_json()


def test_catalog_deterministic_and_scope_changes_plan_id():
    first = connectivity_plan(_window())
    assert first == connectivity_plan(_window())
    moved = TimeWindow(start=_window().start + timedelta(seconds=1), end=_window().end)
    assert first.id != connectivity_plan(moved).id


def test_both_required_connectivity_results_support_known_plan():
    plan = connectivity_plan(_window())
    result = aggregate_plan(plan, results=tuple(_result(check) for check in plan.checks))
    assert result.status == "SUPPORTED"
    assert result.plan_id == plan.id
    assert len(result.check_result_ids) == 2


def test_no_plan_is_uncertain_even_with_supplied_support_result():
    result = aggregate_plan(None, results=(_result(_check()),))
    assert result.status == "UNCERTAIN"
    assert result.plan_id is None and result.check_result_ids == ()


@pytest.mark.parametrize("checks", [(), (_check(requirement="optional"),)])
def test_empty_required_support_set_never_supports(checks):
    plan = _plan(checks)
    result = aggregate_plan(plan, results=tuple(_result(check) for check in checks))
    assert result.status == "UNCERTAIN"
    assert result.reason == "no_required_support_checks"


@pytest.mark.parametrize("mode", ["custom_llm_mode", "payment_application_error", "checkout_resource_saturation"])
def test_unknown_application_and_saturation_modes_are_not_elevated(mode):
    check = _check()
    plan = HypothesisVerificationPlan(failure_mode=mode, checks=(check,))
    assert aggregate_plan(plan, results=(_result(check),)).status == "UNCERTAIN"


def test_unregistered_plan_version_is_uncertain():
    check = _check()
    assert aggregate_plan(_plan((check,), semantic_version="2"), results=(_result(check),)).status == "UNCERTAIN"


@pytest.mark.parametrize("requirement", ["required", "optional"])
def test_unregistered_check_version_cannot_support_through_hand_constructed_result(requirement):
    required = _check()
    future = _check(semantic_version="2", requirement=requirement)
    plan = _plan((required, future))
    result = aggregate_plan(plan, results=(_result(required), _result(future)))
    assert plan.semantic_version == "1"
    assert result.status == "UNCERTAIN"
    assert result.reason == "unsupported_check_version"


@pytest.mark.parametrize("verdict", ["UNKNOWN", "NEUTRAL", "CONTRADICT"])
def test_non_support_required_result_cannot_support(verdict):
    check = _check()
    assert aggregate_plan(_plan((check,)), results=(_result(check, verdict),)).status == "UNCERTAIN"


def test_missing_required_result_cannot_support():
    plan = connectivity_plan(_window())
    assert aggregate_plan(plan, results=(_result(plan.checks[0]),)).status == "UNCERTAIN"


@pytest.mark.parametrize("optional_verdict", ["UNKNOWN", "NEUTRAL", "SUPPORT"])
def test_optional_results_do_not_replace_or_block_required_support(optional_verdict):
    required = _check()
    optional = _check(requirement="optional")
    plan = _plan((required, optional))
    result = aggregate_plan(plan, results=(_result(required), _result(optional, optional_verdict)))
    assert result.status == "SUPPORTED"


def test_explicit_contradiction_on_optional_check_blocks_support():
    required = _check()
    optional = _check(requirement="optional")
    result = aggregate_plan(_plan((required, optional)), results=(_result(required), _result(optional, "CONTRADICT")))
    assert result.status == "UNCERTAIN"
    assert result.reason == "explicit_contradiction"


def test_reserved_contradiction_relation_is_not_executed_as_success_incompatibility():
    required = _check()
    reserved = _check(requirement="optional", relation="contradicts_hypothesis")
    result = aggregate_plan(_plan((required, reserved)), results=(_result(required), _result(reserved, "UNKNOWN")))
    assert result.status == "UNCERTAIN"
    assert result.reason == "contradiction_relations_reserved"


def test_duplicate_semantic_checks_rejected_even_with_different_aliases():
    with pytest.raises(ValidationError, match="duplicate semantic"):
        _plan((_check(alias="one"), _check(alias="two", description="Different prose")))


def test_equal_duplicate_results_and_reordering_do_not_change_result_or_id():
    plan = connectivity_plan(_window())
    results = tuple(_result(check) for check in plan.checks)
    first = aggregate_plan(plan, results=results)
    second = aggregate_plan(plan, results=(*reversed(results), results[0]))
    assert first == second and first.id == second.id


def test_result_display_prose_does_not_create_a_conflicting_duplicate():
    check = _check()
    original = _result(check)
    displayed = original.model_copy(update={"summary": "Another description"})
    assert aggregate_plan(_plan((check,)), results=(original, displayed)) == aggregate_plan(_plan((check,)), results=(original,))


def test_conflicting_results_for_one_check_rejected_in_either_order():
    check = _check()
    first, second = _result(check), _result(check, "NEUTRAL")
    for results in ((first, second), (second, first)):
        with pytest.raises(ValueError, match="conflicting results"):
            aggregate_plan(_plan((check,)), results=results)


def test_foreign_structural_check_result_rejected():
    plan = _plan((_check(),))
    different = _check(semantic_version="2")
    with pytest.raises(ValueError, match="does not belong"):
        aggregate_plan(plan, results=(_result(different),))


@pytest.mark.parametrize("use_legacy_check", [False, True])
def test_native_and_legacy_result_origin_cannot_be_substituted(use_legacy_check):
    plan = connectivity_plan(_window())
    check = next(check for check in plan.checks if isinstance(check.predicate, LegacyEvidenceExists) == use_legacy_check)
    wrong = CheckResult(
        check_id=check.id, verdict="SUPPORT", matched_ids=("fixture-id",), relevant_ids=("fixture-id",),
        input_kind="observation" if use_legacy_check else "legacy_evidence",
        evaluator="observation_verifier_v1" if use_legacy_check else "legacy_compatibility_v1",
        reason="positive_match",
    )
    with pytest.raises(ValueError, match="origin"):
        aggregate_plan(plan, results=(wrong,))


def test_support_without_any_positive_fact_reference_is_rejected():
    check = _check()
    with pytest.raises(ValueError, match="matched admitted facts"):
        aggregate_plan(_plan((check,)), results=(_result(check, matched_ids=()),))


def test_plan_identity_ignores_display_and_order_but_binds_mode_version_and_checks():
    plan = connectivity_plan(_window())
    display = HypothesisVerificationPlan(
        failure_mode=plan.failure_mode,
        checks=tuple(check.model_copy(update={"alias": "display", "description": "changed"}) for check in reversed(plan.checks)),
        catalog_key="another display key", description="changed",
    )
    assert display.id == plan.id
    assert plan.model_copy(update={"failure_mode": "custom"}).id != plan.id
    assert plan.model_copy(update={"semantic_version": "2"}).id != plan.id
    assert _plan((plan.checks[0],)).id != plan.id


def test_no_vote_count_confidence_or_rejection_added_to_aggregation():
    check = _check()
    one = aggregate_plan(_plan((check,)), results=(_result(check, matched_ids=("O-trace-1",)),))
    many = aggregate_plan(_plan((check,)), results=(_result(check, matched_ids=("O-trace-1", "O-trace-2")),))
    assert one.status == many.status == "SUPPORTED"
    assert "confidence" not in one.model_dump()
    assert "REJECTED" not in one.model_dump_json()


@pytest.mark.parametrize("reason", get_args(PlanReason))
def test_every_aggregator_reason_round_trips_with_stable_identity(reason):
    required = _check()
    reserved = _check(requirement="optional", relation="contradicts_hypothesis")
    future = _check(semantic_version="2")
    cases = {
        "plan_missing": (None, ()),
        "unsupported_failure_mode": (
            HypothesisVerificationPlan(failure_mode="custom", checks=(required,)), ()),
        "unsupported_plan_version": (_plan((required,), semantic_version="2"), ()),
        "unsupported_check_version": (_plan((future,)), ()),
        "no_required_support_checks": (_plan(()), ()),
        "contradiction_relations_reserved": (_plan((required, reserved)), ()),
        "explicit_contradiction": (_plan((required,)), (_result(required, "CONTRADICT"),)),
        "required_results_missing": (_plan((required,)), ()),
        "required_check_not_supported": (_plan((required,)), (_result(required, "UNKNOWN"),)),
        "all_required_support_checks_established": (_plan((required,)), (_result(required),)),
    }
    assert set(cases) == set(get_args(PlanReason))
    plan, results = cases[reason]
    original = aggregate_plan(plan, results=results)
    assert original.reason == reason
    restored = PlanVerificationResult.model_validate_json(original.model_dump_json())
    assert restored == original and restored.id == original.id


@pytest.mark.parametrize("grpc_status, expected_status", [(14, "SUPPORTED"), (2, "UNCERTAIN")])
def test_catalog_with_actual_evaluators_preserves_connectivity_and_payment_failure_boundary(
    grpc_status, expected_status,
):
    window = _window()
    service_map = {"oteldemo.PaymentService": "payment"}
    span = SpanRecord(
        trace_id="1" * 32, span_id="a" * 16, service="checkout",
        name="oteldemo.PaymentService/Charge", kind="CLIENT",
        start=window.start + timedelta(seconds=10), duration_ms=5, status="ERROR",
        attributes={"rpc.service": "oteldemo.PaymentService", "rpc.grpc.status_code": grpc_status},
        source="synthetic:rpc",
    )
    log = LogRecord(
        id="connection-log", timestamp=window.start + timedelta(seconds=20),
        service="checkout", severity="ERROR", body="dial payment:50051: connection refused",
        trace_id="2" * 32, attributes={"peer.service": "payment"}, source="synthetic:log",
    )
    observations = observe_spans((span,), service_map, synthetic=True)
    evidence = normalize_logs((log,), (span,), service_map)
    assert observations[0].correlation.trace_id != evidence[0].correlation_group
    assert observations[0].value.outcome == "failure"
    plan = connectivity_plan(window)
    assert len(plan.checks) == 2
    assert all(check.requirement == "required" for check in plan.checks)
    assert {check.scope.signal for check in plan.checks} == {"trace", "log"}
    rpc_check = next(check for check in plan.checks if isinstance(check.predicate, RpcExists))
    legacy_check = next(check for check in plan.checks if isinstance(check.predicate, LegacyEvidenceExists))
    rpc_result = evaluate_check(rpc_check, observations=observations)
    legacy_result = evaluate_legacy_check(legacy_check, evidence=evidence)
    assert legacy_result.verdict == "SUPPORT"
    assert legacy_result.matched_ids == (evidence[0].id,)
    assert legacy_result.input_kind == "legacy_evidence"
    assert all(identifier.startswith("E-") for identifier in legacy_result.relevant_ids)
    result = aggregate_plan(plan, results=(rpc_result, legacy_result))
    assert result.status == expected_status
    for original in (rpc_result, legacy_result, result):
        restored = type(original).model_validate_json(original.model_dump_json())
        assert restored == original and restored.id == original.id
    if grpc_status == 14:
        assert rpc_result.verdict == "SUPPORT"
    else:
        assert observations[0].value.rpc_status_name == "UNKNOWN"
        assert rpc_result.verdict == "UNKNOWN"
        generic_check = CheckSpec(
            scope=rpc_check.scope,
            predicate=RpcExists(local_service="checkout", peer_service="payment", outcomes=("failure",)),
        )
        generic_result = evaluate_check(generic_check, observations=observations)
        assert generic_result.verdict == "SUPPORT"
        assert generic_check.id != rpc_check.id
        with pytest.raises(ValueError, match="does not belong"):
            aggregate_plan(plan, results=(generic_result, legacy_result))
