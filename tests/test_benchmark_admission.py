"""Synthetic admission tests. No reads of private or quarantined recordings."""
import json
from uuid import uuid4

import pytest

import aegis_rca.evaluation.benchmark as benchmark
from aegis_rca.evaluation.__main__ import main
from aegis_rca.evaluation.benchmark import (admission_metadata, open_approved_candidate,
    review_candidate, scan_candidate)
from aegis_rca.evaluation.sanitization import RULESET, canonical_bytes
from aegis_rca.models.telemetry import LogResult, MetricResult
from aegis_rca.tools.snapshot import TelemetrySnapshot

from test_sanitization import decision_file, exported, fixture, recording


def approved(tmp_path, snapshot=None):
    root, candidate_id, source, info = exported(tmp_path, snapshot)
    review_candidate(root, candidate_id, decision_file(root, candidate_id))
    return root, candidate_id, source, info


def forge_decision(root, candidate_id, **updates):
    review = root / "review" / candidate_id
    value = json.loads((review / "decision.template.json").read_text(encoding="utf-8"))
    value.update(decision="approve", reviewer="synthetic-test-reviewer", full_snapshot_reviewed=True)
    value.update(updates)
    (review / "decision.json").write_bytes(canonical_bytes(value))


def test_admission_returns_in_memory_snapshot_from_validated_bytes(tmp_path, monkeypatch):
    root, candidate_id, _, _ = approved(tmp_path)
    snapshot_reads = []
    parsed = []
    original_read = benchmark._read_bytes
    original_validate = TelemetrySnapshot.model_validate

    def counting_read(path):
        data = original_read(path)
        if path.name == "snapshot.json":
            snapshot_reads.append(bytes(data))
            if len(snapshot_reads) > 1:
                raise AssertionError("snapshot.json was reread after the validated byte load")
        return data

    def capturing_validate(data):
        snapshot = original_validate(data)
        parsed.append(snapshot)
        return snapshot

    monkeypatch.setattr(benchmark, "_read_bytes", counting_read)
    monkeypatch.setattr(TelemetrySnapshot, "model_validate", capturing_validate)
    admitted = open_approved_candidate(root, candidate_id)
    assert len(snapshot_reads) == 1
    assert parsed == [admitted.snapshot]
    assert admitted.snapshot is parsed[0]
    assert canonical_bytes(admitted.snapshot.model_dump(mode="json")) == snapshot_reads[0]


def test_admitted_snapshot_does_not_require_rereading_candidate_files(tmp_path):
    root, candidate_id, _, _ = approved(tmp_path)
    admitted = open_approved_candidate(root, candidate_id)
    expected = admitted.snapshot
    (root / "candidates" / candidate_id / "snapshot.json").write_bytes(b"{}")
    (root / "review" / candidate_id / "decision.json").unlink()
    assert admitted.snapshot is expected
    assert admitted.snapshot.model_dump(mode="json") == expected.model_dump(mode="json")


def test_approved_candidate_admits_with_nonsecret_metadata(tmp_path):
    root, candidate_id, _, info = approved(tmp_path)
    admitted = open_approved_candidate(root, candidate_id)
    status = admission_metadata(admitted)
    assert status == {
        "candidate_id": candidate_id,
        "review_status": "approved",
        "benchmark_eligible": True,
        "bundle_sha256": info["bundle_sha256"],
        "scan_sha256": info["scan_sha256"],
        "ruleset": RULESET,
    }
    assert admitted.bundle_sha256 == info["bundle_sha256"]
    assert set(status) == {"candidate_id", "review_status", "benchmark_eligible",
                           "bundle_sha256", "scan_sha256", "ruleset"}


def test_pending_rejected_and_missing_decision_fail_admission(tmp_path):
    root, candidate_id, _, _ = exported(tmp_path)
    with pytest.raises(ValueError, match="not approved"):
        open_approved_candidate(root, candidate_id)
    review_candidate(root, candidate_id, decision_file(root, candidate_id, decision="reject",
                                                       full_snapshot_reviewed=False))
    with pytest.raises(ValueError, match="not approved"):
        open_approved_candidate(root, candidate_id)
    other_root, other_id, _, _ = exported(tmp_path / "other")
    (other_root / "review" / other_id / "decision.template.json").unlink()
    with pytest.raises(ValueError, match="not approved"):
        open_approved_candidate(other_root, other_id)


def test_stale_bytes_invalidate_admission(tmp_path):
    root, candidate_id, _, _ = approved(tmp_path)
    target = root / "candidates" / candidate_id / "snapshot.json"
    target.write_bytes(target.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="changed"):
        open_approved_candidate(root, candidate_id)


@pytest.mark.parametrize("filename", ["injection.json", "audit.json"])
def test_unreviewed_public_files_fail_admission(tmp_path, filename):
    root, candidate_id, _, _ = approved(tmp_path)
    (root / "candidates" / candidate_id / filename).write_text("secret")
    with pytest.raises(ValueError, match="only"):
        open_approved_candidate(root, candidate_id)


