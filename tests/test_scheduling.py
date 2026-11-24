from portfolio_helpers import single_phv, subject
"""P1.7b: logical identity, admission ownership and no-retry check scheduling."""
import asyncio
from datetime import timedelta, timezone
from dataclasses import replace

import pytest
from langgraph.runtime import Runtime
from pydantic import ValidationError, TypeAdapter

from aegis_rca import graph
from aegis_rca.fixtures import FixtureBackend, demo_incident, demo_traces
from aegis_rca.models.incident import Budget, TimeWindow
from aegis_rca.models.scheduling import (
    TaskIntent, SchedulingNeed, ScheduledAcquisitionRequest, IntentRecord,
    SchedulingLimitation, merge_intent_ledger, merge_needs, transition, MAX_INTENTS,
)
from aegis_rca.models.evidence import ToolResult
from aegis_rca.models.telemetry import TraceResult, MetricResult, LogResult
from aegis_rca.policy import make_task
from aegis_rca.runtime import Dependencies, refresh_acquisition_state
from aegis_rca.runner import run_investigation
from aegis_rca.state import initial_state, merge_records, IncidentState
from aegis_rca.scheduling import (
    intent_for, request_for, required_requests, requests_for_checks, initial_survey,
    combine_requests, check_intent, reconcile_intents,
)
from aegis_rca.task_materialization import materialize_task, UnauthorizedAcquisition
from aegis_rca.tools.recorded_replay import RecordedReplayBackend
from aegis_rca.tools.snapshot_v2 import export_snapshot_v2
from aegis_rca.verification.catalog import dependency_connectivity_plan

from test_acquisition_partitions import execute, live
from test_recorded_replay import with_second_attempt
from test_dependency_hypotheses import run as generic_run, span, backend as generic_backend, incident as generic_incident


def intent(signal="trace", services=("checkout",), window=None):
    return TaskIntent(signal=signal, services=services, window=window or demo_incident().window)


def request(signal="trace", **kwargs):
    return request_for(intent(signal), origin="required_check", **kwargs)


def admit(state, deps, requests):
    update = graph.admit_batch({**state, "pending_tasks": requests}, Runtime(context=deps))
    return {**state, **update, "task_specs": merge_records(state["task_specs"], update["task_specs"]),
            "intent_ledger": merge_intent_ledger(state.get("intent_ledger", []), update["intent_ledger"])}


@pytest.mark.parametrize("field", ["goal", "purpose", "priority", "origin", "check_id", "hypothesis_id",
    "backend_kind", "task_spec_id", "attempt_id", "recorded_selection", "alias"])
def test_ti_contract_rejects_non_acquisition_identity_fields(field):
    value = intent()
    with pytest.raises(ValidationError):
        TaskIntent.model_validate({**value.model_dump(), field: "consumer or transport"})


def test_ti_canonical_time_services_and_semantic_changes():
    value = intent(services=("frontend", "catalog", "frontend"))
    shifted = TimeWindow(start=value.window.start.astimezone(timezone(timedelta(hours=8))),
                         end=value.window.end.astimezone(timezone(timedelta(hours=8))))
    assert value == intent(services=("catalog", "frontend"), window=shifted)
    assert value.id == intent(services=("frontend", "catalog"), window=shifted).id
    assert value.id != intent(services=("frontend",)).id
    assert value.id != intent("log", services=value.services).id
    assert value.id != intent(services=value.services, window=TimeWindow(
        start=value.window.start + timedelta(seconds=1), end=value.window.end)).id
    assert TaskIntent.model_validate_json(value.model_dump_json()) == value
    with pytest.raises(ValidationError):
        value.signal = "log"


@pytest.mark.parametrize("changes", [{"services": ()}, {"services": (" ",)}, {"services": ("x"*257,)},
    {"services": tuple(f"s{i}" for i in range(65))}, {"signal": "knowledge"}, {"schema_version": 2}])
def test_intent_contract_rejects_invalid_boundaries(changes):
    with pytest.raises(ValidationError):
        TaskIntent.model_validate({**intent().model_dump(), **changes})


