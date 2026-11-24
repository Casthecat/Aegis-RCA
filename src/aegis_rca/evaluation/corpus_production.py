"""Operator ledger progress derived from existing gates; never creates approval/Gold."""
from datetime import datetime, timezone
import os
from pathlib import Path
import tempfile

from ..models.identity import structural_id
from . import benchmark
from .corpus_recording import CorpusProductionRecord, InjectionTruth
from .gold import BenchmarkGoldLabelV2
from .sanitization import canonical_bytes, digest
from .stage_c import publish, read_contract


def _replace_private(path, contract):
    benchmark._no_links(path)
    fd, name = tempfile.mkstemp(prefix=".production-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(canonical_bytes(contract.model_dump(mode="json")))
            file.flush()
            os.fsync(file.fileno())
        Path(name).replace(path)
    finally:
        Path(name).unlink(missing_ok=True)


def update_production(recordings_root, episode_id, *, candidate_id=None, gold_path=None, exclude_reasons=()):
    """Append a private immutable revision, then atomically update the progress head."""
    root = benchmark._root(Path(recordings_root))
    episode_id = benchmark._opaque(episode_id)
    private = benchmark._safe(root, "private", episode_id)
    record = read_contract(private / "production.json", CorpusProductionRecord)
    if record.private_episode_id != episode_id:
        raise ValueError("production record episode mismatch")
    candidate_id = candidate_id or record.candidate_id
    status, reasons = record.status, record.reason_codes
    admitted = None
    if candidate_id is not None:
        benchmark._opaque(candidate_id)
        if record.candidate_id not in (None, candidate_id):
            raise ValueError("one episode must not be rebound to another candidate")
        audit = benchmark._json(benchmark._read_bytes(benchmark._safe(root, "review", candidate_id, "audit.json")))
        if (audit.get("source_episode") != episode_id or audit.get("source_sha256") != record.source_snapshot_sha256 or
            digest(benchmark._read_bytes(benchmark._safe(root, "quarantine", episode_id, "snapshot.json"))) != record.source_snapshot_sha256):
            raise ValueError("candidate does not derive from this immutable quarantine episode")
        scan = benchmark.scan_candidate(root, candidate_id)
        status = "review_pending"
        if not scan["scan_complete"] or scan["scan_truncated"] or scan["structural_blockers"]:
            status = "scan_blocked"
        if scan["structural_blockers"]:
            reasons = tuple(sorted(set((*reasons, "structural_leakage"))))
        decision = benchmark._safe(root, "review", candidate_id, "decision.json")
        if decision.exists():
            human = read_contract(decision, benchmark.ReviewDecision)
            if human.decision == "reject":
                status, reasons = "review_rejected", tuple(sorted(set((*reasons, "review_rejected"))))
            else:
                admitted = benchmark.open_approved_candidate(root, candidate_id)
                status = "approved"
                if record.status in {"answerability_labeled", "included_dev", "included_replicate_holdout", "included_family_holdout"}:
                    status = record.status
        if gold_path is not None:
            if admitted is None:
                raise ValueError("Gold linkage requires completed manual candidate approval")
            gold = read_contract(Path(gold_path), BenchmarkGoldLabelV2)
            if (gold.candidate_id != candidate_id or gold.bundle_sha256 != admitted.bundle_sha256 or
                gold.snapshot_sha256 != admitted.snapshot_sha256 or gold.review_packet_id is None or
                gold.capture_policy_id != record.capture_policy_id or gold.query_policy_id != record.query_policy_id or
                gold.sanitizer_ruleset != record.sanitizer_ruleset):
                raise ValueError("Gold does not bind this approved candidate/review")
            status = "answerability_labeled"
    elif gold_path is not None:
        raise ValueError("Gold cannot be linked before candidate admission")
    if exclude_reasons:
        status, reasons = "excluded", tuple(sorted(set((*reasons, *exclude_reasons))))
    elif record.status == "excluded":
        status = "excluded"
    updated = CorpusProductionRecord.model_validate({**record.model_dump(), "candidate_id":candidate_id,
        "status":status, "reason_codes":reasons, "updated_at":datetime.now(timezone.utc)})
    truth_path, truth, linked_truth = private / "injection-truth.json", None, None
    if admitted is not None and truth_path.exists():
        truth = read_contract(truth_path, InjectionTruth)
        if truth.private_episode_id != episode_id or truth.candidate_id not in (None, candidate_id):
            raise ValueError("injection truth candidate linkage conflict")
        if truth.candidate_id is None:
            linked_truth = InjectionTruth.model_validate({**truth.model_dump(), "candidate_id":candidate_id})
    # Existing record and new revision both survive; source/quarantine/review bytes do not change.
    for value in (record, updated):
        path = private / "production-history" / f"{structural_id('PDR-', value.model_dump())}.json"
        if not path.exists():
            publish(path, value.model_dump(mode="json"))
    _replace_private(private / "production.json", updated)
    if linked_truth is not None:
        for value in (truth, linked_truth):
            path = private / "truth-history" / f"{structural_id('ITR-',value.model_dump())}.json"
            if not path.exists():
                publish(path, value.model_dump(mode="json"))
        _replace_private(truth_path, linked_truth)
    return updated
