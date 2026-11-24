"""Private, post-execution multi-claim scoring; legacy score_episode is unchanged."""
from pathlib import Path
from typing import Literal, get_args
from pydantic import Field, model_validator
from ..models.incident import Contract
from ..models.identity import structural_id
from .scoring import Label
from .stage_c_models import BenchmarkScoringProtocol
from .stage_c import read_run, publish
from . import benchmark
from .sanitization import digest, canonical_bytes


class ScoredDimension(Contract):
    name: Literal["mechanism_correctness", "known_edge_recall", "supported_claim_recall", "edge_precision",
        "false_supported_claims", "abstention", "answerability_restraint", "root_cause_overclaim",
        "tool_calls", "rounds", "duplicate_intent_count", "intent_efficiency", "acquisition_unavailable",
        "acquisition_failed", "replay_selection_ambiguous", "timeouts", "llm_calls", "llm_tokens", "llm_cost",
        "deterministic_consistency"]
    status: Literal["SCORED", "UNSCORED"]
    value: float | None = Field(default=None, allow_inf_nan=False)
    numerator: int | None = Field(default=None, ge=0)
    denominator: int | None = Field(default=None, ge=1)
    definition: str

    @model_validator(mode="after")
    def coherent(self):
        if (self.status == "SCORED") != (self.value is not None):
            raise ValueError("unscored dimensions require null; scored dimensions require a value")
        if (self.numerator is None) != (self.denominator is None):
            raise ValueError("ratio requires numerator and denominator")
        if self.denominator is not None and self.value != self.numerator / self.denominator:
            raise ValueError("ratio disagrees with value")
        return self


class BenchmarkScoreResult(Contract):
    schema_version: Literal[1] = 1
    scoring_protocol: BenchmarkScoringProtocol
    scoring_protocol_id: str
    run_key_id: str
    semantic_result_id: str
    run_archive_sha256: str
    candidate_id: str
    private_label_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    dimensions: tuple[ScoredDimension, ...]

    @model_validator(mode="after")
    def coherent(self):
        if self.scoring_protocol.id != self.scoring_protocol_id:
            raise ValueError("scoring protocol mismatch")
        if len({d.name for d in self.dimensions}) != len(self.dimensions):
            raise ValueError("duplicate score dimension")
        if {d.name for d in self.dimensions} != set(get_args(ScoredDimension.model_fields["name"].annotation)):
            raise ValueError("all dimensions must be explicitly scored or UNSCORED")
        return self

    @property
    def id(self):
        return structural_id("BS-", self.model_dump())


def root_cause_overclaim(report):
    return report.diagnosis_level not in {"failure_mode", "undetermined"} or report.underlying_cause is not None


