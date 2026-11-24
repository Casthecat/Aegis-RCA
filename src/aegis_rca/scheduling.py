"""Check-driven requests and authoritative logical acquisition reconciliation."""
from .models.check import RpcExists, LegacyEvidenceExists
from .models.scheduling import (
    TaskIntent, SchedulingNeed, ScheduledAcquisitionRequest, IntentRecord,
    SchedulingLimitation, merge_intent_ledger, merge_needs,
)
from .task_materialization import authorize_task
from .verification.catalog import dependency_connectivity_plan
from .verification.persisted import current_hypothesis_verifications

SIGNAL_ORDER = {"metric": 0, "trace": 1, "log": 2}


def intent_for(task):
    return TaskIntent(signal=task.signal, services=task.services, window=task.window)


def request_for(intent, *, origin, check_ids=(), hypothesis_ids=(), goal="Acquire requested telemetry", selector=None):
    return ScheduledAcquisitionRequest(intent=intent, goal=goal, recorded_selection=selector,
        needs=(SchedulingNeed(intent_id=intent.id, origin=origin,
            requested_for_check_ids=check_ids, requested_for_hypothesis_ids=hypothesis_ids),))


def schedule_key(request):
    value = request.intent
    return (SIGNAL_ORDER[value.signal], value.services, value.window.start, value.window.end, value.id)


def combine_requests(requests):
    combined = {}
    for request in requests:
        old = combined.get(request.intent.id)
        if old is not None:
            if old.recorded_selection != request.recorded_selection:
                raise ValueError("conflicting explicit selections for one TI")
            request = ScheduledAcquisitionRequest(intent=request.intent, goal=min(old.goal, request.goal),
                needs=merge_needs((*old.needs, *request.needs)), recorded_selection=request.recorded_selection)
        combined[request.intent.id] = request
    return sorted(combined.values(), key=schedule_key)


def unrequested(state, requests):
    ledger = {r.id: r for r in state.get("intent_ledger", ())}
    return [r for r in combine_requests(requests) if r.intent.id not in ledger]


def initial_survey(state):
    from .policy import make_task
    from .investigation_scope import current_investigation_scope
    current_investigation_scope(state)
    return unrequested(state, [request_for(intent_for(make_task(state["incident"], signal)),
        origin="initial_survey", goal=make_task(state["incident"], signal).goal) for signal in ("metric", "trace")])


def check_intent(check):
    """Only explicit predicate/scope mappings; metric diagnostics are deferred."""
    if ((isinstance(check.predicate, RpcExists) and check.scope.signal == "trace") or
            (isinstance(check.predicate, LegacyEvidenceExists) and check.scope.signal == "log")):
        return TaskIntent(signal=check.scope.signal, services=check.scope.services, window=check.scope.window)
    return None


def required_requests(state):
    requests, updates, limitations = [], [], []
    for bundle in current_hypothesis_verifications(state):
        h, evaluation = bundle.hypothesis_spec, bundle.evaluation
        plan = dependency_connectivity_plan(h.caller, h.callee, h.window, h.operation)
        more, rows, limits = requests_for_checks(state, h.id, plan.checks, (evaluation.trace_check, evaluation.log_check))
        requests.extend(more)
        updates.extend(rows)
        limitations.extend(limits)
    return combine_requests(requests), merge_intent_ledger([], updates), limitations


def requests_for_checks(state, hypothesis_id, checks, results):
    from .investigation_scope import current_investigation_scope
    current_investigation_scope(state)
    results = {r.check_id: r for r in results}
    ledger = {r.id: r for r in state.get("intent_ledger", ())}
    requests, updates, limitations = [], [], []
    for check in checks:
        result = results.get(check.id)
        if check.requirement != "required":
            continue
        established = result is not None and result.verdict in {"SUPPORT", "CONTRADICT"}
        intent = check_intent(check)
        if intent is None:
            if established:
                continue
            limitations.append(SchedulingLimitation(reason="unsupported_check_acquisition_mapping",
                check_id=check.id, hypothesis_id=hypothesis_id))
            continue
        from .policy import make_task
        request = request_for(intent, origin="required_check", check_ids=(check.id,), hypothesis_ids=(hypothesis_id,),
                              goal=make_task(state["incident"], intent.signal).goal)
        # Scope errors are invariants, not acquisition-unavailable dispositions.
        authorize_task(request.physical_request(), state)
        existing = ledger.get(intent.id)
        if existing is None and not established:
            requests.append(request)
        elif existing is not None:
            updates.append(existing.model_copy(update={"needs": merge_needs((*existing.needs, *request.needs))}))
    return combine_requests(requests), merge_intent_ledger([], updates), limitations


def reconcile_intents(state, *, reservations=()):
    """Single completion owner after A records merge; workers never write ledger."""
    ledger = merge_intent_ledger(state.get("intent_ledger", []), reservations)
    attempts = {a.id: a for a in state.get("attempt_records", ())}
    specs = {t.id: t for t in state.get("task_specs", ())}
    updates = []
    for record in ledger:
        if record.materialized_task_spec_id is None:
            continue
        spec = specs.get(record.materialized_task_spec_id)
        if spec is None or intent_for(spec) != record.intent or spec.recorded_selection != record.recorded_selection:
            raise ValueError("ledger must reference exact logical/physical acquisition semantics")
        from .models.acquisition import AttemptIdentity
        if record.reserved_attempt_id != AttemptIdentity(execution_id=state["execution_id"], task_spec_id=spec.id, ordinal=1).id:
            raise ValueError("ledger reservation must belong to this execution; retries are disabled")
        attempt = attempts.get(record.reserved_attempt_id)
        if attempt is None:
            if record.status != "pending":
                raise ValueError("terminal intent requires its committed AttemptRecord")
            continue
        if attempt.execution_id != state["execution_id"] or attempt.task_spec_id != spec.id:
            raise ValueError("ledger execution provenance mismatch")
        status = "completed" if attempt.tool_status in {"ok", "empty"} else "failed"
        updates.append(IntentRecord.model_validate({**record.model_dump(), "status": status,
            "committed_attempt_id": attempt.id}))
    return merge_intent_ledger(ledger, updates)


def scheduling_report_limits(state):
    limits = []
    for record in state.get("intent_ledger", ()):
        if record.status in {"failed", "unavailable", "ambiguous_replay_selection"}:
            why = record.blocking_reason or "acquisition_failed"
            limits.append(f"{record.intent.signal} acquisition for {', '.join(record.intent.services)}: {why}; no negative evidence is inferred.")
    for value in state.get("scheduling_limitations", ()):
        limits.append(f"Required check {value.check_id}: {value.reason}.")
    return limits
