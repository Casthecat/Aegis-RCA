import asyncio
from time import perf_counter

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Send
from typing_extensions import TypedDict
from pydantic import ValidationError

from .models.evidence import Finding, ToolResult
from .models.hypothesis import Verification
from .models.hypothesis import Hypothesis
from .models.incident import IncidentScope, InvestigationTask
from .models.acquisition import AttemptIdentity, MaterializedTask
from .policy import form_hypotheses, make_task
from .report import audit_and_report
from .runtime import Dependencies, ReservedAttempt, refresh_acquisition_state, finalize_acquisition, validate_execution_state
from .models.acquisition_result import AcquisitionResult, TelemetryPartition
from .tools.projection import project_material
from .verification.persisted import current_hypothesis_verifications
from .task_materialization import authorize_task, check_capability, materialize_task
from .state import IncidentState


class WorkerState(TypedDict):
    attempt_id: str


def triage(state: IncidentState):
    from .service_scope import canonical_incident
    try:
        incident = canonical_incident(state["incident"])
    except (ValueError, TypeError):
        return {"scope": None}
    validate_execution_state({**state, "incident": incident})
    return {"incident": incident, "scope": IncidentScope(incident=incident, assumptions=(
        "Investigate caller-side dependency connection failure modes; ultimate cause remains unresolved.",))}


async def planner(state: IncidentState, runtime: Runtime[Dependencies]):
    from .scheduling import initial_survey, required_requests, request_for, TaskIntent, combine_requests, unrequested
    from .models.scheduling import merge_intent_ledger
    from .task_materialization import UnauthorizedAcquisition
    required, updates, limitations = required_requests(state)
    if not runtime.context.execution_policy.required_check_scheduling:
        required = []
    survey = initial_survey(state)
    tasks = required or survey
    notes = list(state["reasoning_notes"])
    # The initial compatibility survey is fixed; optional LLM work cannot replace it.
    # No initial log. Later required checks always take priority over optional work.
    if not required and runtime.context.reasoner is not None:
        from .reasoning import ReasoningError
        try:
            proposal = await runtime.context.reasoner.propose_plan(
                state["incident"], allowed_services=state["incident"].seed_services,
                allowed_signals=("metric", "trace") if survey else ("metric", "trace", "log"), evidence=tuple(state["evidence"]),
                feedback=state["verification"].reason if state["verification"] else "Optional investigation")
            for item in proposal.tasks:
                if survey and item.signal not in {"metric", "trace"}:
                    continue
                if not set(item.services) <= set(state["incident"].seed_services):
                    continue  # Proposal rejection cannot poison an acquisition ledger.
                request = request_for(TaskIntent(signal=item.signal, services=item.services,
                    window=state["incident"].window), origin="llm", goal=item.goal)
                if survey and request.intent.id not in {r.intent.id for r in survey}:
                    continue  # No per-seed fanout or extra acquisitions in the survey.
                try:
                    authorize_task(request.physical_request(), state)
                except UnauthorizedAcquisition:
                    continue
                tasks.append(request)
            tasks = unrequested(state, tasks)
        except ReasoningError as exc:
            notes.append(f"Planner model unavailable ({exc.code}); deterministic fallback used.")
    return {"pending_tasks": combine_requests(tasks), "reasoning_notes": notes,
            "intent_ledger": merge_intent_ledger(state.get("intent_ledger", []), updates),
            "scheduling_limitations": limitations}


