"""Synthetic-only fixtures. No reads of the user's private or quarantined recordings."""
import json
import stat
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from aegis_rca.evaluation import scan_leakage
from aegis_rca.evaluation.__main__ import main
from aegis_rca.evaluation.benchmark import (ScanTerms, export_candidate, load_terms,
    review_candidate, scan_candidate)
from aegis_rca.evaluation.sanitization import (canonical_bytes, digest, sanitize_snapshot,
    scan_payload, structural_blockers)
from aegis_rca.fixtures import demo_incident
from aegis_rca.models.telemetry import LogRecord, LogResult, MetricPoint, MetricResult, MetricSeries, SpanRecord, TraceResult
from aegis_rca.policy import make_task
from aegis_rca.tools.replay import ReplayBackend
from aegis_rca.tools.snapshot import TelemetrySnapshot, read_snapshot, write_snapshot


def pair(key, value):
    return {"key": key, "value": value}


def control(flag="unseen-control-793", value=True):
    return {"name": "feature_flag.evaluation", "timeUnixNano": "1788878400000000000", "attributes": [
        pair("feature_flag.key", {"stringValue": flag}),
        pair("feature_flag.result.value", {"boolValue": value}),
        pair("feature_flag.result.variant", {"stringValue": "on"}),
        pair("feature_flag.provider.name", {"stringValue": "provider-private"}),
        pair("feature_flag.result.reason", {"stringValue": "TARGETING_MATCH"}),
        pair("provider", {"stringValue": "event-private-provider"}),
        pair("reason", {"stringValue": "event-private-reason"})]}


def fixture(flag="unseen-control-793"):
    incident = demo_incident()
    ordinary = {"name": "application.retry", "attributes": [pair("attempt", {"intValue": "2"}),
        pair("provider", {"stringValue": "payment-provider"}), pair("reason", {"stringValue": "connection refused"})]}
    span = SpanRecord(trace_id="1" * 32, span_id="2" * 16, parent_span_id="3" * 16,
        service="checkout", name="oteldemo.PaymentService/Charge", kind="CLIENT", status="ERROR",
        start=incident.window.start, duration_ms=123, source="jaeger:trace/opaque/span/opaque",
        attributes={"rpc.service": "oteldemo.PaymentService", "rpc.grpc.status_code": 14,
                    "aegis.otlp": {"startTimeUnixNano": "1788878400000000000",
                                    "events": [control(flag), ordinary], "resource": {"service.name": "checkout"}}})
    log = LogRecord(id="opaque-document", timestamp=incident.window.start, service="checkout", severity="ERROR",
        body="dial payment:50051: connection refused", trace_id=span.trace_id, span_id=span.span_id,
        attributes={"provider": "business-provider", "reason": "ordinary"}, source="opensearch:logs/_doc/opaque")
    metric = MetricSeries(name="rpc_errors", labels={"service": "checkout", "rpc_status": "UNAVAILABLE"},
        points=(MetricPoint(timestamp=incident.window.start, value=3),), source="prometheus:query/rpc_errors")
    return TelemetrySnapshot(incident=incident, provenance="synthetic_wire_fixture",
        traces=TraceResult(spans=(span,), warnings=("sampling unknown",), truncated=True),
        logs=LogResult(logs=(log,)), metrics=MetricResult(series=(metric,)))


def with_attrs(snapshot, attrs):
    span = snapshot.traces.spans[0].model_copy(update={"attributes": attrs})
    return snapshot.model_copy(update={"traces": snapshot.traces.model_copy(update={"spans": (span,)})})


@pytest.mark.parametrize("flag", ["paymentUnreachable", "paymentFailure", "never-seen-flag-947"])
def test_complete_event_removed_without_name_dictionary(flag):
    snapshot = fixture(flag)
    before = snapshot.model_dump(mode="json")
    assert scan_leakage(before, injected_flags=[flag])
    result = sanitize_snapshot(snapshot)
    after = result.snapshot.model_dump(mode="json")
    assert result.removals == {"events_removed": 1, "attributes_removed": 0, "logs_removed": 0}
    assert not scan_leakage(after, injected_flags=[flag])
    assert not structural_blockers(result.snapshot)
    assert snapshot.model_dump(mode="json") == before
    serialized = canonical_bytes(after)
    for secret in (flag, "feature_flag.evaluation", "event-private-provider", "event-private-reason"):
        assert secret.encode() not in serialized
    assert len(after["traces"]["spans"][0]["attributes"]["aegis.otlp"]["events"]) == 1


