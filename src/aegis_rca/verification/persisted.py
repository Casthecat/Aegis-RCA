"""Current verification contracts and state-only freshness; no backend access."""

from typing import Literal

from pydantic import Field, field_validator, model_validator

from ..models.acquisition import AttemptRecord, TaskSpec
from ..models.check import CheckCoverageView
from ..models.identity import structural_id
from ..models.incident import Contract, TimeWindow
from ..models.hypothesis import HypothesisSpec
from ._shared import semantic_payload
from .catalog import dependency_connectivity_plan
from .connectivity import ConnectivityVerification


class SemanticReference(Contract):
    identifier: str
    fingerprint: str = Field(pattern=r"^sha256-[0-9a-f]{64}$")


class StateVerificationBasis(Contract):
    schema_version: Literal[1] = 1
    plan_id: str
    window: TimeWindow
    task_spec_ids: tuple[str, ...] = ()
    attempts: tuple[SemanticReference, ...] = ()
    log_evidence: tuple[SemanticReference, ...] = ()

    @field_validator("task_spec_ids")
    @classmethod
    def canonical_ids(cls, values):
        return tuple(sorted(set(values)))

    @field_validator("attempts", "log_evidence")
    @classmethod
    def canonical_references(cls, values):
        return tuple(sorted(set(values), key=lambda item: (item.identifier, item.fingerprint)))

    @property
    def id(self):
        return structural_id("VBS-", self.model_dump())


BindingOutcome = Literal[
    "verified", "source_unavailable", "source_digest_mismatch",
    "observation_id_mismatch", "evidence_missing", "binding_unknown",
]


class AttemptBindingResolution(Contract):
    outcome: BindingOutcome
    coverage: CheckCoverageView

    @model_validator(mode="after")
    def binding_consistency(self):
        view = self.coverage
        if view.signal not in {"trace", "log"}:
            raise ValueError("connectivity resolution requires trace or log")
        expected = "observation" if view.signal == "trace" else "legacy_evidence"
        prefix = "O-trace-" if view.signal == "trace" else "E-"
        if view.input_kind != expected or any(not key.startswith(prefix) for key in view.admitted_ids):
            raise ValueError("binding input origin must agree")
        if (view.fact_set_complete != (self.outcome == "verified")
                or (self.outcome != "verified" and view.admitted_ids)):
            raise ValueError("only verified binding may supply facts and certify its fact set")
        if view.pre_capture_omissions != "unknown":
            raise ValueError("current attempts cannot certify acquisition omissions")
        if self.outcome == "verified" and view.query_status != "succeeded":
            raise ValueError("failed attempts cannot supply facts")
        if (view.signal == "trace" and self.outcome == "evidence_missing"
                or view.signal == "log" and self.outcome.startswith("source_")
                or view.signal == "log" and self.outcome == "observation_id_mismatch"):
            raise ValueError("binding outcome must match its input kind")
        return self


class ResolvedEvaluationBasis(Contract):
    schema_version: Literal[1] = 1
    observation_ids: tuple[str, ...] = ()
    log_evidence: tuple[SemanticReference, ...] = ()
    resolutions: tuple[AttemptBindingResolution, ...] = ()

    @field_validator("observation_ids")
    @classmethod
    def canonical_ids(cls, values):
        return tuple(sorted(set(values)))

    @field_validator("log_evidence")
    @classmethod
    def canonical_references(cls, values):
        return tuple(sorted(set(values), key=lambda item: (item.identifier, item.fingerprint)))

    @field_validator("resolutions")
    @classmethod
    def unique_attempts(cls, values):
        if len({item.coverage.acquisition_id for item in values}) != len(values):
            raise ValueError("one binding resolution per attempt is required")
        return tuple(sorted(values, key=lambda item: item.coverage.acquisition_id))

    @model_validator(mode="after")
    def exact_supplied_sets(self):
        native, legacy = set(), set()
        for item in self.resolutions:
            target = native if item.coverage.input_kind == "observation" else legacy
            target.update(item.coverage.admitted_ids)
        if native != set(self.observation_ids) or legacy != {item.identifier for item in self.log_evidence}:
            raise ValueError("resolved input IDs must equal the supplied binding union")
        return self

    @property
    def id(self):
        payload = self.model_dump()
        payload["resolutions"] = [{"outcome": item.outcome,
                                   "coverage": item.coverage.identity_payload()} for item in self.resolutions]
        return structural_id("VBE-", payload)