def score_result(run, label, label_digest, protocol=BenchmarkScoringProtocol(), *, comparison=None):
    if not isinstance(label, Label) or not isinstance(protocol, BenchmarkScoringProtocol):
        raise ValueError("BSP-v1 requires legacy Label schema")
    semantic = run.semantic
    report, usage = semantic.report, semantic.usage
    subjects = {a.hypothesis_spec.id: a.hypothesis_spec for a in report.assessments}
    # Never select display hypotheses[0]; every claim references its structured HS.
    claims = [subjects[c.hypothesis_id] for c in report.claims if c.claim_class == "verified_failure_mode"]
    edge = label.observed_edge
    known = [h for h in claims if edge is None or (h.caller, h.callee) == edge]
    dimensions = []
    def metric(name, value, definition, ratio=None):
        dimensions.append(ScoredDimension(name=name, status="UNSCORED" if value is None else "SCORED",
            value=value, definition=definition, numerator=ratio[0] if ratio else None, denominator=ratio[1] if ratio else None))
    identifiable = label.answerability == "mechanism_identifiable"
    # The old payment label is a private compatibility spelling, never a runtime input.
    mode = "rpc_connection_establishment" if label.failure_mode == "payment_connection_failure" else label.failure_mode
    if identifiable and known and (edge is not None or len(claims) == 1):
        k, n = sum(h.failure_class == mode for h in known), len(known)
        metric("mechanism_correctness", k/n, "Structural mechanism match on the labeled edge (or unique claim); not semantic entailment.", (k, n))
    else:
        metric("mechanism_correctness", None, "No attributable supported claim or no identifiable mechanism label.")
    metric("known_edge_recall", float(bool(known)) if edge else None,
        "Recall of the one known labeled edge; not exhaustive edge-set recall.")
    metric("supported_claim_recall", float(any(h.failure_class == mode for h in known)) if identifiable else None,
        "Recall of the one known mechanism/edge claim when identifiable; no complete claim-set recall is asserted.")
    metric("edge_precision", None, "Legacy Label does not declare exhaustive expected edges.")
    metric("false_supported_claims", float(len(claims)) if label.answerability == "no_incident" else None,
        "All supported claims are overclaims only for an explicit no_incident label; extra edges otherwise remain unscored.")
    metric("abstention", float(not claims), "Observed absence of supported claims, independent of correctness.")
    metric("answerability_restraint", float(not claims) if label.answerability in {"symptom_only", "no_incident"} else None,
        "Abstention for symptom-only/no-incident labels; a correct abstention is not an incorrect diagnosis.")
    metric("root_cause_overclaim", float(root_cause_overclaim(report)), "Structural diagnosis_level/underlying_cause check, not prose parsing.")
    for name, value in (("tool_calls", usage.tool_calls), ("rounds", usage.rounds),
        ("duplicate_intent_count", usage.duplicate_intent_count), ("acquisition_unavailable", usage.unavailable_intents),
        ("acquisition_failed", usage.failed_intents), ("replay_selection_ambiguous", usage.ambiguous_intents),
        ("timeouts", usage.timeouts), ("llm_calls", usage.llm_calls),
        ("llm_tokens", usage.llm_input_tokens + usage.llm_output_tokens), ("llm_cost", usage.llm_cost)):
        metric(name, float(value), "Stable execution usage/disposition count.")
    metric("intent_efficiency", (usage.tool_calls-usage.duplicate_intent_count)/usage.tool_calls if usage.tool_calls else None,
        "Unique physically attempted logical intents / physical calls; not diagnostic information gain.")
    if comparison is not None and comparison.run_key != run.run_key:
        raise ValueError("determinism comparison requires the identical BRK trial")
    distinct_archive = comparison is not None and comparison.archive_sha256 != run.archive_sha256
    metric("deterministic_consistency", float(comparison.semantic_result_id == run.semantic_result_id) if distinct_archive else None,
        "Requires an independently executed rerun of the same BRK; an identical archive or cached reuse cannot establish consistency.")
    return BenchmarkScoreResult(scoring_protocol=protocol, scoring_protocol_id=protocol.id,
        run_key_id=run.run_key_id, semantic_result_id=run.semantic_result_id,
        run_archive_sha256=run.archive_sha256, candidate_id=run.admission.candidate_id,
        private_label_sha256=label_digest, dimensions=tuple(dimensions))


def score_run(run_path, labels_root, output, *, protocol=BenchmarkScoringProtocol(), comparison_path=None):
    if not isinstance(protocol, BenchmarkScoringProtocol):
        raise ValueError("BSP-v1 scoring requires its matching protocol")
    run_path = Path(run_path)
    if comparison_path is not None:
        def archive_path(path):
            path = Path(path)
            return (path / "run.json" if path.is_dir() else path).resolve()
        if archive_path(comparison_path) == archive_path(run_path):
            raise ValueError("determinism comparison requires a separate rerun archive")
    run = read_run(run_path)
    root = benchmark._root(Path(labels_root))
    label_bytes = benchmark._read_bytes(benchmark._safe(root, run.admission.candidate_id + ".json"))
    label = Label.model_validate(benchmark._json(label_bytes))
    result = score_result(run, label, digest(label_bytes), protocol,
        comparison=read_run(comparison_path) if comparison_path else None)
    output = Path(output)
    # Exclusive publication also prevents accidental overwrites of any input artifact.
    if output.exists():
        existing = BenchmarkScoreResult.model_validate(benchmark._json(benchmark._read_bytes(output)))
        if existing != result:
            raise ValueError("score output is immutable; use another path for another BSP/label")
    else:
        publish(output, result.model_dump(mode="json"))
    return result


