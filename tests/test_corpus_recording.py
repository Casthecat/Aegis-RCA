"""System-independent collection checks; these tests never start a live backend."""
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from aegis_rca.evaluation.corpus_recording import (CapturePolicy, CaptureQuality,
    CorpusProductionRecord, HealthQA, InjectionTruth, NEUTRAL_QUERY, PILOT_CAPTURE_POLICY,
    QUERY_POLICY, collection_quality)
from aegis_rca.evaluation.demo_environment import (PILOT_DEMO_RESOURCE_PROFILE,
    apply_resource_profile, build_prepared_environment)
from aegis_rca.models.incident import IncidentInput, TimeWindow
from aegis_rca.models.telemetry import TraceResult
from aegis_rca.settings import BackendSettings
from aegis_rca.tools.snapshot import TelemetrySnapshot


def recorder():
    path = Path(__file__).parents[1] / "scripts" / "record_episode.py"
    spec = importlib.util.spec_from_file_location("corpus_episode_recorder", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def incident(query=NEUTRAL_QUERY, seeds=("checkout",)):
    end = datetime.now(timezone.utc)
    return IncidentInput(query=query, seed_services=seeds,
        window=TimeWindow(start=end - timedelta(seconds=60), end=end))


def truth(**updates):
    episode = str(uuid4())
    fields = dict(private_episode_id=episode, quarantine_recording_id=episode,
        scenario_family_id=str(uuid4()), family_trial_index=1, mechanism="none",
        capture_policy_id=PILOT_CAPTURE_POLICY.id, seed_justification="Checkout alert is the entry context.")
    fields.update(updates)
    return InjectionTruth(**fields)


def setup_recorder(monkeypatch, tmp_path, *, flag=None, broken_catalog=False):
    module = recorder()
    flags = tmp_path / "flags.json"
    flags.write_text(json.dumps({"flags": {
        "paymentUnreachable": {"defaultVariant": "off", "variants": {"on": True, "off": False}},
        "paymentFailure": {"defaultVariant": "off", "variants": {"100%": 1, "off": 0}}}}))
    monkeypatch.setattr(module, "FLAG_FILE", flags)
    settings = BackendSettings(prometheus_url="http://localhost:9090", jaeger_url="http://localhost:16686",
        opensearch_url="http://localhost:9200")
    monkeypatch.setattr(module, "load_settings", lambda _: settings)
    calls = []
    sleeps = []
    instances = []

    class FakeLiveBackend:
        def __init__(self, _settings, incident):
            self.incident = incident
            instances.append(self)

        async def catalog(self):
            return {"test": {"error": "offline"} if broken_catalog else {"values": []}}

        async def query(self, task):
            calls.append(task)

        def snapshot(self):
            return TelemetrySnapshot(incident=self.incident, provenance="live_capture")

        def close(self):
            pass

    monkeypatch.setattr(module, "LiveBackend", FakeLiveBackend)

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr("builtins.input", lambda _: "CONFIRM")
    policy = CapturePolicy(demo_resource_profile_id=PILOT_DEMO_RESOURCE_PROFILE.id,
        warmup_seconds=1, duration_seconds=10, ingest_wait_seconds=1, cooldown_seconds=1)
    policy_file = tmp_path / "capture-policy.json"
    policy_file.write_text(policy.model_dump_json())
    # Structural environment validation remains real in recorder tests; only
    # upstream Compose and telemetry acquisition are synthetic fixtures.
    upstream = {"services": {service: {
        "environment": {"GOMEMLIMIT": "16MiB"},
        "deploy": {"resources": {"limits": {"memory": "20971520"}}}}
        for service in ("checkout", "product-catalog")}}
    source_marker = json.dumps({"repository": "https://github.com/open-telemetry/opentelemetry-demo",
        "tag": "3.0.0", "archive_url": "https://codeload.github.com/open-telemetry/opentelemetry-demo/zip/refs/tags/3.0.0"}).encode()
    resolved = json.dumps(upstream).encode()
    prepared_bytes = json.dumps(apply_resource_profile(upstream, PILOT_DEMO_RESOURCE_PROFILE)).encode()
    prepared = build_prepared_environment(PILOT_DEMO_RESOURCE_PROFILE, prepared_bytes, source_marker,
        b"synthetic upstream compose", b"synthetic observability compose", resolved)
    compose_file = tmp_path / "demo-compose.json"
    compose_file.write_bytes(prepared_bytes)
    receipt_file = tmp_path / "demo-compose.provenance.json"
    receipt_file.write_text(prepared.model_dump_json())
    args = SimpleNamespace(flag=flag, no_injection=flag is None, seconds=None, ingest_wait=None,
        demo_compose=compose_file, demo_environment_receipt=receipt_file,
        capture_policy=policy_file, operator="operator-A", seed_justification="Checkout alert is legitimate entry.",
        scenario_family_id=str(uuid4()), family_trial_index=1,
        backends=Path("unused"), output=tmp_path / "recordings")
    return module, flags, args, calls, sleeps, instances


def load_private(args):
    private = next((args.output / "private").iterdir())
    return (InjectionTruth.model_validate_json((private / "injection-truth.json").read_text()),
            CorpusProductionRecord.model_validate_json((private / "production.json").read_text()))


async def test_no_injection_uses_same_pipeline_without_any_flag_write_or_gold(monkeypatch, tmp_path):
    module, flags, args, calls, sleeps, instances = setup_recorder(monkeypatch, tmp_path)
    original = flags.read_bytes()
    write_bytes, write_text = Path.write_bytes, Path.write_text

    def guard_bytes(path, data):
        assert path != flags, "Healthy mode may not mutate or restore flag bytes"
        return write_bytes(path, data)

    def guard_text(path, data, *a, **kw):
        assert path != flags, "Healthy mode may not mutate or restore flag bytes"
        return write_text(path, data, *a, **kw)

    monkeypatch.setattr(Path, "write_bytes", guard_bytes)
    monkeypatch.setattr(Path, "write_text", guard_text)
    result = await module.record(args)
    injection, production = load_private(args)
    assert result == production
    assert len(instances) == 2  # readiness and the same actual capture backend path
    assert tuple(task.signal for task in calls) == ("metric", "trace", "log")
    assert all(task.services == ("checkout",) for task in calls)
    assert sleeps == [1, 10, 1, 1]
    assert flags.read_bytes() == original
    assert injection.mechanism == "none" and injection.control_flag is None
    assert injection.injection_effective is None
    assert injection.pre_health.passed and injection.recovery_health.passed
    assert injection.previous_fault_restored and injection.cooldown_completed
    assert production.status == "quarantined"
    assert production.source_snapshot_sha256 == injection.source_snapshot_sha256
    assert injection.quality.coverage_incomplete_signals == ("log", "metric", "trace")
    assert "answerability" not in injection.model_dump()
    assert "expected_claims" not in injection.model_dump()
    snapshot = TelemetrySnapshot.model_validate_json(next((args.output / "quarantine").glob("*/snapshot.json")).read_text())
    assert snapshot.incident.query == NEUTRAL_QUERY
    assert {event.stage for event in injection.lifecycle} == {
        "ENV_READY", "STABILIZE", "WARMUP", "CAPTURE", "INGEST_WAIT", "RESTORE", "COOLDOWN"}


@pytest.mark.parametrize("flag,mechanism", [("paymentUnreachable", "payment_unreachable"),
                                           ("paymentFailure", "payment_failure")])
async def test_validated_fault_pipeline_preserves_exact_source_flags(monkeypatch, tmp_path, flag, mechanism):
    module, flags, args, calls, _, _ = setup_recorder(monkeypatch, tmp_path, flag=flag)
    original = flags.read_bytes()
    await module.record(args)
    injection, production = load_private(args)
    assert module.ALLOWED == {"paymentUnreachable", "paymentFailure"}
    assert flags.read_bytes() == original
    assert injection.mechanism == mechanism and injection.injection_effective is True
    assert production.status == "quarantined" and len(calls) == 3
    assert "INJECT" in tuple(event.stage for event in injection.lifecycle)


async def test_failed_onset_is_private_failed_attempt_and_still_restored(monkeypatch, tmp_path):
    module, flags, args, calls, _, _ = setup_recorder(monkeypatch, tmp_path, flag="paymentUnreachable")
    original = flags.read_bytes()
    answers = iter(("CONFIRM", "NO", "CONFIRM"))
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    with pytest.raises(ValueError, match="onset"):
        await module.record(args)
    injection, production = load_private(args)
    assert flags.read_bytes() == original
    assert injection.injection_effective is False
    assert injection.recovery_health.passed
    assert production.status == "recording_failed"
    assert production.reason_codes == ("injection_not_effective",)
    assert not calls


async def test_readiness_failure_gets_ledger_before_any_mutation(monkeypatch, tmp_path):
    module, flags, args, calls, _, _ = setup_recorder(monkeypatch, tmp_path, broken_catalog=True)
    original = flags.read_bytes()
    with pytest.raises(ValueError, match="discovery"):
        await module.record(args)
    _, production = load_private(args)
    assert production.status == "recording_failed"
    assert production.reason_codes == ("capture_failed",)
    assert flags.read_bytes() == original and not calls
    assert not flags.with_suffix(".recording.lock").exists()


async def test_recovery_qa_failure_excludes_captured_source_without_erasing_it(monkeypatch, tmp_path):
    module, flags, args, _, _, _ = setup_recorder(monkeypatch, tmp_path)
    original = flags.read_bytes()
    answers = iter(("CONFIRM", "NO"))
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    with pytest.raises(ValueError, match="health QA"):
        await module.record(args)
    _, production = load_private(args)
    assert production.status == "excluded" and production.reason_codes == ("recovery_failed",)
    assert list((args.output / "quarantine").glob("*/snapshot.json"))
    assert flags.read_bytes() == original


async def test_lock_rejection_has_ledger_but_never_removes_other_owner_lock(monkeypatch, tmp_path):
    module, flags, args, _, _, _ = setup_recorder(monkeypatch, tmp_path)
    lock = flags.with_suffix(".recording.lock")
    lock.write_text("other owner")
    with pytest.raises(FileExistsError):
        await module.record(args)
    production = CorpusProductionRecord.model_validate_json(
        next((args.output / "private").glob("*/production.json")).read_text())
    assert production.reason_codes == ("lock_unavailable",)
    assert lock.read_text() == "other owner"


@pytest.mark.parametrize("query", ["Investigate paymentUnreachable.", "Investigate checkout errors.", NEUTRAL_QUERY + " "])
def test_exact_neutral_query_rejects_hint_or_unfrozen_wording(query):
    with pytest.raises(ValueError, match="query_leakage"):
        QUERY_POLICY.validate_incident(incident(query=query))


def test_checkout_entry_seed_validated_independently_of_private_target():
    assert QUERY_POLICY.validate_incident(incident()).seed_services == ("checkout",)
    with pytest.raises(ValueError, match="checkout"):
        QUERY_POLICY.validate_incident(incident(seeds=("payment",)))
    with pytest.raises(ValidationError):
        truth(seed_services=("payment",))


def test_capture_policy_binds_all_collection_limits_and_window():
    base = PILOT_CAPTURE_POLICY
    for field, replacement in {"warmup_seconds": 31, "duration_seconds": 61, "ingest_wait_seconds": 16,
            "cooldown_seconds": 31, "max_results": 101, "metric_step_seconds": 16,
            "max_response_bytes": 2_000_001, "request_timeout_seconds": 6}.items():
        changed = CapturePolicy(**{**base.model_dump(), field: replacement})
        assert changed.id != base.id
    assert CapturePolicy.model_validate_json(base.model_dump_json()).id == base.id


def test_frozen_limit_mismatch_fails_instead_of_enlarging_per_episode():
    settings = BackendSettings(prometheus_url="http://localhost:9090", jaeger_url="http://localhost:16686",
        opensearch_url="http://localhost:9200", max_results=101)
    with pytest.raises(ValueError, match="max_results"):
        PILOT_CAPTURE_POLICY.validate_settings(settings)


def test_no_injection_is_not_healthy_truth_or_coverage_proof():
    injection = truth()
    assert injection.pre_health is None and injection.recovery_health is None
    assert not injection.previous_fault_restored
    assert "answerability" not in injection.model_dump()
    with pytest.raises(ValidationError):
        truth(answerability="no_incident")
    with pytest.raises(ValidationError):
        truth(injection_effective=True)


def test_quality_records_missing_coverage_and_truncation_without_diagnosis():
    snapshot = TelemetrySnapshot(incident=incident(), provenance="live_capture",
        traces=TraceResult(truncated=True, coverage_complete=False), collection_errors={"log": "backend_error"})
    quality = collection_quality(snapshot, PILOT_CAPTURE_POLICY)
    assert quality.truncated_signals == ("trace",)
    assert quality.collection_error_signals == ("log",)
    assert quality.coverage_incomplete_signals == ("log", "metric", "trace")
    assert "answerability" not in quality.model_dump()


@pytest.mark.parametrize("field,value", [("status", "automatically_approved"),
                                        ("reason_codes", ("arbitrary diagnostic prose",))])
def test_production_state_and_reason_are_closed(field, value):
    timestamp = datetime.now(timezone.utc)
    values = dict(private_episode_id=str(uuid4()), status="recording_failed", reason_codes=("capture_failed",),
        created_at=timestamp, updated_at=timestamp, capture_policy_id=PILOT_CAPTURE_POLICY.id)
    values[field] = value
    with pytest.raises(ValidationError):
        CorpusProductionRecord(**values)


def test_policy_mode_rejects_duration_override_and_unsupported_control(tmp_path):
    module = recorder()
    policy = tmp_path / "capture.json"
    policy.write_text(PILOT_CAPTURE_POLICY.model_dump_json())
    args = SimpleNamespace(capture_policy=policy, operator="qa", seed_justification="checkout alert",
        scenario_family_id=str(uuid4()), family_trial_index=1, seconds=180, ingest_wait=None)
    with pytest.raises(ValueError, match="override"):
        module._policy(args)
    with pytest.raises(SystemExit):
        module.parser().parse_args(["--backends", "unused", "--flag", "dnsFailure"])
    assert module.ALLOWED == {"paymentUnreachable", "paymentFailure"}
