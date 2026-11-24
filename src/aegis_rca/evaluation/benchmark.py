"""Local candidate export and human review, isolated from investigator inputs."""
from dataclasses import dataclass
from pathlib import Path
import json
import stat
from typing import Literal
from uuid import UUID, uuid4

from pydantic import Field, StrictBool, model_validator

from aegis_rca.models.incident import Contract
from aegis_rca.tools.snapshot import TelemetrySnapshot
from .sanitization import (MAX_BYTES, RULESET, canonical_bytes, check_tree, digest,
                           sanitize_snapshot, scan_payload, structural_blockers)


class ScanTerms(Contract):
    injected_flags: tuple[str, ...] = ()
    scenario_answers: tuple[str, ...] = ()

    @model_validator(mode="after")
    def bounded(self):
        values = (*self.injected_flags, *self.scenario_answers)
        if len(values) > 100 or any(not value.strip() or len(value) > 2000 for value in values):
            raise ValueError("scan terms must be nonempty, bounded strings")
        return self


class Disposition(Contract):
    index: int = Field(ge=0)
    classification: Literal["non_leakage", "true_leakage", "unresolved"]
    reason: str = Field(min_length=1, max_length=2000)


class ReviewDecision(Contract):
    bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: Literal["approve", "reject"]
    reviewer: str = Field(min_length=1, max_length=200)
    full_snapshot_reviewed: StrictBool
    dispositions: tuple[Disposition, ...] = ()


class ApprovedCandidate(Contract):
    """Public admission result. Holds the snapshot parsed from the validated bytes."""
    candidate_id: str
    snapshot: TelemetrySnapshot
    snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    ruleset: str
    review_status: Literal["approved"] = "approved"
    benchmark_eligible: Literal[True] = True


@dataclass(frozen=True)
class _LoadedCandidate:
    """Internal load result: one snapshot parse from the bytes that were hashed and scanned."""
    root: Path
    candidate_id: str
    snapshot: TelemetrySnapshot
    scan: dict
    ruleset: str
    snapshot_sha256: str


def _opaque(value: str) -> str:
    try:
        if str(UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError):
        raise ValueError("episode/candidate must be a canonical opaque UUID") from None
    return value


def _no_links(path: Path) -> None:
    for item in (path, *path.parents):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError("symlinks and junctions are not allowed in recording paths")


def _root(path: Path) -> Path:
    _no_links(path.absolute())
    return path.resolve()


def _safe(root: Path, *parts: str) -> Path:
    path = root.joinpath(*parts)
    _no_links(path)
    if not path.resolve().is_relative_to(root):
        raise ValueError("recording path escapes its root")
    return path


def _read_bytes(path: Path) -> bytes:
    _no_links(path)
    with path.open("rb") as handle:
        data = handle.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("artifact exceeds 20 MB limit")
    return data


def _json(data: bytes):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON keys are not accepted")
            result[key] = value
        return result
    def finite(_):
        raise ValueError("non-finite JSON constants are not accepted")
    value = json.loads(data.decode("utf-8-sig"), object_pairs_hook=unique, parse_constant=finite)
    check_tree(value)
    return value


def _write(path: Path, content: bytes):
    if len(content) > MAX_BYTES:
        raise ValueError("artifact exceeds 20 MB limit")
    _no_links(path)
    with path.open("xb") as handle:
        handle.write(content)


def load_terms(path: Path | None) -> ScanTerms:
    return ScanTerms.model_validate(_json(_read_bytes(path))) if path else ScanTerms()


def _bundle(snapshot_bytes: bytes, metadata_bytes: bytes) -> str:
    return digest(canonical_bytes({"snapshot.json": digest(snapshot_bytes),
                                   "candidate.json": digest(metadata_bytes)}))


def _scan(snapshot: TelemetrySnapshot, metadata: dict, terms: ScanTerms, bundle: str) -> dict:
    # Include public filenames and the entire public sidecar in the scan.
    payload = {"snapshot.json": snapshot.model_dump(mode="json"), "candidate.json": metadata}
    return {"bundle_sha256": bundle, "ruleset": RULESET,
            "terms_sha256": digest(canonical_bytes(terms.model_dump(mode="json"))),
            **scan_payload(payload, **terms.model_dump()),
            "structural_blockers": structural_blockers(snapshot)}


def _template(bundle: str, scan: dict) -> dict:
    return {"bundle_sha256": bundle, "scan_sha256": digest(canonical_bytes(scan)),
            "decision": "pending", "reviewer": "", "full_snapshot_reviewed": False,
            "dispositions": [{"index": i, "classification": "unresolved", "reason": ""}
                             for i in range(len(scan["hits"]))]}


def _benchmark_snapshot(payload):
    if payload.get("schema_version", 1) != 1:
        raise ValueError("benchmark pipeline accepts Snapshot V1 only; V2 engineering archives are unsupported")
    return TelemetrySnapshot.model_validate(payload)


