# P1.4-1 review fix response

Both requested fixes are complete; this patch does not start P1.4-2.

- Epistemically unknown/inconsistent outcomes and OTLP UNKNOWN remain unresolved for concrete checks, even with complete acquisition. Explicit requests for these normalized states may support. An independent concrete mismatch still makes a record irrelevant. gRPC UNKNOWN stays concrete protocol code 2.
- CheckResult.reason uses the 18-code CheckReason Literal; PlanVerificationResult.reason uses the 10-code PlanReason Literal. Arbitrary prose is rejected. CheckResult.summary and existing valid structural ID semantics are unchanged.
- Focused P1.4: **218 passed in 0.62s**. Full pytest: **672 passed, 1 skipped in 6.59s**. The unchanged skip is native symlink privilege at tests/test_sanitization.py:387.
- Existing O-trace canonical JSON, O-metric P1.3a/P1.3b-1 full-wire goldens, and pre-hardening valid C/CR/P/PR IDs remain unchanged. Every emitted evaluator/aggregator reason has actual-output round-trip coverage.
- Graph, state, policy, report, observations, metric derivation, benchmark, planner and LLM were not modified. No contradiction/absence predicates were added.

The following are the **complete final contents** of the four files requested by the external reviewer, captured after the passing full run. Paths are repository-relative labels; SHA-256 values identify the original source bytes. Code blocks normalize line endings for copying.

## src/aegis_rca/verification/aggregate.py

SHA-256: `bd548bdad623d3f76607c9971372b441d3b433e556b5a40bf042bbf50cf5e3f8`

```python
"""Pure, conservative aggregation of explicitly structured verification plans."""

from ..models.check import (
    CheckResult,
    HypothesisVerificationPlan,
    LegacyEvidenceExists,
    PlanReason,
    PlanVerificationResult,
)


# A failure-mode name or free-form hypothesis never registers executable policy.
# Future modes and versions require an explicit code change and their own tests.
_SUPPORTED_FAILURE_MODES = frozenset({"payment_connection_failure"})
_SUPPORTED_PLAN_VERSIONS = frozenset({"1"})


def aggregate_plan(
    plan: HypothesisVerificationPlan | None,
    *,
    results: tuple[CheckResult, ...] = (),
) -> PlanVerificationResult:
    """Support a known plan only when all nonempty required checks support it.

    Results belong to structural check identities, not human-readable aliases.
    This function does not reinterpret facts, count votes, or inspect hypothesis
    text. Reserved contradiction relations remain unsupported in this stage.
    """
    if plan is None:
        return PlanVerificationResult(plan_id=None, status="UNCERTAIN", reason="plan_missing")

    checks = {check.id: check for check in plan.checks}
    selected: dict[str, CheckResult] = {}
    for result in results:
        check = checks.get(result.check_id)
        if check is None:
            raise ValueError("check result does not belong to this structural plan")
        legacy = isinstance(check.predicate, LegacyEvidenceExists)
        expected_kind = "legacy_evidence" if legacy else "observation"
        expected_evaluator = "legacy_compatibility_v1" if legacy else "observation_verifier_v1"
        if result.input_kind != expected_kind or result.evaluator != expected_evaluator:
            raise ValueError("check result origin does not match the corresponding predicate")
        if result.verdict == "SUPPORT" and not result.matched_ids:
            raise ValueError("a positive SUPPORT result must reference matched admitted facts")
        existing = selected.get(result.check_id)
        if existing is not None and existing.id != result.id:
            raise ValueError("conflicting results for one structural check are not allowed")
        selected[result.check_id] = result

    result_ids = tuple(result.id for result in selected.values())

    def uncertain(reason: PlanReason) -> PlanVerificationResult:
        return PlanVerificationResult(
            plan_id=plan.id, status="UNCERTAIN", check_result_ids=result_ids, reason=reason,
        )

    if plan.failure_mode not in _SUPPORTED_FAILURE_MODES:
        return uncertain("unsupported_failure_mode")
    if plan.semantic_version not in _SUPPORTED_PLAN_VERSIONS:
        return uncertain("unsupported_plan_version")
    if any(check.semantic_version != "1" for check in plan.checks):
        return uncertain("unsupported_check_version")
    required = tuple(
        check for check in plan.checks
        if check.relation == "supports_hypothesis" and check.requirement == "required"
    )
    if not required:
        return uncertain("no_required_support_checks")
    if any(check.relation == "contradicts_hypothesis" for check in plan.checks):
        return uncertain("contradiction_relations_reserved")
    if any(result.verdict == "CONTRADICT" for result in selected.values()):
        return uncertain("explicit_contradiction")
    if any(check.id not in selected for check in required):
        return uncertain("required_results_missing")
    if any(selected[check.id].verdict != "SUPPORT" for check in required):
        return uncertain("required_check_not_supported")
    return PlanVerificationResult(
        plan_id=plan.id, status="SUPPORTED", check_result_ids=result_ids,
        reason="all_required_support_checks_established",
    )
```

## src/aegis_rca/verification/catalog.py

SHA-256: `251bb43f21487eb0630d7ecd713d0b0fd66d40782a71ec811218730af2203811`

```python
"""Explicit connectivity example for future wiring; no workflow integration."""

from ..models.check import (
    CheckScope,
    CheckSpec,
    HypothesisVerificationPlan,
    LegacyEvidenceExists,
    RpcExists,
)
from ..models.incident import TimeWindow


def connectivity_plan(window: TimeWindow) -> HypothesisVerificationPlan:
    """Describe the existing narrow connectivity evidence requirement.

    RPC UNAVAILABLE and explicit connection-establishment log evidence are
    required separately. Correlation, metrics, success and root cause are not
    part of this catalog entry.
    """
    return HypothesisVerificationPlan(
        failure_mode="payment_connection_failure",
        semantic_version="1",
        catalog_key="H-connectivity",
        checks=(
            CheckSpec(
                alias="conn-rpc-unavailable",
                scope=CheckScope(signal="trace", services=("checkout",), window=window),
                predicate=RpcExists(
                    local_service="checkout",
                    span_kind="CLIENT",
                    peer_service="payment",
                    require_resolved_peer=True,
                    require_unambiguous_peer=True,
                    rpc_status_names=("UNAVAILABLE",),
                ),
            ),
            CheckSpec(
                alias="conn-establishment-log",
                scope=CheckScope(signal="log", services=("checkout",), window=window),
                predicate=LegacyEvidenceExists(
                    service="checkout",
                    peer_service="payment",
                    codes=("connection_refused", "dns_resolution_failed"),
                ),
            ),
        ),
    )
```

## tests/test_check_legacy.py

SHA-256: `8f918114b558bae5b66dc22bb0f3a42ab790c8f9b238ab42f875de953bfa6de2`

```python
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
```

## tests/test_check_aggregation.py

SHA-256: `cdccfd55b8656d3af464533823947fcba01dacfb9a22e883cee13e133f9daf29`

```python
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
    return HypothesisVerificationPlan(failure_mode="payment_connection_failure", checks=checks, **updates)


def test_catalog_preserves_exact_connectivity_matchers_and_no_correlation():
    plan = connectivity_plan(_window())
    assert plan.catalog_key == "H-connectivity"
    assert plan.failure_mode == "payment_connection_failure"
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
```

