"""Archival wire integrity is separate from current semantic producers."""

import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from aegis_rca import graph
from aegis_rca.acquisition_store import AcquisitionStore
from aegis_rca.fixtures import FixtureBackend, demo_incident
from aegis_rca.models.acquisition_result import CommittedAcquisition
from aegis_rca.models.telemetry import MetricResult, MetricSeries, MetricPoint
from aegis_rca.policy import make_task
from aegis_rca.runner import run_investigation
from aegis_rca.runtime import Dependencies
from aegis_rca.state import initial_state
from aegis_rca.task_materialization import materialize_task
from aegis_rca.tools import snapshot_v2 as archive
from aegis_rca.tools.snapshot import read_snapshot


@pytest.fixture
async def history():
    deps = Dependencies(FixtureBackend())
    state, _ = await run_investigation(initial_state(demo_incident(), execution_id="archive-history"), deps)
    return state, deps, archive.export_snapshot_v2(state, deps)


def reseal(snapshot, **updates):
    fields = {k: getattr(snapshot, k) for k in ("incident", "historical_execution_id", "provenance",
                                               "task_specs", "recorded_acquisitions", "investigation_scope")}
    return archive.SnapshotV2.seal(**{**fields, **updates})


def replace_row(snapshot, row):
    return reseal(snapshot, recorded_acquisitions=tuple(row if r.attempt.id == row.attempt.id else r
                                                       for r in snapshot.recorded_acquisitions))


async def test_distinct_archival_type_and_explicit_export_field_allowlist(history, monkeypatch):
    state, deps, snapshot = history
    assert not issubclass(archive.RecordedAcquisitionV2, CommittedAcquisition)
    values = deps.acquisitions.values()
    future = [SimpleNamespace(attempt=c.attempt, tool_call=c.tool_call, evidence=c.evidence,
        partition=c.partition, recorded_from=c.recorded_from, runtime_diagnostics="must stay runtime") for c in values]
    monkeypatch.setattr(deps.acquisitions, "values", lambda: future)
    assert archive.export_snapshot_v2(state, deps) == snapshot
    assert "runtime_diagnostics" not in snapshot.model_dump_json()
    row = snapshot.recorded_acquisitions[0]
    with pytest.raises(ValidationError):
        archive.RecordedAcquisitionV2.model_validate({**row.model_dump(), "runtime_diagnostics": "secret"})
    assert not {"final_report", "hypotheses", "connectivity_verification"} & snapshot.model_dump().keys()


@pytest.mark.parametrize("display", ["warnings", "excerpt", "locator", "alias", "goal", "incident_prose", "raw_warnings"])
async def test_display_only_changes_preserve_manifest_but_change_archive_digest(history, display):
    _, _, snapshot = history
    row = next(r for r in snapshot.recorded_acquisitions if r.admitted_evidence)
    if display == "warnings":
        row = row.model_copy(update={"attempt": row.attempt.model_copy(update={"warnings": ("new prose",)}),
            "tool_call": row.tool_call.model_copy(update={"warnings": ("new prose",)})})
    elif display in {"excerpt", "locator"}:
        e = row.admitted_evidence[0]
        e = e.model_copy(update={"excerpt": "new human prose"} if display == "excerpt" else
            {"source": e.source.model_copy(update={"locator": "another/archive/location"})})
        row = row.model_copy(update={"admitted_evidence": (e, *row.admitted_evidence[1:])})
    elif display == "alias":
        changed = reseal(snapshot, task_specs=tuple(t.model_copy(update={"alias": "new display alias"})
                                                   for t in snapshot.task_specs))
    elif display == "goal":
        row = row.model_copy(update={"tool_call": row.tool_call.model_copy(update={"task":
            row.tool_call.task.model_copy(update={"goal": "different human goal"})})})
    elif display == "incident_prose":
        changed = reseal(snapshot, incident=snapshot.incident.model_copy(update={"query": "new prose"}))
    else:
        part = row.partition.model_copy(update={"material": row.partition.material.model_copy(
            update={"warnings": ("new raw diagnostic warning",)})})
        row = row.model_copy(update={"partition": part, "partition_content_digest": part.content_digest})
    if display not in {"alias", "incident_prose"}:
        changed = replace_row(snapshot, row)
    assert changed.replay_manifest_id == snapshot.replay_manifest_id
    assert changed.archive_content_digest != snapshot.archive_content_digest
    from aegis_rca.tools.recorded_replay import RecordedReplayBackend
    old = materialize_task(make_task(snapshot.incident, "trace"), RecordedReplayBackend(snapshot))
    new = materialize_task(make_task(changed.incident, "trace"), RecordedReplayBackend(changed))
    assert old.id == new.id