def test_every_business_field_and_ordinary_event_preserved():
    before = fixture()
    after = sanitize_snapshot(before).snapshot
    first, second = before.traces.spans[0], after.traces.spans[0]
    assert first.model_dump(exclude={"attributes"}) == second.model_dump(exclude={"attributes"})
    assert second.attributes["rpc.grpc.status_code"] == 14
    assert second.attributes["rpc.service"] == "oteldemo.PaymentService"
    assert second.attributes["aegis.otlp"]["events"] == first.attributes["aegis.otlp"]["events"][1:]
    assert after.logs == before.logs and after.metrics == before.metrics
    assert after.incident == before.incident and after.provenance == before.provenance
    assert after.traces.warnings == before.traces.warnings and after.traces.truncated


@pytest.mark.parametrize("extra", [
    {"feature_flag.key": "unseen", "feature_flag.result.value": True},
    {"nested": {"feature_flag": {"key": "unseen", "result": {"value": True, "variant": "on"}}}},
    {"resource": [pair("feature_flag.key", {"stringValue": "unseen"}), pair("service.name", {"stringValue": "checkout"})]},
    {"nested": {"kvlistValue": {"values": [pair("feature_flag.result.value", {"boolValue": True})]}}},
    {"nested": {"arrayValue": {"values": [{"kvlistValue": {"values": [pair("feature_flag.key", {"stringValue": "unseen"})]}}]}}},
])
def test_nested_namespace_and_otlp_attributes_removed(extra):
    snapshot = fixture()
    attrs = {**snapshot.traces.spans[0].attributes, **extra}
    result = sanitize_snapshot(with_attrs(snapshot, attrs))
    assert result.removals["attributes_removed"] >= 1
    assert not structural_blockers(result.snapshot)
    assert not scan_leakage(result.snapshot.model_dump(mode="json"), injected_flags=["unseen"])


@pytest.mark.parametrize("events", [
    {"events": {"arrayValue": {"values": [control()]}}},
    {"events": [{"kvlistValue": {"values": [pair("name", {"stringValue": "feature_flag.evaluation"}),
        pair("attributes", {"kvlistValue": {"values": [pair("feature_flag.key", {"stringValue": "secret"})]}})]}}]},
    {"nested": {"kvlistValue": {"values": [pair("events", {"arrayValue": {"values": [control()]}})]}}},
])
def test_equivalent_nested_otlp_events_removed_whole(events):
    result = sanitize_snapshot(with_attrs(fixture(), events))
    assert result.removals["events_removed"] == 1
    assert not scan_leakage(result.snapshot.model_dump(mode="json"))
    assert b"provider-private" not in canonical_bytes(result.snapshot.model_dump(mode="json"))


def test_ordinary_names_are_not_generic_deletion_rules():
    attrs = {"flag": True, "flags": {"checkout": True}, "provider": "ordinary", "reason": "ordinary",
             "my_feature_flag": "business", "events": [{"name": "ordinary", "attributes": {"reason": "ordinary"}}]}
    result = sanitize_snapshot(with_attrs(fixture(), attrs))
    assert result.snapshot.traces.spans[0].attributes == attrs


@pytest.mark.parametrize("body", [
    "Business error includes feature_flag.key=paymentUnreachable but is ordinary text",
    "feature_flag.evaluation paymentUnreachable=true", "paymentUnreachable",
    '{"message":"ordinary business log", "feature_flag.key":"paymentUnreachable"}',
])
def test_ambiguous_log_text_is_preserved_and_reported(body):
    snapshot = fixture()
    log = snapshot.logs.logs[0].model_copy(update={"body": body})
    snapshot = snapshot.model_copy(update={"logs": LogResult(logs=(log,))})
    result = sanitize_snapshot(snapshot)
    assert result.snapshot.logs.logs == (log,)
    assert scan_payload(result.snapshot.model_dump(mode="json"), injected_flags=["paymentUnreachable"])["hits"]


