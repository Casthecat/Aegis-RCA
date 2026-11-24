from datetime import datetime, timezone
import json

import pytest
from pydantic import ValidationError

from aegis_rca.models.telemetry import SpanRecord
from aegis_rca.observations import observe_spans as canonical_observe_spans
from aegis_rca.tools.normalize import normalize_spans, observe_spans


STAMP = datetime(2026, 9, 8, 14, 30, tzinfo=timezone.utc)
TRACE = "1" * 32
SPAN = "2" * 16
PARENT = "3" * 16
SERVICE_MAP = {"oteldemo.PaymentService": "payment"}

P1_1_CANONICAL_OBSERVATIONS = json.loads(r"""
[
  {
    "correlation": {
      "parent_span_id": "3333333333333333",
      "rpc_edge_key": "rpc-edge-63ae0d9da8b44b6bcd5d",
      "span_id": "aaaaaaaaaaaaaaaa",
      "trace_id": "11111111111111111111111111111111"
    },
    "id": "O-trace-91a039f19b0ab3aa72bb",
    "kind": "rpc_outcome",
    "provenance": {
      "backend": "jaeger",
      "derivation": "rpc_span_observation",
      "derivation_version": 1,
      "field_paths": [
        "attributes.peer.service",
        "attributes.rpc.grpc.status_code",
        "attributes.rpc.service",
        "kind",
        "name",
        "parent_span_id",
        "service",
        "span_id",
        "start",
        "status",
        "trace_id"
      ],
      "locator": "same-trace",
      "span_id": "aaaaaaaaaaaaaaaa",
      "synthetic": false,
      "trace_id": "11111111111111111111111111111111"
    },
    "quality": {
      "ambiguous": true,
      "peer_candidates": [
        {
          "field_path": "attributes.peer.service",
          "method": "explicit_peer",
          "service": "shipping"
        },
        {
          "field_path": "attributes.rpc.service",
          "method": "rpc_service_map",
          "service": "payment"
        },
        {
          "field_path": "name",
          "method": "span_name_map",
          "service": "payment"
        }
      ],
      "peer_resolution_method": "explicit_peer",
      "rpc_status_explicit": true,
      "warnings": [
        "Peer resolution sources disagree; deterministic precedence was applied."
      ]
    },
    "schema_version": 1,
    "service": "checkout",
    "signal": "trace",
    "summary": "checkout client RPC oteldemo.PaymentService/Charge: OTLP=ERROR; gRPC=UNAVAILABLE; outcome=failure; peer=shipping",
    "timestamp": "2026-09-08T14:30:00Z",
    "value": {
      "operation": "oteldemo.PaymentService/Charge",
      "otlp_status": "ERROR",
      "outcome": "failure",
      "peer_service": "shipping",
      "rpc_status_code": 14,
      "rpc_status_field": "attributes.rpc.grpc.status_code",
      "rpc_status_name": "UNAVAILABLE",
      "rpc_status_raw": 14,
      "rpc_status_sources": [
        {
          "code": 14,
          "field_path": "attributes.rpc.grpc.status_code",
          "name": "UNAVAILABLE",
          "raw_value": 14
        }
      ],
      "rpc_system": null,
      "span_kind": "CLIENT"
    }
  },
  {
    "correlation": {
      "parent_span_id": "3333333333333333",
      "rpc_edge_key": null,
      "span_id": "bbbbbbbbbbbbbbbb",
      "trace_id": "11111111111111111111111111111111"
    },
    "id": "O-trace-2af34a596f3c1521282e",
    "kind": "rpc_outcome",
    "provenance": {
      "backend": "jaeger",
      "derivation": "rpc_span_observation",
      "derivation_version": 1,
      "field_paths": [
        "attributes.rpc.grpc.status_code",
        "attributes.rpc.service",
        "kind",
        "name",
        "parent_span_id",
        "service",
        "span_id",
        "start",
        "status",
        "trace_id"
      ],
      "locator": "same-trace",
      "span_id": "bbbbbbbbbbbbbbbb",
      "synthetic": false,
      "trace_id": "11111111111111111111111111111111"
    },
    "quality": {
      "ambiguous": false,
      "peer_candidates": [],
      "peer_resolution_method": "unresolved",
      "rpc_status_explicit": true,
      "warnings": []
    },
    "schema_version": 1,
    "service": "payment",
    "signal": "trace",
    "summary": "payment server RPC oteldemo.PaymentService/Charge: OTLP=ERROR; gRPC=INTERNAL; outcome=failure; peer=unknown",
    "timestamp": "2026-09-08T14:30:00Z",
    "value": {
      "operation": "oteldemo.PaymentService/Charge",
      "otlp_status": "ERROR",
      "outcome": "failure",
      "peer_service": null,
      "rpc_status_code": 13,
      "rpc_status_field": "attributes.rpc.grpc.status_code",
      "rpc_status_name": "INTERNAL",
      "rpc_status_raw": 13,
      "rpc_status_sources": [
        {
          "code": 13,
          "field_path": "attributes.rpc.grpc.status_code",
          "name": "INTERNAL",
          "raw_value": 13
        }
      ],
      "rpc_system": null,
      "span_kind": "SERVER"
    }
  }
]
""")