def admit_batch(state: IncidentState, runtime: Runtime[Dependencies]):
    """Only physical reservation owns pending; clipped proposals leave no ledger row."""
    from .models.scheduling import IntentRecord, ScheduledAcquisitionRequest, merge_intent_ledger, merge_needs
    from .scheduling import intent_for, request_for, schedule_key, reconcile_intents
    from .tools.recorded_replay import RecordedReplayBackend, RecordedAcquisitionUnavailable, AmbiguousRecordedAcquisition
    from .task_materialization import UnsupportedAcquisition
    context = runtime.context
    if isinstance(context.backend, RecordedReplayBackend):
        context.backend.validate_execution_namespace(state["execution_id"])
    if any(item.identity.execution_id != state["execution_id"] for item in context.attempts.values()):
        raise ValueError("Runtime reservations belong to another execution")
    requests = []
    selections = {}
    for value in state["pending_tasks"]:
        if isinstance(value, ScheduledAcquisitionRequest):
            request = ScheduledAcquisitionRequest.model_validate(value.model_dump())
            task = request.physical_request()
        else:
            task = value
            request = request_for(intent_for(task), origin="programmatic", goal=task.goal,
                                  selector=getattr(task, "recorded_selection", None))
        authorize_task(task, state)  # Invariant failures never become unavailable.
        if request.intent.id in selections and selections[request.intent.id] != request.recorded_selection:
            raise ValueError("conflicting replay selections for one logical intent")
        selections[request.intent.id] = request.recorded_selection
        requests.append((request, task))
    requests.sort(key=lambda pair: (not any(n.origin == "required_check" for n in pair[0].needs), schedule_key(pair[0])))
    ledger = {r.id: r for r in merge_intent_ledger(state.get("intent_ledger", []), list(context.intent_admissions.values()))}
    charged = {item.id for item in state.get("attempt_records", ())} | set(context.attempts)
    legacy_calls = sum(call.attempt_id is None for call in state["tool_calls"])
    remaining = max(0, state["budget"].max_tool_calls - len(charged) - legacy_calls)
    if state["rounds"] >= state["budget"].max_rounds:
        remaining = 0
    aliases = {call.id for call in state["tool_calls"]} | {item.call_id for item in context.attempts.values()}
    existing = {a.task_spec_id: a.id for a in state.get("attempt_records", ())}
    existing.update({r.spec.id: r.id for r in context.attempts.values()})
    selected, specs, updates = [], [], []
    for request, task in requests:
        old = ledger.get(request.intent.id)
        needs = merge_needs((*old.needs, *request.needs)) if old else request.needs
        if old is not None and not (old.status == "ambiguous_replay_selection" and request.recorded_selection is not None):
            updates.append(old.model_copy(update={"needs": needs}))
            continue
        try:
            check_capability(task, context.backend)
            spec = materialize_task(task, context.backend)
        except (AmbiguousRecordedAcquisition, RecordedAcquisitionUnavailable, UnsupportedAcquisition) as exc:
            if old is not None or request.recorded_selection is not None:
                raise  # Invalid explicit selection cannot poison this logical intent.
            reason = ("replay_ambiguous" if isinstance(exc, AmbiguousRecordedAcquisition) else
                      "replay_unavailable" if isinstance(exc, RecordedAcquisitionUnavailable) else "unsupported_capability")
            record = IntentRecord(intent=request.intent, needs=needs, blocking_reason=reason,
                status="ambiguous_replay_selection" if reason == "replay_ambiguous" else "unavailable")
        else:
            reserved_id = existing.get(spec.id)
            if reserved_id is None:
                if not remaining:
                    continue
                identity = AttemptIdentity(execution_id=state["execution_id"], task_spec_id=spec.id, ordinal=1)
                call_id = task.id if task.id not in aliases else identity.id
                reserved = ReservedAttempt(identity, task, spec, state["budget"].tool_timeout_seconds, call_id)
                record = IntentRecord(intent=request.intent, needs=needs, status="pending",
                    materialized_task_spec_id=spec.id, reserved_attempt_id=identity.id, recorded_selection=spec.recorded_selection)
                # Validate the bounded ledger before mutating the reservation journal.
                merge_intent_ledger(list(ledger.values()), [record])
                context.attempts[identity.id] = reserved
                aliases.add(call_id)
                existing[spec.id] = identity.id
                selected.append(task)
                specs.append(spec)
                remaining -= 1
            else:
                # Independent T deduplication: only the exactly materialized query is reused.
                record = IntentRecord(intent=request.intent, needs=needs, status="pending",
                    materialized_task_spec_id=spec.id, reserved_attempt_id=reserved_id, recorded_selection=spec.recorded_selection)
        ledger = {r.id: r for r in merge_intent_ledger(list(ledger.values()), [record])}
        updates.append(record)
        context.intent_admissions[record.id] = ledger[record.id]
    merged = merge_intent_ledger(list(ledger.values()), updates)
    from .state import merge_records
    merged = reconcile_intents({**state, "task_specs": merge_records(state["task_specs"], specs), "intent_ledger": merged})
    return {"pending_tasks": selected, "task_specs": specs, "intent_ledger": merged}


def dispatch(state: IncidentState, runtime: Runtime[Dependencies]):
    if not state["pending_tasks"]:
        return "merge"
    return [Send("worker", WorkerState(attempt_id=next(key for key, item in runtime.context.attempts.items()
                                                     if item.task == task and key not in runtime.context.started_attempts)))
            for task in state["pending_tasks"]]