@pytest.mark.parametrize("as_body", [True, False])
def test_only_structurally_identified_evaluation_logs_removed(as_body):
    snapshot = fixture()
    ordinary = snapshot.logs.logs[0]
    changes = {"body": json.dumps(control())} if as_body else {"attributes": {"event.name": "feature_flag.evaluation", "feature_flag.key": "secret"}}
    controlled = ordinary.model_copy(update={"id": "control", **changes})
    snapshot = snapshot.model_copy(update={"logs": LogResult(logs=(ordinary, controlled))})
    result = sanitize_snapshot(snapshot)
    assert result.snapshot.logs.logs == (ordinary,)
    assert result.removals["logs_removed"] == 1


def test_log_attributes_cleaned_without_changing_business_body_or_correlation():
    snapshot = fixture()
    log = snapshot.logs.logs[0].model_copy(update={"attributes": {"feature_flag": {"key": "x", "value": True}, "rpc.service": "payment"}})
    result = sanitize_snapshot(snapshot.model_copy(update={"logs": LogResult(logs=(log,))}))
    clean = result.snapshot.logs.logs[0]
    assert clean.model_dump(exclude={"attributes"}) == log.model_dump(exclude={"attributes"})
    assert clean.attributes == {"rpc.service": "payment"}


def test_metric_control_labels_not_deleted_or_merged_and_block_eligibility():
    snapshot = fixture()
    series = snapshot.metrics.series[0].model_copy(update={"labels": {"feature_flag_key": "secret", "feature_flag_result_value": "true"}})
    snapshot = snapshot.model_copy(update={"metrics": MetricResult(series=(series,))})
    result = sanitize_snapshot(snapshot)
    assert result.snapshot.metrics == snapshot.metrics
    assert structural_blockers(result.snapshot)


async def test_normal_snapshot_validation_replay_and_legacy_evidence_survive():
    snapshot = fixture()
    result = sanitize_snapshot(snapshot)
    restored = TelemetrySnapshot.model_validate_json(canonical_bytes(result.snapshot.model_dump(mode="json")))
    for signal in ("trace", "log", "metric"):
        task = make_task(snapshot.incident, signal)
        assert await ReplayBackend(restored).query(task) == await ReplayBackend(snapshot).query(task)
    traces = await ReplayBackend(restored).query(make_task(snapshot.incident, "trace"))
    assert traces.evidence[0].observation.code == "rpc_unavailable"


def test_deterministic_content_and_idempotence_without_mutating_raw_input():
    snapshot = fixture()
    raw = canonical_bytes(snapshot.model_dump(mode="json"))
    first, second = sanitize_snapshot(snapshot), sanitize_snapshot(snapshot)
    assert canonical_bytes(first.snapshot.model_dump(mode="json")) == canonical_bytes(second.snapshot.model_dump(mode="json"))
    assert first.removals == second.removals
    assert sanitize_snapshot(first.snapshot).snapshot == first.snapshot
    assert canonical_bytes(snapshot.model_dump(mode="json")) == raw


def recording(tmp_path, snapshot=None):
    root, episode = tmp_path / "recordings", str(uuid4())
    source = root / "quarantine" / episode / "snapshot.json"
    write_snapshot(snapshot or fixture(), source)
    return root, episode, source


def exported(tmp_path, snapshot=None, terms=None):
    root, episode, source = recording(tmp_path, snapshot)
    info = export_candidate(root, episode, terms=terms)
    return root, info["candidate_id"], source, info


def decision_file(root, candidate_id, *, decision="approve", **updates):
    review = root / "review" / candidate_id
    value = json.loads((review / "decision.template.json").read_text(encoding="utf-8"))
    value.update(decision=decision, reviewer="synthetic-test-reviewer", full_snapshot_reviewed=True)
    value.update(updates)
    path = review / (str(uuid4()) + ".json")
    path.write_bytes(canonical_bytes(value))
    return path


