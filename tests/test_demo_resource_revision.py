"""DRP-v2 is an explicit data revision; old policies and prepared bytes stay bound."""
import json
import socket
import subprocess

import pytest

from aegis_rca.evaluation import corpus_cli
from aegis_rca.evaluation.corpus_recording import CapturePolicy, PILOT_CAPTURE_POLICY, QUERY_POLICY
from aegis_rca.evaluation.demo_environment import (
    DEMO_RESOURCE_PROFILE_V1, DEMO_RESOURCE_PROFILE_V2, PILOT_DEMO_RESOURCE_PROFILE,
    DemoResourceProfile, validate_prepared_environment,
)
from aegis_rca.evaluation.gold import DEFAULT_GOLD_RUBRIC
from aegis_rca.evaluation.gold_scoring import BenchmarkScoringProtocolV2
from aegis_rca.tools.live import LiveBackend
from aegis_rca.tools.replay import ReplayBackend
from test_corpus_recording import setup_recorder
from test_demo_environment import prepared_files


V1_DRP = "DRP-9847588deaaf02889737ffb35c7fe46bdf4ede547a8387941d0e669c897ca48b"
V1_CP = "CP-f8f78a35a67b279df75d5ad099b8fa64a00842853f9e4b45354b21b0c6c83b4e"
V2_DRP = "DRP-1cc5f2abd6914a39268b4e60b4108500b996fee7637d7f87c5b578513ebd6791"
V2_CP = "CP-348a6b0f3260f77ca5dc3dff2aa057ab9fef15aad165bef7e489076623612d67"


def test_v2_overrides_exactly_the_two_evidence_backed_services():
    assert PILOT_DEMO_RESOURCE_PROFILE is DEMO_RESOURCE_PROFILE_V2
    assert DEMO_RESOURCE_PROFILE_V2.profile_name == "pilot-checkout-product-catalog-headroom-v2"
    assert DEMO_RESOURCE_PROFILE_V2.schema_version == DEMO_RESOURCE_PROFILE_V1.schema_version == 1
    assert DEMO_RESOURCE_PROFILE_V2.otel_demo_version == "3.0.0"
    assert [(item.service, item.memory_limit_bytes, item.gomemlimit_bytes)
            for item in DEMO_RESOURCE_PROFILE_V2.service_overrides] == [
        ("checkout", 134217728, 100663296),
        ("product-catalog", 134217728, 100663296),
    ]
    assert DEMO_RESOURCE_PROFILE_V1.id == V1_DRP
    assert [item.service for item in DEMO_RESOURCE_PROFILE_V1.service_overrides] == ["checkout"]
    assert DEMO_RESOURCE_PROFILE_V2.id != V1_DRP
    assert DEMO_RESOURCE_PROFILE_V2.id == V2_DRP


def test_v2_capture_policy_changes_only_its_bound_resource_profile():
    old = CapturePolicy(demo_resource_profile_id=DEMO_RESOURCE_PROFILE_V1.id)
    assert old.id == V1_CP
    assert PILOT_CAPTURE_POLICY.id != V1_CP
    assert PILOT_CAPTURE_POLICY.id == V2_CP
    assert PILOT_CAPTURE_POLICY.demo_resource_profile_id == DEMO_RESOURCE_PROFILE_V2.id
    assert old.model_dump(exclude={"demo_resource_profile_id"}) == PILOT_CAPTURE_POLICY.model_dump(
        exclude={"demo_resource_profile_id"})
    # Loading an explicitly old policy preserves its old semantics, never aliases v2.
    assert CapturePolicy.model_validate_json(old.model_dump_json()).id == V1_CP


def test_v1_prepared_environment_still_validates_only_against_v1(tmp_path):
    compose, path, receipt = prepared_files(tmp_path, DEMO_RESOURCE_PROFILE_V1)
    assert validate_prepared_environment(compose, path,
        expected_profile_id=V1_DRP, expected_demo_version="3.0.0") == receipt
    with pytest.raises(ValueError, match="resource profile differs"):
        validate_prepared_environment(compose, path,
            expected_profile_id=DEMO_RESOURCE_PROFILE_V2.id, expected_demo_version="3.0.0")


