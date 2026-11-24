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
_SUPPORTED_FAILURE_MODES = frozenset({"rpc_connection_establishment"})
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