def rpc_span(
    *,
    span_id=SPAN,
    kind="CLIENT",
    otlp_status="UNSET",
    rpc_status=None,
    response_status=None,
    attributes=None,
    name="oteldemo.PaymentService/Charge",
    source="same-trace",
    service="checkout",
):
    attrs = {"rpc.service": "oteldemo.PaymentService"}
    if rpc_status is not None:
        attrs["rpc.grpc.status_code"] = rpc_status
    if response_status is not None:
        attrs["rpc.response.status_code"] = response_status
    attrs.update(attributes or {})
    return SpanRecord(
        trace_id=TRACE,
        span_id=span_id,
        parent_span_id=PARENT,
        service=service,
        name=name,
        kind=kind,
        start=STAMP,
        duration_ms=1,
        status=otlp_status,
        attributes=attrs,
        source=source,
    )


def test_p1_1_frozen_canonical_serialization_and_public_imports():
    spans = (
        rpc_span(
            span_id="a" * 16,
            rpc_status=14,
            otlp_status="ERROR",
            attributes={"peer.service": "shipping"},
        ),
        rpc_span(
            span_id="b" * 16,
            service="payment",
            kind="SERVER",
            rpc_status=13,
            otlp_status="ERROR",
        ),
    )
    facade = [record.model_dump(mode="json") for record in observe_spans(spans, SERVICE_MAP)]
    canonical = [
        record.model_dump(mode="json")
        for record in canonical_observe_spans(spans, SERVICE_MAP)
    ]
    assert facade == P1_1_CANONICAL_OBSERVATIONS
    assert canonical == P1_1_CANONICAL_OBSERVATIONS


@pytest.mark.parametrize(
    "rpc_status,otlp_status,expected_outcome,expected_name",
    [
        (None, "OK", "success", None),
        (None, "ERROR", "failure", None),
        (None, "UNSET", "unknown", None),
        (None, "custom", "unknown", None),
        (0, "OK", "success", "OK"),
        (0, "ERROR", "inconsistent", "OK"),
        (0, "UNSET", "success", "OK"),
        (0, "custom", "success", "OK"),
        (2, "OK", "inconsistent", "UNKNOWN"),
        (2, "ERROR", "failure", "UNKNOWN"),
        (2, "UNSET", "failure", "UNKNOWN"),
        (2, "custom", "failure", "UNKNOWN"),
        (13, "OK", "inconsistent", "INTERNAL"),
        (13, "ERROR", "failure", "INTERNAL"),
        (13, "UNSET", "failure", "INTERNAL"),
        (14, "OK", "inconsistent", "UNAVAILABLE"),
        (14, "ERROR", "failure", "UNAVAILABLE"),
        (14, "UNSET", "failure", "UNAVAILABLE"),
        (99, "OK", "unknown", "UNRECOGNIZED"),
    ],
)
def test_explicit_rpc_status_by_otlp_status_outcome_matrix(
    rpc_status, otlp_status, expected_outcome, expected_name
):
    record = observe_spans(
        (rpc_span(rpc_status=rpc_status, otlp_status=otlp_status),), SERVICE_MAP
    )[0]
    assert record.value.outcome == expected_outcome
    assert record.value.rpc_status_name == expected_name
    assert record.quality.rpc_status_explicit is (rpc_status is not None)
    if rpc_status == 2:
        assert record.value.rpc_status_name == "UNKNOWN"
        assert record.value.outcome != "unknown"