def test_consumer_needs_converge_without_affecting_ti_or_t_a():
    a = request("log", check_ids=("C-b", "C-a", "C-a"), hypothesis_ids=("HS-catalog",), goal="first")
    b = request("log", check_ids=("C-c",), hypothesis_ids=("HS-auth",), goal="second")
    combined, = combine_requests((a, b))
    assert combined.intent.id == a.intent.id == b.intent.id
    assert combined.needs[0].requested_for_check_ids == ("C-a", "C-b", "C-c")
    assert combined.needs[0].requested_for_hypothesis_ids == ("HS-auth", "HS-catalog")
    deps = Dependencies(FixtureBackend())
    state = admit(initial_state(demo_incident()), deps, [a, b])
    assert len(deps.attempts) == len(state["task_specs"]) == len(state["intent_ledger"]) == 1
    assert state["intent_ledger"][0].needs == combined.needs
    assert state["task_specs"][0].id == materialize_task(make_task(demo_incident(), "log"), deps.backend).id
    assert "intent" not in state["task_specs"][0].model_dump_json()


async def test_planner_emits_ephemeral_requests_and_admission_owns_pending():
    state, deps = initial_state(demo_incident()), Dependencies(FixtureBackend())
    update = await graph.planner(state, Runtime(context=deps))
    assert [r.intent.signal for r in update["pending_tasks"]] == ["metric", "trace"]
    assert state["intent_ledger"] == update["intent_ledger"] == [] and not deps.attempts
    admitted = admit(state, deps, update["pending_tasks"])
    for record in admitted["intent_ledger"]:
        assert record.status == "pending" and record.committed_attempt_id is None
        assert record.reserved_attempt_id in deps.attempts
        assert record.materialized_task_spec_id in {s.id for s in admitted["task_specs"]}
    # A successful worker publishes only physical completion. Reconciliation owns the transition.
    worker_update = await graph.worker({"attempt_id": next(iter(deps.attempts))}, Runtime(context=deps))
    assert "intent_ledger" not in worker_update
    assert all(r.status == "pending" for r in admitted["intent_ledger"])
    for field, values in worker_update.items():
        admitted[field] = merge_records(admitted[field], values)
    reconciled = reconcile_intents(admitted)
    assert {r.status for r in reconciled} == {"pending", "completed"}


@pytest.mark.parametrize("budget,expected", [(Budget(max_tool_calls=0), 0), (Budget(max_tool_calls=1), 1)])
async def test_budget_clipping_cannot_leave_orphan_pending(budget, expected):
    state, deps = initial_state(demo_incident(), budget), Dependencies(FixtureBackend())
    candidates = initial_survey(state)
    admitted = admit(state, deps, candidates)
    assert len(deps.attempts) == len(admitted["intent_ledger"]) == expected
    assert len(initial_survey(admitted)) == 2 - expected
    final, _ = await run_investigation(initial_state(demo_incident(), budget), Dependencies(FixtureBackend()))
    assert all(r.status != "pending" for r in final["intent_ledger"])
    assert final["final_report"].status == "INCONCLUSIVE"


def test_round_clipping_and_physical_T_dedup_do_not_reserve_again():
    deps = Dependencies(FixtureBackend())
    state = {**initial_state(demo_incident(), Budget(max_rounds=1)), "rounds": 1}
    assert admit(state, deps, [request()])["intent_ledger"] == [] and not deps.attempts
    state = initial_state(demo_incident())
    state = admit(state, deps, [request()])
    # Simulate a legacy caller with a saved T/reservation but no logical ledger.
    deps.intent_admissions.clear()
    second = admit({**state, "intent_ledger": []}, deps, [request(goal="new alias/prose")])
    assert len(deps.attempts) == 1 and second["pending_tasks"] == []
    assert second["intent_ledger"][0].reserved_attempt_id == state["intent_ledger"][0].reserved_attempt_id


async def test_broad_completed_log_does_not_cover_narrow_check_intent():
    incident = generic_incident(("frontend", "catalog"))
    deps = Dependencies(generic_backend(seeds=incident.seed_services))
    state = await execute(initial_state(incident), deps, [make_task(incident, "log")])
    plan = dependency_connectivity_plan("frontend", "catalog", incident.window)
    log = next(c for c in plan.checks if c.scope.signal == "log")
    requests, _, _ = requests_for_checks(state, "HS-example", (log,), ())
    assert len(requests) == 1 and requests[0].intent.services == ("frontend",)
    assert requests[0].intent.id != state["intent_ledger"][0].id
    # Even with a completed broader acquisition, the exact intent remains new.
    assert state["intent_ledger"][0].status == "completed"


