"""Structural report scoring, separately recorded human review, and split guards."""
import math
import re
from statistics import median
from typing import Any, Literal

from pydantic import Field, model_validator

from aegis_rca.models.evidence import Evidence
from aegis_rca.models.hypothesis import IncidentReport
from aegis_rca.models.incident import Contract, TimeWindow


class Label(Contract):
    answerability: Literal["mechanism_identifiable", "symptom_only", "no_incident"]
    failure_mode: str | None = None
    observed_edge: tuple[str, str] | None = None
    origin_service: str | None = None

    @model_validator(mode="after")
    def identifiable_requires_mechanism(self):
        if self.answerability == "mechanism_identifiable" and not (self.failure_mode or "").strip():
            raise ValueError("identifiable labels require an explicit failure_mode")
        if self.answerability == "no_incident" and self.failure_mode is not None:
            raise ValueError("no_incident cannot have a failure_mode")
        return self


class HumanReview(Contract):
    reviewer: str = Field(min_length=1)
    checked_claims: int = Field(ge=0)
    unsupported_claims: int = Field(ge=0)
    semantic_mechanism_correct: bool | None = None

    @model_validator(mode="after")
    def coherent_counts(self):
        if self.unsupported_claims > self.checked_claims:
            raise ValueError("unsupported_claims exceeds checked_claims")
        return self


class EpisodeSpec(Contract):
    id: str = Field(min_length=1)
    group: str = Field(min_length=1)
    parent_episode: str | None = None
    split: Literal["dev", "validation", "test"]
    report: str
    evidence: str
    label: str
    window: TimeWindow
    services: tuple[str, ...] = Field(min_length=1)
    human_review: str | None = None
    latency_ms: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class Manifest(Contract):
    episodes: tuple[EpisodeSpec, ...]
    injected_flags: tuple[str, ...] = ()
    scenario_answers: tuple[str, ...] = ()


def validate_splits(episodes: tuple[EpisodeSpec, ...] | list[EpisodeSpec]) -> None:
    seen: dict[tuple[str, str], str] = {}
    ids: set[str] = set()
    for episode in episodes:
        if episode.id in ids:
            raise ValueError(f"duplicate episode id: {episode.id}")
        ids.add(episode.id)
        keys = [("episode", episode.id), ("group", episode.group)]
        if episode.parent_episode:
            keys.append(("episode", episode.parent_episode))
        for key in keys:
            if key in seen and seen[key] != episode.split:
                raise ValueError(f"split leakage for {key[0]} {key[1]!r}")
            seen[key] = episode.split


def scan_leakage(payload: Any, *, injected_flags=(), scenario_answers=(), max_hits: int = 100) -> list[dict]:
    """Heuristic candidate hits in public paths/keys/text, not proof of isolation."""
    if isinstance(max_hits, bool) or not isinstance(max_hits, int) or max_hits < 1:
        raise ValueError("max_hits must be a positive integer")
    needles = [("injected_flag", value.casefold()) for value in injected_flags if value.strip()]
    needles += [("scenario_answer", value.casefold()) for value in scenario_answers if value.strip()]
    hits: list[dict] = []
    stack = [("$", payload)]
    while stack and len(hits) < max_hits:
        path, value = stack.pop()
        text = (path + " " + (value if isinstance(value, str) else "")).casefold()
        reasons = [kind for kind, needle in needles if needle in text]
        if re.search(r"feature_flag[._]|injection\.json", text):
            reasons.append("injection_metadata")
        if reasons:
            hits.append({"path": path, "reasons": sorted(set(reasons))})
        if isinstance(value, dict):
            stack.extend((f"{path}.{key}", child) for key, child in reversed(list(value.items())))
        elif isinstance(value, (list, tuple)):
            stack.extend((f"{path}[{i}]", child) for i, child in reversed(list(enumerate(value))))
    return hits


