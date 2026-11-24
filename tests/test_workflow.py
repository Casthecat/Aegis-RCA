"""Executable workflow and adapter-boundary checks, not an RCA benchmark."""

import asyncio
from datetime import timedelta

import pytest

from aegis_rca.fixtures import FixtureBackend, demo_evidence, demo_incident, demo_traces
from aegis_rca.graph import build_graph
from aegis_rca.models.evidence import Observation, ToolResult
from aegis_rca.models.incident import Budget
from aegis_rca.report import to_markdown
from aegis_rca.runtime import Dependencies
from aegis_rca.state import initial_state


class RecordingBackend(FixtureBackend):
    def __init__(self, records=None, *, traces=None):
        super().__init__(records, traces=traces)
        self.attempted = []

    async def query(self, task):
        self.attempted.append(task.signal)
        return await super().query(task)


async def run_workflow(backend=None, budget=None, incident=None, execution_id=None):
    """Capture intermediate verification decisions as well as the final report."""
    backend = backend if backend is not None else RecordingBackend()
    snapshots = [
        state
        async for state in build_graph().astream(
            initial_state(incident or demo_incident(), budget, execution_id=execution_id),
            context=Dependencies(backend=backend),
            stream_mode="values",
        )
    ]
    decisions = [
        state["verification"].decision
        for state in snapshots
        if state["verification"] is not None
    ]
    return snapshots[-1], decisions, backend


async def test_targeted_followup_adds_cited_evidence_without_repeating_queries():
    state, decisions, backend = await run_workflow()
    report = state["final_report"]

    assert "targeted_followup" in decisions
    assert decisions[-1] == "finalize"
    assert sorted(backend.attempted) == ["log", "metric", "trace"]
    assert backend.attempted[-1] == "log"
    assert report.status == "SUPPORTED"
    assert report.diagnosis_level == "failure_mode"
    assert report.rounds == 2
    assert report.tool_calls == 3
    assert {e.id for e in state["evidence"]} == {"E-M1", "E-T1", "E-L1"}
    assert len(report.claims) == 1
    assert set(report.claims[0].evidence_ids) == {"E-T1", "E-L1"}
    assert all(call.status == "ok" for call in state["tool_calls"])


async def test_empty_queries_stop_after_initial_survey_without_inventing_an_incident():
    state, decisions, backend = await run_workflow(RecordingBackend(records=()))
    report = state["final_report"]

    assert "replan" not in decisions
    assert "targeted_followup" not in decisions
    assert decisions[-1] == "inconclusive"
    assert sorted(backend.attempted) == ["metric", "trace"]
    assert state["evidence"] == []
    assert report.status == "INCONCLUSIVE"
    assert report.claims == ()
    assert report.underlying_cause is None
    assert {call.status for call in state["tool_calls"]} == {"empty"}
    assert any("absence is unproven" in line for line in report.limitations)


async def test_missing_logs_preserve_partial_findings_and_do_not_prove_a_root_cause():
    records = tuple(e for e in demo_evidence() if e.signal != "log")
    state, decisions, backend = await run_workflow(RecordingBackend(records, traces=demo_traces()))
    report = state["final_report"]

    assert "targeted_followup" in decisions
    assert backend.attempted.count("log") == 1
    assert {e.signal for e in state["evidence"]} == {"metric", "trace"}
    assert report.status == "INCONCLUSIVE"
    assert report.claims == ()
    assert report.underlying_cause is None
    assert any("log query returned empty" in line for line in report.limitations)
    assert all(h.status != "rejected" for h in report.hypotheses)


@pytest.mark.parametrize("limit", [0, 1, 2, 3])
async def test_parallel_dispatch_respects_tool_budget_including_final_useful_call(limit):
    state, _, backend = await run_workflow(budget=Budget(max_tool_calls=limit))
    report = state["final_report"]

    assert len(backend.attempted) == limit
    assert len(state["tool_calls"]) == limit
    assert report.tool_calls == limit
    assert len(set(backend.attempted)) == limit
    assert report.underlying_cause is None
    if limit == 3:
        # Evidence arriving on the last allowed attempt remains usable.
        assert report.status == "SUPPORTED"
        assert report.termination_reason == "budget_exhausted"  # Current support is retained at a spent budget.
    else:
        assert report.status == "INCONCLUSIVE"
        assert report.termination_reason == "budget_exhausted"
        assert report.claims == ()


async def test_round_limit_stops_before_followup_even_with_unused_tool_budget():
    state, _, backend = await run_workflow(budget=Budget(max_rounds=1, max_tool_calls=10))
    report = state["final_report"]

    assert set(backend.attempted) == {"metric", "trace"}
    assert report.rounds == 1
    assert report.status == "INCONCLUSIVE"
    assert report.termination_reason == "budget_exhausted"


class FailingBackend(RecordingBackend):
    async def query(self, task):
        self.attempted.append(task.signal)
        raise RuntimeError("private-token-example-do-not-display")


async def test_backend_errors_are_counted_and_reported_without_leaking_exception_text():
    state, _, backend = await run_workflow(FailingBackend())
    report = state["final_report"]

    assert len(backend.attempted) == 2
    assert report.status == "INCONCLUSIVE"
    assert {call.status for call in state["tool_calls"]} == {"error"}
    assert all("RuntimeError" in " ".join(call.warnings) for call in state["tool_calls"])
    assert "private-token-example-do-not-display" not in report.model_dump_json()
    assert "private-token-example-do-not-display" not in str(state["tool_calls"])
    assert state["evidence"] == []


