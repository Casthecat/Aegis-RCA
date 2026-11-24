"""Fact assessment and consumer-scoped acquisition limitations, without re-evaluation."""
from .models.hypothesis import HypothesisAssessment, AssessmentLimitation
from .verification.persisted import current_hypothesis_verifications
from .verification.catalog import dependency_connectivity_plan
from .scheduling import check_intent
from .task_materialization import authorize_task


def current_assessments(state):
    values = []
    ledger = {r.id: r for r in state.get("intent_ledger", ())}
    budget_stop = bool(state.get("verification") and state["verification"].decision == "stop_budget"
                       and state["verification"].reason == "budget_exhausted")
    for bundle in current_hypothesis_verifications(state):
        h, evaluation = bundle.hypothesis_spec, bundle.evaluation
        plan = dependency_connectivity_plan(h.caller, h.callee, h.window, h.operation)
        results = {r.check_id: r for r in (evaluation.trace_check, evaluation.log_check)}
        status = ("SUPPORTED" if evaluation.plan_result.status == "SUPPORTED" else
                  "CONTRADICTED" if evaluation.plan_result.reason == "explicit_contradiction"
                  and any(r.verdict == "CONTRADICT" for r in results.values()) else "UNRESOLVED")
        missing, limits = [], []
        for check in plan.checks:
            if check.requirement != "required" or results[check.id].verdict in {"SUPPORT", "CONTRADICT"}:
                continue
            missing.append(check.id)
            intent = check_intent(check)
            if intent is None:
                if any(l.check_id == check.id and l.hypothesis_id == h.id for l in state.get("scheduling_limitations", ())):
                    limits.append(AssessmentLimitation(reason="unsupported_check_acquisition_mapping", check_id=check.id))
                continue
            row = ledger.get(intent.id)
            consumed = row and any(h.id in n.requested_for_hypothesis_ids or check.id in n.requested_for_check_ids for n in row.needs)
            if consumed:
                reason = {"failed": "required_acquisition_failed", "unavailable": "required_acquisition_unavailable",
                          "ambiguous_replay_selection": "replay_selection_ambiguous"}.get(row.status)
                if reason:
                    limits.append(AssessmentLimitation(reason=reason, check_id=check.id, intent_id=intent.id))
            elif row is None and budget_stop:
                from .scheduling import request_for
                authorize_task(request_for(intent, origin="required_check").physical_request(), state)
                limits.append(AssessmentLimitation(reason="budget_prevented_required_acquisition", check_id=check.id, intent_id=intent.id))
        values.append(HypothesisAssessment(hypothesis_spec=h, status=status, unestablished_check_ids=tuple(missing),
            limitations=tuple(limits), next_question=f"Why the {h.caller} to {h.callee} dependency was unreachable remains unresolved."))
    return tuple(values)