def test_export_separation_source_immutability_and_nonrevealing_sidecars(tmp_path):
    root, episode, source = recording(tmp_path, fixture("paymentUnreachable"))
    original = source.read_bytes()
    private = root / "private" / episode
    private.mkdir(parents=True)
    label = private / "injection.json"
    label.write_text("private-secret-original")
    info = export_candidate(root, episode, terms=ScanTerms(injected_flags=("paymentUnreachable",)))
    candidate_id = info["candidate_id"]
    assert str(UUID(candidate_id)) == candidate_id and candidate_id != episode
    assert source.read_bytes() == original and label.read_text() == "private-secret-original"
    candidate = root / "candidates" / candidate_id
    assert {p.name for p in candidate.iterdir()} == {"snapshot.json", "candidate.json"}
    public_bytes = b"".join(p.read_bytes() for p in candidate.iterdir())
    for term in (b"paymentUnreachable", episode.encode(), digest(original).encode(), b"source_episode", b"scan_terms"):
        assert term not in public_bytes
    snapshot = read_snapshot(candidate / "snapshot.json")
    assert set(snapshot.model_dump()) == set(fixture().model_dump())
    assert not info["benchmark_eligible"] and info["review_status"] == "pending_review"
    assert info["heuristic_candidates"] == 0 and info["scan_complete"]
    audit = json.loads((root / "review" / candidate_id / "audit.json").read_text())
    assert audit["source_sha256"] == digest(original) and audit["removals"]["events_removed"] == 1


def test_two_exports_have_identical_snapshot_bytes_and_new_opaque_ids(tmp_path):
    root, episode, _ = recording(tmp_path)
    first, second = export_candidate(root, episode), export_candidate(root, episode)
    assert first["candidate_id"] != second["candidate_id"]
    assert (root / "candidates" / first["candidate_id"] / "snapshot.json").read_bytes() == (root / "candidates" / second["candidate_id"] / "snapshot.json").read_bytes()


def test_zero_hits_still_requires_explicit_human_review(tmp_path):
    root, candidate_id, _, info = exported(tmp_path)
    assert not scan_candidate(root, candidate_id)["benchmark_eligible"]
    template = root / "review" / candidate_id / "decision.template.json"
    with pytest.raises(ValueError):
        review_candidate(root, candidate_id, template)
    result = review_candidate(root, candidate_id, decision_file(root, candidate_id))
    assert result["benchmark_eligible"] and result["review_status"] == "approved"
    assert scan_candidate(root, candidate_id)["benchmark_eligible"]
    with pytest.raises(FileExistsError):
        review_candidate(root, candidate_id, decision_file(root, candidate_id))


@pytest.mark.parametrize("updates", [{"full_snapshot_reviewed": False}, {"reviewer": " "}, {"bundle_sha256": "0" * 64}, {"scan_sha256": "0" * 64}])
def test_incomplete_or_stale_human_decision_rejected(tmp_path, updates):
    root, candidate_id, _, _ = exported(tmp_path)
    with pytest.raises(ValueError):
        review_candidate(root, candidate_id, decision_file(root, candidate_id, **updates))
    assert not (root / "review" / candidate_id / "decision.json").exists()


@pytest.mark.parametrize("classification", ["true_leakage", "unresolved", "non_leakage"])
def test_heuristic_dispositions_remain_distinct_from_confirmed_leakage(tmp_path, classification):
    snapshot = fixture()
    log = snapshot.logs.logs[0].model_copy(update={"body": "business explanation of feature_flag.key semantics"})
    snapshot = snapshot.model_copy(update={"logs": LogResult(logs=(log,))})
    root, candidate_id, _, info = exported(tmp_path, snapshot)
    assert info["heuristic_candidates"] > 0 and info["structural_blockers"] == 0
    dispositions = [{"index": i, "classification": classification, "reason": "Synthetic reviewer disposition"}
                    for i in range(info["heuristic_candidates"])]
    path = decision_file(root, candidate_id, dispositions=dispositions)
    if classification == "non_leakage":
        assert review_candidate(root, candidate_id, path)["benchmark_eligible"]
    else:
        with pytest.raises(ValueError):
            review_candidate(root, candidate_id, path)
        assert not scan_candidate(root, candidate_id)["benchmark_eligible"]


