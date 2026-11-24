"""One deterministic dependency failure-mode path; no ultimate cause inference."""
from .models.evidence import Evidence
from .models.hypothesis import Hypothesis
from .models.incident import IncidentInput, InvestigationTask, Signal
from .verification.persisted import current_hypothesis_verifications


def make_task(incident: IncidentInput, signal: Signal) -> InvestigationTask:
    goals = {
        "metric": "Inspect background symptoms in the requested window",
        "trace": "Inspect original-seed client RPC failures and their peers",
        "log": "Distinguish connection establishment failures from application errors",
        "knowledge": "Find missing verification guidance",
    }
    return InvestigationTask(id=f"query-{signal}", signal=signal, goal=goals[signal],
                             services=incident.seed_services, window=incident.window)


def select_evidence_citations(evidence: list[Evidence], code: str, signal: str, *, caller, callee, window=None) -> tuple[str, ...]:
    """Select compatibility artifacts, never determine diagnostic truth."""
    return tuple(e.id for e in evidence if e.signal == signal and e.service == caller
                 and e.observation.code == code and e.observation.peer_service == callee
                 and (window is None or window.start <= e.timestamp < window.end))


def connectivity_citations(evidence: list[Evidence], subject) -> tuple[str, ...]:
    args = dict(caller=subject.caller, callee=subject.callee, window=subject.window)
    return (*select_evidence_citations(evidence, "rpc_unavailable", "trace", **args),
            *select_evidence_citations(evidence, "connection_refused", "log", **args),
            *select_evidence_citations(evidence, "dns_resolution_failed", "log", **args))


def form_hypotheses(state) -> list[Hypothesis]:
    projections = []
    for bundle in current_hypothesis_verifications(state):
        subject, evaluation = bundle.hypothesis_spec, bundle.evaluation
        supported = evaluation.plan_result.status == "SUPPORTED"
        projections.append(Hypothesis(id=subject.id, affected_service=subject.caller,
            failure_mode=subject.failure_class,
            claim=f"{subject.caller} could not establish its {subject.callee} dependency.",
            supporting_evidence_ids=connectivity_citations(state["evidence"], subject),
            missing_checks=() if supported else (f"Obtain caller-side connection establishment logs for {subject.caller} toward {subject.callee}.",),
            status="supported" if supported else "uncertain"))
    return projections
