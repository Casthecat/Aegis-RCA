import asyncio
import math
from time import monotonic

from .graph import build_graph
from .models.evidence import ToolResult
from .models.acquisition_result import AcquisitionResult
from .models.hypothesis import Verification
from .report import audit_and_report
from .runtime import Dependencies, refresh_acquisition_state, finalize_acquisition
from .state import IncidentState, merge_records


async def run_investigation(state: IncidentState, dependencies: Dependencies, *, deadline_seconds=120):
    from .tools.recorded_replay import RecordedReplayBackend
    if isinstance(dependencies.backend, RecordedReplayBackend):
        dependencies.backend.validate_execution_namespace(state["execution_id"])
    if not math.isfinite(deadline_seconds) or not 0 < deadline_seconds <= 3600:
        raise ValueError("deadline_seconds must be finite and between 0 and 3600")
    if dependencies.attempts:
        raise ValueError("Create fresh runtime dependencies for each incident")
    started = monotonic()
    events = []
    try:
        async with asyncio.timeout(deadline_seconds):
            async for mode, payload in build_graph().astream(
                state, context=dependencies, config={"recursion_limit": 64},
                stream_mode=["values", "updates"],
            ):
                if mode == "values":
                    state = payload
                else:
                    events.append({"nodes": list(payload), "elapsed_ms": round((monotonic() - started) * 1000)})
    except TimeoutError:
        state = dict(state)
        # Parallel subgraph writes may not have reached the parent's checkpoint.
        for update in dependencies.completions.values():
            for field in ("evidence", "findings", "tool_calls", "attempt_records"):
                state[field] = merge_records(state[field], update[field])
        completed = dependencies.acquisitions.ids()
        timed_out, attempt_records = [], []
        for key in sorted(set(dependencies.attempts) - completed):
            reserved = dependencies.attempts[key]
            acquisition = AcquisitionResult(signal=reserved.spec.signal,
                projection=ToolResult(status="timeout", warnings=("Global investigation deadline reached.",)))
            completion = finalize_acquisition(reserved, dependencies.backend, acquisition,
                error_class="global_deadline", original_result=False)
            committed = dependencies.acquisitions.commit(completion)
            timed_out.append(committed.tool_call)
            attempt_records.append(committed.attempt)
        specs = [item.spec for item in dependencies.attempts.values()]
        state = {**state, "tool_calls": merge_records(state["tool_calls"], timed_out),
                 "attempt_records": merge_records(state["attempt_records"], attempt_records),
                 "task_specs": merge_records(state["task_specs"], specs),
                 "verification": Verification(decision="stop_budget", reason="deadline_exhausted")}
        from .models.scheduling import merge_intent_ledger
        state["intent_ledger"] = merge_intent_ledger(state.get("intent_ledger", []), list(dependencies.intent_admissions.values()))
        state.update(refresh_acquisition_state(state, dependencies))
        state["final_report"] = audit_and_report(state)
        events.append({"nodes": ["deadline_stop"], "elapsed_ms": round((monotonic() - started) * 1000)})
    return state, events