def test_structural_blocker_cannot_be_overridden_as_benign(tmp_path):
    snapshot = fixture()
    series = snapshot.metrics.series[0].model_copy(update={"labels": {"feature_flag_key": "secret"}})
    root, candidate_id, _, info = exported(tmp_path, snapshot.model_copy(update={"metrics": MetricResult(series=(series,))}))
    dispositions = [{"index": i, "classification": "non_leakage", "reason": "attempted override"} for i in range(info["heuristic_candidates"])]
    with pytest.raises(ValueError):
        review_candidate(root, candidate_id, decision_file(root, candidate_id, dispositions=dispositions))
    assert not scan_candidate(root, candidate_id)["benchmark_eligible"]


def test_post_scan_exact_cap_is_incomplete_and_blocks_approval(tmp_path):
    snapshot = fixture()
    logs = tuple(snapshot.logs.logs[0].model_copy(update={"id": str(i), "body": "feature_flag.key in business text"}) for i in range(100))
    root, candidate_id, _, info = exported(tmp_path, snapshot.model_copy(update={"logs": LogResult(logs=logs)}))
    assert info["heuristic_candidates"] == 100 and info["scan_truncated"] and not info["scan_complete"]
    dispositions = [{"index": i, "classification": "non_leakage", "reason": "still cannot override the cap"} for i in range(100)]
    with pytest.raises(ValueError):
        review_candidate(root, candidate_id, decision_file(root, candidate_id, dispositions=dispositions))


def test_incomplete_scan_with_zero_hits_is_not_clean(monkeypatch, tmp_path):
    monkeypatch.setattr("aegis_rca.evaluation.sanitization.scan_leakage", lambda *a, **k: (_ for _ in ()).throw(ValueError("synthetic failure")))
    root, candidate_id, _, info = exported(tmp_path)
    assert info["heuristic_candidates"] == 0 and not info["scan_complete"] and info["scan_truncated"]
    with pytest.raises(ValueError):
        review_candidate(root, candidate_id, decision_file(root, candidate_id))


def test_human_rejection_persisted_as_ineligible(tmp_path):
    root, candidate_id, _, _ = exported(tmp_path)
    result = review_candidate(root, candidate_id, decision_file(root, candidate_id, decision="reject", full_snapshot_reviewed=False))
    assert result["review_status"] == "ineligible" and not result["benchmark_eligible"]
    assert not scan_candidate(root, candidate_id)["benchmark_eligible"]


@pytest.mark.parametrize("filename", ["snapshot.json", "candidate.json"])
def test_candidate_byte_changes_invalidate_prior_approval(tmp_path, filename):
    root, candidate_id, _, _ = exported(tmp_path)
    review_candidate(root, candidate_id, decision_file(root, candidate_id))
    target = root / "candidates" / candidate_id / filename
    target.write_bytes(target.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="changed"):
        scan_candidate(root, candidate_id)


def test_unreviewed_public_files_are_rejected(tmp_path):
    root, candidate_id, _, _ = exported(tmp_path)
    (root / "candidates" / candidate_id / "injection.json").write_text("secret")
    with pytest.raises(ValueError, match="only"):
        scan_candidate(root, candidate_id)


@pytest.mark.parametrize("episode", ["paymentUnreachable", "../private", "../00000000-0000-4000-8000-000000000001", "ABCDEFAB-0000-4000-8000-000000000001"])
def test_nonopaque_or_traversal_identifiers_rejected_before_writes(tmp_path, episode):
    root = tmp_path / "recordings"
    with pytest.raises(ValueError):
        export_candidate(root, episode)
    assert not root.exists()


