import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from aegis_rca.cli import run
from aegis_rca.fixtures import demo_incident
from aegis_rca.tools.snapshot import TelemetrySnapshot, write_snapshot


async def test_existing_output_fails_before_model_or_backend_work(monkeypatch, tmp_path):
    target = tmp_path / "prior"
    target.mkdir()
    prior = target / "llm_usage.json"
    prior.write_text("original")
    args = SimpleNamespace(output=target, llm=True, mode="live")
    monkeypatch.setattr("aegis_rca.cli.model_runtime", lambda _: pytest.fail("Must reject output first"))
    with pytest.raises(FileExistsError):
        await run(args)
    assert prior.read_text() == "original"


def test_snapshot_exclusive_create_preserves_previous_bytes(tmp_path):
    target = tmp_path / "snapshot.json"
    target.write_text("previous")
    snapshot = TelemetrySnapshot(incident=demo_incident(), provenance="synthetic_wire_fixture")
    with pytest.raises(FileExistsError):
        write_snapshot(snapshot, target)
    assert target.read_text() == "previous"


def test_demo_pin_and_both_flag_mounts(tmp_path, monkeypatch):
    path = Path(__file__).parents[1] / "scripts" / "demo.py"
    spec = importlib.util.spec_from_file_location("demo_controller", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = tmp_path / "source"
    source.mkdir()
    (source / ".aegis-source.json").write_text("{}")
    flags = tmp_path / "flags"
    flags.mkdir()
    output = tmp_path / "compose.json"
    monkeypatch.setattr(module, "SOURCE", source)
    monkeypatch.setattr(module, "FLAGS", flags)
    monkeypatch.setattr(module, "COMPOSE", output)
    def docker(*args, **kwargs):
        assert kwargs["environment"]["DEMO_VERSION"] == "3.0.0"
        return SimpleNamespace(stdout=json.dumps({"networks": {"default": {}}, "services": {
            "checkout": {"image": "ghcr.io/open-telemetry/demo:3.0.0-checkout", "container_name": "checkout",
                         "ports": [{"target": 5050, "published": "5050"}]},
            "flagd": {"volumes": [{"source": "upstream", "target": "/etc/flagd"}]},
            "flagd-ui": {"volumes": [{"source": "upstream", "target": "/app/data"}]}}}))
    monkeypatch.setattr(module, "docker", docker)
    module.prepare()
    result = json.loads(output.read_text())
    assert "container_name" not in result["services"]["checkout"]
    assert result["services"]["checkout"]["ports"][0]["host_ip"] == "127.0.0.1"
    assert all(result["services"][service]["volumes"][0]["source"] == str(flags) for service in ("flagd", "flagd-ui"))
    with pytest.raises(ValueError):
        module.validate_image_pins({"services": {"checkout": {"image": "ghcr.io/open-telemetry/demo:latest-checkout"}}})
