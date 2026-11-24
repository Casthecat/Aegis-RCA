from portfolio_helpers import single_phv, subject
import asyncio
from dataclasses import replace

import pytest

from aegis_rca.fixtures import FixtureBackend, demo_evidence, demo_incident
from aegis_rca.models.evidence import ToolResult
from aegis_rca.models.incident import Budget
from aegis_rca.models.telemetry import LogRecord, SpanRecord
from aegis_rca.reasoning import PlanProposal, TaskProposal, HypothesisProposalSet, incident_key
from aegis_rca.runner import run_investigation
from aegis_rca.runtime import Dependencies
from aegis_rca.state import initial_state
from aegis_rca.tools.normalize import normalize_logs, normalize_spans


class LogFirstReasoner:
    async def propose_plan(self, incident, **kwargs):
        return PlanProposal(incident_key=incident_key(incident), status="provisional", rationale="Read errors first",
            tasks=(TaskProposal(signal="log", services=("checkout",), goal="Read errors"),))

    async def propose_hypotheses(self, incident, evidence, **kwargs):
        return HypothesisProposalSet(incident_key=incident_key(incident), status="provisional", hypotheses=())


async def test_model_cannot_replace_initial_survey_with_logs():
    state, events = await run_investigation(initial_state(demo_incident(), reasoning_mode="llm_assisted"),
        Dependencies(backend=FixtureBackend(), reasoner=LogFirstReasoner()))
    assert state["final_report"].status == "SUPPORTED"
    assert {c.task.signal for c in state["tool_calls"]} == {"metric", "trace", "log"}
    assert state["rounds"] == 2
    assert state["hypotheses"][0].id == single_phv(state).hypothesis_spec.id


async def test_global_deadline_preserves_completed_parallel_worker():
    class PartialBackend:
        async def query(self, task):
            if task.signal == "metric":
                return ToolResult(status="ok", evidence=(demo_evidence()[0],))
            await asyncio.Event().wait()
    state, events = await run_investigation(initial_state(demo_incident(), Budget(tool_timeout_seconds=10)),
        Dependencies(backend=PartialBackend()), deadline_seconds=0.05)
    assert [e.id for e in state["evidence"]] == ["E-M1"]
    statuses = {c.task.signal: c.status for c in state["tool_calls"]}
    assert statuses == {"metric": "ok", "trace": "timeout"}
    assert state["final_report"].termination_reason == "deadline_exhausted"


@pytest.mark.parametrize("value", [float("inf"), float("nan"), 0, -1])
async def test_deadline_cannot_be_unbounded(value):
    with pytest.raises(ValueError):
        await run_investigation(initial_state(demo_incident()), Dependencies(backend=FixtureBackend()), deadline_seconds=value)


def raw_span(span_id="2" * 16):
    return SpanRecord(trace_id="1" * 32, span_id=span_id, service="checkout", name="oteldemo.PaymentService/Charge",
        kind="CLIENT", start=demo_incident().window.start, duration_ms=1, status="ERROR",
        attributes={"rpc.grpc.status_code": 14, "rpc.service": "oteldemo.PaymentService"}, source="same-trace")


def raw_log(body, id="first"):
    return LogRecord(id=id, timestamp=demo_incident().window.start, service="checkout", severity="ERROR",
        body=body, trace_id="1" * 32, source="same-search")


def test_same_trace_or_same_log_body_do_not_collide():
    mapping = {"oteldemo.PaymentService": "payment"}
    spans = normalize_spans((raw_span(), raw_span("3" * 16)), mapping)
    assert len({e.id for e in spans}) == 2
    logs = normalize_logs((raw_log("dial payment:50051: connection refused"),
                           raw_log("dial payment:50051: connection refused", "second")), (), mapping)
    assert len({e.id for e in logs}) == 2


def test_peer_does_not_depend_on_query_completion_order_or_entire_trace():
    mapping = {"oteldemo.PaymentService": "payment"}
    log = raw_log("payment unrelated; dial unknown:1234: connection refused")
    without_spans = normalize_logs((log,), (), mapping)
    with_spans = normalize_logs((log,), (raw_span(),), mapping)
    assert without_spans == with_spans
    assert with_spans[0].observation.peer_service is None


def test_explicit_dns_failure_keeps_underlying_configuration_unknown():
    evidence = normalize_logs((raw_log("lookup payment on 127.0.0.1:53: no such host"),), (), {"p": "payment"})
    assert evidence[0].observation.code == "dns_resolution_failed"
    assert evidence[0].observation.peer_service == "payment"


@pytest.mark.parametrize("rpc_value", [[], {}, None, 3])
def test_malformed_rpc_attribute_cannot_crash_log_normalization(rpc_value):
    log = raw_log("dial payment:50051: connection refused").model_copy(update={"attributes": {"rpc.service": rpc_value}})
    assert len(normalize_logs((log,), (), {"p": "payment"})) == 1


def test_successful_name_resolution_is_not_failure_evidence():
    assert normalize_logs((raw_log("name resolution succeeded: lookup payment"),), (), {"p": "payment"}) == ()


def test_long_span_names_are_bounded_without_losing_other_evidence():
    span = raw_span().model_copy(update={"name": "X" * 5000})
    assert len(normalize_spans((span,), {"p": "payment"})[0].excerpt) == 2000