def test_output_collision_never_overwrites_existing_files(tmp_path, monkeypatch):
    root, episode, source = recording(tmp_path)
    original = source.read_bytes()
    fixed = uuid4()
    target = root / "candidates" / str(fixed)
    target.mkdir(parents=True)
    (target / "snapshot.json").write_text("prior bytes")
    monkeypatch.setattr("aegis_rca.evaluation.benchmark.uuid4", lambda: fixed)
    with pytest.raises(FileExistsError):
        export_candidate(root, episode)
    assert (target / "snapshot.json").read_text() == "prior bytes" and source.read_bytes() == original


def test_duplicate_json_keys_rejected_before_export(tmp_path):
    root, episode, source = recording(tmp_path)
    original = source.read_bytes()
    source.write_bytes(original.replace(b'{', b'{"schema_version":1,', 1))
    with pytest.raises(ValueError, match="duplicate"):
        export_candidate(root, episode)
    assert not (root / "candidates").exists()


def test_processing_depth_exhaustion_fails_closed():
    value = {}
    for _ in range(70):
        value = {"nested": value}
    assert not scan_payload(value)["scan_complete"]
    with pytest.raises(ValueError, match="bounds"):
        sanitize_snapshot(with_attrs(fixture(), value))


def test_symlink_source_or_output_is_rejected_if_platform_supports_it(tmp_path):
    root, episode, source = recording(tmp_path)
    original = source.read_bytes()
    target = tmp_path / "linked-target"
    target.mkdir()
    link = root / "candidates"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation requires OS privileges")
    with pytest.raises(ValueError, match="symlinks"):
        export_candidate(root, episode)
    assert not list(target.iterdir()) and source.read_bytes() == original


def test_cli_sanitize_scan_review_and_private_vocabulary(tmp_path, capsys):
    root, episode, _ = recording(tmp_path, fixture("secret-new-flag"))
    terms = tmp_path / "private-terms.json"
    terms.write_bytes(canonical_bytes({"injected_flags": ["secret-new-flag"], "scenario_answers": []}))
    main(["sanitize", "--recordings-root", str(root), "--episode", episode, "--terms-file", str(terms)])
    info = json.loads(capsys.readouterr().out)
    candidate_id = info["candidate_id"]
    main(["scan", "--recordings-root", str(root), "--candidate", candidate_id])
    assert not json.loads(capsys.readouterr().out)["benchmark_eligible"]
    decision = decision_file(root, candidate_id)
    main(["review", "--recordings-root", str(root), "--candidate", candidate_id, "--decision-file", str(decision)])
    assert json.loads(capsys.readouterr().out)["benchmark_eligible"]
    assert load_terms(terms).injected_flags == ("secret-new-flag",)


def test_cli_errors_do_not_echo_injected_labels(tmp_path, capsys):
    with pytest.raises(SystemExit):
        main(["sanitize", "--recordings-root", str(tmp_path), "--episode", "paymentUnreachable"])
    assert "paymentUnreachable" not in capsys.readouterr().err


@pytest.mark.parametrize("count,complete", [(99, True), (100, False), (101, False)])
def test_scan_cap_boundary_is_conservative(count, complete):
    result = scan_payload(["feature_flag.key" for _ in range(count)])
    assert result["scan_complete"] is complete
    assert result["scan_truncated"] is not complete
    assert len(result["hits"]) == min(count, 100)


def test_reparse_point_guard_runs_without_symlink_creation_privileges(tmp_path, monkeypatch):
    root, episode, source = recording(tmp_path)
    original = source.read_bytes()
    target = root / "candidates"
    original_lstat = Path.lstat
    def simulated_reparse(path):
        if path == target:
            return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
        return original_lstat(path)
    monkeypatch.setattr(Path, "lstat", simulated_reparse)
    with pytest.raises(ValueError, match="junctions"):
        export_candidate(root, episode)
    assert source.read_bytes() == original


