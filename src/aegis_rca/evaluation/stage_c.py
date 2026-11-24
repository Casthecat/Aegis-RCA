"""Approved-only offline execution. Admission ends before any runtime work begins."""
import os
import tempfile
from pathlib import Path
from time import perf_counter

from ..runtime import Dependencies
from ..runner import run_investigation
from ..state import initial_state
from ..tools.replay import ReplayBackend
from ..scheduling import required_requests, intent_for
from . import benchmark
from .sanitization import canonical_bytes, digest
from .stage_c_models import (AdmissionReference, BenchmarkRunKey, BenchmarkSemanticResult,
    BenchmarkRunResult, BenchmarkUsage, BenchmarkBudgetSpec, BenchmarkExecutionProtocol,
    TrialSpec, VARIANTS, ExperimentSpec, ExperimentManifest, HarnessFailure)


def read_contract(path, contract):
    data = benchmark._read_bytes(Path(path))
    value = contract.model_validate(benchmark._json(data))
    return value


def publish(path, payload):
    """Atomic, exclusive publication: hard-link the complete fsynced temporary file."""
    path = Path(path)
    benchmark._no_links(path.absolute())
    path.parent.mkdir(parents=True, exist_ok=True)
    content = canonical_bytes(payload)
    if len(content) > benchmark.MAX_BYTES:
        raise ValueError("result exceeds archive limit")
    fd, name = tempfile.mkstemp(prefix=".stage-c-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        def native(p):
            value = str(Path(p).absolute())
            return "\\\\?\\" + value if os.name == "nt" and not value.startswith("\\\\?\\") else value
        os.link(native(name), native(path))  # Exclusive; never replace a completed/partial target.
    finally:
        Path(name).unlink(missing_ok=True)


def read_run(path, expected_key=None):
    path = Path(path)
    if path.is_dir():
        path = path / "run.json"
    data = benchmark._read_bytes(path)
    result = BenchmarkRunResult.model_validate(benchmark._json(data))
    if data != canonical_bytes(result.model_dump(mode="json")):
        raise ValueError("archive must use its canonical byte encoding")
    # Exact full archive bytes, including the embedded digest, have a separate receipt.
    receipt = benchmark._read_bytes(path.with_suffix(".sha256")).decode("ascii")
    if receipt != digest(data):
        raise ValueError("full archive byte digest mismatch")
    if expected_key is not None and result.run_key != expected_key:
        raise ValueError("completed archive belongs to a different trial")
    return result


def publish_run(path, result):
    path = Path(path)
    if path.exists() or path.with_suffix(".sha256").exists():
        previous = read_run(path, result.run_key)
        if previous.semantic_result_id != result.semantic_result_id:
            raise ValueError("deterministic trial produced conflicting semantic result")
        return previous
    # Receipt first; run.json is the completion artifact. A missing member is partial.
    receipt = path.with_suffix(".sha256")
    content = canonical_bytes(result.model_dump(mode="json"))
    benchmark._no_links(receipt.absolute())
    receipt.parent.mkdir(parents=True, exist_ok=True)
    with receipt.open("xb") as handle:
        handle.write(digest(content).encode("ascii"))
        handle.flush()
        os.fsync(handle.fileno())
    publish(path, result.model_dump(mode="json"))
    return read_run(path, result.run_key)


def failure_categories(state, policy):
    result = set()
    portfolio = state.get("hypothesis_portfolio")
    if state.get("scope") is None:
        result.add("invalid_scope")
    if not portfolio or not portfolio.active:
        result.add("no_hypothesis_candidate")
    if portfolio and portfolio.overflow_count:
        result.add("hypothesis_overflow")
    requests, _, _ = required_requests(state)
    if requests and not policy.required_check_scheduling:
        result.add("variant_did_not_schedule_required_intent")
    if state["verification"] and state["verification"].reason in {"budget_exhausted", "deadline_exhausted"}:
        result.add("budget_exhausted")
    for row in state.get("intent_ledger", ()):
        reason = {"failed": "acquisition_failed", "ambiguous_replay_selection": "replay_selection_ambiguous"}.get(row.status)
        if row.status == "unavailable" and any(n.origin == "required_check" for n in row.needs):
            reason = "required_acquisition_unavailable"
        if reason:
            result.add(reason)
    for assessment in state["final_report"].assessments:
        for limit in assessment.limitations:
            result.add("acquisition_failed" if limit.reason == "required_acquisition_failed" else limit.reason)
    for phv in state.get("hypothesis_verifications", ()):
        for check in (phv.evaluation.trace_check, phv.evaluation.log_check):
            if check.verdict == "UNKNOWN" and any(v.query_status == "succeeded" and v.fact_set_complete for v in check.coverage_used):
                result.add("evidence_acquired_check_unknown")
    return tuple(sorted(result))


def usage(state, attempts):
    statuses = [r.status for r in state["intent_ledger"]]
    specs = {t.id: t for t in state["task_specs"]}
    return BenchmarkUsage(rounds=state["rounds"], tool_calls=len(attempts),
        completed_intents=statuses.count("completed"), failed_intents=statuses.count("failed"),
        unavailable_intents=statuses.count("unavailable"), ambiguous_intents=statuses.count("ambiguous_replay_selection"),
        pending_intents=statuses.count("pending"), timeouts=sum(a.tool_status == "timeout" for a in attempts),
        duplicate_intent_count=len(attempts) - len({intent_for(specs[a.task_spec_id]).id for a in attempts}))


def run_key(admitted, variant, budget, protocol, trial):
    return BenchmarkRunKey(candidate_id=admitted.candidate_id, bundle_sha256=admitted.bundle_sha256,
        snapshot_sha256=admitted.snapshot_sha256, variant_id=variant.id, budget_id=budget.id,
        execution_protocol_id=protocol.id, trial=trial)


def admission_reference(admitted):
    return AdmissionReference(**{k: getattr(admitted, k) for k in
        ("candidate_id", "bundle_sha256", "snapshot_sha256", "scan_sha256", "ruleset")})


async def _execute_admitted(admitted, variant, budget, protocol, trial):
    """Internal boundary: no paths, labels, review data or external backend argument."""
    key = run_key(admitted, variant, budget, protocol, trial)
    started = perf_counter()
    backend = ReplayBackend(admitted.snapshot)  # THE exact admitted object; no reopening.
    deps = Dependencies(backend, execution_policy=variant.runtime_policy)
    from langsmith.run_helpers import tracing_context
    # Ambient tracing configuration must not turn an offline experiment into network I/O.
    with tracing_context(enabled=False):
        state, _ = await run_investigation(initial_state(admitted.snapshot.incident, budget.runtime_budget(),
            execution_id=key.id, source_mode="snapshot"), deps, deadline_seconds=budget.global_deadline_seconds)
    by_id = {a.id: a for a in state["attempt_records"]}
    attempts = tuple(by_id[k] for k in deps.attempts)
    semantic = BenchmarkSemanticResult(run_key_id=key.id, execution_id=key.id,
        termination_reason=state["final_report"].termination_reason, report=state["final_report"],
        portfolio=state["hypothesis_portfolio"], hypothesis_verifications=state["hypothesis_verifications"],
        attempted_tasks=tuple(r.spec for r in deps.attempts.values()), attempts=attempts,
        intents=tuple(state["intent_ledger"]), usage=usage(state, attempts),
        failure_categories=failure_categories(state, variant.runtime_policy))
    payload = dict(run_key=key.model_dump(mode="json"), run_key_id=key.id,
        admission=admission_reference(admitted).model_dump(mode="json"),
        variant=variant.model_dump(mode="json"), budget=budget.model_dump(mode="json"),
        execution_protocol=protocol.model_dump(mode="json"), semantic=semantic.model_dump(mode="json"),
        semantic_result_id=semantic.id, duration_ms=round((perf_counter() - started) * 1000, 3), schema_version=1)
    return BenchmarkRunResult.model_validate({**payload, "archive_sha256": digest(canonical_bytes(payload))})


def freeze_manifest(output, spec, admissions=()):
    root = benchmark._root(Path(output))
    manifest = ExperimentManifest(spec=spec, variants=tuple(VARIANTS[v] for v in spec.variants), admissions=admissions)
    path = benchmark._safe(root, "experiment.json")
    if path.exists():
        if read_contract(path, ExperimentManifest) != manifest:
            raise ValueError("experiment manifest is immutable")
    else:
        publish(path, manifest.model_dump(mode="json"))
    return root, manifest


def separate_output(recordings_root, output):
    if output is not None and benchmark._root(Path(output)).is_relative_to(benchmark._root(Path(recordings_root))):
        raise ValueError("formal output must be outside immutable recording/admission roots")


async def run_candidate(recordings_root, candidate_id, variant_key="B2", *, trial_index=0, output=None):
    """The only public formal telemetry input is a canonical approved candidate ID."""
    spec = ExperimentSpec(candidate_ids=(candidate_id,), variants=(variant_key,), trial=TrialSpec(trial_index=trial_index))
    separate_output(recordings_root, output)
    variant, budget, protocol = VARIANTS[variant_key], BenchmarkBudgetSpec(), BenchmarkExecutionProtocol()
    admitted = benchmark.open_approved_candidate(Path(recordings_root), candidate_id)
    root = freeze_manifest(output, spec, (admission_reference(admitted),))[0] if output is not None else None
    key = run_key(admitted, variant, budget, protocol, spec.trial)
    path = benchmark._safe(root, "runs", key.id, "run.json") if root else None
    if path and path.parent.exists():
        return read_run(path, key)  # Empty/partial/corrupt directory is NOT complete.
    result = await _execute_admitted(admitted, variant, budget, protocol, spec.trial)
    return publish_run(path, result) if path else result


async def run_experiment(recordings_root, spec, output, *, expected_admissions=None):
    spec = ExperimentSpec.model_validate(spec.model_dump())
    separate_output(recordings_root, output)
    expected = None
    if expected_admissions is not None:
        expected = {a.candidate_id: a for a in expected_admissions}
        if len(expected) != len(expected_admissions) or set(expected) != set(spec.candidate_ids):
            raise ValueError("corpus admission references must exactly cover requested candidates")
    admissions = {}
    for candidate in spec.candidate_ids:
        try:
            admitted = benchmark.open_approved_candidate(Path(recordings_root), candidate)
            if expected is not None and admission_reference(admitted) != expected[candidate]:
                raise ValueError("candidate differs from frozen corpus admission")
            admissions[candidate] = admitted
        except (ValueError, OSError, TypeError, KeyError):
            admissions[candidate] = None
    root, manifest = freeze_manifest(output, spec, tuple(admission_reference(a) for a in admissions.values() if a is not None))
    for candidate, admitted in admissions.items():
        for key in spec.variants:
            variant = VARIANTS[key]
            failure_path = benchmark._safe(root, "failures", candidate, key + ".json")
            # Failure records are immutable attempt receipts, not successful trials.
            if failure_path.exists():
                read_contract(failure_path, HarnessFailure)
                continue
            try:
                if admitted is None:
                    raise ValueError("admission rejected")
                trial_key = run_key(admitted, variant, manifest.budget, manifest.execution_protocol, spec.trial)
                path = benchmark._safe(root, "runs", trial_key.id, "run.json")
                if path.parent.exists():
                    read_run(path, trial_key)
                    continue
                result = await _execute_admitted(admitted, variant, manifest.budget, manifest.execution_protocol, spec.trial)
                publish_run(path, result)
            except Exception:
                failure = HarnessFailure(candidate_id=candidate, variant_id=variant.id,
                    reason="admission_rejected" if admitted is None else "execution_or_archive_failed")
                publish(failure_path, failure.model_dump(mode="json"))
    return summarize(root)


def summarize(output):
    root = benchmark._root(Path(output))
    manifest = read_contract(benchmark._safe(root, "experiment.json"), ExperimentManifest)
    valid, corrupt, conflicted = {}, 0, set()
    expected_admissions = {a.candidate_id: a for a in manifest.admissions}
    variant_keys = {v.id: k for k, v in zip(manifest.spec.variants, manifest.variants)}
    runs = benchmark._safe(root, "runs")
    if runs.exists():
        for directory in runs.iterdir():
            try:
                run = read_run(directory / "run.json")
                if (directory.name != run.run_key_id or run.admission.candidate_id not in manifest.spec.candidate_ids or
                    run.budget != manifest.budget or run.execution_protocol != manifest.execution_protocol or
                    run.run_key.trial != manifest.spec.trial or run.variant.id not in variant_keys or
                    expected_admissions.get(run.admission.candidate_id) != run.admission):
                    raise ValueError("run does not belong to this manifest")
                cell = (run.admission.candidate_id, variant_keys[run.variant.id])
                if cell in valid or cell in conflicted:
                    valid.pop(cell, None)
                    conflicted.add(cell)
                    raise ValueError("duplicate trial for candidate/variant")
                valid[cell] = run
            except (ValueError, OSError, TypeError, KeyError):
                corrupt += 1
    complete = [c for c in manifest.spec.candidate_ids if all((c, v) in valid for v in manifest.spec.variants)]
    # Candidate IDs alone do not prove paired bytes; bind exact admission metadata too.
    paired = [c for c in complete if len({valid[c, v].admission.model_dump_json() for v in manifest.spec.variants}) == 1]
    denominators = {}
    for variant in manifest.spec.variants:
        rows = [valid[c, variant] for c in paired]
        denominators[variant] = dict(requested=len(manifest.spec.candidate_ids),
            valid=sum((c, variant) in valid for c in manifest.spec.candidate_ids), paired_n=len(paired),
            paired_supported=sum(r.semantic.report.status == "SUPPORTED" for r in rows),
            paired_tool_calls=sum(r.semantic.usage.tool_calls for r in rows),
            paired_rounds=sum(r.semantic.usage.rounds for r in rows),
            paired_failed_intents=sum(r.semantic.usage.failed_intents for r in rows),
            paired_unavailable_intents=sum(r.semantic.usage.unavailable_intents for r in rows))
    missing = [{"candidate_id": c, "variant": v, "status": "missing_or_failed_or_invalid"}
        for c in manifest.spec.candidate_ids for v in manifest.spec.variants if (c, v) not in valid]
    failures = []
    for c in manifest.spec.candidate_ids:
        for v in manifest.spec.variants:
            path = benchmark._safe(root, "failures", c, v + ".json")
            if path.exists():
                receipt = read_contract(path, HarnessFailure)
                if receipt.candidate_id != c or receipt.variant_id != VARIANTS[v].id:
                    raise ValueError("failure receipt does not match its trial cell")
                failures.append(receipt.model_dump(mode="json"))
    from .stage_c_scoring import summarize_scores
    from .stage_c_summary import extend_summary
    scores = summarize_scores(benchmark._safe(root, "scores"), valid, paired, manifest.spec.variants)
    attempted = set(valid)
    attempted.update((f["candidate_id"], variant_keys[f["variant_id"]]) for f in failures)
    # A frozen manifest alone is not an execution attempt. A partial canonical
    # BRK directory is an attempt, but never a completed archive.
    for reference in manifest.admissions:
        for variant in manifest.variants:
            key = run_key(reference, variant, manifest.budget, manifest.execution_protocol, manifest.spec.trial)
            if benchmark._safe(root, "runs", key.id).exists():
                attempted.add((reference.candidate_id, variant_keys[variant.id]))
    payload = dict(manifest_id=manifest.id, paired_candidate_ids=paired, by_variant=denominators,
        incomplete_trials=missing, invalid_archives=corrupt, harness_failures=failures,
        admission_mismatch_candidates=sorted(set(complete)-set(paired)), scored_comparisons=scores,
        notice="Paired metrics use the same complete-case candidates. Missing/failed trials remain in requested denominators. Deterministic reruns are not independent samples; accuracy requires separate labels/scores.")
    return extend_summary(payload, manifest, valid, attempted).model_dump(mode="json")