def summarize_scores(scores_root, runs, paired, variants):
    """Optional score artifacts; each BSP and metric gets explicit shared denominators."""
    from statistics import mean, median
    from .stage_c_summary import COMPARISONS, MAX_SCORING_PROTOCOLS, rate
    if not scores_root.exists():
        return {"protocols": [], "invalid_scores": 0}
    scores, invalid, conflicted = {}, 0, set()
    for path in scores_root.rglob("*.json"):
        try:
            payload = benchmark._json(benchmark._read_bytes(path))
            if isinstance(payload, dict) and payload.get("schema_version") == 2:
                from .gold_scoring import BenchmarkScoreResultV2
                score = BenchmarkScoreResultV2.model_validate(payload)
            else:
                score = BenchmarkScoreResult.model_validate(payload)
            entry = next(((cell, r) for cell, r in runs.items() if r.run_key_id == score.run_key_id), None)
            run = entry[1] if entry else None
            if (run is None or run.semantic_result_id != score.semantic_result_id or
                run.archive_sha256 != score.run_archive_sha256 or run.admission.candidate_id != score.candidate_id):
                raise ValueError("score does not reference a current valid run")
            cell = score.scoring_protocol_id, score.candidate_id, entry[0][1]
            if cell in conflicted or (cell in scores and scores[cell] != score):
                scores.pop(cell, None)
                conflicted.add(cell)
                raise ValueError("conflicting scores for one protocol/trial")
            scores[cell] = score
        except (ValueError, OSError, KeyError, TypeError):
            invalid += 1
    protocols = []
    protocol_ids = sorted({p for p, _, _ in scores})
    if len(protocol_ids) > MAX_SCORING_PROTOCOLS:
        raise ValueError("experiment exceeds bounded scoring-protocol count")
    for protocol in protocol_ids:
        candidates = [c for c in paired if all((protocol, c, v) in scores for v in variants)]
        # A paired score also requires the SAME private label bytes across variants.
        candidates = [c for c in candidates if len({scores[protocol, c, v].private_label_sha256 for v in variants}) == 1]
        metrics, deltas = [], {(a, b): [] for a, b in COMPARISONS if a in variants and b in variants}
        names = sorted({d.name for key, score in scores.items() if key[0] == protocol for d in score.dimensions})
        for name in names:
            values = {(c, v): next((d for d in scores[protocol, c, v].dimensions if d.name == name), None)
                      for c in candidates for v in variants}
            eligible = [c for c in candidates if all(values[c, v] is not None and values[c, v].status == "SCORED" for v in variants)]
            aggregates = {}
            for variant in variants:
                dimensions = [values[c, variant] for c in eligible]
                observed = [d.value for d in dimensions]
                n, total = len(observed), sum(observed)
                # Candidate macro mean and pooled claim ratio are different estimands.
                pooled = bool(dimensions) and all(d.numerator is not None for d in dimensions)
                pooled_k = sum(d.numerator for d in dimensions) if pooled else None
                pooled_n = sum(d.denominator for d in dimensions) if pooled else None
                aggregates[variant] = dict(status="SCORED" if n else "UNSCORED", k=total, n=n,
                    total=total, mean=total/n if n else None, median=median(observed) if n else None,
                    pooled_k=pooled_k, pooled_n=pooled_n, pooled_value=pooled_k/pooled_n if pooled else None,
                    any_positive_rate=rate(sum(v > 0 for v in observed), n).model_dump(mode="json"))
            metrics.append(dict(name=name, paired_n=len(eligible), eligible_candidate_ids=eligible,
                by_variant=aggregates))
            for (a, b), comparisons in deltas.items():
                rows = [dict(candidate_id=c, value=values[c,b].value-values[c,a].value) for c in eligible]
                comparisons.append(dict(name=name, paired_n=len(rows), rows=rows,
                    mean=mean(r["value"] for r in rows) if rows else None))
        protocols.append(dict(scoring_protocol_id=protocol, paired_scored_candidate_ids=candidates, metrics=metrics,
            deltas=[dict(from_variant=a, to_variant=b, metrics=rows) for (a,b),rows in deltas.items()]))
    return {"protocols": protocols, "invalid_scores": invalid}