def test_export_failure_leaves_no_reviewable_candidate(tmp_path, monkeypatch):
    from aegis_rca.evaluation import benchmark
    root, episode, source = recording(tmp_path)
    original = source.read_bytes()
    write = benchmark._write
    def fail(path, content):
        if path.name == "audit.json":
            raise OSError("synthetic write failure")
        write(path, content)
    monkeypatch.setattr(benchmark, "_write", fail)
    with pytest.raises(OSError):
        export_candidate(root, episode)
    candidate_id = next((root / "candidates").iterdir()).name
    assert not (root / "review" / candidate_id / "ready.json").exists()
    with pytest.raises(FileNotFoundError):
        scan_candidate(root, candidate_id)
    assert source.read_bytes() == original


def test_frozen_scan_vocabulary_cannot_be_silently_changed(tmp_path):
    root, candidate_id, _, _ = exported(tmp_path)
    path = root / "review" / candidate_id / "audit.json"
    audit = json.loads(path.read_bytes())
    audit["scan_terms"] = {"injected_flags": ["new-term"]}
    path.write_bytes(canonical_bytes(audit))
    with pytest.raises(ValueError, match="scan changed"):
        scan_candidate(root, candidate_id)


@pytest.mark.parametrize("value", ["", " " * 10, "x" * 2001])
def test_scan_vocabulary_is_bounded(value):
    with pytest.raises(ValueError):
        ScanTerms(injected_flags=(value,))


def test_malformed_otlp_control_container_fails_closed():
    snapshot = with_attrs(fixture(), {"events": [{"kvlistValue": None}]})
    with pytest.raises(ValueError, match="malformed"):
        sanitize_snapshot(snapshot)


def test_no_full_pipeline_diagnosis_change_after_sanitization_is_required():
    # ERROR/UNKNOWN is preserved without introducing a new fault detector.
    snapshot = fixture()
    attrs = dict(snapshot.traces.spans[0].attributes)
    attrs["rpc.grpc.status_code"] = 2
    result = sanitize_snapshot(with_attrs(snapshot, attrs))
    assert result.snapshot.traces.spans[0].status == "ERROR"
    assert result.snapshot.traces.spans[0].attributes["rpc.grpc.status_code"] == 2


def test_nested_control_metadata_in_business_json_log_is_retained_but_blocked():
    snapshot = fixture()
    body = '{"message":"business failure", "context":{"feature_flag":{"key":"new-control", "value":true}}}'
    log = snapshot.logs.logs[0].model_copy(update={"body": body})
    cleaned = sanitize_snapshot(snapshot.model_copy(update={"logs": LogResult(logs=(log,))})).snapshot
    assert cleaned.logs.logs[0].body == body
    assert structural_blockers(cleaned)


def test_provider_and_reason_outside_control_events_remain_untouched():
    snapshot = fixture()
    attrs = {"feature_flag.key": "secret", "provider": "ordinary-provider", "reason": "ordinary-reason"}
    cleaned = sanitize_snapshot(with_attrs(snapshot, attrs)).snapshot
    assert cleaned.traces.spans[0].attributes == {"provider": "ordinary-provider", "reason": "ordinary-reason"}


def test_removal_manifest_has_original_event_and_attribute_locations():
    attrs = {"events": [control(), {"name": "ordinary"}, control()],
             "feature_flag.key": "secret-key", "nested": [pair("feature_flag.result.value", {"boolValue": True})]}
    result = sanitize_snapshot(with_attrs(fixture(), attrs))
    assert result.removal_manifest == (
        {"path": "$.traces.spans[0].attributes{0}[0]", "rule": "control_event"},
        {"path": "$.traces.spans[0].attributes{0}[2]", "rule": "control_event"},
        {"path": "$.traces.spans[0].attributes{1}", "rule": "control_attribute"},
        {"path": "$.traces.spans[0].attributes{2}[0]", "rule": "control_attribute"},
    )
    assert result.removals == {"events_removed": 2, "attributes_removed": 2, "logs_removed": 0}


