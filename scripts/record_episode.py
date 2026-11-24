"""Private operator-controlled recorder, never imported by an investigator.

--capture-policy enables the pilot protocol and real-time human QA attestations.
--no-injection uses the same readiness/lock/capture/recovery path without changing
any flag bytes. Recording never assigns Gold, approves a candidate, or runs Aegis.
"""
import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from aegis_rca.evaluation.corpus_recording import (CapturePolicy, CorpusProductionRecord,
    HealthQA, InjectionTruth, LifecycleEvent, NEUTRAL_QUERY, QUERY_POLICY, PILOT_CAPTURE_POLICY,
    collection_quality, validate_corpus_incident)
from aegis_rca.evaluation.demo_environment import validate_prepared_environment
from aegis_rca.models.incident import IncidentInput, TimeWindow
from aegis_rca.policy import make_task
from aegis_rca.settings import load_settings
from aegis_rca.tools.live import LiveBackend
from aegis_rca.tools.snapshot import write_snapshot

ROOT = Path(__file__).resolve().parents[1]
FLAG_FILE = ROOT / "artifacts" / "demo-flags" / "demo.flagd.json"
DEMO_COMPOSE = ROOT / "artifacts" / "demo-compose.json"
DEMO_ENVIRONMENT_RECEIPT = ROOT / "artifacts" / "demo-compose.provenance.json"
VARIANTS = {"paymentUnreachable": ("on", True), "paymentFailure": ("100%", 1)}
ALLOWED = set(VARIANTS)


class RecordingProtocolError(ValueError):
    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason


def now():
    return datetime.now(timezone.utc)


def _write_private(path, payload):
    """Replace a private progress ledger; public/quarantine sources stay immutable."""
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def _health_qa(operator, phase):
    print(f"{phase}: verify actual workload, successful checkout business path, restored faults, "
          "and target-service health under OTel Demo 3.0.0. Catalog discovery alone is insufficient.")
    if input("Type CONFIRM only after all four checks pass: ").strip() != "CONFIRM":
        raise RecordingProtocolError("health_qa_failed", "Operator health QA did not pass")
    return HealthQA(at=now(), reviewer=operator, workload_ran=True,
        business_path_succeeded=True, no_residual_fault=True, services_healthy=True,
        notes=f"Interactive operator attestation at {phase}; no benchmark diagnosis inferred.")


def _onset_qa(operator):
    print("Privately verify the selected injection took effect; do not run Aegis or use its output.")
    if input(f"{operator}: type CONFIRM after onset is independently observed: ").strip() != "CONFIRM":
        raise RecordingProtocolError("injection_not_effective", "Injection onset was not independently confirmed")
    return True


def _policy(args):
    if getattr(args, "capture_policy", None):
        path = Path(args.capture_policy)
        if path.stat().st_size > 65536:
            raise ValueError("Capture policy is oversized")
        policy = CapturePolicy.model_validate_json(path.read_text(encoding="utf-8-sig"))
        if not (getattr(args, "operator", None) or "").strip() or not (getattr(args, "seed_justification", None) or "").strip():
            raise ValueError("Formal corpus recording requires operator and incident-entry seed justification")
        if not getattr(args, "scenario_family_id", None) or getattr(args, "family_trial_index", None) is None:
            raise ValueError("Formal recording requires a predeclared private family and trial index")
        if getattr(args, "seconds", None) is not None or getattr(args, "ingest_wait", None) is not None:
            raise ValueError("Capture policy fixes duration and ingest wait; do not override per episode")
        return policy, True
    return CapturePolicy(demo_resource_profile_id=PILOT_CAPTURE_POLICY.demo_resource_profile_id,
        warmup_seconds=0, cooldown_seconds=0,
        duration_seconds=60 if getattr(args, "seconds", None) is None else args.seconds,
        ingest_wait_seconds=15 if getattr(args, "ingest_wait", None) is None else args.ingest_wait), False