async def worker(state: WorkerState, runtime: Runtime[Dependencies]):
    reserved = runtime.context.attempts[state["attempt_id"]]
    task = reserved.task
    if reserved.id in runtime.context.started_attempts:
        raise ValueError("Attempt dispatch cannot execute twice; retries are disabled")
    runtime.context.started_attempts.add(reserved.id)
    original_result, error_class = False, "none"
    try:
        request = MaterializedTask(**{**task.model_dump(include=set(InvestigationTask.model_fields)), "services": reserved.spec.services,
                                      "window": reserved.spec.window}, task_spec=reserved.spec)
        acquire = getattr(runtime.context.backend, "acquire", None)
        expires = perf_counter() + reserved.timeout_seconds
        # Keep immediate backends inline, as positive-timeout wait_for does on
        # Python 3.12. The context records expiry even if a backend suppresses
        # cancellation, including coarse Windows event-loop timer ticks.
        async with asyncio.timeout(reserved.timeout_seconds) as deadline:
            raw = await (acquire(request) if acquire else runtime.context.backend.query(request))
            completed_at = perf_counter()
        if deadline.expired() or completed_at >= expires:
            raise TimeoutError
        if acquire:
            acquisition = AcquisitionResult.model_validate_json(raw.model_dump_json()) if isinstance(raw, AcquisitionResult) else AcquisitionResult.model_validate(raw)
            from .fixtures import FixtureBackend
            if (acquisition.projection_origin == "legacy" or
                    acquisition.projection_origin == "fixture" and not isinstance(runtime.context.backend, FixtureBackend)):
                raise InvalidAcquisition("material-aware backend must preserve acquisition coherence")
        else:
            acquisition = AcquisitionResult(signal=task.signal, projection=ToolResult.model_validate(raw), projection_origin="legacy")
        if acquisition.signal != task.signal:
            raise InvalidAcquisition("acquisition signal differs from request")
        selection, reference = reserved.spec.recorded_selection, acquisition.recorded_from
        if acquisition.material is not None and selection is not None and reference is None:
            raise InvalidAcquisition("recorded replay requires source lineage")
        if reference is not None:
            if selection is None or any(getattr(reference, k) != v for k, v in selection.model_dump().items()):
                raise InvalidAcquisition("source lineage differs from recorded selection")
            part = TelemetryPartition(attempt_id=reserved.id, task_spec_id=reserved.spec.id,
                signal=reserved.spec.signal, material=acquisition.material, source=acquisition.source)
            if part.content_digest != reference.recorded_content_digest:
                raise InvalidAcquisition("recorded lineage content differs from returned material")
        if acquisition.projection_origin == "normalized" and acquisition.material is not None:
            if acquisition.projection != project_material(request, acquisition.material, acquisition.source):
                raise InvalidAcquisition("projection differs from this acquisition material")
        original_result = True
        error_class = {"error": "backend_error", "timeout": "tool_timeout"}.get(acquisition.projection.status, "none")
    except TimeoutError:
        error_class = "tool_timeout"
        acquisition = AcquisitionResult(signal=task.signal,
            projection=ToolResult(status="timeout", warnings=("Backend query timed out.",)))
    except Exception as exc:
        error_class = "invalid_result" if isinstance(exc, (ValidationError, InvalidAcquisition)) else "backend_error"
        acquisition = AcquisitionResult(signal=task.signal,
            projection=ToolResult(status="error", warnings=(f"Backend failed: {type(exc).__name__}",)))
    completion = finalize_acquisition(reserved, runtime.context.backend, acquisition,
                                     error_class=error_class, original_result=original_result)
    # There is no await or second authoritative write between completion and raw
    # partition publication. A late worker cannot overwrite a terminal timeout.
    committed = runtime.context.acquisitions.commit(completion)
    return committed.state_update()


class InvalidAcquisition(ValueError):
    pass



def merge_batch(state: IncidentState):
    return {"rounds": state["rounds"] + bool(state["pending_tasks"]), "pending_tasks": []}


def persist_verification(state: IncidentState, runtime: Runtime[Dependencies]):
    return refresh_acquisition_state(state, runtime.context)


def investigate_graph():
    graph = StateGraph(IncidentState, context_schema=Dependencies)
    graph.add_node("admit", admit_batch)
    graph.add_node("worker", worker)
    graph.add_node("merge", merge_batch)
    graph.add_node("persist_verification", persist_verification)
    graph.add_edge(START, "admit")
    graph.add_conditional_edges("admit", dispatch, ["worker", "merge"])
    graph.add_edge("worker", "merge")
    graph.add_edge("merge", "persist_verification")
    graph.add_edge("persist_verification", END)
    return graph.compile()


