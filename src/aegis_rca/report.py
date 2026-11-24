from .models.hypothesis import IncidentReport, ReportClaim, PortfolioOverflowSummary
from .policy import connectivity_citations, form_hypotheses
from .state import IncidentState
from .verification.persisted import current_hypothesis_verifications


def audit_and_report(state: IncidentState) -> IncidentReport:
    """Audit a fixed structured claim template, not arbitrary LLM prose.

    A future LLM report needs a separate semantic entailment check. Citation
    existence alone does not mean its contents support the claim.
    """
    evidence = state["evidence"]
    verification = state["verification"]
    from .portfolio import current_hypothesis_portfolio
    from .assessments import current_assessments
    bundles = current_hypothesis_verifications(state)
    portfolio = current_hypothesis_portfolio(state)
    assessments = current_assessments(state)
    supported = {a.hypothesis_spec.id for a in assessments if a.status == "SUPPORTED"}
    mode = state.get("source_mode", "synthetic_fixture")
    reasoning = state.get("reasoning_mode", "deterministic")
    limits = [
        ("Synthetic telemetry; this is not measured RCA performance." if mode == "synthetic_fixture" else
         "Recorded telemetry replay; data completeness and deployment cause are not guaranteed." if mode == "snapshot" else
         "Live read-only telemetry; data completeness and deployment cause are not guaranteed."),
        ("Deterministic investigation policy; no LLM was used." if reasoning == "deterministic" else
         "LLM proposals are provisional; the supported finding uses deterministic evidence predicates."),
        "Configuration, DNS, process health and deployment cause remain unverified.",
        "Log and trace evidence may describe the same RPC; they are not independent trials.",
        "No temporal fault-propagation or exclusive incident cause has been established.",
    ]
    limits.extend(state.get("reasoning_notes", []))
    from .scheduling import scheduling_report_limits
    limits.extend(scheduling_report_limits(state))
    for call in state["tool_calls"]:
        if call.status in {"error", "timeout", "empty"}:
            limits.append(f"{call.task.signal} query returned {call.status}; absence is unproven.")
        if call.truncated:
            limits.append(f"{call.task.signal} result was truncated.")
        if call.coverage_complete is not True:
            limits.append(f"{call.task.signal} telemetry coverage is unknown or incomplete.")
        limits.extend(call.warnings)
    claims, questions = [], []
    for bundle in bundles:
        subject = bundle.hypothesis_spec
        questions.extend((f"Inspect {subject.caller}'s configured endpoint for {subject.callee} and verify DNS/connectivity.",
                          f"Why the {subject.callee} dependency was unreachable remains unresolved; inspect both sides."))
        if subject.id in supported:
            result = bundle.evaluation
            claims.append(ReportClaim(text=f"{subject.caller} could not establish its {subject.callee} dependency.",
                claim_class="verified_failure_mode", hypothesis_id=subject.id, plan_result_id=result.plan_result.id,
                check_result_ids=(result.trace_check.id, result.log_check.id),
                evidence_ids=connectivity_citations(evidence, subject)))
    active_ids = {b.hypothesis_spec.id for b in bundles}
    provisional = tuple(h.model_copy(update={"status": "uncertain"}) for h in state.get("provisional_hypotheses", ()) if h.id not in active_ids)[:3]
    return IncidentReport(
        status="SUPPORTED" if claims else "INCONCLUSIVE",
        diagnosis_level="failure_mode" if claims else "undetermined", underlying_cause=None,
        summary=(claims[0].text if len(claims) == 1 else f"{len(claims)} dependency failure modes verified; ultimate cause remains unresolved.")
                if claims else "Insufficient evidence for a supported diagnosis.",
        claims=tuple(claims), hypotheses=tuple(form_hypotheses(state)), assessments=assessments,
        provisional_hypotheses=provisional,
        portfolio_overflow=PortfolioOverflowSummary(count=portfolio.overflow_count, policy_limit=portfolio.policy.max_active_hypotheses) if portfolio else None,
        limitations=tuple(dict.fromkeys(limits)),
        next_checks=tuple(questions) if questions else
                    (("Obtain caller-side RPC and connection-establishment evidence for the original seeds.",) if state["scope"] else
                     ("Supply an incident with valid bounded service seeds and a valid window.",)),
        rounds=state["rounds"], tool_calls=len(state["tool_calls"]),
        termination_reason=verification.reason if verification else "invalid_scope",
        mode=mode, reasoning_mode=reasoning,
    )


def to_markdown(report: IncidentReport) -> str:
    lines = ["# AegisRCA investigation report", "", f"Status: **{report.status}**",
             f"Diagnosis level: `{report.diagnosis_level}`", "",
             f"> Data mode: {report.mode}; reasoning: {report.reasoning_mode}. No benchmark claim.", "",
             report.summary, "", "Underlying cause: **undetermined**", "", "## Claims", ""]
    lines.extend(f"- {claim.text} [{', '.join(claim.evidence_ids)}]" for claim in report.claims)
    if not report.claims:
        lines.append("No supported diagnostic claim.")
    lines.extend(["", "## Current hypothesis assessments", ""])
    lines.extend(f"- {a.status}: {a.hypothesis_spec.caller} → {a.hypothesis_spec.callee} ({a.hypothesis_spec.id})"
                 for a in report.assessments)
    for a in report.assessments:
        if a.status != "SUPPORTED":
            lines.append(f"- Unestablished checks: {', '.join(a.unestablished_check_ids)}. {a.next_question}")
            lines.extend(f"- {limit.reason}: {limit.check_id}" for limit in a.limitations)
    if report.portfolio_overflow and report.portfolio_overflow.count:
        lines.append(f"- Portfolio overflow: {report.portfolio_overflow.count}; active policy limit: {report.portfolio_overflow.policy_limit}.")
    if report.provisional_hypotheses:
        lines.extend(["", "## Provisional LLM hypotheses (unverified)", ""])
        lines.extend(f"- {h.claim}" for h in report.provisional_hypotheses)
    lines.extend(["", "## Limitations", ""])
    lines.extend(f"- {limit}" for limit in report.limitations)
    lines.extend(["", "## Next checks", ""])
    lines.extend(f"- {check}" for check in report.next_checks)
    lines.extend(["", f"Rounds: {report.rounds}; tool calls: {report.tool_calls}",
                  f"Termination: {report.termination_reason}", ""])
    return "\n".join(lines)
