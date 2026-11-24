"""BSP-v2: frozen reviewer Gold claims scored after immutable Stage C execution.

This module receives Gold only. Injection truth and experiment-family metadata are
neither inputs nor lookup sources. BSP-v1 remains independently available.
"""
from pathlib import Path
from typing import Literal, get_args

from pydantic import Field, model_validator

from ..models.incident import Contract
from ..models.identity import structural_id
from . import benchmark
from .gold import BenchmarkGoldLabelV2, DEFAULT_GOLD_RUBRIC
from .sanitization import digest
from .stage_c import publish, read_run
from .stage_c_scoring import ScoredDimension, root_cause_overclaim


class BenchmarkScoringProtocolV2(Contract):
    schema_version: Literal[2] = 2
    label_schema: Literal["BenchmarkGoldLabelV2"] = "BenchmarkGoldLabelV2"
    gold_diagnostic_rubric_id: str = Field(default=DEFAULT_GOLD_RUBRIC.id, pattern=r"^GDR-[0-9a-f]{64}$")
    metrics: Literal["stage_c_expected_diagnostic_claims_v2"] = "stage_c_expected_diagnostic_claims_v2"
    extra_claims: Literal["unscored_without_exhaustive_claim_labels"] = "unscored_without_exhaustive_claim_labels"
    answerability: Literal["four_state_restraint_separate_from_set_precision"] = "four_state_restraint_separate_from_set_precision"
    root_cause: Literal["structural_diagnosis_level_and_underlying_cause"] = "structural_diagnosis_level_and_underlying_cause"
    revision: str = Field(default="p2.2-v1", min_length=1, max_length=80)

    @property
    def id(self):
        return structural_id("BSP-", self.model_dump())


class GoldScoredDimension(ScoredDimension):
    name: Literal["mechanism_correctness", "known_edge_recall", "supported_claim_recall", "edge_precision",
        "false_supported_claims", "abstention", "answerability_restraint", "root_cause_overclaim",
        "tool_calls", "rounds", "duplicate_intent_count", "intent_efficiency", "acquisition_unavailable",
        "acquisition_failed", "replay_selection_ambiguous", "timeouts", "llm_calls", "llm_tokens", "llm_cost",
        "deterministic_consistency", "claim_precision", "answerability_overclaim"]


class BenchmarkScoreResultV2(Contract):
    schema_version: Literal[2] = 2
    scoring_protocol: BenchmarkScoringProtocolV2
    scoring_protocol_id: str = Field(pattern=r"^BSP-[0-9a-f]{64}$")
    run_key_id: str = Field(pattern=r"^BRK-[0-9a-f]{64}$")
    semantic_result_id: str = Field(pattern=r"^BRS-[0-9a-f]{64}$")
    run_archive_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_id: str
    private_label_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    gold_label_id: str = Field(pattern=r"^GL-[0-9a-f]{64}$")
    dimensions: tuple[GoldScoredDimension, ...]

    @model_validator(mode="after")
    def coherent(self):
        from .stage_c_models import opaque
        opaque(self.candidate_id)
        if self.scoring_protocol.id != self.scoring_protocol_id:
            raise ValueError("scoring protocol mismatch")
        names = [d.name for d in self.dimensions]
        if len(set(names)) != len(names) or set(names) != set(get_args(GoldScoredDimension.model_fields["name"].annotation)):
            raise ValueError("all BSP-v2 dimensions must be explicitly scored or UNSCORED")
        return self

    @property
    def id(self):
        return structural_id("BS-", self.model_dump())


