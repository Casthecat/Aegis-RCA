import json
from pathlib import Path

import httpx

from aegis_rca.adapters import JsonHttp
from aegis_rca.fixtures import demo_incident
from aegis_rca.models.incident import Budget
from aegis_rca.runner import run_investigation
from aegis_rca.runtime import Dependencies
from aegis_rca.settings import BackendSettings
from aegis_rca.state import initial_state
from aegis_rca.tools.live import LiveBackend
from aegis_rca.tools.replay import ReplayBackend
from aegis_rca.tools.snapshot import read_snapshot, write_snapshot


async def test_synthetic_wire_responses_round_trip_through_real_adapters_and_graph(monkeypatch, tmp_path):
    """Wire contracts are synthetic; the complete adapter→graph→snapshot path is real code."""
    incident = demo_incident()
    timestamp = incident.window.start.isoformat()
    nanos = str(int(incident.window.start.timestamp()) * 1_000_000_000)
    trace_id, span_id = "1" * 32, "2" * 16
    calls = []
    def respond(request):
        calls.append(request.url.path)
        if request.url.path == "/api/v3/traces":
            return httpx.Response(200, json={"result": {"resourceSpans": [{
                "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "checkout"}}]},
                "scopeSpans": [{"spans": [{"traceId": trace_id, "spanId": span_id,
                    "name": "oteldemo.PaymentService/Charge", "kind": 3,
                    "startTimeUnixNano": nanos, "endTimeUnixNano": str(int(nanos) + 1000),
                    "status": {"code": 2}, "attributes": [
                        {"key": "rpc.grpc.status_code", "value": {"intValue": "14"}},
                        {"key": "rpc.service", "value": {"stringValue": "oteldemo.PaymentService"}}]}]}]}]}})
        if request.url.path == "/otel-logs-*/_search":
            query = json.loads(request.content)
            assert {"term": {"resource.service.name.keyword": "checkout"}} in query["query"]["bool"]["filter"]
            return httpx.Response(200, json={"timed_out": False,
                "_shards": {"total": 1, "successful": 1, "failed": 0, "skipped": 0},
                "hits": {"total": {"value": 1, "relation": "eq"}, "hits": [
                    {"_index": "otel-logs-example", "_id": "a", "_source": {
                        "@timestamp": timestamp, "resource": {"service.name": "checkout"},
                        "severity": {"text": "ERROR"}, "body": "dial tcp payment:50051: connection refused",
                        "traceId": trace_id, "spanId": span_id, "attributes": {}}}]}})
        return httpx.Response(500)
    monkeypatch.setattr("aegis_rca.tools.live.JsonHttp", lambda url, **opts:
                        JsonHttp(url, transport=httpx.MockTransport(respond), **opts))
    settings = BackendSettings(prometheus_url="http://metrics.test", jaeger_url="http://traces.test",
                               opensearch_url="http://logs.test")
    backend = LiveBackend(settings, incident)
    try:
        state, _ = await run_investigation(initial_state(incident, source_mode="live"), Dependencies(backend))
        assert state["final_report"].status == "SUPPORTED"
        assert state["final_report"].mode == "live"
        assert state["final_report"].underlying_cause is None
        snapshot = backend.snapshot().model_copy(update={"provenance": "synthetic_wire_fixture"})
        path = tmp_path / "snapshot.json"
        write_snapshot(snapshot, path)
        replay, _ = await run_investigation(initial_state(incident), Dependencies(ReplayBackend(read_snapshot(path))))
        assert replay["final_report"].summary == state["final_report"].summary
        assert replay["final_report"].status == "SUPPORTED"
        assert [e.id for e in replay["evidence"]] == [e.id for e in state["evidence"]]
        assert len(calls) == 2  # No unconfigured PromQL, automatic retries, or hidden pagination.
    finally:
        backend.close()