def export_candidate(recordings_root: Path, episode: str, *, terms: ScanTerms | None = None) -> dict:
    root, episode = _root(recordings_root), _opaque(episode)
    terms = terms or ScanTerms()
    source = _safe(root, "quarantine", episode, "snapshot.json")
    original = _read_bytes(source)
    snapshot = _benchmark_snapshot(_json(original))
    before = scan_payload(snapshot.model_dump(mode="json"), **terms.model_dump())
    cleaned = sanitize_snapshot(snapshot)
    content = canonical_bytes(cleaned.snapshot.model_dump(mode="json"))
    candidate_id = str(uuid4())
    metadata = {"format_version": 1, "artifact_kind": "sanitized_benchmark_candidate",
                "candidate_id": candidate_id, "derived_from": "quarantine_snapshot",
                "ruleset": RULESET, "snapshot_sha256": digest(content)}
    metadata_bytes = canonical_bytes(metadata)
    bundle = _bundle(content, metadata_bytes)
    scan = _scan(cleaned.snapshot, metadata, terms, bundle)
    audit = {"format_version": 1, "candidate_id": candidate_id, "ruleset": RULESET,
             "source_episode": episode, "source_sha256": digest(original),
             "bundle_sha256": bundle, "removals": cleaned.removals,
             "removal_manifest": {"path_format": "source-positions-v1", "entries": cleaned.removal_manifest},
             "scan_terms": terms.model_dump(mode="json"), "before_scan": before}
    # All contents are prepared and validated before reserving new directories.
    outputs = {"snapshot.json": content, "candidate.json": metadata_bytes}
    private = {"audit.json": canonical_bytes(audit), "scan.json": canonical_bytes(scan),
               "decision.template.json": canonical_bytes(_template(bundle, scan))}
    if any(len(value) > MAX_BYTES for value in (*outputs.values(), *private.values())):
        raise ValueError("export exceeds artifact limits")
    if digest(_read_bytes(source)) != digest(original):
        raise ValueError("source changed during sanitization")
    candidate = _safe(root, "candidates", candidate_id)
    review = _safe(root, "review", candidate_id)
    candidate.parent.mkdir(parents=True, exist_ok=True)
    review.parent.mkdir(parents=True, exist_ok=True)
    candidate.mkdir(exist_ok=False)
    review.mkdir(exist_ok=False)
    for name, value in outputs.items():
        _write(_safe(root, "candidates", candidate_id, name), value)
    for name, value in private.items():
        _write(_safe(root, "review", candidate_id, name), value)
    # A partial/failed export never obtains this completion marker.
    if digest(_read_bytes(source)) != digest(original):
        raise ValueError("source changed during sanitization")
    _write(_safe(root, "review", candidate_id, "ready.json"), canonical_bytes({"bundle_sha256": bundle}))
    return _summary(candidate_id, scan, "pending_review")


def _load(recordings_root: Path, candidate_id: str) -> _LoadedCandidate:
    root, candidate_id = _root(recordings_root), _opaque(candidate_id)
    candidate = _safe(root, "candidates", candidate_id)
    # Unexpected public files are not covered by this review. Reject the bundle.
    if {item.name for item in candidate.iterdir()} != {"snapshot.json", "candidate.json"}:
        raise ValueError("candidate must contain only snapshot.json and candidate.json")
    content = _read_bytes(_safe(root, "candidates", candidate_id, "snapshot.json"))
    metadata_bytes = _read_bytes(_safe(root, "candidates", candidate_id, "candidate.json"))
    metadata = _json(metadata_bytes)
    bundle = _bundle(content, metadata_bytes)
    ready = _json(_read_bytes(_safe(root, "review", candidate_id, "ready.json")))
    audit = _json(_read_bytes(_safe(root, "review", candidate_id, "audit.json")))
    if bundle != ready["bundle_sha256"] or bundle != audit["bundle_sha256"]:
        raise ValueError("candidate bytes changed; create a new candidate")
    if (metadata.get("ruleset") != RULESET or audit.get("ruleset") != RULESET or
            metadata.get("candidate_id") != candidate_id or metadata.get("snapshot_sha256") != digest(content)):
        raise ValueError("candidate provenance does not match the current ruleset")
    snapshot = _benchmark_snapshot(_json(content))
    terms = ScanTerms.model_validate(audit["scan_terms"])
    scan = _scan(snapshot, metadata, terms, bundle)
    if _read_bytes(_safe(root, "review", candidate_id, "scan.json")) != canonical_bytes(scan):
        raise ValueError("scan changed; create a new candidate before review")
    return _LoadedCandidate(root, candidate_id, snapshot, scan, RULESET, digest(content))


def _approval_valid(decision: ReviewDecision, scan: dict) -> bool:
    if (decision.bundle_sha256 != scan["bundle_sha256"] or
            decision.scan_sha256 != digest(canonical_bytes(scan)) or decision.decision != "approve" or
            not decision.reviewer.strip() or not decision.full_snapshot_reviewed or
            not scan["scan_complete"] or scan["scan_truncated"] or scan["structural_blockers"]):
        return False
    entries = decision.dispositions
    return (len(entries) == len(scan["hits"]) and
            {entry.index for entry in entries} == set(range(len(scan["hits"]))) and
            all(entry.classification == "non_leakage" and entry.reason.strip() for entry in entries))


