"""Verifier reasons are closed semantic codes; display prose stays outside identity."""

from typing import get_args

import pytest
from pydantic import ValidationError

from aegis_rca.models.check import (
    CategoryPresent, CheckReason, CheckResult, CounterDeltaCompare,
    HypothesisVerificationPlan, MetricSummaryExists, PlanReason, PlanVerificationResult,
)
from aegis_rca.models.metric_observation import MetricObservationContext
from aegis_rca.models.telemetry import MetricResult
from aegis_rca.verification.aggregate import aggregate_plan
from aegis_rca.verification.evaluate import evaluate_check
from aegis_rca.verification.legacy_bridge import evaluate_legacy_check

import test_check_legacy as legacy
import test_check_results as native
import test_metric_observations as metrics


def _round_trip(original):
    restored = type(original).model_validate_json(original.model_dump_json())
    assert restored == original
    assert restored.id == original.id


@pytest.mark.parametrize("reason", get_args(CheckReason))
def test_check_reason_vocabulary_round_trips(reason):
    result = CheckResult(
        check_id="C-fixture", verdict="UNKNOWN", input_kind="observation",
        evaluator="observation_verifier_v1", reason=reason,
    )
    _round_trip(result)


@pytest.mark.parametrize("reason", get_args(PlanReason))
def test_plan_reason_vocabulary_round_trips(reason):
    _round_trip(PlanVerificationResult(plan_id=None, status="UNCERTAIN", reason=reason))


@pytest.mark.parametrize("reason", ["The query looks complete, so no matching RPC exists.", "test_fixture", ""])
def test_arbitrary_reason_prose_is_rejected_by_both_contracts(reason):
    with pytest.raises(ValidationError, match="reason"):
        CheckResult(
            check_id="C-fixture", verdict="UNKNOWN", input_kind="observation",
            evaluator="observation_verifier_v1", reason=reason,
        )
    with pytest.raises(ValidationError, match="reason"):
        PlanVerificationResult(plan_id=None, status="UNCERTAIN", reason=reason)


def test_human_readable_summary_remains_free_form_and_outside_identity():
    original = evaluate_legacy_check(legacy.check(), evidence=(legacy.log(),))
    displayed = CheckResult.model_validate({
        **original.model_dump(), "summary": "An admitted checkout log describes a connection failure.",
    })
    assert displayed.summary != original.summary
    assert displayed.id == original.id
    _round_trip(displayed)