@pytest.mark.parametrize("code", [0, 2])
async def test_completed_nonfailure_trace_stops_after_initial_survey(code):
    state, _ = await generic_run(spans=(span(code=code),))
    assert state["rounds"] == 1 and len(state["tool_calls"]) == 2
    assert {r.intent.signal for r in state["intent_ledger"]} == {"metric", "trace"}
    assert all(r.status == "completed" for r in state["intent_ledger"])
    assert single_phv(state) is None
    assert required_requests(state) == ([], [], [])


async def test_empty_log_is_completed_even_when_required_check_stays_unknown():
    deps = Dependencies(FixtureBackend(records=tuple(), traces=demo_traces()))
    state, _ = await run_investigation(initial_state(demo_incident()), deps)
    log = next(r for r in state["intent_ledger"] if r.intent.signal == "log")
    assert log.status == "completed"
    assert single_phv(state).evaluation.log_check.verdict == "UNKNOWN"
    assert required_requests(state)[0] == []
    assert len(deps.attempts) == 3
    again = admit(state, deps, [request("log")])
    assert again["pending_tasks"] == [] and len(deps.attempts) == 3


@pytest.mark.parametrize("failure", ["error", "timeout", "invalid_result"])
async def test_failed_required_acquisition_never_retries(failure):
    class Backend(FixtureBackend):
        async def query(self, task):
            if task.signal == "log":
                if failure == "timeout":
                    await asyncio.Event().wait()
                if failure == "invalid_result":
                    return None
                raise RuntimeError("private backend diagnostic")
            return await super().query(task)
    deps = Dependencies(Backend())
    state, _ = await run_investigation(initial_state(demo_incident(), Budget(tool_timeout_seconds=0.02)), deps)
    row = next(r for r in state["intent_ledger"] if r.intent.signal == "log")
    assert row.status == "failed" and row.blocking_reason is None
    assert row.committed_attempt_id in {a.id for a in state["attempt_records"]}
    assert required_requests(state)[0] == []
    assert len(deps.attempts) == 3 and all(a.ordinal == 1 for a in state["attempt_records"])
    assert state["final_report"].underlying_cause is None
    assert "private backend diagnostic" not in state["final_report"].model_dump_json()


async def test_live_multiseed_capability_is_nonexecuted_unavailable(live):
    value = demo_incident().model_copy(update={"seed_services": ("checkout", "frontend")})
    deps = Dependencies(live)
    state, _ = await run_investigation(initial_state(value), deps)
    assert not deps.attempts and state["task_specs"] == state["attempt_records"] == state["tool_calls"] == []
    assert len(state["intent_ledger"]) == 2
    assert all(r.status == "unavailable" and r.blocking_reason == "unsupported_capability" for r in state["intent_ledger"])
    assert state["final_report"].status == "INCONCLUSIVE" and state["rounds"] == 0


async def test_replay_unavailable_is_nonexecution_not_negative_evidence():
    deps = Dependencies(FixtureBackend())
    history = await execute(initial_state(demo_incident()), deps, [make_task(demo_incident(), "metric")])
    replay_deps = Dependencies(RecordedReplayBackend(export_snapshot_v2(history, deps)))
    state, _ = await run_investigation(initial_state(demo_incident(), execution_id="replay-new"), replay_deps)
    row = next(r for r in state["intent_ledger"] if r.intent.signal == "trace")
    assert row.status == "unavailable" and row.blocking_reason == "replay_unavailable"
    assert row.materialized_task_spec_id is row.reserved_attempt_id is None
    assert len(state["attempt_records"]) == 1 and state["final_report"].status == "INCONCLUSIVE"