async def _record_locked(args):
    episode_id = str(uuid4())
    private = args.output / "private" / episode_id
    private.mkdir(parents=True)
    created = now()
    policy = PILOT_CAPTURE_POLICY
    record = CorpusProductionRecord(private_episode_id=episode_id, status="recording_failed",
        reason_codes=("capture_failed",), created_at=created, updated_at=created, capture_policy_id=policy.id)
    truth = None
    original = None
    mutated = False
    formal = False
    completed_capture = False
    events = []
    manifest = {"episode": episode_id, "restored": False,
        "readiness": "catalog_only; workload and instrumentation readiness require separate checks"}

    def event(stage):
        events.append(LifecycleEvent(stage=stage, at=now()))

    def save():
        nonlocal truth, record
        record = record.model_copy(update={"updated_at": now()})
        _write_private(private / "production.json", record.model_dump(mode="json"))
        if truth is not None:
            truth = truth.model_copy(update={"lifecycle": tuple(events)})
            _write_private(private / "injection-truth.json", truth.model_dump(mode="json"))
        _write_private(private / "injection.json", manifest)

    save()
    try:
        policy, formal = _policy(args)
        record = record.model_copy(update={"capture_policy_id": policy.id})
        no_injection = bool(getattr(args, "no_injection", False))
        flag = getattr(args, "flag", None)
        if (not no_injection and flag not in ALLOWED) or (no_injection and flag is not None):
            raise ValueError("Choose exactly one of the two pinned payment flags or explicit no-injection")
        mechanism = "none" if no_injection else {
            "paymentUnreachable": "payment_unreachable", "paymentFailure": "payment_failure"}[flag]
        variant = None if no_injection else VARIANTS[flag][0]
        truth = InjectionTruth(private_episode_id=episode_id, quarantine_recording_id=episode_id,
            experiment_revision="p2.2-pilot-v1" if formal else "engineering-unreviewed-v1",
            scenario_family_id=getattr(args, "scenario_family_id", None) or str(uuid4()),
            family_trial_index=getattr(args, "family_trial_index", None) or 1,
            mechanism=mechanism, control_flag=flag, variant=variant,
            private_target=None if no_injection else "payment", capture_policy_id=policy.id,
            seed_justification=getattr(args, "seed_justification", None) or "Engineering capture; operator corpus QA absent.",
            private_notes="No injection or catalog success implies no diagnostic Gold.")
        manifest.update(flag=flag, no_injection=no_injection)
        if formal:
            prepared = validate_prepared_environment(
                getattr(args, "demo_compose", None) or DEMO_COMPOSE,
                getattr(args, "demo_environment_receipt", None) or DEMO_ENVIRONMENT_RECEIPT,
                expected_profile_id=policy.demo_resource_profile_id,
                expected_demo_version=policy.otel_demo_version)
            receipt_path = private / "demo-environment.json"
            _write_private(receipt_path, prepared.model_dump(mode="json"))
            manifest.update(demo_resource_profile_id=policy.demo_resource_profile_id,
                prepared_environment_receipt_sha256=hashlib.sha256(receipt_path.read_bytes()).hexdigest())
        if not FLAG_FILE.is_file():
            raise ValueError("Prepare the dedicated Compose configuration first")
        settings = load_settings(args.backends)
        if formal:
            policy.validate_settings(settings)
        probe_end = now()
        incident = IncidentInput(query=NEUTRAL_QUERY, seed_services=QUERY_POLICY.seed_services,
            window=TimeWindow(start=probe_end - timedelta(minutes=5), end=probe_end))
        validate_corpus_incident(incident)
        backend = LiveBackend(settings, incident)
        try:
            catalog = await backend.catalog()
            if any("error" in item for item in catalog.values()):
                raise ValueError("All backends must pass discovery before recording")
        finally:
            backend.close()
        event("ENV_READY")
        if formal:
            pre_health = _health_qa(args.operator, "Pre-recording health")
            truth = truth.model_copy(update={"pre_health": pre_health,
                                            "previous_fault_restored": pre_health.no_residual_fault})
        event("STABILIZE")
        original = FLAG_FILE.read_bytes()
        payload = json.loads(original)
        if not no_injection:
            config = payload["flags"][flag]
            if config["variants"].get(variant) != VARIANTS[flag][1]:
                raise ValueError("Pinned flag schema changed; inspect before continuing")
        if formal:
            for controlled_flag in ALLOWED:
                config_before = payload["flags"][controlled_flag]
                selected = config_before["defaultVariant"]
                if config_before["variants"].get(selected) not in (False, 0):
                    raise ValueError("Residual validated fault is active before capture")
        (private / "original-flags.json").write_bytes(original)
        if not no_injection:
            config["defaultVariant"] = variant
            mutated = True
            FLAG_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            event("INJECT")
            manifest["injected_at"] = events[-1].at.isoformat()
        print(f"Recording private episode {episode_id}; no diagnostic Gold is assigned.")
        event("WARMUP")
        if policy.warmup_seconds:
            await asyncio.sleep(policy.warmup_seconds)
        if formal and not no_injection:
            truth = truth.model_copy(update={"injection_effective": _onset_qa(args.operator)})
        start = now()
        end = start + timedelta(seconds=policy.duration_seconds)
        window = TimeWindow(start=start, end=end)
        truth = truth.model_copy(update={"capture_window": window})
        record = record.model_copy(update={"capture_window": window})
        event("CAPTURE")
        save()
        await asyncio.sleep(policy.duration_seconds)
        event("INGEST_WAIT")
        if policy.ingest_wait_seconds:
            await asyncio.sleep(policy.ingest_wait_seconds)
        incident = validate_corpus_incident(IncidentInput(query=NEUTRAL_QUERY,
            seed_services=QUERY_POLICY.seed_services, window=window))
        backend = LiveBackend(settings, incident)
        try:
            for signal in ("metric", "trace", "log"):
                await backend.query(make_task(incident, signal))
            snapshot = backend.snapshot()
            quarantine = args.output / "quarantine" / episode_id / "snapshot.json"
            write_snapshot(snapshot, quarantine)
            source_hash = hashlib.sha256(quarantine.read_bytes()).hexdigest()
            quality = collection_quality(snapshot, policy)
            truth = truth.model_copy(update={"source_snapshot_sha256": source_hash,
                "quality": quality, "collection_errors": dict(snapshot.collection_errors)})
            reasons = tuple(reason for reason, applies in (
                ("collection_error", bool(snapshot.collection_errors)),
                ("truncation", bool(quality.truncated_signals))) if applies)
            record = record.model_copy(update={"status": "quarantined", "reason_codes": reasons,
                "source_snapshot_sha256": source_hash, "quality": quality})
            manifest.update(collection_errors=snapshot.collection_errors, end=end.isoformat())
            completed_capture = True
        finally:
            backend.close()
    except BaseException as exc:
        reason = ("capture_cancelled" if isinstance(exc, asyncio.CancelledError) else
                  exc.reason if isinstance(exc, RecordingProtocolError) else "capture_failed")
        if reason == "injection_not_effective" and truth is not None:
            truth = truth.model_copy(update={"injection_effective": False})
        record = record.model_copy(update={"status": "recording_failed", "reason_codes": (reason,)})
        raise
    finally:
        try:
            if original is not None:
                if mutated:
                    FLAG_FILE.write_bytes(original)
                restored = FLAG_FILE.read_bytes() == original
                event("RESTORE")
                manifest["restored"] = restored
                manifest["restoration_attempted_at"] = now().isoformat()
                if not restored:
                    raise ValueError("Original flag bytes were not restored")
                event("COOLDOWN")
                if policy.cooldown_seconds:
                    await asyncio.sleep(policy.cooldown_seconds)
                if truth is not None:
                    truth = truth.model_copy(update={"cooldown_completed": True})
                if formal:
                    recovery = _health_qa(args.operator, "Post-cooldown recovery health")
                    truth = truth.model_copy(update={"recovery_health": recovery})
        except BaseException:
            record = record.model_copy(update={"status": "excluded" if completed_capture else "recording_failed",
                                               "reason_codes": ("recovery_failed",)})
            raise
        finally:
            save()
    print("Capture is quarantined. Human leakage review, approval and blinded Gold review remain mandatory.")
    return record