@pytest.mark.parametrize("change", ["v1_receipt", "product_catalog_bytes"])
async def test_recorder_rejects_v1_or_altered_v2_before_any_acquisition_or_injection(
        tmp_path, monkeypatch, change):
    module, flags, args, calls, sleeps, instances = setup_recorder(
        monkeypatch, tmp_path, flag="paymentUnreachable")
    original_flags = flags.read_bytes()
    if change == "v1_receipt":
        old = tmp_path / "old-environment"
        old.mkdir()
        args.demo_compose, args.demo_environment_receipt, _ = prepared_files(
            old, DEMO_RESOURCE_PROFILE_V1)
        message = "resource profile differs"
    else:
        compose = json.loads(args.demo_compose.read_bytes())
        compose["services"]["product-catalog"]["environment"]["GOMEMLIMIT"] = "16MiB"
        args.demo_compose.write_text(json.dumps(compose), encoding="utf-8")
        message = "Compose bytes differ"
    monkeypatch.setattr("builtins.input", lambda *_: pytest.fail("Human attestation reached"))
    with pytest.raises(ValueError, match=message):
        await module.record(args)
    assert not calls and not sleeps and not instances
    assert flags.read_bytes() == original_flags
    assert not list(args.output.glob("quarantine/*/snapshot.json"))


def test_pilot03_init_is_protocol_only_and_preserves_prior_pilot_bytes(tmp_path, monkeypatch, capsys):
    old_policy = CapturePolicy(demo_resource_profile_id=DEMO_RESOURCE_PROFILE_V1.id)
    for name in ("p22-pilot-01", "p22-pilot-02"):
        folder = tmp_path / name / "protocol"
        folder.mkdir(parents=True)
        (folder / "capture-policy.json").write_text(old_policy.model_dump_json(), encoding="utf-8")
        (folder / "demo-resource-profile.json").write_text(
            DEMO_RESOURCE_PROFILE_V1.model_dump_json(), encoding="utf-8")
        (folder / "query-policy.json").write_text(QUERY_POLICY.model_dump_json(), encoding="utf-8")
        (folder / "rubric.json").write_text(DEFAULT_GOLD_RUBRIC.model_dump_json(), encoding="utf-8")
        (folder / "scoring-protocol.json").write_text(
            BenchmarkScoringProtocolV2().model_dump_json(), encoding="utf-8")
    # The earlier pre-binding pilot policy is deliberately left in its old schema.
    earliest = tmp_path / "p22-pilot-01" / "protocol" / "capture-policy.json"
    unbound = old_policy.model_dump(exclude={"demo_resource_profile_id"})
    unbound["schema_version"] = 1
    earliest.write_text(json.dumps(unbound), encoding="utf-8")
    flags = tmp_path / "flags.json"
    flags.write_bytes(b"untouched fault-control bytes")
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}

    def forbidden(*args, **kwargs):
        pytest.fail("Protocol initialization cannot start Docker, record, connect, or execute Stage C")
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(corpus_cli, "execute_corpus", forbidden)
    monkeypatch.setattr(LiveBackend, "__init__", forbidden)
    monkeypatch.setattr(ReplayBackend, "__init__", forbidden)
    output = tmp_path / "p22-pilot-03" / "protocol"
    corpus_cli.main(["init-pilot", "--output", str(output)])
    result = json.loads(capsys.readouterr().out)
    assert result["recordings_started"] == 0
    assert result["demo_resource_profile_id"] == DEMO_RESOURCE_PROFILE_V2.id
    assert result["capture_policy_id"] == PILOT_CAPTURE_POLICY.id
    profile = DemoResourceProfile.model_validate_json((output / "demo-resource-profile.json").read_bytes())
    policy = CapturePolicy.model_validate_json((output / "capture-policy.json").read_bytes())
    assert profile == DEMO_RESOURCE_PROFILE_V2
    assert policy == PILOT_CAPTURE_POLICY
    assert {p.name for p in output.iterdir()} == {
        "capture-policy.json", "demo-resource-profile.json", "query-policy.json",
        "rubric.json", "scoring-protocol.json"}
    assert {path: (tmp_path / path).read_bytes() for path in before} == before
    assert {p.relative_to(tmp_path) for p in tmp_path.rglob("*") if p.is_file()} == (
        set(before) | {p.relative_to(tmp_path) for p in output.iterdir()})