async def test_replay_ambiguity_explicit_selection_changes_T_not_TI():
    deps = Dependencies(FixtureBackend())
    history = await execute(initial_state(demo_incident()), deps, [make_task(demo_incident(), "trace")])
    archive = export_snapshot_v2(history, deps)
    old = archive.recorded_acquisitions[0]
    archive, second = with_second_attempt(archive, old)
    backend = RecordedReplayBackend(archive)
    replay_deps = Dependencies(backend)
    state = admit(initial_state(demo_incident(), execution_id="select-current"), replay_deps, [request()])
    row, = state["intent_ledger"]
    assert row.status == "ambiguous_replay_selection" and row.blocking_reason == "replay_ambiguous"
    assert not replay_deps.attempts and state["task_specs"] == []
    assert admit(state, replay_deps, [request()])["pending_tasks"] == []
    a = request(selector=backend.selector_for_attempt(old.attempt.id))
    b = request(selector=backend.selector_for_attempt(second.attempt.id))
    assert a.intent.id == b.intent.id == intent_for(old.tool_call.task).id
    assert materialize_task(a.physical_request(), backend).id != materialize_task(b.physical_request(), backend).id
    state = await execute(state, replay_deps, [a])
    assert state["intent_ledger"][0].status == "completed" and len(state["attempt_records"]) == 1
    assert state["intent_ledger"][0].recorded_selection == a.recorded_selection
    # A different selector is not a retry exemption after the intent completed.
    assert admit(state, replay_deps, [b])["pending_tasks"] == [] and len(replay_deps.attempts) == 1


async def test_scope_rejection_does_not_poison_later_authorized_intent():
    deps = Dependencies(FixtureBackend(materials={("trace", ("payment",)): TraceResult()}))
    state = initial_state(demo_incident())
    neighbor = request_for(intent(services=("payment",)), origin="programmatic")
    with pytest.raises(UnauthorizedAcquisition):
        admit(state, deps, [neighbor])
    assert state["intent_ledger"] == [] and not deps.intent_admissions
    state = await execute(state, deps, [request()])
    assert "payment" in state["investigation_scope"].admitted_services
    state = await execute(state, deps, [neighbor])
    assert any(r.intent.services == ("payment",) and r.status == "completed" for r in state["intent_ledger"])


async def test_stale_scope_is_invariant_and_hop_two_is_not_scheduled():
    state, deps = await generic_run(spans=(span(), span(local="catalog", peer="database", key="3")))
    assert all("database" not in r.intent.services for r in state["intent_ledger"])
    changed = {**state, "investigation_scope": state["investigation_scope"].model_copy(update={"seed_services": ("other",)})}
    with pytest.raises(ValueError):
        admit(changed, deps, [request()])


async def test_budget_before_log_never_manufactures_pending_or_negative_finding():
    state, _ = await run_investigation(initial_state(demo_incident(), Budget(max_tool_calls=2)), Dependencies(FixtureBackend()))
    assert state["final_report"].status == "INCONCLUSIVE"
    assert single_phv(state).evaluation.log_check.verdict == "UNKNOWN"
    assert {r.intent.signal for r in state["intent_ledger"]} == {"metric", "trace"}
    assert all(r.status == "completed" for r in state["intent_ledger"])


async def test_global_deadline_reconciles_admission_journal_without_worker_ledger_writes():
    class Backend(FixtureBackend):
        async def query(self, task):
            if task.signal == "trace":
                await asyncio.Event().wait()
            return await super().query(task)
    state, _ = await run_investigation(initial_state(demo_incident()), Dependencies(Backend()), deadline_seconds=0.05)
    assert {r.status for r in state["intent_ledger"]} == {"completed", "failed"}
    assert all(r.committed_attempt_id in {a.id for a in state["attempt_records"]} for r in state["intent_ledger"])


def test_closed_mapping_failure_has_no_fabricated_query():
    from test_check_results import metric_check
    from aegis_rca.models.check import CategoryPresent
    from test_metric_observations import STATUS
    check = metric_check(CategoryPresent(label=STATUS, value="ERROR"))
    assert check_intent(check) is None
    requests, rows, limits = requests_for_checks(initial_state(demo_incident()), "HS-example", (check,), ())
    assert requests == rows == []
    assert limits == [SchedulingLimitation(reason="unsupported_check_acquisition_mapping", check_id=check.id, hypothesis_id="HS-example")]


