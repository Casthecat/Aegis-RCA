# Telemetry backend contracts

These adapters query telemetry. They do not enable faults, create indexes, change retention, or execute remediation. Constructors do not contact a backend. All contract fixtures in `tests/test_adapters.py` are **hand-authored synthetic payloads**, not recordings of a running Demo. Passing them establishes parser and request contracts; live integration remains a separate check.

## Shared behavior

```python
from aegis_rca.adapters import JsonHttp, PrometheusAdapter

with JsonHttp("http://localhost:9090", timeout_seconds=5,
              max_response_bytes=4_000_000) as http:
    metrics = PrometheusAdapter(http)
    catalog = metrics.label_values("__name__")
```

`JsonHttp` uses HTTPX synchronously. An async caller can wrap a complete adapter method with `asyncio.to_thread`; its own investigation deadline must still bound the task. HTTPX connect/read/write/pool timeouts and an elapsed check between received chunks are applied. The timeout is **not a strict process cancellation guarantee**: cancelling an async waiter does not kill its worker thread. No retry is implicit. Environment proxy settings are ignored, TLS verification stays enabled, and redirects are refused. Auth can be configured through constructor headers, which are never placed in result sources.

Only relative backend paths are accepted; absolute URLs, traversal, and query fragments are refused. Transport methods are restricted to GET and POST ending in `/_search`. The decoded response has a configurable byte cap. HTTP status, timeout, byte-budget, invalid JSON, and schema errors raise `AdapterError`/`SchemaError` without copying response bodies, credentials, query text, or URLs into the error. Backends and their telemetry remain untrusted input; returned warnings and telemetry need the same redaction as other evidence before sending them to a model.

Normalized telemetry uses `MetricResult`, `TraceResult`, and `LogResult`. Their `warnings`, `truncated`, and `coverage_complete` fields must reach the investigation/report layer. `coverage_complete=None` means unknown, not complete. Neither a successful HTTP response nor zero matching rows proves that all relevant instrumentation exists or arrived. No adapter turns missing telemetry into a causal diagnosis.

All required time windows use timezone-aware datetimes and `[start, end)`. No implicit local timezone conversion is used. Query result counts are bounded; they do not silently paginate or perform extra requests. Catalog methods are capped at 10,000 returned values. Query `limit` accepts integers from 1 to 1,000.

## Prometheus

Methods:

- `label_names(start=None, end=None) -> CatalogResult`
- `label_values(label, start=None, end=None) -> CatalogResult`
- `query_range(query, start, end, step_seconds=15, limit=100) -> MetricResult`

They use `/api/v1/labels`, `/api/v1/label/{name}/values`, and `/api/v1/query_range`. `label_values("__name__")` discovers metric names; deployments should discover service labels rather than assume that every signal uses `service_name`. The adapter currently accepts ASCII label names. Query responses must be successful matrix envelopes. Upstream warnings and infos are retained. NaN and infinity samples are omitted with a warning; they never become zero. A response at the configured series limit is marked potentially truncated. More than 11,000 steps per series is rejected before the request.

Prometheus range-query `end` is inclusive. The adapter sends the requested bound and filters samples to the application's exclusive end on normalization. This avoids subtracting a floating-point epsilon or changing the query step grid. Metadata endpoints can describe series without samples in the exact supplied interval; their catalogs do not establish incident coverage. [Prometheus HTTP API](https://prometheus.io/docs/prometheus/latest/querying/api/)

## Jaeger

Methods:

- `services() -> CatalogResult`
- `search(service, start, end, limit=100, operation=None) -> TraceResult`
- `get_trace(trace_id, start=None, end=None) -> TraceResult`