@pytest.mark.parametrize(
    "raw,expected_code,expected_name",
    [
        ("OK", 0, "OK"),
        ("2", 2, "UNKNOWN"),
        ("INTERNAL", 13, "INTERNAL"),
        ("UNAVAILABLE", 14, "UNAVAILABLE"),
        (99, 99, "UNRECOGNIZED"),
        ("vendor-status", None, "UNRECOGNIZED"),
    ],
)
def test_numeric_named_and_unrecognized_rpc_statuses_are_preserved(
    raw, expected_code, expected_name
):
    record = observe_spans((rpc_span(rpc_status=raw, otlp_status="ERROR"),), SERVICE_MAP)[0]
    assert record.value.rpc_status_raw == raw
    assert record.value.rpc_status_code == expected_code
    assert record.value.rpc_status_name == expected_name
    assert record.quality.rpc_status_explicit


def test_missing_rpc_status_with_otlp_error_is_failure_with_unknown_rpc_status():
    record = observe_spans((rpc_span(otlp_status="ERROR"),), SERVICE_MAP)[0]
    assert record.value.outcome == "failure"
    assert record.value.rpc_status_name is None
    assert record.value.rpc_status_code is None
    assert record.quality.rpc_status_explicit is False


def test_conflicting_explicit_rpc_status_fields_are_inconsistent_and_preserved():
    record = observe_spans(
        (rpc_span(rpc_status=14, response_status="OK", otlp_status="ERROR"),), SERVICE_MAP
    )[0]
    assert record.value.outcome == "inconsistent"
    assert record.value.rpc_status_name == "UNAVAILABLE"
    assert [(item.field_path, item.name) for item in record.value.rpc_status_sources] == [
        ("attributes.rpc.grpc.status_code", "UNAVAILABLE"),
        ("attributes.rpc.response.status_code", "OK"),
    ]
    assert any("Conflicting explicit RPC status" in warning for warning in record.quality.warnings)


def test_client_server_success_and_failure_are_observed_without_diagnostic_labels():
    records = observe_spans(
        (
            rpc_span(span_id="4" * 16, rpc_status=0, otlp_status="OK"),
            rpc_span(span_id="5" * 16, kind="SERVER", rpc_status=13, otlp_status="ERROR"),
            rpc_span(span_id="6" * 16, rpc_status=2, otlp_status="ERROR"),
        ),
        SERVICE_MAP,
    )
    assert [(record.value.span_kind, record.value.outcome) for record in records] == [
        ("CLIENT", "success"),
        ("SERVER", "failure"),
        ("CLIENT", "failure"),
    ]
    assert {record.value.rpc_status_name for record in records} == {"OK", "INTERNAL", "UNKNOWN"}


def test_peer_resolution_conflicts_are_visible_while_precedence_is_deterministic():
    record = observe_spans(
        (
            rpc_span(
                rpc_status=14,
                attributes={"peer.service": "shipping"},
            ),
        ),
        SERVICE_MAP,
    )[0]
    assert record.value.peer_service == "shipping"
    assert record.quality.peer_resolution_method == "explicit_peer"
    assert record.quality.ambiguous is True
    assert {(candidate.method, candidate.service) for candidate in record.quality.peer_candidates} == {
        ("explicit_peer", "shipping"),
        ("rpc_service_map", "payment"),
        ("span_name_map", "payment"),
    }
    assert any("sources disagree" in warning for warning in record.quality.warnings)
    assert {
        "attributes.peer.service",
        "attributes.rpc.service",
        "name",
    } <= set(record.provenance.field_paths)


def test_unresolved_peer_preserves_observation_and_quality():
    span = rpc_span(
        rpc_status=2,
        name="custom/Call",
        attributes={"rpc.service": "unmapped.Service"},
    )
    record = observe_spans((span,), SERVICE_MAP)[0]
    assert record.value.peer_service is None
    assert record.quality.peer_resolution_method == "unresolved"
    assert record.quality.ambiguous is False


def test_server_local_rpc_identity_does_not_fabricate_a_self_peer_or_edge():
    record = observe_spans(
        (
            rpc_span(
                service="payment",
                kind="SERVER",
                rpc_status=13,
                otlp_status="ERROR",
                name="oteldemo.PaymentService/Charge",
                attributes={"rpc.service": "oteldemo.PaymentService"},
            ),
        ),
        SERVICE_MAP,
    )[0]
    assert record.service == "payment"
    assert record.value.peer_service is None
    assert record.quality.peer_resolution_method == "unresolved"
    assert record.quality.peer_candidates == ()
    assert record.correlation.rpc_edge_key is None
    assert "attributes.rpc.service" in record.provenance.field_paths
    assert "name" in record.provenance.field_paths


