"""Canonical, pure connectivity evaluation shared by orchestration and reporting."""

from ..models.check import CheckCoverageView, CheckResult, LegacyEvidenceExists, PlanVerificationResult, RpcExists
from ..models.evidence import Evidence
from ..models.incident import Contract, TimeWindow
from ..models.observation import ObservationRecord
from .aggregate import aggregate_plan
from .catalog import dependency_connectivity_plan
from .evaluate import evaluate_check
from .legacy_bridge import evaluate_legacy_check


class ConnectivityVerification(Contract):
    trace_check: CheckResult
    log_check: CheckResult
    plan_result: PlanVerificationResult


def evaluate_connectivity(
    window: TimeWindow, *, trace_observations: tuple[ObservationRecord, ...] = (),
    legacy_log_evidence: tuple[Evidence, ...] = (),
    trace_coverage: tuple[CheckCoverageView, ...] = (),
    log_coverage: tuple[CheckCoverageView, ...] = (),
    caller: str = "checkout", callee: str = "payment", operation: str | None = None,
) -> ConnectivityVerification:
    """Evaluate positive existence only; no acquisition or coverage is fabricated."""
    plan = dependency_connectivity_plan(caller, callee, window, operation)
    trace_spec = next(check for check in plan.checks if isinstance(check.predicate, RpcExists))
    log_spec = next(check for check in plan.checks if isinstance(check.predicate, LegacyEvidenceExists))
    trace = evaluate_check(trace_spec, observations=trace_observations, coverage=trace_coverage)
    log = evaluate_legacy_check(log_spec, evidence=legacy_log_evidence, coverage=log_coverage)
    return ConnectivityVerification(trace_check=trace, log_check=log,
                                    plan_result=aggregate_plan(plan, results=(trace, log)))
