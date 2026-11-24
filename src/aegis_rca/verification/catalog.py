"""Parameterized dependency connection-establishment failure-mode plan."""

from ..models.check import (
    CheckScope,
    CheckSpec,
    HypothesisVerificationPlan,
    LegacyEvidenceExists,
    RpcExists,
)
from ..models.incident import TimeWindow


def dependency_connectivity_plan(caller: str, callee: str, window: TimeWindow, operation=None) -> HypothesisVerificationPlan:
    """Describe the existing narrow connectivity evidence requirement.

    RPC UNAVAILABLE and explicit connection-establishment log evidence are
    required separately. Correlation, metrics, success and root cause are not
    part of this catalog entry.
    """
    from ..models.hypothesis import HypothesisSpec
    subject = HypothesisSpec(caller=caller, callee=callee, window=window, operation=operation)
    return HypothesisVerificationPlan(
        failure_mode=subject.failure_class,
        semantic_version="1",
        catalog_key=subject.kind,
        checks=(
            CheckSpec(
                alias="conn-rpc-unavailable",
                scope=CheckScope(signal="trace", services=(caller,), window=window),
                predicate=RpcExists(
                    local_service=caller,
                    span_kind="CLIENT",
                    peer_service=callee,
                    operation=operation,
                    require_resolved_peer=True,
                    require_unambiguous_peer=True,
                    rpc_status_names=("UNAVAILABLE",),
                ),
            ),
            CheckSpec(
                alias="conn-establishment-log",
                scope=CheckScope(signal="log", services=(caller,), window=window),
                predicate=LegacyEvidenceExists(
                    service=caller,
                    peer_service=callee,
                    codes=("connection_refused", "dns_resolution_failed"),
                ),
            ),
        ),
    )


def connectivity_plan(window: TimeWindow) -> HypothesisVerificationPlan:
    """Legacy example API only; delegates to the one generic factory."""
    return dependency_connectivity_plan("checkout", "payment", window)