def test_every_native_and_legacy_evaluator_reason_round_trips():
    spec = native.trace_check()
    fact = native.rpc_fact(rpc_status=14, otlp_status="ERROR")
    conflict = fact.model_copy(update={"value": fact.value.model_copy(update={"peer_service": "shipping"})})
    unresolved = fact.model_copy(update={"value": fact.value.model_copy(update={"peer_service": None})})
    summaries = metrics.summaries(metrics.observe(MetricResult(series=(metrics.series(),))))
    summary_check = native.metric_check(MetricSummaryExists(series=summaries[0].series))
    categories = native.category_facts()
    other_categories = metrics.categories(metrics.observe(
        MetricResult(series=metrics.payment_result().series[:1]),
        context=MetricObservationContext(category_labels=(metrics.STATUS,)),
    ))
    category_check = native.metric_check(CategoryPresent(label=metrics.STATUS, value="UNAVAILABLE"))
    absent_category = native.metric_check(CategoryPresent(label=metrics.STATUS, value="UNKNOWN"))
    deltas = native.numeric_facts()
    multiple_deltas = native.numeric_facts(((0, 1), (10, 4), (20, 4)))
    delta_check = native.metric_check(CounterDeltaCompare(series=deltas[0].series, operator="gt", threshold=0))
    false_delta = native.metric_check(CounterDeltaCompare(series=deltas[0].series, operator="gt", threshold=5))
    native_outputs = {
        "conflicting_fact_identity": native.evaluate(spec, (fact, conflict)),
        "unsupported_relation": native.evaluate(spec.model_copy(update={"relation": "contradicts_hypothesis"})),
        "unsupported_check_version": native.evaluate(spec.model_copy(update={"semantic_version": "2"})),
        "positive_match": native.evaluate(spec, (fact,)),
        "unresolved_rpc_fields": native.evaluate(spec, (unresolved,)),
        "complete_no_match": evaluate_check(spec, coverage=(native.complete(spec),)),
        "open_scope_no_match": native.evaluate(spec),
        "returned_category_present": native.evaluate(category_check, categories),
        "ambiguous_category_fact": native.evaluate(absent_category, categories + other_categories),
        "returned_category_not_present": native.evaluate(absent_category, categories),
        "missing_category_fact": native.evaluate(category_check),
        "summary_present": native.evaluate(summary_check, summaries),
        "missing_summary_fact": native.evaluate(summary_check),
        "ambiguous_metric_fact": native.evaluate(delta_check, multiple_deltas),
        "missing_metric_fact": native.evaluate(delta_check),
        "comparison_true": native.evaluate(delta_check, deltas),
        "comparison_false": native.evaluate(false_delta, deltas),
    }
    legacy_outputs = {
        "conflicting_fact_identity": evaluate_legacy_check(legacy.check(), evidence=(legacy.log(), legacy.log(peer="shipping"))),
        "unsupported_relation": evaluate_legacy_check(legacy.check(relation="contradicts_hypothesis")),
        "unsupported_check_version": evaluate_legacy_check(legacy.check().model_copy(update={"semantic_version": "2"})),
        "positive_match": evaluate_legacy_check(legacy.check(), evidence=(legacy.log(),)),
        "unresolved_legacy_peer": evaluate_legacy_check(legacy.check(), evidence=(legacy.log(peer=None),)),
        "complete_no_match": evaluate_legacy_check(legacy.check(query="query-logs"), coverage=(legacy.complete_view(),)),
        "open_scope_no_match": evaluate_legacy_check(legacy.check()),
    }
    assert set(native_outputs) == set(get_args(CheckReason)) - {"unresolved_legacy_peer"}
    assert set(native_outputs) | set(legacy_outputs) == set(get_args(CheckReason))
    for expected_kind, outputs in (("observation", native_outputs), ("legacy_evidence", legacy_outputs)):
        for reason, original in outputs.items():
            assert original.reason == reason
            assert original.input_kind == expected_kind
            _round_trip(original)


def test_reason_identity_goldens_with_p17_plan_migration():
    # C/CR captured before reason hardening; P/PR explicitly migrated in P1.7a.
    spec = native.trace_check()
    fact = native.rpc_fact(rpc_status=14, otlp_status="ERROR")
    native_result = evaluate_check(spec, observations=(fact,), coverage=(native.complete(spec, (fact,)),))
    legacy_result = evaluate_legacy_check(legacy.check(), evidence=(legacy.log(),))
    plan = HypothesisVerificationPlan(failure_mode="rpc_connection_establishment", checks=(spec,))
    aggregate = aggregate_plan(plan, results=(native_result,))
    assert spec.id == "C-ceff4d241d0e43b113bace600ac1298c373de7f541b8e17bb00f651fbfa354d6"
    assert native_result.id == "CR-cf3be656f7b42d473a21f473ae65ed47c29968a73cdf97210690079ea7491511"
    assert legacy_result.id == "CR-7232c37186afe027e42f08b7161a96cba8fb5ffc3c30aede1bd321cd8222c7b3"
    assert plan.id == "P-8e9922ae7db87d9c95daf78d38dd34edf3ac7a88096bf01a1d7e232a85fa9039"
    assert aggregate.id == "PR-e610014ff666ad212efa1295492fe22c7a6c3aefa716bc49557e391bf2689ca8"
