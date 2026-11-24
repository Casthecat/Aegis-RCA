import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def recorder():
    path = Path(__file__).parents[1] / "scripts" / "record_episode.py"
    spec = importlib.util.spec_from_file_location("episode_recorder", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("flag,variant,variants", [
    ("paymentUnreachable", "on", {"on": True, "off": False}),
    ("paymentFailure", "100%", {"100%": 1, "off": 0}),
])
async def test_injection_is_restored_when_recording_is_cancelled(monkeypatch, tmp_path, flag, variant, variants):
    module = recorder()
    path = tmp_path / "flags.json"
    original = json.dumps({"flags": {flag: {"defaultVariant": "off", "variants": variants}}}).encode()
    path.write_bytes(original)
    monkeypatch.setattr(module, "FLAG_FILE", path)
    monkeypatch.setattr(module, "load_settings", lambda _: None)
    class Ready:
        def __init__(self, *args):
            pass
        async def catalog(self):
            return {"test": {"values": []}}
        def close(self):
            pass
    monkeypatch.setattr(module, "LiveBackend", Ready)
    async def cancel(_):
        assert json.loads(path.read_text())["flags"][flag]["defaultVariant"] == variant
        raise module.asyncio.CancelledError()
    monkeypatch.setattr(module.asyncio, "sleep", cancel)
    args = SimpleNamespace(flag=flag, seconds=10, ingest_wait=0,
                           backends=Path("unused"), output=tmp_path / "episodes")
    with pytest.raises(module.asyncio.CancelledError):
        await module.record(args)
    assert path.read_bytes() == original
    audit = next((args.output / "private").glob("*/injection.json"))
    assert json.loads(audit.read_text())["restored"] is True
    assert not path.with_suffix(".recording.lock").exists()


async def test_concurrent_injection_is_rejected_before_touching_flags(monkeypatch, tmp_path):
    module = recorder()
    path = tmp_path / "flags.json"
    path.write_text("original")
    lock = path.with_suffix(".recording.lock")
    lock.write_text("other owner")
    monkeypatch.setattr(module, "FLAG_FILE", path)
    with pytest.raises(FileExistsError):
        await module.record(None)
    assert path.read_text() == "original"
    assert lock.read_text() == "other owner"
