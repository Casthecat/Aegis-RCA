"""Persisted attempt consistency and pre-migration structural ID goldens."""

from datetime import datetime, timedelta, timezone
import json

import pytest
from pydantic import ValidationError

from aegis_rca.models.acquisition import AttemptRecord, TaskSpec
from aegis_rca.models.identity import structural_id
from test_acquisition_identity import attempt, metric_spec


VALID_PAIRS = {
    ("ok", "none"), ("empty", "none"),
    ("error", "backend_error"), ("error", "invalid_result"),
    ("timeout", "tool_timeout"), ("timeout", "global_deadline"),
}


@pytest.mark.parametrize("status", ["ok", "empty", "error", "timeout"])
@pytest.mark.parametrize("error_class", ["none", "backend_error", "invalid_result", "tool_timeout", "global_deadline"])
def test_attempt_status_error_matrix_on_construction_and_restoration(status, error_class):
    payload = {**attempt().model_dump(mode="json"), "tool_status": status, "error_class": error_class}
    if (status, error_class) in VALID_PAIRS:
        record = AttemptRecord(**payload)
        assert AttemptRecord.model_validate_json(json.dumps(payload)) == record
        assert AttemptRecord.model_validate_json(record.model_dump_json()) == record
        assert record.id == "A-562da0510c8cf7df34598c70d683f2a59d6dbf28881b5d2fafa05f9489b847b6"
    else:
        with pytest.raises(ValidationError, match="error_class is inconsistent with tool_status"):
            AttemptRecord(**payload)
        with pytest.raises(ValidationError, match="error_class is inconsistent with tool_status"):
            AttemptRecord.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize("status", ["ok", "empty", "error", "timeout"])
def test_default_error_class_cannot_bypass_status_consistency(status):
    payload = {**attempt().model_dump(mode="json"), "tool_status": status}
    del payload["error_class"]
    if status in {"ok", "empty"}:
        assert AttemptRecord(**payload).error_class == "none"
        assert AttemptRecord.model_validate_json(json.dumps(payload)).error_class == "none"
    else:
        with pytest.raises(ValidationError, match="error_class is inconsistent with tool_status"):
            AttemptRecord(**payload)
        with pytest.raises(ValidationError, match="error_class is inconsistent with tool_status"):
            AttemptRecord.model_validate_json(json.dumps(payload))


def test_task_and_attempt_ids_match_pre_migration_goldens():
    spec = metric_spec()
    assert spec.id == "T-497c07dbb2521cfe5241e981d3ccb36f87a75cf3e39332f738ac62c30ddd425d"
    assert TaskSpec.model_validate_json(spec.model_dump_json()).id == spec.id
    assert attempt().id == "A-562da0510c8cf7df34598c70d683f2a59d6dbf28881b5d2fafa05f9489b847b6"
    assert attempt(ordinal=2).id == "A-b0dfd7088afb90ffbe58144c924e9336d4c2b80da32a5b6379e9dc044cdd0ef2"


def test_neutral_encoder_preserves_pre_migration_canonical_encoding():
    payload = {
        "unicode": "\u652f\u4ed8",
        "nested": {"zero": -0.0, "float": 1.5, "items": (1, True, None)},
        "timestamp": datetime(2026, 1, 2, 3, 4, 5, 6, tzinfo=timezone(timedelta(hours=8))),
    }
    golden = "X-e222db710e2212df2aa628c24e997ee15901103e3d6f50b8ef8d4743e99eca7f"
    assert structural_id("X-", payload) == golden
    equivalent = {
        "timestamp": payload["timestamp"].astimezone(timezone.utc),
        "nested": {"items": [1, True, None], "float": 1.5, "zero": 0.0},
        "unicode": payload["unicode"],
    }
    assert structural_id("X-", equivalent) == golden
    assert structural_id("X-", {"value": 1}) != structural_id("X-", {"value": 1.0})
