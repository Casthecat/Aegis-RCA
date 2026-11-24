"""Synthetic wire contract fixtures, hand-authored from official schemas.

These tests do not record or claim to validate a running telemetry backend.
"""
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from aegis_rca.adapters import (
    AdapterError, JaegerAdapter, JsonHttp, OpenSearchAdapter, OpenSearchFields,
    PrometheusAdapter, SchemaError,
)

START = datetime(2026, 7, 1, tzinfo=timezone.utc)
END = START + timedelta(minutes=1)
TRACE = "1234567890abcdef1234567890abcdef"
SPAN = "1234567890abcdef"
START_NS = int(START.timestamp()) * 1_000_000_000


def client(payload=None, *, content=None, status=200, handler=None, **kwargs):
    if handler is None:
        def handler(request):
            return httpx.Response(status, json=payload) if content is None else httpx.Response(status, content=content)
    return JsonHttp("http://telemetry.invalid", transport=httpx.MockTransport(handler), **kwargs)


def synthetic_trace(*, begin=START_NS + 123, finish=START_NS + 1_000_000_123):
    return {"result": {"resourceSpans": [{
        "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "checkout"}}]},
        "scopeSpans": [{"spans": [{
            "traceId": TRACE, "spanId": SPAN, "name": "charge", "kind": 3,
            "startTimeUnixNano": str(begin), "endTimeUnixNano": str(finish),
            "status": {"code": 2, "message": "connection refused"},
            "attributes": [{"key": "rpc.grpc.status_code", "value": {"intValue": "14"}}],
        }]}],
    }]}}


def synthetic_logs(count=1):
    return {"timed_out": False, "_shards": {"total": 1, "successful": 1, "failed": 0},
            "hits": {"total": {"value": count, "relation": "eq"}, "hits": [
                {"_index": "otel-logs-2026-07-01", "_id": str(i), "_source": {
                    "@timestamp": "2026-07-01T00:00:10.123456789Z", "body": "connection refused",
                    "resource": {"service.name": "checkout"}, "severity": {"text": "ERROR"},
                    "traceId": TRACE, "spanId": SPAN, "attributes": {"rpc.grpc.status_code": 14},
                }} for i in range(count)
            ]}}


def test_transport_does_not_follow_redirect_or_retry_or_expose_response():
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": "https://other.invalid/private"}, text="private-token")
    with client(handler=respond) as http:
        with pytest.raises(AdapterError, match="HTTP status 302") as error:
            http.request("GET", "/api/v1/labels")
    assert len(calls) == 1
    assert "private" not in str(error.value)


def test_transport_timeout_is_sanitized_and_single_attempt():
    calls = []
    def respond(request):
        calls.append(request)
        raise httpx.ReadTimeout("secret-token in upstream exception", request=request)
    with client(handler=respond) as http:
        with pytest.raises(AdapterError, match="timed out") as error:
            http.request("GET", "/api/v1/labels")
    assert "secret" not in str(error.value)
    assert len(calls) == 1


def test_transport_bounds_even_chunked_response():
    with client(content=b'{"secret":' + b' ' * 100, max_response_bytes=20) as http:
        with pytest.raises(AdapterError, match="byte budget"):
            http.request("GET", "/api/v1/labels")


@pytest.mark.parametrize("path", ["//external.invalid/query", "/../secret", "/%2e%2e/secret", "/x?bad=y", "/x\\y"])
def test_transport_rejects_path_escape(path):
    with client({}) as http, pytest.raises(ValueError):
        http.request("GET", path)


def test_transport_restricts_mutations():
    with client({}) as http, pytest.raises(ValueError):
        http.request("POST", "/otel-logs/_delete_by_query")


@pytest.mark.parametrize("body", [b'{"x":', b'{} {}', b'\xff'])
def test_json_failure_is_sanitized(body):
    with client(content=body) as http, pytest.raises(SchemaError):
        http.request("GET", "/api/v1/labels")