async def record(args):
    lock_path = FLAG_FILE.with_suffix(".recording.lock")
    try:
        lock = lock_path.open("x", encoding="utf-8")
    except OSError:
        if args is not None and getattr(args, "output", None) is not None:
            episode_id = str(uuid4())
            private = args.output / "private" / episode_id
            private.mkdir(parents=True)
            timestamp = now()
            record = CorpusProductionRecord(private_episode_id=episode_id, status="recording_failed",
                reason_codes=("lock_unavailable",), created_at=timestamp, updated_at=timestamp,
                capture_policy_id=PILOT_CAPTURE_POLICY.id)
            _write_private(private / "production.json", record.model_dump(mode="json"))
        raise
    try:
        lock.write("An episode recorder owns this file. Inspect private audit before recovering an interrupted run.\n")
        lock.flush()
        return await _record_locked(args)
    finally:
        lock.close()
        lock_path.unlink()


def parser():
    command = argparse.ArgumentParser(description=__doc__)
    command.add_argument("--backends", type=Path, required=True)
    mode = command.add_mutually_exclusive_group(required=True)
    mode.add_argument("--flag", choices=sorted(ALLOWED))
    mode.add_argument("--no-injection", action="store_true")
    command.add_argument("--seconds", type=int)
    command.add_argument("--ingest-wait", type=int)
    command.add_argument("--capture-policy", type=Path)
    command.add_argument("--demo-compose", type=Path, default=DEMO_COMPOSE)
    command.add_argument("--demo-environment-receipt", type=Path, default=DEMO_ENVIRONMENT_RECEIPT)
    command.add_argument("--operator")
    command.add_argument("--seed-justification")
    command.add_argument("--scenario-family-id")
    command.add_argument("--family-trial-index", type=int)
    command.add_argument("--output", type=Path, default=ROOT / "data" / "recordings")
    return command


if __name__ == "__main__":
    command = parser()
    try:
        asyncio.run(record(command.parse_args()))
    except (ValueError, OSError) as exc:
        command.exit(2, f"Episode recording stopped ({type(exc).__name__}); inspect readiness and private audit.\n")