def test_ledger_transition_contract_rejects_impossible_states_and_retries():
    state = admit(initial_state(demo_incident()), Dependencies(FixtureBackend()), [request()])
    row, = state["intent_ledger"]
    terminal = IntentRecord.model_validate({**row.model_dump(), "status": "completed", "committed_attempt_id": row.reserved_attempt_id})
    assert transition(row, terminal) == terminal
    assert merge_intent_ledger([terminal], [row]) == [terminal]  # repeated ancestor, not rollback
    with pytest.raises(ValueError):
        transition(terminal, row)
    for updates in ({"reserved_attempt_id": None}, {"materialized_task_spec_id": None}, {"blocking_reason": "replay_unavailable"},
                    {"status": "completed"}, {"status": "unavailable", "blocking_reason": "unsupported_capability"}):
        with pytest.raises(ValidationError):
            IntentRecord.model_validate({**row.model_dump(), **updates})
    for reason in ("budget_exhausted", "outside_scope", "diagnostic prose", "backend_error"):
        with pytest.raises(ValidationError):
            IntentRecord(intent=row.intent, needs=row.needs, status="unavailable", blocking_reason=reason)
    unavailable = IntentRecord(intent=row.intent, needs=row.needs, status="unavailable", blocking_reason="unsupported_capability")
    with pytest.raises(ValueError):
        transition(unavailable, row)
    assert IntentRecord.model_validate_json(terminal.model_dump_json()) == terminal


def test_ledger_is_bounded():
    rows = []
    for i in range(MAX_INTENTS + 1):
        value = intent(services=(f"service-{i}",))
        rows.append(IntentRecord(intent=value, needs=request_for(value, origin="programmatic").needs,
            status="unavailable", blocking_reason="unsupported_capability"))
    assert len(merge_intent_ledger([], rows[:-1])) == MAX_INTENTS
    with pytest.raises(ValueError, match="bound"):
        merge_intent_ledger([], rows)


async def test_default_and_generic_provenance_roundtrip_and_no_neighbor_queries():
    default_deps = Dependencies(FixtureBackend())
    default, _ = await run_investigation(initial_state(demo_incident(), execution_id="p16a-golden"), default_deps)
    assert [r.spec.signal for r in default_deps.attempts.values()] == ["metric", "trace", "log"]
    generic, _ = await generic_run()
    for state in (default, generic):
        report = state["final_report"]
        assert (report.rounds, report.tool_calls, report.status) == (2, 3, "SUPPORTED")
        assert all(r.status == "completed" for r in state["intent_ledger"])
        assert all(r.intent.services == state["incident"].seed_services for r in state["intent_ledger"])
        h = single_phv(state).hypothesis_spec
        for signal in ("trace", "log"):
            row = next(r for r in state["intent_ledger"] if r.intent.signal == signal)
            consumers = next(n for n in row.needs if n.origin == "required_check")
            check = next(c for c in dependency_connectivity_plan(h.caller, h.callee, h.window).checks if c.scope.signal == signal)
            assert consumers.requested_for_hypothesis_ids == (h.id,) and consumers.requested_for_check_ids == (check.id,)
        restored = TypeAdapter(IncidentState).validate_json(TypeAdapter(IncidentState).dump_json(state))
        assert restored["intent_ledger"] == state["intent_ledger"]
    assert default["final_report"].claims[0].evidence_ids == ("E-T1", "E-L1")
    assert {s.id for s in default["task_specs"]} == {
        "T-1b2766336940c98744c8226297da12c07cf0ac18c42adc0dcdc80807b99a6caa",
        "T-b26df642516c0a47ba96502f7d8b5cb3a15d22eb596794600cb560389a982970",
        "T-ec204b2730d15ff0c918a8d0575d108bde4003f26563a4e2b3d625a13fbe5258"}


def test_ti_golden_ids_are_new_without_migrating_existing_identities():
    assert {signal: intent(signal).id for signal in ("metric", "trace", "log")} == {
        "metric": "TI-19817adc54092b7c1d65259692f65d68d910654f0b214200be59ac4f7bc27969",
        "trace": "TI-913a0d896f7cd311925e86b009055715fc06896d2b5dc007e80c252af60afb01",
        "log": "TI-ffcc5a09df51afe36391955cd49b019ebd6794602da691f77982a5545733f686"}


async def test_exact_window_reuse_only_even_for_completed_acquisition():
    deps = Dependencies(FixtureBackend(records=(), traces=demo_traces()))
    state = await execute(initial_state(demo_incident()), deps, [request("log")])
    window = demo_incident().window
    narrower = TimeWindow(start=window.start + timedelta(seconds=1), end=window.end)
    check = next(c for c in dependency_connectivity_plan("checkout", "payment", narrower).checks if c.scope.signal == "log")
    requests, _, _ = requests_for_checks(state, "HS-example", (check,), ())
    assert len(requests) == 1 and requests[0].intent.window == narrower
    assert requests[0].intent.id != state["intent_ledger"][0].id