def test_removal_manifest_logs_use_source_indices_before_removal():
    snapshot = fixture()
    ordinary = snapshot.logs.logs[0]
    control_log = ordinary.model_copy(update={"body": json.dumps(control())})
    attributed = ordinary.model_copy(update={"attributes": {"feature_flag.key": "secret"}})
    snapshot = snapshot.model_copy(update={"logs": LogResult(logs=(control_log, ordinary, attributed))})
    result = sanitize_snapshot(snapshot)
    assert {"path": "$.logs.logs[0]", "rule": "control_log"} in result.removal_manifest
    assert {"path": "$.logs.logs[2].attributes{0}", "rule": "control_attribute"} in result.removal_manifest
    assert len(result.removal_manifest) == sum(result.removals.values())


def test_removal_manifest_never_copies_dynamic_keys_or_secret_payloads():
    secrets = ("secret-ancestor", "private-flag-name", "private-result", "private-variant", "private-log-body")
    attrs = {secrets[0]: {"feature_flag." + secrets[1]: secrets[2]},
             "events": [control(secrets[1])], "feature_flag.result.variant": secrets[3]}
    snapshot = with_attrs(fixture(), attrs)
    log = snapshot.logs.logs[0].model_copy(update={"body": secrets[4],
        "attributes": {"event.name": "feature_flag.evaluation", "feature_flag.key": secrets[1]}})
    result = sanitize_snapshot(snapshot.model_copy(update={"logs": LogResult(logs=(log,))}))
    encoded = canonical_bytes(result.removal_manifest)
    for secret in (*secrets, "provider-private", "event-private-reason", "feature_flag", log.trace_id):
        assert secret.encode() not in encoded
    assert all(set(entry) == {"path", "rule"} for entry in result.removal_manifest)
    assert {entry["rule"] for entry in result.removal_manifest} == {"control_event", "control_attribute", "control_log"}


def test_removal_manifest_is_deterministic_across_map_insertion_order():
    attrs = {"feature_flag.key": "secret", "events": [control()],
             "nested": {"z": "ordinary", "feature_flag.value": True}}
    reordered = {key: (dict(reversed(list(value.items()))) if isinstance(value, dict) else value)
                 for key, value in reversed(list(attrs.items()))}
    first = sanitize_snapshot(with_attrs(fixture(), attrs))
    second = sanitize_snapshot(with_attrs(fixture(), reordered))
    assert canonical_bytes(first.removal_manifest) == canonical_bytes(second.removal_manifest)
    assert canonical_bytes(first.snapshot.model_dump(mode="json")) == canonical_bytes(second.snapshot.model_dump(mode="json"))


def test_manifest_is_persisted_only_in_private_audit_and_public_bytes_stay_compatible(tmp_path):
    root, candidate_id, _, _ = exported(tmp_path)
    candidate = root / "candidates" / candidate_id
    snapshot_bytes = (candidate / "snapshot.json").read_bytes()
    # Captured from the synthetic fixture before removal-manifest support.
    old_snapshot_hash = "6a3c76157ad3a7a11bccc6677bc4e2b7281a7f155a67098d22c68d821fe229da"
    assert digest(snapshot_bytes) == old_snapshot_hash
    expected_metadata = {"format_version": 1, "artifact_kind": "sanitized_benchmark_candidate",
        "candidate_id": candidate_id, "derived_from": "quarantine_snapshot", "ruleset": "control-metadata-v1",
        "snapshot_sha256": old_snapshot_hash}
    assert (candidate / "candidate.json").read_bytes() == canonical_bytes(expected_metadata)
    assert {path.name for path in candidate.iterdir()} == {"snapshot.json", "candidate.json"}
    audit = json.loads((root / "review" / candidate_id / "audit.json").read_bytes())
    manifest = audit["removal_manifest"]
    assert manifest == {"path_format": "source-positions-v1", "entries": [
        {"path": "$.traces.spans[0].attributes{0}{0}[0]", "rule": "control_event"}]}
    assert b"unseen-control-793" not in canonical_bytes(manifest)
    assert all(entry["path"].encode() not in snapshot_bytes for entry in manifest["entries"])


def test_clean_snapshot_has_empty_removal_manifest():
    cleaned = sanitize_snapshot(fixture()).snapshot
    result = sanitize_snapshot(cleaned)
    assert result.removal_manifest == () and sum(result.removals.values()) == 0