Only the public v3 routes are used: `/api/v3/services`, `/api/v3/traces`, and `/api/v3/traces/{traceId}`. Search uses `query.serviceName`, `query.startTimeMin`, `query.startTimeMax`, `query.searchDepth`, and `query.rawTraces=true`. Get uses `startTime`, `endTime`, and `rawTraces=true`. No internal legacy `/api/traces` or `/api/services` route is used. The upper search bound is exclusive and `searchDepth` can be approximate depending on storage. [Public API contract](https://github.com/jaegertracing/jaeger-idl/blob/main/swagger/api_v3/query_service.openapi.yaml)

The Demo 3.0.0 pins Jaeger 2.19.0. Its HTTP gateway combines returned traces into a `{"result": {"resourceSpans": [...]}}` envelope; it does not currently stream those traces. Its upstream schema also describes streaming behavior. The parser accepts one envelope or consecutive newline/whitespace-separated result envelopes. An error envelope or incomplete tail fails the whole request so an earlier successful chunk cannot masquerade as complete evidence. The pinned gateway returns HTTP 404 for an empty trace search; this is surfaced as an error because a status alone cannot distinguish empty storage from an unavailable endpoint. [Pinned gateway implementation](https://github.com/jaegertracing/jaeger/blob/v2.19.0/cmd/jaeger/internal/extension/jaegerquery/internal/apiv3/http_gateway.go)

Spans are read from resource/scope groups. Hexadecimal IDs, numeric kind/status enums, and decimal nanosecond timestamp strings follow OTLP JSON. `duration_ms` is computed from integer nanoseconds. Python datetimes retain microseconds, so the original start/end nanoseconds are retained under `attributes["aegis.otlp"]`; nanosecond boundary comparisons occur before conversion. Resource attributes, span attributes, events, and the status message remain available for diagnosis. Dropped OTLP data, missing service identity, local caps, or clipped cross-boundary spans produce warnings. Trace search depth is a trace bound; `JaegerAdapter(..., max_spans=10000)` separately bounds normalized spans. [OTLP JSON encoding](https://opentelemetry.io/docs/specs/otlp/#json-protobuf-encoding)

## OpenSearch

Methods:

- `mapping() -> dict`
- `search(start, end, service=None, text=None, trace_id=None, limit=100) -> LogResult`
- `context(trace_id, start, end, service=None, limit=100) -> LogResult`

`context` is a bounded trace-ID-correlated search, not an automatic fetch of every surrounding log. `search` issues one POST `/{index}/_search`. It combines a timestamp range, exact service/trace filters, and optional lexical `match` text with AND semantics. It requests `limit + 1` hits to detect a full page, tracks total hits, sorts by timestamp, and applies a server search timeout. Text is never interpreted as raw query DSL or `query_string` syntax. No vector plugin is required.

Defaults are verified against Demo **3.0.0**, whose observability collector configuration sends logs to the `otel-logs` prefix with a `yyyy-MM-dd` suffix and uses Collector **0.157.0**. [Pinned Demo collector exporter configuration](https://github.com/open-telemetry/opentelemetry-demo/blob/3.0.0/src/otel-collector/otelcol-config-observability.yml), [pinned image versions](https://github.com/open-telemetry/opentelemetry-demo/blob/3.0.0/.env)

| Adapter setting | Default |
| --- | --- |
| Index pattern | `otel-logs-*` |
| Timestamp source/query field | `@timestamp` |
| Service source field | `resource.service.name` |
| Service exact query field | `resource.service.name.keyword` |
| Body source/query field | `body` |
| Severity source field | `severity.text` |
| Trace source / exact query field | `traceId` / `traceId.keyword` |
| Span source field | `spanId` |
| Attributes source field | `attributes` |

The Collector's default SS4O encoding stores resource keys such as `service.name` literally inside the `resource` object. Source extraction supports both those literal dotted keys and equivalent nested objects. The source field layout is verified in the pinned encoder/model. The `.keyword` exact fields assume OpenSearch dynamic string mapping; inspect `mapping()` on the running index and override `OpenSearchFields` if an index template instead maps the base field directly as keyword. Field mapping is explicit rather than silently probing alternate fields after an empty response. [Pinned SS4O model](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/v0.157.0/exporter/opensearchexporter/sso_model.go), [pinned encoder](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/v0.157.0/exporter/opensearchexporter/encoder.go)

OpenSearch `timed_out`, failed shards, early termination, lower-bound totals, and truncation are visible. Missing required source fields or malformed hit/shard envelopes raise a schema error. An empty result is accepted only as an empty query result with unknown instrumentation coverage. Alternative source paths and ISO/epoch-millisecond/epoch-second timestamps can be configured using `OpenSearchFields`; raw timestamp text is retained with the record's index metadata.

## Verification remaining for a live deployment

Run the repository's environment diagnostics, start the pinned observability services, inspect metric catalogs and OpenSearch mappings, and query a known incident window. Save sanitized responses with their actual source provenance before treating them as replay fixtures. A genuine live check should report backend versions, requested time bounds, result counts, warnings, and whether the returned events can be cross-correlated by trace ID. The synthetic contract tests do not satisfy that acceptance criterion.