def test_prometheus_catalog_preserves_warnings():
    seen = []
    def respond(request):
        seen.append(request)
        return httpx.Response(200, json={"status": "success", "data": ["checkout", "payment"],
                                          "warnings": ["partial catalog"], "infos": ["metadata may be stale"]})
    with client(handler=respond) as http:
        result = PrometheusAdapter(http).label_values("service_name", START, END)
    assert result.values == ("checkout", "payment")
    assert result.warnings == ("partial catalog", "info: metadata may be stale")
    assert seen[0].url.path == "/api/v1/label/service_name/values"
    assert seen[0].url.params["start"] == "2026-07-01T00:00:00Z"


def test_prometheus_exclusive_end_and_nonfinite_not_zero():
    t = START.timestamp()
    payload = {"status": "success", "data": {"resultType": "matrix", "result": [
        {"metric": {"__name__": "up", "service_name": "checkout"},
         "values": [[t, "1"], [t + 15, "NaN"], [t + 30, "+Inf"], [t + 60, "0"]]}
    ]}}
    with client(payload) as http:
        result = PrometheusAdapter(http).query_range("up", START, END)
    assert [(p.timestamp, p.value) for p in result.series[0].points] == [(START, 1)]
    assert "2 non-finite" in result.warnings[0]
    assert result.coverage_complete is False


@pytest.mark.parametrize("payload", [
    {"data": []}, {"status": "success", "data": {"resultType": "vector", "result": []}},
    {"status": "success", "data": {"resultType": "matrix", "result": [{"metric": {}}]}},
])
def test_prometheus_malformed_is_not_empty_evidence(payload):
    with client(payload) as http, pytest.raises(SchemaError):
        PrometheusAdapter(http).query_range("up", START, END)


def test_prometheus_backend_error_does_not_leak_message():
    with client({"status": "error", "error": "a secret query"}) as http:
        with pytest.raises(AdapterError) as error:
            PrometheusAdapter(http).label_names()
    assert "secret" not in str(error.value)


def test_jaeger_v3_nanos_resource_attributes_and_exclusive_window():
    seen = []
    def respond(request):
        seen.append(request)
        return httpx.Response(200, json=synthetic_trace())
    with client(handler=respond) as http:
        result = JaegerAdapter(http).search("checkout", START, END)
    span = result.spans[0]
    assert span.service == "checkout" and span.kind == "CLIENT" and span.status == "ERROR"
    assert span.duration_ms == 1000 and span.start == START
    assert span.attributes["rpc.grpc.status_code"] == 14
    assert span.attributes["aegis.otlp"]["startTimeUnixNano"] == str(START_NS + 123)
    assert seen[0].url.path == "/api/v3/traces"
    assert seen[0].url.params["query.startTimeMax"] == "2026-07-01T00:01:00Z"
    assert seen[0].url.params["query.rawTraces"] == "true"


def test_jaeger_joins_multiple_envelopes_and_deduplicates():
    first = synthetic_trace()
    second = deepcopy(first)
    second["result"]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["spanId"] = "abcdef1234567890"
    wire = "\n".join(json.dumps(x) for x in [first, second, first])
    with client(content=wire) as http:
        result = JaegerAdapter(http).get_trace(TRACE, START, END)
    assert len(result.spans) == 2
    assert len({span.source for span in result.spans}) == 2


@pytest.mark.parametrize("tail", ['{"result":', json.dumps({"error": {"message": "secret"}})])
def test_jaeger_failed_tail_discards_earlier_spans(tail):
    wire = json.dumps(synthetic_trace()) + "\n" + tail
    with client(content=wire) as http, pytest.raises(AdapterError) as error:
        JaegerAdapter(http).search("checkout", START, END)
    assert "secret" not in str(error.value)


def test_jaeger_exclusive_boundary_and_search_depth_warning():
    begin = int(END.timestamp()) * 1_000_000_000
    with client(synthetic_trace(begin=begin, finish=begin + 1)) as http:
        result = JaegerAdapter(http).search("checkout", START, END, limit=1)
    assert not result.spans and result.coverage_complete is False
    assert result.truncated
    assert any("cross-boundary" in warning for warning in result.warnings)