async def test_raw_change_changes_both_declared_identities(history):
    _, _, snapshot = history
    row = next(r for r in snapshot.recorded_acquisitions if r.partition.signal == "metric")
    material = MetricResult(series=(MetricSeries(name="different", source="synthetic", points=(
        MetricPoint(timestamp=demo_incident().window.start, value=9),)),))
    part = row.partition.model_copy(update={"material": material})
    changed = replace_row(snapshot, row.model_copy(update={"partition": part, "partition_content_digest": part.content_digest}))
    assert snapshot.replay_manifest_id != changed.replay_manifest_id
    assert snapshot.archive_content_digest != changed.archive_content_digest


@pytest.mark.parametrize("field,prefix", [("replay_manifest_id", "RM-"), ("archive_content_digest", "sha256-")])
async def test_declared_digests_must_match_on_load(history, field, prefix):
    snapshot = history[2]
    payload = snapshot.model_dump(mode="json")
    payload[field] = prefix + "0" * 64
    with pytest.raises(ValidationError, match="declaration mismatch"):
        archive.SnapshotV2.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize("damage", ["attempt_namespace", "missing_task", "duplicate_attempt", "duplicate_task",
                                  "partition_ref", "partition_digest", "scope_witness", "missing_partition"])
async def test_structural_cross_references_cannot_be_resealed_as_valid(history, damage):
    snapshot = history[2]
    row = next(r for r in snapshot.recorded_acquisitions if r.partition.signal == "trace")
    with pytest.raises(ValueError):
        if damage == "missing_task":
            reseal(snapshot, task_specs=())
        elif damage == "duplicate_task":
            reseal(snapshot, task_specs=(*snapshot.task_specs, snapshot.task_specs[0]))
        elif damage == "duplicate_attempt":
            reseal(snapshot, recorded_acquisitions=(*snapshot.recorded_acquisitions, row))
        elif damage == "attempt_namespace":
            changed = row.model_copy(update={"attempt": row.attempt.model_copy(update={"execution_id": "foreign"})})
            reseal(snapshot, recorded_acquisitions=tuple(changed if r == row else r for r in snapshot.recorded_acquisitions))
        elif damage == "partition_ref":
            part = row.partition.model_copy(update={"attempt_id": "A-" + "0" * 64})
            replace_row(snapshot, row.model_copy(update={"partition": part}))
        elif damage == "partition_digest":
            replace_row(snapshot, row.model_copy(update={"partition_content_digest": "sha256-" + "0" * 64}))
        elif damage == "missing_partition":
            replace_row(snapshot, row.model_copy(update={"partition": None, "partition_content_digest": None}))
        else:
            scope = snapshot.investigation_scope
            reseal(snapshot, investigation_scope=scope.model_copy(update={"expansions": (
                scope.expansions[0].model_copy(update={"canonical_witness_attempt_id": "A-" + "0" * 64}),)}))


@pytest.mark.parametrize("damage", ["missing_store", "extra_started", "attempt_warning", "evidence_excerpt", "missing_spec", "scope_stale"])
async def test_export_state_store_mismatch_does_not_publish(history, tmp_path, damage):
    state, deps, _ = history
    state = dict(state)
    if damage == "missing_store":
        deps = Dependencies(deps.backend)
    elif damage == "extra_started":
        deps.started_attempts.add("A-" + "0" * 64)
    elif damage == "attempt_warning":
        state["attempt_records"] = [a.model_copy(update={"warnings": ("changed",)}) for a in state["attempt_records"]]
        state["tool_calls"] = [c.model_copy(update={"warnings": ("changed",)}) for c in state["tool_calls"]]
    elif damage == "evidence_excerpt":
        state["evidence"] = [e.model_copy(update={"excerpt": "changed"}) for e in state["evidence"]]
    elif damage == "missing_spec":
        state["task_specs"] = []
    else:
        state["incident"] = state["incident"].model_copy(update={"seed_services": ("payment",)})
    target = tmp_path / "snapshot.v2.json"
    with pytest.raises(ValueError):
        archive.write_snapshot_v2(archive.export_snapshot_v2(state, deps), target)
    assert not target.exists() and not list(tmp_path.iterdir())