def _summary(candidate_id: str, scan: dict, status: str) -> dict:
    return {"candidate_id": candidate_id, "review_status": status,
            "benchmark_eligible": status == "approved", "bundle_sha256": scan["bundle_sha256"],
            "scan_sha256": digest(canonical_bytes(scan)), "scan_complete": scan["scan_complete"],
            "scan_truncated": scan["scan_truncated"], "heuristic_candidates": len(scan["hits"]),
            "structural_blockers": len(scan["structural_blockers"])}


def scan_candidate(recordings_root: Path, candidate_id: str) -> dict:
    loaded = _load(recordings_root, candidate_id)
    path = _safe(loaded.root, "review", loaded.candidate_id, "scan.json")
    status = "pending_review"
    decision_path = _safe(loaded.root, "review", loaded.candidate_id, "decision.json")
    if decision_path.exists():
        decision = ReviewDecision.model_validate(_json(_read_bytes(decision_path)))
        status = "approved" if _approval_valid(decision, loaded.scan) else "ineligible"
    return {**_summary(loaded.candidate_id, loaded.scan, status), "private_scan_report": str(path)}


def review_candidate(recordings_root: Path, candidate_id: str, decision_file: Path) -> dict:
    loaded = _load(recordings_root, candidate_id)
    decision = ReviewDecision.model_validate(_json(_read_bytes(decision_file)))
    if (decision.bundle_sha256 != loaded.scan["bundle_sha256"] or
            decision.scan_sha256 != digest(canonical_bytes(loaded.scan))):
        raise ValueError("review is stale or does not refer to this complete scan")
    if decision.decision == "approve" and not _approval_valid(decision, loaded.scan):
        raise ValueError("approval requires a complete uncapped scan, no structural blockers, full human review and resolved non-leakage dispositions")
    _write(_safe(loaded.root, "review", loaded.candidate_id, "decision.json"),
           canonical_bytes(decision.model_dump(mode="json")))
    return _summary(loaded.candidate_id, loaded.scan, "approved" if decision.decision == "approve" else "ineligible")


def admission_metadata(candidate: ApprovedCandidate) -> dict:
    """Non-secret admission fields only. Never includes snapshot content or review paths."""
    return {"candidate_id": candidate.candidate_id, "review_status": candidate.review_status,
            "benchmark_eligible": candidate.benchmark_eligible, "bundle_sha256": candidate.bundle_sha256,
            "scan_sha256": candidate.scan_sha256, "ruleset": candidate.ruleset}


def open_approved_candidate(recordings_root: Path, candidate_id: str) -> ApprovedCandidate:
    loaded = _load(recordings_root, candidate_id)
    decision_path = _safe(loaded.root, "review", loaded.candidate_id, "decision.json")
    if not decision_path.exists():
        raise ValueError("candidate is not approved for benchmark use")
    decision = ReviewDecision.model_validate(_json(_read_bytes(decision_path)))
    if not _approval_valid(decision, loaded.scan):
        raise ValueError("candidate is not approved for benchmark use")
    return ApprovedCandidate(
        candidate_id=loaded.candidate_id,
        snapshot=loaded.snapshot,
        snapshot_sha256=loaded.snapshot_sha256,
        bundle_sha256=loaded.scan["bundle_sha256"],
        scan_sha256=digest(canonical_bytes(loaded.scan)),
        ruleset=loaded.ruleset,
    )


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["sanitize", "scan", "review", "admit"])
    parser.add_argument("--recordings-root", type=Path, default=Path("data/recordings"))
    parser.add_argument("--episode")
    parser.add_argument("--candidate")
    parser.add_argument("--terms-file", type=Path, help="Private scan vocabulary; never copied into the public candidate")
    parser.add_argument("--decision-file", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.action == "sanitize":
            if not args.episode or args.candidate or args.decision_file:
                raise ValueError("sanitize requires --episode and accepts only --terms-file")
            result = export_candidate(args.recordings_root, args.episode, terms=load_terms(args.terms_file))
        elif args.action == "admit":
            if not args.candidate or args.episode or args.terms_file or args.decision_file:
                raise ValueError("admit requires --candidate")
            result = admission_metadata(open_approved_candidate(args.recordings_root, args.candidate))
        else:
            if not args.candidate or args.episode or args.terms_file:
                raise ValueError("scan/review require --candidate; vocabulary is frozen at export")
            if args.action == "review":
                if not args.decision_file:
                    raise ValueError("review requires --decision-file")
                result = review_candidate(args.recordings_root, args.candidate, args.decision_file)
            else:
                if args.decision_file:
                    raise ValueError("scan does not accept --decision-file")
                result = scan_candidate(args.recordings_root, args.candidate)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
        # No payloads, scan terms, source paths or private values in CLI errors.
        parser.exit(2, f"Benchmark operation rejected ({type(exc).__name__}); check paths, immutable artifacts and review requirements.\n")