def test_server_explicit_remote_peer_orients_edge_from_caller_to_local_service():
    client = observe_spans(
        (rpc_span(rpc_status=13, otlp_status="ERROR"),), SERVICE_MAP
    )[0]
    server = observe_spans(
        (
            rpc_span(
                service="payment",
                kind="SERVER",
                rpc_status=13,
                otlp_status="ERROR",
                attributes={"peer.service": "checkout"},
            ),
        ),
        SERVICE_MAP,
    )[0]
    assert server.value.peer_service == "checkout"
    assert server.quality.peer_resolution_method == "explicit_peer"
    assert {(candidate.method, candidate.service) for candidate in server.quality.peer_candidates} == {
        ("explicit_peer", "checkout"),
    }
    assert server.correlation.rpc_edge_key == client.correlation.rpc_edge_key


def test_observation_identity_is_stable_and_separate_from_display_and_legacy_id():
    first = rpc_span(span_id="7" * 16, rpc_status=14, otlp_status="ERROR")
    second = rpc_span(span_id="8" * 16, rpc_status=0, otlp_status="OK")
    forward = observe_spans((first, second), SERVICE_MAP)
    backward = observe_spans((second, first), SERVICE_MAP)
    assert {item.id for item in forward} == {item.id for item in backward}
    assert forward[0].model_copy(update={"summary": "different display text"}).id == forward[0].id
    changed = observe_spans(
        (first.model_copy(update={"name": "oteldemo.PaymentService/Refund"}),), SERVICE_MAP
    )[0]
    assert changed.id != forward[0].id
    changed_source = observe_spans(
        (first.model_copy(update={"source": "another-locator"}),), SERVICE_MAP
    )[0]
    changed_status = observe_spans(
        (
            first.model_copy(update={
                "attributes": {**first.attributes, "rpc.grpc.status_code": 13}
            }),
        ),
        SERVICE_MAP,
    )[0]
    assert changed_source.id != forward[0].id
    assert changed_status.id != forward[0].id
    legacy = normalize_spans((first,), SERVICE_MAP)[0]
    assert forward[0].id.startswith("O-trace-")
    assert legacy.id.startswith("E-trace-")
    assert forward[0].id != legacy.id


def test_observation_record_and_nested_values_are_immutable():
    record = observe_spans((rpc_span(rpc_status=0, otlp_status="OK"),), SERVICE_MAP)[0]
    with pytest.raises(ValidationError):
        record.summary = "mutated"
    with pytest.raises(ValidationError):
        record.value.outcome = "failure"


def test_legacy_unavailable_projection_preserves_complete_serialized_contract():
    span = rpc_span(rpc_status=14, otlp_status="ERROR")
    actual = normalize_spans((span,), SERVICE_MAP)[0].model_dump(mode="json")
    assert actual == {
        "id": "E-trace-c8b1a6fc967ad4b63563",
        "signal": "trace",
        "service": "checkout",
        "timestamp": "2026-09-08T14:30:00Z",
        "observation": {"code": "rpc_unavailable", "peer_service": "payment"},
        "excerpt": (
            "checkout client RPC oteldemo.PaymentService/Charge: "
            "gRPC UNAVAILABLE; peer=payment"
        ),
        "source": {
            "backend": "jaeger",
            "locator": "same-trace",
            "synthetic": False,
        },
        "correlation_group": TRACE,
    }


def test_legacy_projection_excludes_other_statuses_and_server_spans():
    records = (
        rpc_span(span_id="4" * 16, rpc_status=0, otlp_status="OK"),
        rpc_span(span_id="5" * 16, rpc_status=2, otlp_status="ERROR"),
        rpc_span(span_id="6" * 16, rpc_status=13, otlp_status="ERROR"),
        rpc_span(span_id="7" * 16, kind="SERVER", rpc_status=14, otlp_status="ERROR"),
    )
    assert normalize_spans(records, SERVICE_MAP) == ()


def test_non_rpc_spans_are_not_forced_into_rpc_observations():
    span = rpc_span(name="internal-work", attributes={}).model_copy(
        update={"attributes": {}, "kind": "INTERNAL"}
    )
    assert observe_spans((span,), SERVICE_MAP) == ()
