"""Neutral runtime mechanism switches; no experiment names or ground truth."""
from typing import Literal
from pydantic import StrictBool
from .incident import Contract
from .portfolio import HypothesisAdmissionPolicy


class InvestigationExecutionPolicy(Contract):
    schema_version: Literal[1] = 1
    required_check_scheduling: StrictBool = True
    hypothesis_policy: HypothesisAdmissionPolicy = HypothesisAdmissionPolicy()


DEFAULT_EXECUTION_POLICY = InvestigationExecutionPolicy()