async def test_deterministic_sorted_roundtrip_and_path_independence(history, tmp_path):
    state, deps, snapshot = history
    before = tuple(c.model_dump_json() for c in deps.acquisitions.values())
    reversed_state = {**state, "task_specs": list(reversed(state["task_specs"])),
        "attempt_records": list(reversed(state["attempt_records"])), "evidence": list(reversed(state["evidence"]))}
    reverse = archive.export_snapshot_v2(reversed_state, deps)
    first, second = tmp_path / "a.json", tmp_path / "elsewhere" / "moved.json"
    archive.write_snapshot_v2(snapshot, first)
    loaded = archive.load_recording(first)
    archive.write_snapshot_v2(reverse, second)
    assert first.read_bytes() == second.read_bytes()
    assert loaded == archive.read_snapshot_v2(second) == snapshot
    assert [t.id for t in loaded.task_specs] == sorted(t.id for t in loaded.task_specs)
    assert [r.attempt.id for r in loaded.recorded_acquisitions] == sorted(r.attempt.id for r in loaded.recorded_acquisitions)
    assert before == tuple(c.model_dump_json() for c in deps.acquisitions.values())
    with pytest.raises(FileExistsError):
        archive.write_snapshot_v2(snapshot, first)
    assert first.read_bytes() == second.read_bytes()
    assert not list(tmp_path.glob(".aegis-v2-*.tmp"))


@pytest.mark.parametrize("stage", ["fsync", "link"])
async def test_atomic_write_failure_has_no_partial_target(history, tmp_path, monkeypatch, stage):
    def fail(*a, **kw):
        raise OSError("injected publication failure")
    monkeypatch.setattr(archive.os, stage, fail)
    with pytest.raises(OSError):
        archive.write_snapshot_v2(history[2], tmp_path / "snapshot.v2.json")
    assert list(tmp_path.iterdir()) == []


async def test_invalid_archive_rejected_before_creating_directory(history, tmp_path):
    broken = history[2].model_copy(update={"archive_content_digest": "sha256-" + "0" * 64})
    with pytest.raises(ValueError):
        archive.write_snapshot_v2(broken, tmp_path / "new" / "a.json")
    assert list(tmp_path.iterdir()) == []


async def test_load_does_not_run_current_observation_projection_or_verifier(history, monkeypatch):
    import aegis_rca.trace_acquisition as native
    import aegis_rca.tools.projection as projection
    import aegis_rca.verification.connectivity as verification
    def forbidden(*a, **kw):
        pytest.fail("current semantic producer used as archival corruption check")
    monkeypatch.setattr(native, "observe_spans", forbidden)
    monkeypatch.setattr(projection, "project_material", forbidden)
    monkeypatch.setattr(verification, "evaluate_connectivity", forbidden)
    assert archive.SnapshotV2.model_validate_json(history[2].model_dump_json()) == history[2]
    # Historical ToolResult is not stored in full: retain, do not reconstruct its digest.
    row = history[2].recorded_acquisitions[0]
    changed = replace_row(history[2], row.model_copy(update={"attempt": row.attempt.model_copy(
        update={"tool_result_digest": "sha256-" + "a" * 64})}))
    assert changed.replay_manifest_id == history[2].replay_manifest_id


async def test_v1_reader_and_benchmark_reject_v2(history, tmp_path):
    from uuid import uuid4
    from aegis_rca.evaluation.benchmark import export_candidate, _benchmark_snapshot
    snapshot = history[2]
    target = tmp_path / "v2.json"
    archive.write_snapshot_v2(snapshot, target)
    with pytest.raises(ValueError):
        read_snapshot(target)
    with pytest.raises(ValueError, match="V1 only"):
        _benchmark_snapshot(snapshot.model_dump(mode="json"))
    episode = str(uuid4())
    quarantine = tmp_path / "recordings" / "quarantine" / episode
    quarantine.mkdir(parents=True)
    (quarantine / "snapshot.json").write_bytes(target.read_bytes())
    with pytest.raises(ValueError, match="V1 only"):
        export_candidate(tmp_path / "recordings", episode)
    assert not (tmp_path / "recordings" / "candidates").exists()