class MalformedBackend(RecordingBackend):
    def __init__(self, payload):
        super().__init__()
        self.payload = payload

    async def query(self, task):
        self.attempted.append(task.signal)
        return self.payload


@pytest.mark.parametrize("payload", [None, {"status": "ok", "evidence": "invalid-evidence"}])
async def test_malformed_adapter_response_becomes_recorded_error_instead_of_graph_crash(payload):
    state, _, backend = await run_workflow(MalformedBackend(payload))
    report = state["final_report"]

    assert len(backend.attempted) == 2
    assert report.status == "INCONCLUSIVE"
    assert {call.status for call in state["tool_calls"]} == {"error"}
    assert state["evidence"] == []
    assert report.claims == ()
    assert report.underlying_cause is None
    assert "invalid-evidence" not in report.model_dump_json()


class HangingBackend(RecordingBackend):
    async def query(self, task):
        self.attempted.append(task.signal)
        await asyncio.Event().wait()


async def test_backend_timeouts_terminate_and_consume_budget_without_hidden_retries():
    state, _, backend = await run_workflow(
        HangingBackend(), Budget(max_tool_calls=3, tool_timeout_seconds=0.01)
    )
    report = state["final_report"]

    assert len(backend.attempted) == 2
    assert report.status == "INCONCLUSIVE"
    assert report.termination_reason == "no_admissible_new_intent"
    assert {call.status for call in state["tool_calls"]} == {"timeout"}
    assert report.tool_calls == 2
    assert report.claims == ()


@pytest.mark.parametrize("services", [(" ",), ("x" * 257,), tuple(f"s{i}" for i in range(65))])
async def test_unsupported_scope_does_not_query_or_reuse_checkout_fixture(services):
    incident = demo_incident().model_copy(update={"seed_services": services})
    state, _, backend = await run_workflow(incident=incident)
    report = state["final_report"]

    assert backend.attempted == []
    assert state["scope"] is None
    assert state["evidence"] == []
    assert report.status == "INCONCLUSIVE"
    assert report.termination_reason == "invalid_scope"
    assert report.tool_calls == 0
    assert report.rounds == 0


class BadLogAdapter(RecordingBackend):
    """Return invalid data without FixtureBackend's own scope filtering."""

    def __init__(self, invalid_record, *, status="ok"):
        super().__init__()
        self.invalid_record = invalid_record
        self.status = status

    async def query(self, task):
        if task.signal == "log":
            self.attempted.append(task.signal)
            return ToolResult(status=self.status, evidence=(self.invalid_record,))
        return await super().query(task)


@pytest.mark.parametrize("violation", ["before_window", "at_window_end", "other_service", "wrong_signal"])
async def test_untrusted_adapter_results_cannot_cross_incident_boundaries(violation):
    incident = demo_incident()
    record = next(e for e in demo_evidence() if e.signal == "log")
    updates = {
        "before_window": {"timestamp": incident.window.start - timedelta(microseconds=1)},
        "at_window_end": {"timestamp": incident.window.end},
        "other_service": {"service": "shipping"},
        "wrong_signal": {"signal": "trace"},
    }
    state, _, _ = await run_workflow(BadLogAdapter(record.model_copy(update=updates[violation])))

    assert record.id not in {e.id for e in state["evidence"]}
    assert state["final_report"].status == "INCONCLUSIVE"
    assert state["final_report"].claims == ()
    call = next(c for c in state["tool_calls"] if c.task.signal == "log")
    assert call.evidence_ids == ()
    assert any("outside scope" in warning for warning in call.warnings)


@pytest.mark.parametrize("status", ["error", "timeout", "empty"])
async def test_unsuccessful_query_payload_cannot_be_used_as_positive_evidence(status):
    record = next(e for e in demo_evidence() if e.signal == "log")
    state, _, _ = await run_workflow(BadLogAdapter(record, status=status))

    assert record.id not in {e.id for e in state["evidence"]}
    assert state["final_report"].status == "INCONCLUSIVE"
    assert state["final_report"].underlying_cause is None


@pytest.mark.parametrize("substitute", ["server_span_absent", "application_error", "wrong_peer"])
async def test_missing_span_or_different_failure_does_not_prove_connection_refusal(substitute):
    records = []
    for record in demo_evidence():
        if record.signal == "log":
            observation = (
                Observation(code="connection_refused", peer_service="shipping")
                if substitute == "wrong_peer"
                else Observation(code=substitute, peer_service="payment")
            )
            record = record.model_copy(update={"observation": observation, "excerpt": substitute})
        records.append(record)
    state, _, _ = await run_workflow(RecordingBackend(tuple(records), traces=demo_traces()))

    assert state["final_report"].status == "INCONCLUSIVE"
    assert state["final_report"].claims == ()
    assert state["final_report"].underlying_cause is None


async def test_supported_report_does_not_overstate_root_cause_or_independent_confirmation():
    state, _, _ = await run_workflow()
    report = state["final_report"]

    assert report.status == "SUPPORTED"
    assert report.underlying_cause is None
    assert report.diagnosis_level == "failure_mode"
    assert "crashed" not in report.summary.lower()
    assert "dns" not in report.summary.lower()
    assert "configured" not in report.summary.lower()
    assert all(h.status != "rejected" for h in report.hypotheses)
    assert any("not independent trials" in line for line in report.limitations)
    assert any("remain unverified" in line for line in report.limitations)
    assert "Underlying cause: **undetermined**" in to_markdown(report)