def score_gold_result(run, label, label_digest, protocol=None, *, comparison=None):
    """Match all verified structured claims; never infer a label from runtime output."""
    protocol = protocol or BenchmarkScoringProtocolV2()
    if not isinstance(protocol, BenchmarkScoringProtocolV2) or not isinstance(label, BenchmarkGoldLabelV2):
        raise ValueError("BSP-v2 requires BenchmarkGoldLabelV2")
    if (label.candidate_id != run.admission.candidate_id or
        label.snapshot_sha256 != run.admission.snapshot_sha256 or
        label.bundle_sha256 != run.admission.bundle_sha256 or
        label.sanitizer_ruleset != run.admission.ruleset):
        raise ValueError("Gold label does not bind this admitted candidate")
    if label.gold_diagnostic_rubric_id != protocol.gold_diagnostic_rubric_id:
        raise ValueError("Gold label diagnostic rubric differs from BSP-v2")
    report, usage = run.semantic.report, run.semantic.usage
    subjects = {a.hypothesis_spec.id: a.hypothesis_spec for a in report.assessments}
    claims = { (h.failure_class, h.caller, h.callee, h.operation)
        for c in report.claims if c.claim_class == "verified_failure_mode"
        for h in (subjects[c.hypothesis_id],) }
    expected = {(c.failure_mode, c.caller, c.callee, c.operation) for c in label.expected_claims}
    # Operation is unconstrained by GDR-v1; an operation-specific supported
    # instance satisfies its matching mechanism+edge claim, not another edge.
    projected = {(mode, caller, callee, None) for mode, caller, callee, _ in claims}
    edges = {(caller, callee) for _, caller, callee, _ in projected}
    expected_edges = {(caller, callee) for _, caller, callee, _ in expected}
    matched = projected & expected
    attributable = {claim for claim in projected if (claim[1], claim[2]) in expected_edges}
    dimensions = []

    def metric(name, value, definition, ratio=None):
        dimensions.append(GoldScoredDimension(name=name,
            status="UNSCORED" if value is None else "SCORED", value=value, definition=definition,
            numerator=ratio[0] if ratio else None, denominator=ratio[1] if ratio else None))

    def fraction(name, k, n, definition):
        metric(name, k/n if n else None, definition, (k, n) if n else None)

    fraction("mechanism_correctness", len(matched), len(attributable),
        "Mechanism correctness of supported claims on known-positive edges under the frozen GDR; extra unlabelled edges do not count as errors.")
    fraction("known_edge_recall", len(edges & expected_edges), len(expected_edges),
        "Recall of edges derived from expected diagnostic claims; absent positive labels yield UNSCORED.")
    fraction("supported_claim_recall", len(matched), len(expected),
        "Known-positive structured mechanism+edge claim recall; does not require exhaustive labels.")
    if label.claim_labels_exhaustive:
        fraction("claim_precision", len(matched), len(projected),
            "Set precision of structured mechanism+edge claims against an explicitly exhaustive Gold set; zero predictions yield UNSCORED.")
        fraction("edge_precision", len(edges & expected_edges), len(edges),
            "Edge-set precision derived from an explicitly exhaustive claim set; zero predictions yield UNSCORED.")
        metric("false_supported_claims", float(len(projected - expected)),
            "Supported mechanism+edge claims outside the explicitly exhaustive Gold set.")
    else:
        metric("claim_precision", None, "Non-exhaustive known positives do not establish extra-claim false positives.")
        metric("edge_precision", None, "Non-exhaustive known positives do not establish extra-edge false positives.")
        metric("false_supported_claims", None, "Extra supported claims remain UNSCORED without an exhaustive Gold claim set.")
    nonpositive = label.answerability in {"symptom_only", "no_incident", "insufficient_evidence"}
    metric("abstention", float(not projected), "Observed absence of verified failure-mode claims, independent of correctness.")
    metric("answerability_restraint", float(not projected) if nonpositive else None,
        "Abstention under a non-mechanism-identifiable Gold review; separate from exhaustive claim-set correctness.")
    metric("answerability_overclaim", float(bool(projected)) if nonpositive else None,
        "A definitive mechanism claim despite symptom-only/no-incident/insufficient-evidence Gold; an epistemic overclaim indicator, not an extra-claim false-positive count.")
    metric("root_cause_overclaim", float(root_cause_overclaim(report)),
        "Structural diagnosis_level/underlying_cause check; ultimate root cause is never an expected GDR-v1 claim.")
    for name, value in (("tool_calls", usage.tool_calls), ("rounds", usage.rounds),
        ("duplicate_intent_count", usage.duplicate_intent_count), ("acquisition_unavailable", usage.unavailable_intents),
        ("acquisition_failed", usage.failed_intents), ("replay_selection_ambiguous", usage.ambiguous_intents),
        ("timeouts", usage.timeouts), ("llm_calls", usage.llm_calls),
        ("llm_tokens", usage.llm_input_tokens + usage.llm_output_tokens), ("llm_cost", usage.llm_cost)):
        metric(name, float(value), "Stable immutable execution usage/disposition count.")
    metric("intent_efficiency", (usage.tool_calls-usage.duplicate_intent_count)/usage.tool_calls if usage.tool_calls else None,
        "Unique physically attempted logical intents / physical calls; not diagnostic information gain.")
    if comparison is not None and comparison.run_key != run.run_key:
        raise ValueError("determinism comparison requires the identical BRK trial")
    distinct = comparison is not None and comparison.archive_sha256 != run.archive_sha256
    metric("deterministic_consistency", float(comparison.semantic_result_id == run.semantic_result_id) if distinct else None,
        "Requires an independently executed rerun of the same BRK; an identical archive or cached reuse cannot establish consistency.")
    return BenchmarkScoreResultV2(scoring_protocol=protocol, scoring_protocol_id=protocol.id,
        run_key_id=run.run_key_id, semantic_result_id=run.semantic_result_id,
        run_archive_sha256=run.archive_sha256, candidate_id=run.admission.candidate_id,
        private_label_sha256=label_digest, gold_label_id=label.id, dimensions=tuple(dimensions))


def score_gold_run(run_path, labels_root, output, *, protocol=None, comparison_path=None):
    """Read only completed run archives and the explicitly selected Gold label."""
    protocol = protocol or BenchmarkScoringProtocolV2()
    if not isinstance(protocol, BenchmarkScoringProtocolV2):
        raise ValueError("BSP-v2 scoring requires its matching protocol")
    if comparison_path is not None:
        def archive_path(path):
            path = Path(path)
            return (path / "run.json" if path.is_dir() else path).resolve()
        if archive_path(run_path) == archive_path(comparison_path):
            raise ValueError("determinism comparison requires a separate rerun archive")
    run = read_run(run_path)
    root = benchmark._root(Path(labels_root))
    label_bytes = benchmark._read_bytes(benchmark._safe(root, run.admission.candidate_id + ".json"))
    label = BenchmarkGoldLabelV2.model_validate(benchmark._json(label_bytes))
    result = score_gold_result(run, label, digest(label_bytes), protocol,
        comparison=read_run(comparison_path) if comparison_path else None)
    output = Path(output)
    if output.exists():
        existing = BenchmarkScoreResultV2.model_validate(benchmark._json(benchmark._read_bytes(output)))
        if existing != result:
            raise ValueError("score output is immutable; use another path for another BSP/label")
    else:
        publish(output, result.model_dump(mode="json"))
    return result