@pytest.mark.parametrize("scenario,expected", [("connectivity", "SUPPORTED"), ("missing-logs", "INCONCLUSIVE"),
                                               ("empty", "INCONCLUSIVE")])
async def test_cli_fixture_and_v2_replay_export_current_v2(tmp_path, scenario, expected):
    from aegis_rca.cli import run
    args = dict(mode="fixture", scenario=scenario, incident=None, snapshot=None, backends=None,
        llm=False, max_tool_calls=6, max_rounds=3, tool_timeout=5, deadline=10)
    await run(SimpleNamespace(**args, output=tmp_path / "fixture"))
    path = tmp_path / "fixture" / "snapshot.v2.json"
    fixture = archive.read_snapshot_v2(path)
    assert fixture.provenance == "fixture_investigation" and not (path.parent / "snapshot.json").exists()
    args.update(mode="replay", snapshot=path)
    await run(SimpleNamespace(**args, output=tmp_path / "replay"))
    replay = archive.read_snapshot_v2(tmp_path / "replay" / "snapshot.v2.json")
    assert json.loads((tmp_path / "replay" / "report.json").read_text())["status"] == expected
    assert replay.historical_execution_id != fixture.historical_execution_id
    assert all(t.schema_version == 2 for t in replay.task_specs)
    assert all(r.recorded_from for r in replay.recorded_acquisitions)


async def test_live_cli_export_does_not_call_mutable_snapshot(tmp_path, monkeypatch):
    from aegis_rca.cli import run
    from aegis_rca.tools.live import LiveBackend
    from aegis_rca.settings import BackendSettings
    from aegis_rca.fixtures import demo_traces
    monkeypatch.setattr(LiveBackend, "snapshot", lambda _: pytest.fail("V2 exported from mutable buffers"))
    from aegis_rca.adapters import JaegerAdapter, OpenSearchAdapter
    monkeypatch.setattr(JaegerAdapter, "search", lambda *a, **kw: demo_traces())
    monkeypatch.setattr(OpenSearchAdapter, "search", lambda *a, **kw: FixtureBackend().logs)
    incident, settings = tmp_path / "incident.json", tmp_path / "backends.json"
    incident.write_text(demo_incident().model_dump_json())
    settings.write_text(BackendSettings(prometheus_url="http://metrics.test", jaeger_url="http://trace.test",
                                      opensearch_url="http://log.test").model_dump_json())
    await run(SimpleNamespace(mode="live", scenario="connectivity", incident=incident, snapshot=None,
        backends=settings, llm=False, max_tool_calls=6, max_rounds=3, tool_timeout=5, deadline=10, output=tmp_path / "run"))
    assert archive.read_snapshot_v2(tmp_path / "run" / "snapshot.v2.json").provenance == "live_investigation"


async def test_standalone_collect_still_writes_v1_without_execution(tmp_path, monkeypatch):
    import aegis_rca.collect as collect
    from aegis_rca.tools.snapshot import TelemetrySnapshot
    from aegis_rca.models.evidence import ToolResult
    class Backend:
        def __init__(self, *a):
            pass
        async def query(self, task):
            return ToolResult(status="empty")
        def snapshot(self):
            return TelemetrySnapshot(incident=demo_incident(), provenance="live_capture")
        def close(self):
            pass
    monkeypatch.setattr(collect, "LiveBackend", Backend)
    monkeypatch.setattr(collect, "load_settings", lambda _: None)
    monkeypatch.setattr(collect, "load_incident", lambda _: demo_incident())
    path = tmp_path / "capture.json"
    assert await collect.collect(SimpleNamespace(action="capture", incident="synthetic", backends=None, output=path)) == 0
    payload = json.loads(path.read_text())
    assert payload["schema_version"] == 1
    assert not {"historical_execution_id", "recorded_acquisitions", "task_specs"} & payload.keys()
