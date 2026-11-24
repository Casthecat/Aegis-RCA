"""Formal environment binding fails before any live acquisition or fault mutation."""
import json

import pytest
from pydantic import ValidationError

from aegis_rca.evaluation.corpus_recording import CapturePolicy, PILOT_CAPTURE_POLICY
from aegis_rca.evaluation.demo_environment import PreparedDemoEnvironment
from aegis_rca.evaluation.gold import DEFAULT_GOLD_RUBRIC
from aegis_rca.evaluation.gold_scoring import BenchmarkScoringProtocolV2
from aegis_rca.evaluation.stage_c_models import (
    VARIANTS, BenchmarkBudgetSpec, BenchmarkExecutionProtocol, BenchmarkScoringProtocol,
)
from test_corpus_recording import setup_recorder


@pytest.mark.parametrize("invalid_environment", [
    "missing_provenance", "resource_profile_mismatch", "generated_compose_changed",
    "demo_version_mismatch", "inconsistent_receipt_profile",
])
async def test_formal_recorder_rejects_environment_before_acquisition_or_injection(
        monkeypatch, tmp_path, invalid_environment):
    module, flags, args, calls, sleeps, instances = setup_recorder(
        monkeypatch, tmp_path, flag="paymentUnreachable")
    original = flags.read_bytes()
    if invalid_environment == "missing_provenance":
        args.demo_environment_receipt.unlink()
    elif invalid_environment == "resource_profile_mismatch":
        policy = json.loads(args.capture_policy.read_text())
        policy["demo_resource_profile_id"] = "DRP-" + "a" * 64
        args.capture_policy.write_text(json.dumps(policy))
    elif invalid_environment == "generated_compose_changed":
        args.demo_compose.write_bytes(args.demo_compose.read_bytes() + b"\n")
    else:
        receipt = json.loads(args.demo_environment_receipt.read_text())
        if invalid_environment == "demo_version_mismatch":
            receipt["otel_demo_version"] = "3.0.1"
        else:
            receipt["demo_resource_profile_id"] = "DRP-" + "a" * 64
        args.demo_environment_receipt.write_text(json.dumps(receipt))

    monkeypatch.setattr("builtins.input", lambda *_: pytest.fail("Health/onset attestation reached"))
    with pytest.raises((ValueError, FileNotFoundError)):
        await module.record(args)

    assert not instances, "The live readiness backend must not be constructed"
    assert not calls and not sleeps
    assert flags.read_bytes() == original
    assert not flags.with_suffix(".recording.lock").exists()
    assert not list(args.output.glob("quarantine/*/snapshot.json"))
    assert module.ALLOWED == {"paymentUnreachable", "paymentFailure"}


async def test_validated_environment_receipt_is_retained_with_mocked_episode(monkeypatch, tmp_path):
    module, flags, args, _, _, _ = setup_recorder(monkeypatch, tmp_path)
    original = flags.read_bytes()
    expected = PreparedDemoEnvironment.model_validate_json(args.demo_environment_receipt.read_bytes())
    await module.record(args)
    saved = next((args.output / "private").glob("*/demo-environment.json"))
    archived = PreparedDemoEnvironment.model_validate_json(saved.read_bytes())
    assert archived == expected
    assert archived.demo_resource_profile_id == PILOT_CAPTURE_POLICY.demo_resource_profile_id
    assert flags.read_bytes() == original


def test_capture_policy_explicitly_binds_resource_profile_identity():
    original = PILOT_CAPTURE_POLICY
    alternative = CapturePolicy.model_validate({
        **original.model_dump(), "demo_resource_profile_id": "DRP-" + "b" * 64,
    })
    assert alternative.id != original.id
    assert original.id != "CP-3f4215f44800bdd822d62680dab992e36f3bf8a01c7f7b1258ed03ae765fff98"
    assert CapturePolicy.model_validate_json(original.model_dump_json()) == original


@pytest.mark.parametrize("schema_version", [1, 2])
def test_capture_policy_without_resource_binding_is_not_current_pilot(schema_version):
    old = PILOT_CAPTURE_POLICY.model_dump(exclude={"demo_resource_profile_id"})
    old["schema_version"] = schema_version
    with pytest.raises(ValidationError):
        CapturePolicy.model_validate(old)


def test_old_capture_policy_schema_is_not_silently_upgraded():
    with pytest.raises(ValidationError):
        CapturePolicy.model_validate({**PILOT_CAPTURE_POLICY.model_dump(), "schema_version": 1})
    with pytest.raises(ValidationError):
        CapturePolicy()


def test_environment_profile_does_not_change_execution_or_gold_protocols():
    assert {name: variant.id for name, variant in VARIANTS.items()} == {
        "B0": "BV-e2a36cc8655540d2eab82f663b1252ae6c3c76b27c1f3e80fbf6f9b82b418056",
        "B1": "BV-45e4972f7a1c94c3feb9c6f9b546471334e37f562eb8f0731c534f7200fd82d1",
        "B2": "BV-728e8e179311d1d519bf76d493cfda4110b4700877a916dc2dc50f9f9c379058",
    }
    assert BenchmarkBudgetSpec().id == "BB-9e94e60d067ec0a721ff82e033f86da064c828d348b4980b0ab4ca003f110822"
    assert BenchmarkExecutionProtocol().id == "BEP-e73516d551d3ec56cb5becb81768d6980c7f853f3cc885fd704889c3f2e65250"
    assert BenchmarkScoringProtocol().id == "BSP-67b7d104807c65947098fe28efdc5dc971376aa3dafdd84ea03658e050f80daa"
    assert BenchmarkScoringProtocolV2().id == "BSP-12450a5e12c072ab98bc8fdf768a2186b8f135d42e7ec6a061b31d58155e80ec"
    assert DEFAULT_GOLD_RUBRIC.id == "GDR-4f39dcda468d6319c95f36d6379fcfc786dd6ee4bbde79bc6d4bae8036497b20"