def score_episode(report: IncidentReport, evidence: list[Evidence] | tuple[Evidence, ...], label: Label,
                  window: TimeWindow, services: tuple[str, ...], *, human_review: HumanReview | None = None,
                  latency_ms: float | None = None) -> dict:
    if latency_ms is not None and (not math.isfinite(latency_ms) or latency_ms < 0):
        raise ValueError("latency_ms must be finite and nonnegative")
    if report.tool_calls < 0:
        raise ValueError("tool_calls cannot be negative")
    registry = {item.id: item for item in evidence}
    if len(registry) != len(evidence):
        raise ValueError("duplicate evidence IDs make citation identity ambiguous")
    leading = report.hypotheses[0] if report.hypotheses else None
    conclusive = bool(report.status == "SUPPORTED" and report.diagnosis_level == "failure_mode"
                      and leading and leading.status == "supported")
    eligible = label.answerability == "mechanism_identifiable"
    match = bool(conclusive and leading.failure_mode == label.failure_mode) if eligible else None
    cited = [claim for claim in report.claims if claim.evidence_ids]
    def valid(ids):
        return all(id in registry and window.start <= registry[id].timestamp < window.end
                   and registry[id].service in services and registry[id].source.locator.strip() for id in ids)
    validity = sum(bool(valid(claim.evidence_ids)) for claim in cited)
    hypothesis_ids = [h.supporting_evidence_ids + h.contradicting_evidence_ids for h in report.hypotheses
                      if h.supporting_evidence_ids or h.contradicting_evidence_ids]
    human = human_review.model_dump() if human_review else None
    return {"answerability": label.answerability, "mechanism_exact_match": match,
            "conclusive": conclusive, "structural_citations": {"k": validity, "n": len(cited)},
            "hypothesis_structural_citations": {"k": sum(bool(valid(ids)) for ids in hypothesis_ids), "n": len(hypothesis_ids)},
            "uncited_claims": len(report.claims) - len(cited),
            "healthy_false_positive": (report.status == "SUPPORTED" or any(h.status == "supported" for h in report.hypotheses)) if label.answerability == "no_incident" else None,
            "symptom_only_structural_restraint": (report.status == "INCONCLUSIVE" and not any(h.status == "supported" for h in report.hypotheses)) if label.answerability == "symptom_only" else None,
            "human_review": human, "localization": "not_scored_report_has_no_structured_origin_or_edge",
            "latency_ms": latency_ms, "tool_calls": report.tool_calls}


def _ratio(k: int, n: int) -> dict:
    return {"k": k, "n": n, "value": k / n if n else None, "display": f"{k}/{n}" if n else "N/A"}


def _distribution(values: list[float]) -> dict:
    values = sorted(values)
    return {"n": len(values), "total": sum(values) if values else None,
            "median": median(values) if values else None,
            "p95": values[math.ceil(0.95 * len(values)) - 1] if values else None}


def aggregate(rows: list[dict]) -> dict:
    eligible = [row for row in rows if row["mechanism_exact_match"] is not None]
    selected = [row for row in eligible if row["conclusive"]]
    reviewed = [row["human_review"] for row in rows if row["human_review"] is not None]
    semantic = [review["semantic_mechanism_correct"] for review in reviewed if review["semantic_mechanism_correct"] is not None]
    def boolean_ratio(field):
        values = [row[field] for row in rows if row[field] is not None]
        return _ratio(sum(values), len(values))
    return {"report_records": len(rows),
            "mechanism_exact_match": _ratio(sum(row["mechanism_exact_match"] for row in eligible), len(eligible)),
            "conclusive_coverage": _ratio(len(selected), len(eligible)),
            "selective_exact_match": _ratio(sum(row["mechanism_exact_match"] for row in selected), len(selected)),
            "structural_citation_validity": _ratio(sum(row["structural_citations"]["k"] for row in rows), sum(row["structural_citations"]["n"] for row in rows)),
            "healthy_false_positive": boolean_ratio("healthy_false_positive"),
            "symptom_only_structural_restraint": boolean_ratio("symptom_only_structural_restraint"),
            "human_unsupported_claim_rate": _ratio(sum(review["unsupported_claims"] for review in reviewed), sum(review["checked_claims"] for review in reviewed)),
            "human_semantic_mechanism_correct": _ratio(sum(semantic), len(semantic)),
            "human_reviewed_reports": len(reviewed),
            "latency_ms": _distribution([row["latency_ms"] for row in rows if row["latency_ms"] is not None]),
            "tool_calls": _distribution([row["tool_calls"] for row in rows if row["tool_calls"] is not None])}