async def test_required_work_precedes_optional_llm_work_and_blocks_initial_log_or_fanout():
    from types import SimpleNamespace
    class Reasoner:
        calls = 0
        async def propose_plan(self, incident, **kwargs):
            self.calls += 1
            return SimpleNamespace(tasks=[SimpleNamespace(signal=s, services=services, goal="LLM display")
                for s, services in (("log", ("checkout",)), ("trace", ("payment",)), ("trace", ("checkout",)))])
    reasoner = Reasoner()
    deps = Dependencies(FixtureBackend(), reasoner=reasoner)
    state = initial_state(demo_incident())
    proposed = await graph.planner(state, Runtime(context=deps))
    assert [r.intent.signal for r in proposed["pending_tasks"]] == ["metric", "trace"]
    assert all(r.intent.services == ("checkout",) for r in proposed["pending_tasks"])
    assert proposed["intent_ledger"] == [] and not deps.intent_admissions
    state = await execute(state, deps, proposed["pending_tasks"])
    proposed = await graph.planner(state, Runtime(context=deps))
    assert [r.intent.signal for r in proposed["pending_tasks"]] == ["log"]
    assert reasoner.calls == 1  # Required deterministic work did not consult the model.
    assert proposed["pending_tasks"][0].needs[0].origin == "required_check"


async def test_support_or_contradict_never_schedules_even_without_matching_ti():
    state, _ = await generic_run()
    bundle = single_phv(state)
    h = bundle.hypothesis_spec
    log = next(c for c in dependency_connectivity_plan(h.caller, h.callee, h.window).checks if c.scope.signal == "log")
    for verdict in ("SUPPORT", "CONTRADICT"):
        result = bundle.evaluation.log_check.model_copy(update={"verdict": verdict})
        requests, _, _ = requests_for_checks({**state, "intent_ledger": []}, h.id, (log,), (result,))
        assert requests == []


async def test_replay_invalid_or_conflicting_selectors_never_poison_intent():
    from aegis_rca.tools.recorded_replay import RecordedAcquisitionUnavailable
    deps = Dependencies(FixtureBackend())
    history = await execute(initial_state(demo_incident()), deps, [request(), request("metric")])
    archive = export_snapshot_v2(history, deps)
    row = next(r for r in archive.recorded_acquisitions if r.partition.signal == "trace")
    metric = next(r for r in archive.recorded_acquisitions if r.partition.signal == "metric")
    archive, second = with_second_attempt(archive, row)
    backend = RecordedReplayBackend(archive)
    current_deps = Dependencies(backend)
    state = initial_state(demo_incident(), execution_id="selection-validation")
    invalid = request(selector=backend.selector_for_attempt(metric.attempt.id))
    with pytest.raises(RecordedAcquisitionUnavailable):
        admit(state, current_deps, [invalid])
    assert not current_deps.attempts and not current_deps.intent_admissions
    a = request(selector=backend.selector_for_attempt(row.attempt.id))
    b = request(selector=backend.selector_for_attempt(second.attempt.id))
    with pytest.raises(ValueError, match="conflicting"):
        admit(state, current_deps, [a, b])
    assert not current_deps.attempts and not current_deps.intent_admissions
    state = admit(state, current_deps, [request()])
    with pytest.raises(RecordedAcquisitionUnavailable):
        admit(state, current_deps, [invalid])
    assert state["intent_ledger"][0].status == "ambiguous_replay_selection"
    assert not current_deps.attempts


def test_ledger_revalidates_left_hand_persisted_states():
    row = admit(initial_state(demo_incident()), Dependencies(FixtureBackend()), [request()])["intent_ledger"][0]
    with pytest.raises(ValidationError):
        merge_intent_ledger([row.model_copy(update={"reserved_attempt_id": None})], [])
    with pytest.raises(ValueError, match="reservation"):
        state = initial_state(demo_incident(), execution_id="different-execution")
        reconcile_intents({**state, "task_specs": [materialize_task(request().physical_request(), FixtureBackend())], "intent_ledger": [row]})