def test_truncated_scan_cannot_be_admitted_even_with_forged_approve(tmp_path):
    snapshot = fixture()
    logs = tuple(snapshot.logs.logs[0].model_copy(update={"id": str(i), "body": "feature_flag.key in business text"})
                 for i in range(100))
    root, candidate_id, _, info = exported(tmp_path, snapshot.model_copy(update={"logs": LogResult(logs=logs)}))
    assert info["scan_truncated"] and not info["scan_complete"]
    dispositions = [{"index": i, "classification": "non_leakage", "reason": "cannot override the cap"}
                    for i in range(100)]
    forge_decision(root, candidate_id, dispositions=dispositions)
    with pytest.raises(ValueError, match="not approved"):
        open_approved_candidate(root, candidate_id)


def test_structural_blocker_cannot_be_admitted_even_with_forged_approve(tmp_path):
    snapshot = fixture()
    series = snapshot.metrics.series[0].model_copy(update={"labels": {"feature_flag_key": "secret"}})
    root, candidate_id, _, info = exported(tmp_path, snapshot.model_copy(update={"metrics": MetricResult(series=(series,))}))
    dispositions = [{"index": i, "classification": "non_leakage", "reason": "attempted override"}
                    for i in range(info["heuristic_candidates"])]
    forge_decision(root, candidate_id, dispositions=dispositions)
    with pytest.raises(ValueError, match="not approved"):
        open_approved_candidate(root, candidate_id)


def test_quarantine_episode_and_snapshot_paths_are_not_admission_ids(tmp_path):
    root, episode, source = recording(tmp_path)
    with pytest.raises((ValueError, OSError, FileNotFoundError)):
        open_approved_candidate(root, episode)
    with pytest.raises(ValueError, match="opaque"):
        open_approved_candidate(root, str(source))
    with pytest.raises(ValueError, match="opaque"):
        open_approved_candidate(root, "ABCDEFAB-0000-4000-8000-000000000001")


def test_admission_result_does_not_expose_private_review_material(tmp_path):
    root, episode, source = recording(tmp_path, fixture("paymentUnreachable"))
    from aegis_rca.evaluation.benchmark import ScanTerms, export_candidate
    info = export_candidate(root, episode, terms=ScanTerms(injected_flags=("paymentUnreachable",)))
    candidate_id = info["candidate_id"]
    review_candidate(root, candidate_id, decision_file(root, candidate_id))
    admitted = open_approved_candidate(root, candidate_id)
    public = json.dumps(admission_metadata(admitted))
    dumped = json.dumps(admitted.model_dump(mode="json", exclude={"snapshot"}))
    secrets = (episode, "paymentUnreachable", "review/", "private/", "private_scan_report",
               "source_episode", "scan_terms", "hits", "removal_manifest", "reviewer")
    for text in (public, dumped):
        for secret in secrets:
            assert secret not in text
    assert "private_scan_report" not in admitted.model_dump()


def test_scan_candidate_remains_review_tooling_and_does_not_admit(tmp_path):
    root, candidate_id, _, _ = exported(tmp_path)
    scanned = scan_candidate(root, candidate_id)
    assert not scanned["benchmark_eligible"]
    assert scanned["review_status"] == "pending_review"
    assert scanned["private_scan_report"].endswith("scan.json")
    with pytest.raises(ValueError, match="not approved"):
        open_approved_candidate(root, candidate_id)
    review_candidate(root, candidate_id, decision_file(root, candidate_id))
    scanned = scan_candidate(root, candidate_id)
    assert scanned["benchmark_eligible"]
    assert "private_scan_report" in scanned
    admitted = open_approved_candidate(root, candidate_id)
    assert "private_scan_report" not in admission_metadata(admitted)


def test_cli_admit_prints_only_nonsecret_metadata(tmp_path, capsys):
    root, candidate_id, _, info = approved(tmp_path)
    main(["admit", "--recordings-root", str(root), "--candidate", candidate_id])
    out = capsys.readouterr().out
    result = json.loads(out)
    assert result["candidate_id"] == candidate_id
    assert result["review_status"] == "approved"
    assert result["benchmark_eligible"] is True
    assert result["bundle_sha256"] == info["bundle_sha256"]
    assert result["scan_sha256"] == info["scan_sha256"]
    assert result["ruleset"] == RULESET
    assert set(result) == {"candidate_id", "review_status", "benchmark_eligible",
                           "bundle_sha256", "scan_sha256", "ruleset"}
    for leaked in ("review/", "private/", "private_scan_report", "traces", "spans",
                   "snapshot.json", str(root)):
        assert leaked not in out


def test_cli_admit_rejects_pending_and_does_not_echo_labels(tmp_path, capsys):
    root, candidate_id, _, _ = exported(tmp_path, fixture("secret-new-flag"))
    with pytest.raises(SystemExit):
        main(["admit", "--recordings-root", str(root), "--candidate", candidate_id])
    err = capsys.readouterr().err
    assert "secret-new-flag" not in err
    assert "Benchmark operation rejected" in err


def test_cli_admit_rejects_snapshot_path_and_extra_flags(tmp_path, capsys):
    root, candidate_id, _, _ = approved(tmp_path)
    with pytest.raises(SystemExit):
        main(["admit", "--recordings-root", str(root), "--candidate", str(uuid4()),
              "--episode", str(uuid4())])
    with pytest.raises(ValueError, match="opaque"):
        open_approved_candidate(root, str(root / "candidates" / candidate_id / "snapshot.json"))
