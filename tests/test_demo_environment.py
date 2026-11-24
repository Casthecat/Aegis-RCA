"""Resource preparation is deterministic, explicit and does not run a pilot."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from aegis_rca.evaluation.demo_environment import (
    DemoResourceProfile, DemoServiceResourceOverride, PILOT_DEMO_RESOURCE_PROFILE,
    DEMO_RESOURCE_PROFILE_V1,
    PreparedDemoEnvironment, SOURCE_ARCHIVE_URL, SOURCE_REPOSITORY,
    apply_resource_profile, build_prepared_environment, sha256,
    validate_prepared_environment,
)


def upstream_config():
    return {"name": "opentelemetry-demo", "networks": {"default": {}}, "services": {
        "checkout": {
            "image": "ghcr.io/open-telemetry/demo:3.0.0-checkout",
            "deploy": {"resources": {"limits": {"memory": "20971520", "cpus": "0.5"},
                                      "reservations": {"memory": "1048576"}}},
            "environment": {"GOMEMLIMIT": "16MiB", "OTHER": "preserved"},
            "command": ["./checkout"], "healthcheck": {"interval": "5s"},
            "volumes": [{"source": "other", "target": "/data"}],
        },
        "product-catalog": {
            "image": "ghcr.io/open-telemetry/demo:3.0.0-product-catalog",
            "deploy": {"resources": {"limits": {"memory": "20971520"}}},
            "environment": {"GOMEMLIMIT": "16MiB"},
        },
        "unrelated": {"deploy": {"resources": {"limits": {"memory": "1G", "pids": 100}}},
                      "environment": {"JAVA_TOOL_OPTIONS": "-Xmx256m"}},
    }}


def marker_bytes():
    return json.dumps({"repository": SOURCE_REPOSITORY, "tag": "3.0.0",
                       "archive_url": SOURCE_ARCHIVE_URL}, indent=2).encode()


def prepared_files(tmp_path, profile=PILOT_DEMO_RESOURCE_PROFILE):
    compose = tmp_path / "compose.json"
    receipt_path = tmp_path / "compose.provenance.json"
    raw = json.dumps(upstream_config()).encode()
    compose_bytes = json.dumps(apply_resource_profile(upstream_config(), profile)).encode()
    receipt = build_prepared_environment(profile, compose_bytes, marker_bytes(),
                                        b"upstream YAML", b"observability YAML", raw)
    compose.write_bytes(compose_bytes)
    receipt_path.write_text(receipt.model_dump_json(), encoding="utf-8")
    return compose, receipt_path, receipt


@pytest.mark.parametrize("profile,services", [
    (DEMO_RESOURCE_PROFILE_V1, ("checkout",)),
    (PILOT_DEMO_RESOURCE_PROFILE, ("checkout", "product-catalog")),
])
def test_profile_changes_only_selected_resource_fields_and_preserves_input(profile, services):
    original = upstream_config()
    before = deepcopy(original)
    result = apply_resource_profile(original, profile)
    expected = deepcopy(before)
    for service in services:
        expected["services"][service]["deploy"]["resources"]["limits"]["memory"] = "134217728"
        expected["services"][service]["environment"]["GOMEMLIMIT"] = "100663296B"
    assert result == expected
    assert original == before
    if "product-catalog" not in services:
        assert result["services"]["product-catalog"] == before["services"]["product-catalog"]
    assert result["services"]["unrelated"] == before["services"]["unrelated"]


@pytest.mark.parametrize("change", ["memory", "go", "services"])
def test_resource_identity_binds_every_override(change):
    data = PILOT_DEMO_RESOURCE_PROFILE.model_dump()
    overrides = list(data["service_overrides"])
    if change == "memory":
        overrides[0]["memory_limit_bytes"] += 1024 ** 2
    elif change == "go":
        overrides[0]["gomemlimit_bytes"] -= 1024 ** 2
    else:
        overrides.append({"service": "synthetic-only", "memory_limit_bytes": 100,
                          "gomemlimit_bytes": 50})
    data["service_overrides"] = overrides
    assert DemoResourceProfile.model_validate(data).id != PILOT_DEMO_RESOURCE_PROFILE.id


def test_profile_override_order_is_canonical_and_duplicate_services_rejected():
    first = PILOT_DEMO_RESOURCE_PROFILE.service_overrides[0]
    second = DemoServiceResourceOverride(service="synthetic-only", memory_limit_bytes=100,
                                        gomemlimit_bytes=50)
    a = DemoResourceProfile(profile_name="synthetic", service_overrides=(first, second))
    b = DemoResourceProfile(profile_name="synthetic", service_overrides=(second, first))
    assert a.id == b.id
    with pytest.raises(ValidationError):
        DemoResourceProfile(profile_name="synthetic", service_overrides=(first, first))
    with pytest.raises(ValidationError):
        DemoServiceResourceOverride(service="checkout", memory_limit_bytes="128MiB", gomemlimit_bytes=1)


def test_prepared_receipt_round_trip_binds_profile_source_and_compose(tmp_path):
    compose, path, receipt = prepared_files(tmp_path)
    actual = validate_prepared_environment(compose, path,
        expected_profile_id=PILOT_DEMO_RESOURCE_PROFILE.id, expected_demo_version="3.0.0")
    assert actual == receipt == PreparedDemoEnvironment.model_validate_json(path.read_bytes())
    assert actual.upstream_source_marker_sha256 == sha256(marker_bytes())
    assert actual.generated_compose_sha256 == sha256(compose.read_bytes())
    assert actual.upstream_source_marker.tag == "3.0.0"


@pytest.mark.parametrize("failure", ["missing", "profile", "version", "compose", "receipt_version", "declared_profile"])
def test_prepared_environment_rejects_unbound_or_changed_material(tmp_path, failure):
    compose, path, _ = prepared_files(tmp_path)
    expected = {"expected_profile_id": PILOT_DEMO_RESOURCE_PROFILE.id, "expected_demo_version": "3.0.0"}
    if failure == "missing":
        path.unlink()
    elif failure == "profile":
        expected["expected_profile_id"] = "DRP-" + "0" * 64
    elif failure == "version":
        expected["expected_demo_version"] = "3.0.1"
    elif failure == "compose":
        compose.write_bytes(compose.read_bytes() + b"\n")
    else:
        data = json.loads(path.read_bytes())
        data["otel_demo_version" if failure == "receipt_version" else "demo_resource_profile_id"] = (
            "3.0.1" if failure == "receipt_version" else "DRP-" + "0" * 64)
        path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises((ValueError, OSError)):
        validate_prepared_environment(compose, path, **expected)


def test_receipt_cannot_claim_missing_overrides_or_unpinned_source(tmp_path):
    args = (PILOT_DEMO_RESOURCE_PROFILE, json.dumps(upstream_config()).encode(),
            marker_bytes(), b"yaml", b"yaml", b"resolved")
    with pytest.raises(ValueError, match="resources differ"):
        build_prepared_environment(*args)
    valid_bytes = json.dumps(apply_resource_profile(upstream_config(), PILOT_DEMO_RESOURCE_PROFILE)).encode()
    bad_marker = json.loads(marker_bytes())
    bad_marker["tag"] = "latest"
    with pytest.raises(ValidationError):
        build_prepared_environment(PILOT_DEMO_RESOURCE_PROFILE, valid_bytes,
                                   json.dumps(bad_marker).encode(), b"yaml", b"yaml", b"resolved")


def demo_module(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("profile_demo", Path(__file__).parents[1] / "scripts" / "demo.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = tmp_path / "source"
    source.mkdir()
    (source / ".aegis-source.json").write_bytes(marker_bytes())
    (source / "compose.yaml").write_bytes(b"upstream YAML")
    (source / "compose.observability.yaml").write_bytes(b"upstream observability YAML")
    flags = tmp_path / "flags"
    flags.mkdir()
    (flags / "flags.json").write_bytes(b"original flag bytes")
    monkeypatch.setattr(module, "SOURCE", source)
    monkeypatch.setattr(module, "FLAGS", flags)
    monkeypatch.setattr(module, "COMPOSE", tmp_path / "compose.json")
    commands = []
    def docker(*args, **kwargs):
        commands.append(args)
        assert args[-3:] == ("config", "--format", "json")
        return SimpleNamespace(stdout=json.dumps(upstream_config()))
    monkeypatch.setattr(module, "docker", docker)
    return module, commands


def test_prepare_profile_is_explicit_and_does_not_start_or_inject(tmp_path, monkeypatch):
    module, commands = demo_module(tmp_path, monkeypatch)
    receipt_path = tmp_path / "compose.provenance.json"
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(PILOT_DEMO_RESOURCE_PROFILE.model_dump_json(), encoding="utf-8")
    module.prepare()
    unprofiled = json.loads(module.COMPOSE.read_bytes())
    assert not receipt_path.exists()
    assert unprofiled["services"]["checkout"]["environment"]["GOMEMLIMIT"] == "16MiB"
    assert unprofiled["services"]["checkout"]["deploy"]["resources"]["limits"]["memory"] == "20971520"
    assert unprofiled["services"]["product-catalog"]["environment"]["GOMEMLIMIT"] == "16MiB"
    assert unprofiled["services"]["product-catalog"]["deploy"]["resources"]["limits"]["memory"] == "20971520"
    receipt = module.prepare(profile_path)
    profiled = json.loads(module.COMPOSE.read_bytes())
    assert profiled == apply_resource_profile(unprofiled, PILOT_DEMO_RESOURCE_PROFILE)
    assert receipt == validate_prepared_environment(module.COMPOSE, receipt_path,
        expected_profile_id=PILOT_DEMO_RESOURCE_PROFILE.id, expected_demo_version="3.0.0")
    assert len(commands) == 2
    assert (module.FLAGS / "flags.json").read_bytes() == b"original flag bytes"
    assert not (tmp_path / "recordings").exists()
    # Returning to an engineering environment cannot reuse profiled provenance.
    module.prepare()
    assert not receipt_path.exists()


def test_prepare_receipt_publication_failure_is_fail_closed(tmp_path, monkeypatch):
    module, _ = demo_module(tmp_path, monkeypatch)
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(PILOT_DEMO_RESOURCE_PROFILE.model_dump_json(), encoding="utf-8")
    module.prepare(profile_path)
    receipt_path = tmp_path / "compose.provenance.json"
    atomic = module._atomic_write
    def fail_receipt(path, data):
        if path == receipt_path:
            raise OSError("simulated interrupted receipt publication")
        return atomic(path, data)
    monkeypatch.setattr(module, "_atomic_write", fail_receipt)
    with pytest.raises(OSError):
        module.prepare(profile_path)
    assert not receipt_path.exists()
    with pytest.raises(OSError):
        validate_prepared_environment(module.COMPOSE, receipt_path,
            expected_profile_id=PILOT_DEMO_RESOURCE_PROFILE.id, expected_demo_version="3.0.0")