async def hypothesize(state: IncidentState, runtime: Runtime[Dependencies]):
    deterministic = form_hypotheses(state)
    if runtime.context.reasoner is not None:
        from .reasoning import ReasoningError
        try:
            proposal = await runtime.context.reasoner.propose_hypotheses(
                state["incident"], tuple(state["evidence"]), allowed_services=state["incident"].seed_services)
            ids = {h.id for h in deterministic}
            provisional = [Hypothesis(**h.model_dump(exclude={"status"}), status="uncertain")
                           for h in proposal.hypotheses if h.id not in ids][:3]
            return {"hypotheses": deterministic, "provisional_hypotheses": provisional}
        except ReasoningError as exc:
            return {"hypotheses": deterministic, "provisional_hypotheses": [], "reasoning_notes": [
                *state["reasoning_notes"], f"Hypothesis model unavailable ({exc.code}); deterministic fallback used."]}
    return {"hypotheses": deterministic, "provisional_hypotheses": []}


def verify(state: IncidentState, runtime: Runtime[Dependencies]):
    from .scheduling import required_requests
    bundles = current_hypothesis_verifications(state)
    requests, updates, limitations = required_requests(state)
    unique = bundles[0].hypothesis_spec.id if len(bundles) == 1 else None
    common = {"intent_ledger": updates, "scheduling_limitations": limitations}
    exhausted = (state["rounds"] >= state["budget"].max_rounds or len(state["tool_calls"]) >= state["budget"].max_tool_calls)
    if not runtime.context.execution_policy.required_check_scheduling:
        return {**common, "verification": Verification(
            decision="finalize" if any(b.evaluation.plan_result.status == "SUPPORTED" for b in bundles) else "inconclusive",
            leading_hypothesis_id=unique, reason="required_check_scheduling_disabled")}
    if requests and not exhausted:
        return {**common, "verification": Verification(decision="targeted_followup", leading_hypothesis_id=unique,
            reason="need_explicit_connection_evidence"), "pending_tasks": requests}
    if exhausted:
        return {**common, "verification": Verification(decision="stop_budget", leading_hypothesis_id=unique, reason="budget_exhausted")}
    if any(b.evaluation.plan_result.status == "SUPPORTED" for b in bundles):
        return {**common, "verification": Verification(decision="finalize", leading_hypothesis_id=unique,
            reason="observed_failure_mode_supported")}
    return {**common, "verification": Verification(decision="inconclusive", leading_hypothesis_id=unique,
        reason="no_admissible_new_intent")}


def after_plan(state: IncidentState):
    if (state["pending_tasks"] and state["rounds"] < state["budget"].max_rounds
            and len(state["tool_calls"]) < state["budget"].max_tool_calls):
        return "investigate"
    return "audit_report"


def after_verify(state: IncidentState):
    return {"targeted_followup": "investigate", "replan": "planner"}.get(
        state["verification"].decision, "audit_report")


def finalize(state: IncidentState, runtime: Runtime[Dependencies]):
    if state["verification"] is None and state["scope"] is not None:
        exhausted = (len(state["tool_calls"]) >= state["budget"].max_tool_calls or
                     state["rounds"] >= state["budget"].max_rounds)
        state = {**state, "verification": Verification(
            decision="stop_budget" if exhausted else "inconclusive",
            reason="budget_exhausted" if exhausted else "no_useful_untried_checks")}
    return {"final_report": audit_and_report(state)}


def build_graph():
    """Six top-level nodes; compiled subgraph implements bounded parallel tools."""
    graph = StateGraph(IncidentState, context_schema=Dependencies)
    graph.add_node("triage", triage)
    graph.add_node("planner", planner)
    graph.add_node("investigate", investigate_graph())
    graph.add_node("hypothesize", hypothesize)
    graph.add_node("verify", verify)
    graph.add_node("audit_report", finalize)
    graph.add_edge(START, "triage")
    graph.add_conditional_edges("triage", lambda s: "planner" if s["scope"] else "audit_report",
                                ["planner", "audit_report"])
    graph.add_conditional_edges("planner", after_plan, ["investigate", "audit_report"])
    graph.add_edge("investigate", "hypothesize")
    graph.add_edge("hypothesize", "verify")
    graph.add_conditional_edges("verify", after_verify, ["investigate", "planner", "audit_report"])
    graph.add_edge("audit_report", END)
    return graph.compile()