class PersistedHypothesisVerification(Contract):
    schema_version: Literal[1] = 1
    state_basis: StateVerificationBasis
    evaluation_basis: ResolvedEvaluationBasis
    evaluation: ConnectivityVerification
    hypothesis_spec: HypothesisSpec

    @model_validator(mode="after")
    def intrinsic_consistency(self):
        h = self.hypothesis_spec
        if h.window != self.state_basis.window:
            raise ValueError("hypothesis and check windows must agree")
        plan = dependency_connectivity_plan(h.caller, h.callee, h.window, h.operation)
        checks = {item.scope.signal: item for item in plan.checks}
        trace, log, result = self.evaluation.trace_check, self.evaluation.log_check, self.evaluation.plan_result
        if self.state_basis.plan_id != plan.id or result.plan_id != plan.id:
            raise ValueError("persisted plan IDs must agree structurally")
        if set(result.check_result_ids) != {trace.id, log.id}:
            raise ValueError("plan must refer to its own current check results")
        basis = self.evaluation_basis
        views = {structural_id("", item.coverage.identity_payload()) for item in basis.resolutions}
        for signal, check, supplied, origin in (
            ("trace", trace, set(basis.observation_ids), "observation"),
            ("log", log, {item.identifier for item in basis.log_evidence}, "legacy_evidence"),
        ):
            if check.check_id != checks[signal].id or check.input_kind != origin:
                raise ValueError("persisted check identity/origin must match catalog")
            if not set(check.relevant_ids) <= supplied:
                raise ValueError("check input references must belong to resolved inputs")
            if any(structural_id("", view.identity_payload()) not in views for view in check.coverage_used):
                raise ValueError("check coverage must belong to resolved inputs")
            if check.verdict == "SUPPORT" and not check.matched_ids:
                raise ValueError("positive result must reference supplied matches")
        supported = trace.verdict == log.verdict == "SUPPORT"
        expected_reason = ("all_required_support_checks_established" if supported else
                           "explicit_contradiction" if "CONTRADICT" in {trace.verdict, log.verdict} else
                           "required_check_not_supported")
        if result.status != ("SUPPORTED" if supported else "UNCERTAIN") or result.reason != expected_reason:
            raise ValueError("plan result must agree with its contained checks")
        return self


def relevant_attempts(state, subject):
    """Select by authoritative signal/service/overlapping half-open scope, never alias."""
    plan = dependency_connectivity_plan(subject.caller, subject.callee, subject.window, subject.operation)
    scopes = {check.scope.signal: check.scope for check in plan.checks}
    specs = {spec.id: spec for spec in state.get("task_specs", ())}
    selected = []
    for attempt in state.get("attempt_records", ()):
        spec = specs.get(attempt.task_spec_id)
        if spec is None:
            raise ValueError("attempt must reference a saved TaskSpec")
        scope = scopes.get(spec.signal)
        if (scope is not None and set(scope.services) & set(spec.services)
                and spec.window.start < scope.window.end and scope.window.start < spec.window.end):
            if attempt.execution_id != state["execution_id"]:
                raise ValueError("relevant attempt belongs to another execution")
            selected.append((attempt, spec))
    return tuple(sorted(selected, key=lambda pair: pair[0].id))


def evidence_references(records):
    return tuple(sorted({SemanticReference(identifier=record.id,
        fingerprint=structural_id("sha256-", semantic_payload(record))) for record in records},
        key=lambda item: (item.identifier, item.fingerprint)))


def state_verification_basis(state, subject) -> StateVerificationBasis:
    window = state["incident"].window
    selected = relevant_attempts(state, subject)
    fields = {"task_spec_id", "tool_status", "truncated", "coverage_complete", "pre_capture_omissions",
              "observation_binding", "evidence_binding", "source_material_digest", "tool_result_digest",
              "admitted_observation_ids", "admitted_evidence_ids"}
    bound_log_ids = {identifier for attempt, spec in selected if spec.signal == "log"
                     for identifier in attempt.admitted_evidence_ids}
    logs = tuple(e for e in state["evidence"] if e.id in bound_log_ids or (
        e.signal == "log" and e.service == subject.caller and e.observation.peer_service == subject.callee
        and e.observation.code in {"connection_refused", "dns_resolution_failed"}
        and window.start <= e.timestamp < window.end))
    return StateVerificationBasis(plan_id=dependency_connectivity_plan(subject.caller, subject.callee, window, subject.operation).id, window=window,
        task_spec_ids=tuple(sorted({spec.id for _, spec in selected})),
        attempts=tuple(SemanticReference(identifier=attempt.id,
            fingerprint=structural_id("sha256-", attempt.model_dump(include=fields))) for attempt, _ in selected),
        log_evidence=evidence_references(logs))