def test_jaeger_malformed_start_is_visible():
    data = synthetic_trace()
    data["result"]["resourceSpans"][0]["scopeSpans"][0]["spans"][0].pop("startTimeUnixNano")
    with client(data) as http, pytest.raises(SchemaError):
        JaegerAdapter(http).search("checkout", START, END)


def test_jaeger_services_and_404_remain_distinct_from_verified_absence():
    with client({"services": ["checkout"]}) as http:
        assert JaegerAdapter(http).services().values == ("checkout",)
    with client(status=404) as http, pytest.raises(AdapterError, match="HTTP status 404"):
        JaegerAdapter(http).search("checkout", START, END)


def test_opensearch_ss4o_lexical_query_and_nested_dotted_keys():
    seen = []
    def respond(request):
        seen.append(request)
        return httpx.Response(200, json=synthetic_logs())
    with client(handler=respond) as http:
        result = OpenSearchAdapter(http).search(START, END, service="checkout", text="connection refused")
    assert result.logs[0].service == "checkout" and result.logs[0].trace_id == TRACE
    assert result.logs[0].severity == "ERROR"
    assert result.logs[0].timestamp.microsecond == 123456
    query = json.loads(seen[0].content)
    assert seen[0].method == "POST" and seen[0].url.path == "/otel-logs-*/_search"
    assert query["query"]["bool"]["filter"][0] == {"range": {"@timestamp": {"gte": "2026-07-01T00:00:00Z", "lt": "2026-07-01T00:01:00Z"}}}
    assert query["query"]["bool"]["filter"][1] == {"term": {"resource.service.name.keyword": "checkout"}}
    assert "match" in query["query"]["bool"]["must"][0]


def test_opensearch_context_and_truncation_visible():
    seen = []
    def respond(request):
        seen.append(request)
        return httpx.Response(200, json=synthetic_logs(2))
    with client(handler=respond) as http:
        result = OpenSearchAdapter(http).context(TRACE, START, END, limit=1)
    assert len(result.logs) == 1 and result.truncated and result.coverage_complete is False
    assert {"term": {"traceId.keyword": TRACE}} in json.loads(seen[0].content)["query"]["bool"]["filter"]


def test_opensearch_partial_response_flags_preserved():
    data = synthetic_logs()
    data["timed_out"] = True
    data["_shards"]["failed"] = 1
    with client(data) as http:
        result = OpenSearchAdapter(http).search(START, END)
    assert len(result.logs) == 1 and result.coverage_complete is False
    assert any("timed_out" in warning for warning in result.warnings)
    assert any("shards failed" in warning for warning in result.warnings)


def test_opensearch_locators_distinguish_documents_with_identical_bodies():
    with client(synthetic_logs(2)) as http:
        result = OpenSearchAdapter(http).search(START, END)
    assert len({log.source for log in result.logs}) == 2
    assert all("/_doc/" in log.source for log in result.logs)


def test_opensearch_malformed_mapping_does_not_look_like_no_logs():
    data = synthetic_logs()
    data["hits"]["hits"][0]["_source"].pop("resource")
    with client(data) as http, pytest.raises(SchemaError, match="service field is missing"):
        OpenSearchAdapter(http).search(START, END)


def test_opensearch_explicit_alternative_fields():
    data = synthetic_logs()
    data["hits"]["hits"][0]["_source"] = {
        "time": START.timestamp() * 1000, "serviceName": "checkout", "message": "failed",
        "level": "ERROR", "trace": TRACE,
    }
    fields = OpenSearchFields(timestamp="time", timestamp_format="epoch_millis", service="serviceName",
                              service_exact="serviceName", body="message", severity="level", trace_id="trace")
    with client(data) as http:
        result = OpenSearchAdapter(http, fields=fields).search(START, END)
    assert result.logs[0].timestamp == START and result.logs[0].body == "failed"


def test_every_adapter_rejects_naive_or_reversed_windows_before_network():
    with client({}) as http:
        operations = [
            lambda: PrometheusAdapter(http).query_range("up", END, START),
            lambda: JaegerAdapter(http).search("checkout", START.replace(tzinfo=None), END),
            lambda: OpenSearchAdapter(http).search(END, START),
        ]
        for operation in operations:
            with pytest.raises(ValueError):
                operation()
