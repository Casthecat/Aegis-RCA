"""Bounded, versioned complete-case statistics; no telemetry or label reads."""
from collections import Counter
from statistics import mean, median
from typing import Literal
from pydantic import Field, model_validator
from ..models.incident import Contract
from .stage_c_models import FailureCategory, HarnessFailure

VariantKey = Literal["B0", "B1", "B2"]
MAX_CANDIDATES = 256
MAX_SCORING_PROTOCOLS = 32
COMPARISONS = (("B0", "B1"), ("B1", "B2"))


class Rate(Contract):
    k: int = Field(ge=0, le=768)
    n: int = Field(ge=0, le=768)
    value: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)

    @model_validator(mode="after")
    def coherent(self):
        if self.k > self.n or self.value != (self.k / self.n if self.n else None):
            raise ValueError("rate requires k <= n and exact k/n; empty denominator is null")
        return self


def rate(k, n):
    return Rate(k=k, n=n, value=k/n if n else None)


class Distribution(Contract):
    n: int = Field(ge=0, le=MAX_CANDIDATES)
    total: float = Field(ge=0, allow_inf_nan=False)
    mean: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    median: float | None = Field(default=None, ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def coherent(self):
        if self.n == 0:
            if self.total != 0 or self.mean is not None or self.median is not None:
                raise ValueError("empty distribution must have zero total and null summaries")
        elif self.mean != self.total / self.n or self.median is None or self.median > self.total:
            raise ValueError("distribution total/count/mean/median disagree")
        return self


def distribution(values):
    return Distribution(n=len(values), total=sum(values), mean=sum(values)/len(values) if values else None,
                        median=median(values) if values else None)


class VariantSummary(Contract):
    requested: int = Field(ge=1, le=MAX_CANDIDATES)
    admitted: int = Field(ge=0, le=MAX_CANDIDATES)
    attempted: int = Field(ge=0, le=MAX_CANDIDATES)
    completed: int = Field(ge=0, le=MAX_CANDIDATES)
    valid: int = Field(ge=0, le=MAX_CANDIDATES)  # Compatibility spelling of completed.
    harness_errors: int = Field(ge=0, le=MAX_CANDIDATES)
    completion_rate: Rate
    harness_error_rate: Rate
    paired_n: int = Field(ge=0, le=MAX_CANDIDATES)
    paired_supported: int = Field(ge=0, le=MAX_CANDIDATES)
    paired_tool_calls: int = Field(ge=0)
    paired_rounds: int = Field(ge=0)
    paired_failed_intents: int = Field(ge=0)
    paired_unavailable_intents: int = Field(ge=0)
    paired_inconclusive_rate: Rate
    paired_abstention_rate: Rate
    paired_root_cause_overclaim_rate: Rate
    tool_calls: Distribution
    rounds: Distribution
    limitation_histogram: dict[FailureCategory, int] = Field(max_length=13)
    llm_calls: Literal[0] = 0
    llm_tokens: Literal[0] = 0
    llm_cost: Literal[0] = 0

    @model_validator(mode="after")
    def coherent(self):
        if not (self.paired_n <= self.completed == self.valid <= self.attempted <= self.requested):
            raise ValueError("invalid completion/attempt/paired counts")
        if not self.completed <= self.admitted <= self.requested or self.harness_errors > self.attempted:
            raise ValueError("invalid admission/harness counts")
        if self.completion_rate != rate(self.completed, self.requested) or self.harness_error_rate != rate(self.harness_errors, self.requested):
            raise ValueError("intention-to-test rates require requested denominator")
        if self.paired_supported > self.paired_n or self.paired_inconclusive_rate != rate(self.paired_n-self.paired_supported, self.paired_n):
            raise ValueError("paired status totals disagree")
        if any(r.n != self.paired_n for r in (self.paired_abstention_rate, self.paired_root_cause_overclaim_rate)):
            raise ValueError("paired rates require shared denominator")
        if self.tool_calls.n != self.paired_n or self.rounds.n != self.paired_n:
            raise ValueError("cost statistics require shared paired subset")
        if self.tool_calls.total != self.paired_tool_calls or self.rounds.total != self.paired_rounds:
            raise ValueError("cost totals disagree")
        if any(k < 1 or k > self.attempted for k in self.limitation_histogram.values()):
            raise ValueError("histogram counts distinct observed trial cells")
        return self


class PairedCell(Contract):
    run_key_id: str = Field(pattern=r"^BRK-[0-9a-f]{64}$")
    semantic_result_id: str = Field(pattern=r"^BRS-[0-9a-f]{64}$")
    status: Literal["SUPPORTED", "INCONCLUSIVE"]
    tool_calls: int = Field(ge=0, le=100)
    rounds: int = Field(ge=0, le=10)
    supported_claims: int = Field(ge=0, le=16)
    failure_categories: tuple[FailureCategory, ...] = Field(max_length=13)


class PairedRow(Contract):
    candidate_id: str
    by_variant: dict[VariantKey, PairedCell] = Field(min_length=1, max_length=3)


class DeltaValues(Contract):
    tool_calls: float | None = Field(allow_inf_nan=False)
    rounds: float | None = Field(allow_inf_nan=False)
    supported_claims: float | None = Field(allow_inf_nan=False)
    supported: float | None = Field(allow_inf_nan=False)


class DeltaRow(DeltaValues):
    candidate_id: str


class PairedDelta(Contract):
    from_variant: VariantKey
    to_variant: VariantKey
    paired_n: int = Field(ge=0, le=MAX_CANDIDATES)
    rows: tuple[DeltaRow, ...] = Field(max_length=MAX_CANDIDATES)
    means: DeltaValues

    @model_validator(mode="after")
    def coherent(self):
        if (self.from_variant, self.to_variant) not in COMPARISONS or len(self.rows) != self.paired_n:
            raise ValueError("invalid primary comparison")
        for field in DeltaValues.model_fields:
            values = [getattr(r, field) for r in self.rows]
            if any(v is None for v in values) or getattr(self.means, field) != (mean(values) if values else None):
                raise ValueError("delta means must use explicit paired rows")
        return self


class MetricAggregate(Contract):
    status: Literal["SCORED", "UNSCORED"]
    k: float = Field(allow_inf_nan=False)  # Sum of candidate values; a count metric may exceed n.
    n: int = Field(ge=0, le=MAX_CANDIDATES)
    total: float = Field(allow_inf_nan=False)
    mean: float | None = Field(allow_inf_nan=False)
    median: float | None = Field(allow_inf_nan=False)
    pooled_k: int | None = Field(default=None, ge=0)
    pooled_n: int | None = Field(default=None, ge=1)
    pooled_value: float | None = Field(default=None, allow_inf_nan=False)
    any_positive_rate: Rate

    @model_validator(mode="after")
    def coherent(self):
        if self.total != self.k or self.mean != (self.k/self.n if self.n else None):
            raise ValueError("metric sum/count/mean disagree")
        if self.status != ("SCORED" if self.n else "UNSCORED") or self.any_positive_rate.n != self.n:
            raise ValueError("metric scoring denominator mismatch")
        if not self.n and (self.k != 0 or self.median is not None):
            raise ValueError("empty metric summaries must be null")
        if self.n and self.median is None:
            raise ValueError("scored metric median required")
        if self.pooled_n is None:
            if self.pooled_k is not None or self.pooled_value is not None:
                raise ValueError("inapplicable pooled ratio must be null")
        elif self.pooled_k is None or self.pooled_value != self.pooled_k/self.pooled_n:
            raise ValueError("pooled ratio mismatch")
        return self


class MetricSummary(Contract):
    name: str = Field(min_length=1, max_length=80)
    paired_n: int = Field(ge=0, le=MAX_CANDIDATES)
    eligible_candidate_ids: tuple[str, ...] = Field(max_length=MAX_CANDIDATES)
    by_variant: dict[VariantKey, MetricAggregate] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def coherent(self):
        if self.paired_n != len(self.eligible_candidate_ids) or any(v.n != self.paired_n for v in self.by_variant.values()):
            raise ValueError("all metric variants must share the explicit candidate denominator")
        return self


class MetricDeltaRow(Contract):
    candidate_id: str
    value: float = Field(allow_inf_nan=False)


class MetricDelta(Contract):
    name: str = Field(min_length=1, max_length=80)
    paired_n: int = Field(ge=0, le=MAX_CANDIDATES)
    rows: tuple[MetricDeltaRow, ...] = Field(max_length=MAX_CANDIDATES)
    mean: float | None = Field(allow_inf_nan=False)

    @model_validator(mode="after")
    def coherent(self):
        if len(self.rows) != self.paired_n or self.mean != (mean(r.value for r in self.rows) if self.rows else None):
            raise ValueError("scored delta must use explicit paired rows")
        return self


class ScoredDelta(Contract):
    from_variant: VariantKey
    to_variant: VariantKey
    metrics: tuple[MetricDelta, ...] = Field(max_length=22)


class ScoredProtocolSummary(Contract):
    scoring_protocol_id: str = Field(pattern=r"^BSP-[0-9a-f]{64}$")
    paired_scored_candidate_ids: tuple[str, ...] = Field(max_length=MAX_CANDIDATES)
    metrics: tuple[MetricSummary, ...] = Field(max_length=22)
    deltas: tuple[ScoredDelta, ...] = Field(max_length=2)


class ScoredComparisons(Contract):
    protocols: tuple[ScoredProtocolSummary, ...] = Field(max_length=MAX_SCORING_PROTOCOLS)
    invalid_scores: int = Field(ge=0)


class IncompleteTrial(Contract):
    candidate_id: str
    variant: VariantKey
    status: Literal["missing_or_failed_or_invalid"]


class ExperimentSummary(Contract):
    schema_version: Literal[1] = 1
    manifest_id: str = Field(pattern=r"^BM-[0-9a-f]{64}$")
    candidate_ids: tuple[str, ...] = Field(min_length=1, max_length=MAX_CANDIDATES)
    candidate_count: int = Field(ge=1, le=MAX_CANDIDATES)
    admitted_candidate_count: int = Field(ge=0, le=MAX_CANDIDATES)
    requested_trial_count: int = Field(ge=1, le=768)
    attempted_trial_count: int = Field(ge=0, le=768)
    completed_trial_count: int = Field(ge=0, le=768)
    paired_candidate_ids: tuple[str, ...] = Field(max_length=MAX_CANDIDATES)
    by_variant: dict[VariantKey, VariantSummary] = Field(min_length=1, max_length=3)
    incomplete_trials: tuple[IncompleteTrial, ...] = Field(max_length=768)
    invalid_archives: int = Field(ge=0)
    harness_failures: tuple[HarnessFailure, ...] = Field(max_length=768)
    admission_mismatch_candidates: tuple[str, ...] = Field(max_length=MAX_CANDIDATES)
    limitation_histogram: dict[FailureCategory, int] = Field(max_length=13)
    paired_rows: tuple[PairedRow, ...] = Field(max_length=MAX_CANDIDATES)
    deltas: tuple[PairedDelta, ...] = Field(max_length=2)
    scored_comparisons: ScoredComparisons
    llm_calls: Literal[0] = 0
    llm_tokens: Literal[0] = 0
    llm_cost: Literal[0] = 0
    notice: str = Field(max_length=2000)

    @model_validator(mode="after")
    def coherent(self):
        from .stage_c_models import opaque
        if self.candidate_ids != tuple(sorted(set(opaque(c) for c in self.candidate_ids))) or self.candidate_count != len(self.candidate_ids):
            raise ValueError("canonical bounded candidate set/count required")
        if self.admitted_candidate_count > self.candidate_count or self.requested_trial_count != self.candidate_count*len(self.by_variant):
            raise ValueError("requested/admitted counts disagree")
        if self.attempted_trial_count != sum(v.attempted for v in self.by_variant.values()) or self.completed_trial_count != sum(v.completed for v in self.by_variant.values()):
            raise ValueError("global trial counts disagree")
        if any(v.requested != self.candidate_count or v.admitted != self.admitted_candidate_count or v.paired_n != len(self.paired_candidate_ids) for v in self.by_variant.values()):
            raise ValueError("variant denominators must match manifest/common subset")
        if self.paired_candidate_ids != tuple(sorted(set(self.paired_candidate_ids))) or not set(self.paired_candidate_ids) <= set(self.candidate_ids):
            raise ValueError("invalid paired subset")
        if tuple(r.candidate_id for r in self.paired_rows) != self.paired_candidate_ids or any(set(r.by_variant) != set(self.by_variant) for r in self.paired_rows):
            raise ValueError("paired rows must exactly cover shared candidates and variants")
        incomplete = {(r.candidate_id, r.variant) for r in self.incomplete_trials}
        if (len(incomplete) != len(self.incomplete_trials) or
            any(c not in self.candidate_ids or v not in self.by_variant or c in self.paired_candidate_ids for c,v in incomplete) or
            any(sum(v == variant for _,v in incomplete) != self.candidate_count-row.completed
                for variant,row in self.by_variant.items())):
            raise ValueError("incomplete cells must uniquely account for every noncompleted trial")
        for variant, aggregate in self.by_variant.items():
            cells = [r.by_variant[variant] for r in self.paired_rows]
            if aggregate.tool_calls != distribution([c.tool_calls for c in cells]) or aggregate.rounds != distribution([c.rounds for c in cells]):
                raise ValueError("paired distributions must equal paired rows")
            if aggregate.paired_supported != sum(c.status == "SUPPORTED" for c in cells):
                raise ValueError("paired report counts disagree")
        expected_pairs = tuple((a,b) for a,b in COMPARISONS if a in self.by_variant and b in self.by_variant)
        if tuple((d.from_variant,d.to_variant) for d in self.deltas) != expected_pairs:
            raise ValueError("primary delta set/order mismatch")
        for delta in self.deltas:
            if tuple(r.candidate_id for r in delta.rows) != self.paired_candidate_ids:
                raise ValueError("deltas must use the global complete-case subset")
            for change, row in zip(delta.rows, self.paired_rows):
                a,b = row.by_variant[delta.from_variant],row.by_variant[delta.to_variant]
                expected = dict(tool_calls=b.tool_calls-a.tool_calls, rounds=b.rounds-a.rounds,
                    supported_claims=b.supported_claims-a.supported_claims,
                    supported=int(b.status == "SUPPORTED")-int(a.status == "SUPPORTED"))
                if change.model_dump(exclude={"candidate_id"}) != expected:
                    raise ValueError("delta must be right minus left for the same candidate")
        histogram = Counter()
        for v in self.by_variant.values():
            histogram.update(v.limitation_histogram)
        if dict(histogram) != self.limitation_histogram:
            raise ValueError("global histogram must equal per-variant trial counts")
        for protocol in self.scored_comparisons.protocols:
            allowed = set(protocol.paired_scored_candidate_ids)
            if not allowed <= set(self.paired_candidate_ids):
                raise ValueError("scored pairing must be a subset of execution pairing")
            if any(set(m.by_variant) != set(self.by_variant) or not set(m.eligible_candidate_ids) <= allowed for m in protocol.metrics):
                raise ValueError("metric denominators must preserve paired score subset")
        return self


def extend_summary(payload, manifest, runs, attempted):
    """Inputs are already validated execution artifacts; never resolve telemetry here."""
    from .stage_c_scoring import root_cause_overclaim
    paired, variants = payload["paired_candidate_ids"], manifest.spec.variants
    failures = {(f["candidate_id"],f["variant_id"]):f for f in payload["harness_failures"]}
    histogram = Counter()
    for key, variant in zip(variants,manifest.variants):
        all_runs = [run for (c,v),run in runs.items() if v == key]
        rows = [runs[c,key] for c in paired]
        per_variant = Counter()
        failure_count = 0
        for candidate in manifest.spec.candidate_ids:
            run = runs.get((candidate,key))
            categories = set(run.semantic.failure_categories) if run else set()
            if (candidate,variant.id) in failures or ((candidate,key) in attempted and run is None):
                categories.add("harness_error")
                failure_count += 1
            per_variant.update(categories)
        histogram.update(per_variant)
        data = payload["by_variant"][key]
        data.update(admitted=len(manifest.admissions), attempted=sum((c,key) in attempted for c in manifest.spec.candidate_ids),
            completed=len(all_runs), harness_errors=failure_count,
            completion_rate=rate(len(all_runs),len(manifest.spec.candidate_ids)),
            harness_error_rate=rate(failure_count,len(manifest.spec.candidate_ids)),
            paired_inconclusive_rate=rate(sum(r.semantic.report.status == "INCONCLUSIVE" for r in rows),len(rows)),
            paired_abstention_rate=rate(sum(not r.semantic.report.claims for r in rows),len(rows)),
            paired_root_cause_overclaim_rate=rate(sum(root_cause_overclaim(r.semantic.report) for r in rows),len(rows)),
            tool_calls=distribution([r.semantic.usage.tool_calls for r in rows]),
            rounds=distribution([r.semantic.usage.rounds for r in rows]), limitation_histogram=dict(sorted(per_variant.items())))
    paired_rows = [PairedRow(candidate_id=c, by_variant={v:PairedCell(run_key_id=r.run_key_id,
        semantic_result_id=r.semantic_result_id,status=r.semantic.report.status,tool_calls=r.semantic.usage.tool_calls,
        rounds=r.semantic.usage.rounds,supported_claims=len(r.semantic.report.claims),failure_categories=r.semantic.failure_categories)
        for v in variants for r in [runs[c,v]]}) for c in paired]
    deltas = []
    for a,b in COMPARISONS:
        if a not in variants or b not in variants:
            continue
        rows = []
        for row in paired_rows:
            left,right = row.by_variant[a],row.by_variant[b]
            rows.append(DeltaRow(candidate_id=row.candidate_id,tool_calls=right.tool_calls-left.tool_calls,
                rounds=right.rounds-left.rounds,supported_claims=right.supported_claims-left.supported_claims,
                supported=int(right.status == "SUPPORTED")-int(left.status == "SUPPORTED")))
        deltas.append(PairedDelta(from_variant=a,to_variant=b,paired_n=len(rows),rows=tuple(rows),
            means=DeltaValues(**{f:mean(getattr(r,f) for r in rows) if rows else None for f in DeltaValues.model_fields})))
    return ExperimentSummary(**payload,candidate_ids=manifest.spec.candidate_ids,candidate_count=len(manifest.spec.candidate_ids),
        admitted_candidate_count=len(manifest.admissions),requested_trial_count=len(manifest.spec.candidate_ids)*len(variants),
        attempted_trial_count=len(attempted),completed_trial_count=len(runs),limitation_histogram=dict(sorted(histogram.items())),
        paired_rows=tuple(paired_rows),deltas=tuple(deltas))
