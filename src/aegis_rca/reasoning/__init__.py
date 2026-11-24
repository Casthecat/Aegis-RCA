"""Optional structured LLM proposals with incident-scoped reservations."""

from .budget import BudgetManager, BudgetSnapshot, Reservation, conservative_input_tokens
from .models import (BudgetExceeded, HypothesisProposal, HypothesisProposalSet,
                     PlanProposal, ReasoningConfig, ReasoningError,
                     ReasoningLimits, TaskProposal)
from .provider import OpenAICompatibleReasoner, incident_key

__all__ = [
    "BudgetExceeded", "BudgetManager", "BudgetSnapshot", "Reservation",
    "HypothesisProposal", "HypothesisProposalSet", "OpenAICompatibleReasoner",
    "PlanProposal", "ReasoningConfig", "ReasoningError", "ReasoningLimits",
    "TaskProposal", "conservative_input_tokens", "incident_key",
]