def attempt_coverage(attempt: AttemptRecord, spec: TaskSpec, *, admitted_ids=(), verified=False):
    return CheckCoverageView(signal=spec.signal, services=spec.services, window=spec.window,
        input_kind="observation" if spec.signal == "trace" else "legacy_evidence",
        acquisition_id=attempt.id, query_identity=spec.id,
        query_status="succeeded" if attempt.tool_status in {"ok", "empty"} else "failed",
        truncated=attempt.truncated, coverage_complete=attempt.coverage_complete,
        pre_capture_omissions=attempt.pre_capture_omissions,
        admitted_ids=tuple(admitted_ids), fact_set_complete=verified)


def validate_hypothesis_verification(state, bundle):
    """No backend or evaluator: absent is conservative, stale is an invariant failure."""
    # Revalidate even model_copy/model_construct instances at this trust boundary.
    bundle = PersistedHypothesisVerification.model_validate(bundle.model_dump())
    from ..investigation_scope import current_investigation_scope
    scope = current_investigation_scope(state)
    h = bundle.hypothesis_spec
    allowed = scope.admitted_services if scope is not None else state["incident"].seed_services
    if h.caller not in state["incident"].seed_services or h.callee not in allowed or h.window != state["incident"].window:
        raise ValueError("stale hypothesis permission/window")
    if state_verification_basis(state, h).id != bundle.state_basis.id:
        raise ValueError("stale connectivity verification")
    selected = {attempt.id: (attempt, spec) for attempt, spec in relevant_attempts(state, h)}
    resolutions = bundle.evaluation_basis.resolutions
    if {item.coverage.acquisition_id for item in resolutions} != set(selected):
        raise ValueError("resolution attempts must equal relevant state attempts")
    for item in resolutions:
        attempt, spec = selected[item.coverage.acquisition_id]
        verified = item.outcome == "verified"
        expected = attempt_coverage(attempt, spec, admitted_ids=item.coverage.admitted_ids, verified=verified)
        if item.coverage.identity_payload() != expected.identity_payload():
            raise ValueError("resolved coverage differs from authoritative attempt")
        if verified:
            bound = attempt.observation_binding if spec.signal == "trace" else attempt.evidence_binding
            historical = attempt.admitted_observation_ids if spec.signal == "trace" else attempt.admitted_evidence_ids
            if bound != "bound" or set(item.coverage.admitted_ids) != set(historical):
                raise ValueError("verified resolution must equal historical binding")
    if not set(bundle.evaluation_basis.log_evidence) <= set(evidence_references(state["evidence"])):
        raise ValueError("resolved evidence differs from authoritative state")
    return bundle


# Import compatibility only: the class is the same PHV contract, never a second store.
PersistedConnectivityVerification = PersistedHypothesisVerification


def current_hypothesis_verifications(state):
    from ..portfolio import current_hypothesis_portfolio
    portfolio = current_hypothesis_portfolio(state)
    bundles = tuple(state.get("hypothesis_verifications", ()))
    expected = tuple(h.id for h in portfolio.active) if portfolio else ()
    if tuple(b.hypothesis_spec.id for b in bundles) != expected:
        raise ValueError("current PHV set/order must exactly equal active portfolio")
    return tuple(validate_hypothesis_verification(state, b) for b in bundles)


def current_connectivity_verification(state, subject=None, *, required=False):
    """Derived compatibility projection only; multi-HS never chooses a winner."""
    bundles = current_hypothesis_verifications(state)
    if subject is None:
        bundle = bundles[0] if len(bundles) == 1 else None
    else:
        key = subject if isinstance(subject, str) else subject.id
        bundle = next((b for b in bundles if b.hypothesis_spec.id == key), None)
    if bundle is None and required:
        raise ValueError("current connectivity verification requires an unambiguous active subject")
    return bundle.evaluation if bundle else None
